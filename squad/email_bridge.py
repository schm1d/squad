"""TLS SMTP/IMAP email adapter. Incoming mail is authenticated by the trusted mailbox server."""
from __future__ import annotations
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import mimetypes
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from . import attachments, messaging

MAX_MESSAGE = 16 * 1024 * 1024


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', value) or len(value) > 254:
        raise ValueError('Use a plain email address without a display name')
    return value.lower()


def validate(cfg):
    cfg['address'] = address(cfg.get('address'))
    users = cfg.get('allowed_senders')
    if not isinstance(users, list) or not users:
        raise ValueError('Email requires an explicit --allow-sender address')
    cfg['allowed_senders'] = sorted({address(v) for v in users})
    if cfg['address'] in cfg['allowed_senders']:
        raise ValueError('Use separate bot and human email addresses to prevent reply loops')
    for name in ['imap_host', 'smtp_host', 'authserv_id']:
        if not isinstance(cfg.get(name), str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', cfg[name]):
            raise ValueError(f'Email requires {name}')
    for name, default in [('imap_port', 993), ('smtp_port', 465)]:
        messaging.bounded_int(cfg.setdefault(name, default), name, 1, 65535)
    if cfg.setdefault('smtp_security', 'tls') not in {'tls', 'starttls'}:
        raise ValueError('SMTP requires tls or starttls')
    for name in ('password_env', 'imap_password_env'):
        messaging.env_name(cfg.setdefault(name, 'SQUAD_EMAIL_PASSWORD'))
    for name in ('username', 'smtp_username'):
        value = cfg.setdefault(name, cfg['address'])
        if not isinstance(value, str) or not value or len(value) > 254 or any(c in value for c in '\r\n\x00'):
            raise ValueError('Invalid email login username')
    if not re.fullmatch(r'[A-Za-z0-9_./ -]{1,100}', cfg.setdefault('mailbox', 'INBOX')):
        raise ValueError('Invalid IMAP mailbox')
    return messaging.validate_common(cfg)


def account_key(cfg):
    return json.dumps([cfg['address'], cfg['imap_host'], cfg['username'], cfg['mailbox']])


def authorized(cfg, env):
    return env.get('sender') in cfg['allowed_senders'] and env.get('authenticated') is True


def allowed_route(cfg, route):
    return route.get('to') in cfg['allowed_senders'] and route.get('scope') == route.get('to')


def _sender(msg, cfg):
    if len(msg.get_all('From', [])) != 1:
        return None
    parsed = email.utils.getaddresses(msg.get_all('From', []))
    if len(parsed) != 1:
        return None
    try:
        sender = address(parsed[0][1])
    except ValueError:
        return None
    if sender not in cfg['allowed_senders']:
        return None
    if msg.get('Auto-Submitted', 'no').strip().lower() != 'no' or msg.get('List-Id') or msg.get_content_type() == 'multipart/report':
        return None
    if msg.get('Precedence', '').lower() in {'bulk', 'junk', 'list'}:
        return None
    # Trust only the first result stamped by the configured receiving service.
    # The mailbox provider must strip forged Authentication-Results for its own ID.
    auth = next((str(h) for h in msg.get_all('Authentication-Results', [])
                 if re.split(r'[;\s]', str(h).strip(), 1)[0].lower() == cfg['authserv_id'].lower()), '')
    domain = sender.rsplit('@', 1)[1]
    clauses = auth.split(';')[1:]
    if not any(re.search(r'\bdmarc\s*=\s*pass\b', part, re.I) and
               re.search(r'\bheader\.from\s*=\s*"?' + re.escape(domain) + r'"?(?=[\s;)]|$)', part, re.I) for part in clauses):
        return None
    return sender


def parse_message(bridge, raw, uid_key):
    if len(raw) > MAX_MESSAGE:
        raise ValueError('Email is too large')
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    sender = _sender(msg, bridge.cfg)
    if not sender:
        return None
    mid = str(msg.get('Message-ID', ''))
    if not re.fullmatch(r'<[^<>\s]{1,400}>', mid):
        mid = 'imap:' + uid_key
    reply = str(msg.get('In-Reply-To', ''))
    if not re.fullmatch(r'<[^<>\s]{1,400}>', reply):
        reply = ''
    parts = list(msg.walk())
    if len(parts) > 64:
        raise ValueError('Too many MIME parts')
    text, files, total = '', [], 0
    for part in parts:
        if part.is_multipart():
            continue
        data = part.get_payload(decode=True) or b''
        if part.get_filename() or part.get_content_disposition() == 'attachment':
            total += len(data)
            if len(files) >= 8 or total > attachments.MAX_BYTES:
                raise ValueError('At most eight attachments, 10 MiB total')
            files.append(attachments.save(bridge.root, 'email', bridge.key + ':' + sender, mid,
                part.get_filename() or 'attachment.bin', data, part.get_content_type()))
        elif not text and part.get_content_type() == 'text/plain':
            text = data.decode(part.get_content_charset() or 'utf-8', errors='replace')
    # Do not treat quoted mail as a fresh @mention or as fresh authorization.
    text = re.split(r'(?m)^On .+wrote:\s*$|^>.*$|^-{2,}\s*Original Message', text, maxsplit=1)[0].strip()
    subject = str(msg.get('Subject', ''))[:200].replace('\r', ' ').replace('\n', ' ')
    clean_subject = re.sub(r'^(?:(?:re|fwd?):\s*)+', '', subject, flags=re.I)
    if not messaging.MENTION.search(text) and (clean_subject.startswith('@') or clean_subject.startswith('/ask ')):
        text = clean_subject + '\n' + text
    return {'id': mid, 'sender': sender, 'authenticated': True, 'text': text, 'attachments': files,
            'reply_id': reply, 'conversation': sender + ':' + attachments.digest(mid),
            'route': {'to': sender, 'scope': sender, 'subject': subject or 'Squad', 'reply_id': mid}}


class MailClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.password = messaging.secret(cfg['password_env'])
        self.imap_password = messaging.secret(cfg['imap_password_env'])

    def poll(self, bridge):
        cfg = self.cfg
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(cfg['imap_host'], cfg['imap_port'], ssl_context=ssl.create_default_context(), timeout=20)
            conn.login(cfg['username'], self.imap_password)
            code, _ = conn.select('"' + cfg['mailbox'] + '"', readonly=True)
            if code != 'OK':
                raise ValueError('IMAP mailbox is unavailable')
            validity = (conn.response('UIDVALIDITY')[1] or [None])[0]
            next_uid = (conn.response('UIDNEXT')[1] or [None])[0]
            if not validity or not next_uid:
                raise ValueError('IMAP did not provide stable UID state')
            validity, next_uid = int(validity), int(next_uid)
            cursor = json.loads(bridge.state('imap_cursor') or '{}')
            if cursor.get('validity') != validity:
                bridge.state('imap_cursor', json.dumps({'validity': validity, 'uid': next_uid - 1}))
                return  # First start or recreated mailbox: never replay an old inbox.
            code, data = conn.uid('search', None, 'UID', f'{cursor["uid"] + 1}:*')
            if code != 'OK':
                raise ValueError('IMAP search failed')
            uids = sorted({int(v) for v in (data[0] or b'').split() if int(v) > cursor['uid']})[:25]
            for uid in uids:
                code, metadata = conn.uid('fetch', str(uid), '(RFC822.SIZE)')
                match = re.search(rb'RFC822.SIZE\s+(\d+)', b' '.join(v for v in metadata if isinstance(v, bytes)))
                if code != 'OK' or not match:
                    raise ValueError('IMAP message metadata unavailable')
                if int(match[1]) <= MAX_MESSAGE:
                    code, bodies = conn.uid('fetch', str(uid), '(BODY.PEEK[])')
                    if code != 'OK':
                        raise ValueError('IMAP message retrieval failed')
                    body = next((v[1] for v in bodies if isinstance(v, tuple)), None)
                    if not isinstance(body, bytes):
                        raise ValueError('IMAP returned no message')
                    try:
                        envelope = parse_message(bridge, body, f'{validity}:{uid}')
                    except (ValueError, LookupError):
                        envelope = None
                        bridge.state('last_rejected', f'UID {uid}: invalid MIME content or attachment limits')
                    if envelope:
                        bridge.ingest(envelope)
                    else:
                        bridge.state('last_ignored', f'UID {uid}: sender/authentication/automatic-mail checks or content limits rejected the message')
                else:
                    bridge.state('last_rejected', f'UID {uid}: message exceeds 16 MiB')
                bridge.state('imap_cursor', json.dumps({'validity': validity, 'uid': uid}))
        except (imaplib.IMAP4.error, OSError):
            raise messaging.DeliveryError('Email IMAP connection/login failed; check hosts, TLS and app-password environment locally') from None
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    def send(self, text, route, delivery_id):
        if not allowed_route(self.cfg, route):
            raise messaging.DeliveryError('Email recipient is not allowlisted')
        cfg = self.cfg
        msg = EmailMessage()
        msg['From'], msg['To'] = cfg['address'], route['to']
        subject = str(route.get('subject', 'Squad')).replace('\r', ' ').replace('\n', ' ')[:200]
        msg['Subject'] = subject if subject.lower().startswith('re:') else 'Re: ' + subject
        msg['Date'] = email.utils.formatdate(localtime=False)
        mid = '<squad.' + attachments.digest(delivery_id) + '@' + cfg['address'].split('@')[1] + '>'
        msg['Message-ID'] = mid
        msg['Auto-Submitted'] = 'auto-replied'
        msg['X-Auto-Response-Suppress'] = 'All'
        if re.fullmatch(r'<[^<>\s]{1,400}>', route.get('reply_id', '')):
            msg['In-Reply-To'] = route['reply_id']
            msg['References'] = route['reply_id']
        msg.set_content(text)
        for info in route.get('files', []):
            data = attachments.read_saved(Path(route['root']), info)
            mime = mimetypes.guess_type(info['name'])[0] or 'application/octet-stream'
            main, sub = mime.split('/', 1)
            msg.add_attachment(data, maintype=main, subtype=sub, filename=info['name'])
        sending = False
        conn = None
        try:
            if cfg['smtp_security'] == 'tls':
                conn = smtplib.SMTP_SSL(cfg['smtp_host'], cfg['smtp_port'], timeout=20, context=ssl.create_default_context())
            else:
                conn = smtplib.SMTP(cfg['smtp_host'], cfg['smtp_port'], timeout=20)
                conn.ehlo(); conn.starttls(context=ssl.create_default_context()); conn.ehlo()
            conn.login(cfg['smtp_username'], self.password)
            sending = True
            conn.send_message(msg, from_addr=cfg['address'], to_addrs=[route['to']])
            return mid
        except (smtplib.SMTPException, OSError):
            raise messaging.DeliveryError('Email SMTP delivery failed; check TLS, credentials and mailbox locally', uncertain=sending) from None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

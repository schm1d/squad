"""WhatsApp Business Cloud API: signed webhooks, agent chat and bounded media."""
from __future__ import annotations
import hashlib
import base64
import hmac
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from . import attachments, messaging

DOCUMENTS = {'text/plain', 'application/pdf', 'application/msword', 'application/vnd.ms-excel',
    'application/vnd.ms-powerpoint', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation'}
TYPES = {'image/jpeg': 'image', 'image/png': 'image', 'audio/aac': 'audio', 'audio/mp4': 'audio',
         'audio/mpeg': 'audio', 'audio/amr': 'audio', 'audio/ogg': 'audio',
         'video/mp4': 'video', 'video/3gpp': 'video', **{k: 'document' for k in DOCUMENTS}}


def phone(value):
    if not isinstance(value, str) or not re.fullmatch(r'\+?[1-9][0-9]{5,14}', value):
        raise ValueError('Use a WhatsApp phone number with country code, e.g. +41791234567')
    return value.lstrip('+')


def validate(cfg):
    if not isinstance(cfg.get('phone_number_id'), str) or not re.fullmatch(r'[0-9]{1,30}', cfg['phone_number_id']):
        raise ValueError('WhatsApp needs the Meta phone-number ID (--phone-number-id)')
    if not isinstance(cfg.get('api_version'), str) or not re.fullmatch(r'v[0-9]{1,3}\.0', cfg['api_version']):
        raise ValueError('Set --api-version to the supported Graph API version shown in your Meta dashboard')
    users = cfg.get('allowed_users')
    if not isinstance(users, list) or not users:
        raise ValueError('WhatsApp needs at least one --allow-user phone number')
    cfg['allowed_users'] = sorted({phone(v) for v in users})
    groups = cfg.setdefault('allowed_groups', [])
    if not isinstance(groups, list) or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_@.-]{1,160}', v) for v in groups):
        raise ValueError('Invalid Cloud API group ID')
    for name, default in [('token_env', 'SQUAD_WHATSAPP_TOKEN'), ('app_secret_env', 'SQUAD_WHATSAPP_APP_SECRET'),
                          ('verify_token_env', 'SQUAD_WHATSAPP_VERIFY_TOKEN')]:
        messaging.env_name(cfg.setdefault(name, default))
    return messaging.validate_common(cfg)


def account_key(cfg):
    return cfg['phone_number_id']


def authorized(cfg, env):
    if env.get('sender') not in cfg['allowed_users']:
        return False
    group = env.get('group')
    return not group or group in cfg['allowed_groups']


def allowed_route(cfg, route):
    if route.get('recipient_type', 'individual') == 'group':
        return route.get('to') in cfg['allowed_groups'] and route.get('sender') in cfg['allowed_users']
    return route.get('to') in cfg['allowed_users']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise messaging.DeliveryError('WhatsApp redirect refused', uncertain=True)


class CloudAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = messaging.secret(cfg['token_env'])
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, url, data=None, content_type='application/json', binary=False):
        request = urllib.request.Request(url, data=data, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': content_type})
        maximum = attachments.MAX_BYTES if binary else 1_000_000
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(maximum + 1)
            if len(body) > maximum:
                raise messaging.DeliveryError('WhatsApp response exceeds the size limit', uncertain=data is not None)
            if binary:
                return body
            result = json.loads(body)
            if not isinstance(result, dict) or result.get('error'):
                raise messaging.DeliveryError('WhatsApp returned an invalid response', uncertain=data is not None)
            return result
        except urllib.error.HTTPError as exc:
            raise messaging.DeliveryError(f'WhatsApp API rejected request (HTTP {exc.code}); check account, permissions and messaging window', uncertain=exc.code >= 500) from None
        except messaging.DeliveryError:
            raise
        except Exception:
            raise messaging.DeliveryError('WhatsApp connection/response failed', uncertain=data is not None) from None

    def graph(self, suffix, payload=None):
        url = 'https://graph.facebook.com/' + self.cfg['api_version'] + '/' + suffix
        return self._request(url, json.dumps(payload).encode() if payload is not None else None)

    def download(self, media_id):
        if not isinstance(media_id, str) or not re.fullmatch(r'[0-9]{1,100}', media_id):
            raise ValueError('Invalid WhatsApp media ID')
        info = self.graph(media_id + '?phone_number_id=' + self.cfg['phone_number_id'])
        if type(info.get('file_size')) is not int or not 0 < info['file_size'] <= attachments.MAX_BYTES:
            raise ValueError('WhatsApp media must be under 10 MiB')
        url = info.get('url', '')
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not (host == 'lookaside.fbsbx.com' or host.endswith('.fbcdn.net'))):
            raise ValueError('WhatsApp returned an untrusted media host')
        data = self._request(url, binary=True)
        if len(data) != info['file_size']:
            raise ValueError('WhatsApp media size changed')
        sha = info.get('sha256', '')
        checksum = hashlib.sha256(data)
        if sha and sha not in {checksum.hexdigest(), base64.b64encode(checksum.digest()).decode()}:
            raise ValueError('WhatsApp media checksum mismatch')
        return data, str(info.get('mime_type', 'application/octet-stream'))

    def prepare(self, bridge, env):
        media = env.get('media')
        if media:
            data, mime = self.download(media['id'])
            name = media.get('filename') or ('attachment' + (mimetypes.guess_extension(mime) or '.bin'))
            env = {**env, 'attachments': [attachments.save(bridge.root, 'whatsapp', bridge.key + ':' + env['conversation'],
                                                          env['id'], name, data, mime)]}
        return env

    def upload(self, info, root):
        data = attachments.read_saved(root, info)
        mime = mimetypes.guess_type(info['name'])[0] or info['mime']
        kind = TYPES.get(mime)
        if not kind or (kind == 'image' and len(data) > 5 * 1024 * 1024):
            raise messaging.DeliveryError('Unsupported WhatsApp file type or image over 5 MiB; send TXT, PDF, Office, supported image/audio/video')
        boundary = 'squad' + uuid.uuid4().hex
        name = attachments.filename(info['name'])
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\n{mime}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += data + f'\r\n--{boundary}--\r\n'.encode()
        result = self._request('https://graph.facebook.com/' + self.cfg['api_version'] + '/' + self.cfg['phone_number_id'] + '/media',
                               body, 'multipart/form-data; boundary=' + boundary)
        if not isinstance(result.get('id'), str):
            raise messaging.DeliveryError('WhatsApp upload returned no media ID')
        return result['id'], kind

    def send(self, text, route, delivery_id):
        if not allowed_route(self.cfg, route):
            raise messaging.DeliveryError('WhatsApp recipient is not allowlisted')
        if route.get('recipient_type', 'individual') == 'individual' and time.time() - route.get('received_at', 0) >= 86400:
            raise messaging.DeliveryError('WhatsApp 24-hour reply window is closed; ask the user to message again or use an approved template outside Squad')
        payload = {'messaging_product': 'whatsapp', 'recipient_type': route.get('recipient_type', 'individual'), 'to': route['to']}
        if route.get('reply_id'):
            payload['context'] = {'message_id': route['reply_id']}
        files = route.get('files', [])
        if files:
            if len(files) != 1:
                raise ValueError('WhatsApp supports one file per send command')
            mid, kind = self.upload(files[0], Path(route['root']))
            payload.update(type=kind)
            payload[kind] = {'id': mid}
            if kind == 'document':
                payload[kind]['filename'] = files[0]['name']
            if kind != 'audio' and text:
                payload[kind]['caption'] = text[:1000]
        else:
            text = text if len(text) <= 4000 else text[:3950] + '\n[Reply shortened to fit WhatsApp.]'
            payload.update(type='text', text={'body': text, 'preview_url': False})
        result = self.graph(self.cfg['phone_number_id'] + '/messages', payload)
        try:
            return str(result['messages'][0]['id'])
        except (KeyError, IndexError, TypeError):
            raise messaging.DeliveryError('WhatsApp accepted a request without a message ID', uncertain=True) from None


def receive(bridge, payload):
    if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
        return
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            if change.get('field') != 'messages' or value.get('metadata', {}).get('phone_number_id') != bridge.cfg['phone_number_id']:
                continue
            for msg in value.get('messages', []):
                sender = msg.get('from')
                group = msg.get('group_id')
                env = {'id': msg.get('id'), 'sender': sender, 'group': group}
                if not authorized(bridge.cfg, env):
                    continue
                stamp = int(msg.get('timestamp', 0))
                if stamp <= 0 or stamp > time.time() + 300:
                    continue
                kind = msg.get('type')
                text, media = '', None
                if kind == 'text':
                    text = msg.get('text', {}).get('body', '')
                elif kind in {'document', 'image', 'audio', 'video'}:
                    media = msg.get(kind, {})
                    text = media.get('caption', '')
                else:
                    continue
                scope = ('group:' + group) if group else ('user:' + sender)
                env.update(text=text, media=media, reply_id=msg.get('context', {}).get('id'), conversation=scope,
                    route={'to': group or sender, 'sender': sender, 'scope': scope,
                           'recipient_type': 'group' if group else 'individual', 'reply_id': msg['id'], 'received_at': stamp})
                bridge.ingest(env)
            for status in value.get('statuses', []):
                state = status.get('status')
                if state not in {'delivered', 'read', 'failed'}:
                    continue
                with bridge.broker.db() as db:
                    # Do not downgrade read to delivered if webhooks arrive out of order.
                    db.execute("UPDATE channel_outbox SET state=?,error=? WHERE bridge=? AND remote_id=? AND state IN ('sent','delivered')",
                        (state, 'WhatsApp reported delivery failure; check account/window locally' if state == 'failed' else '',
                         bridge.key, status.get('id')))


def webhook_server(bridge, host, port):
    app_secret = messaging.secret(bridge.cfg['app_secret_env']).encode()
    verify_token = messaging.secret(bridge.cfg['verify_token_env'])
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
        def log_message(self, *args):
            pass  # Never log verification tokens or message bodies.
        def respond(self, status, data=b''):
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != '/webhook':
                return self.respond(404)
            if params.get('hub.mode') == ['subscribe'] and hmac.compare_digest(params.get('hub.verify_token', [''])[0].encode(), verify_token.encode()):
                return self.respond(200, params.get('hub.challenge', [''])[0].encode()[:1000])
            self.respond(403)
        def do_POST(self):
            if self.path != '/webhook':
                return self.respond(404)
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    return self.respond(400)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1_000_000:
                    return self.respond(413)
                raw = self.rfile.read(length)
                expected = 'sha256=' + hmac.new(app_secret, raw, hashlib.sha256).hexdigest()
                given = self.headers.get('X-Hub-Signature-256', '')
                if len(raw) != length or not hmac.compare_digest(expected.encode(), given.encode()):
                    return self.respond(403)
                # Durably enqueue before acknowledging. Model work runs outside the HTTP handler.
                with workspace_reload_lock(bridge):
                    bridge.reload()
                    receive(bridge, json.loads(raw))
                self.respond(200, b'ok')
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                self.respond(503)
    return HTTPServer((host, port), Handler)


def workspace_reload_lock(bridge):
    from .locking import workspace_lock
    return workspace_lock(bridge.root, 'whatsapp-webhook')

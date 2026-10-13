"""Allowlisted Telegram Bot API bridge with durable inbox and delivery records.

Polling does not execute shell commands or models. /task queues bounded read-only
work for separately running workers. Ambiguous sends are never retried silently.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from . import store
from .broker import Broker, bounded_int, identifier
from .locking import workspace_lock


class TelegramError(RuntimeError):
    def __init__(self, message, *, retry_after=None, uncertain=False):
        super().__init__(message)
        self.retry_after, self.uncertain = retry_after, uncertain


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TelegramError('Telegram redirect refused', uncertain=True)


class BotAPI:
    def __init__(self, token: str):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('Invalid Telegram bot token format')
        self._token = token
        self.bot_id = token.split(':', 1)[0]
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, payload: dict, timeout: int = 30):
        if method not in {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage', 'getFile'}:
            raise ValueError('Unsupported Telegram method')
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{self._token}/{method}',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        return self._request(req, timeout)

    def _request(self, req, timeout):
        try:
            try:
                response = self.opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise TelegramError('Telegram response exceeded size limit', uncertain=True)
                data = json.loads(raw)
            if not isinstance(data, dict):
                raise TelegramError('Invalid Telegram response', uncertain=True)
            if not data.get('ok'):
                code = data.get('error_code', 0)
                retry = (data.get('parameters') or {}).get('retry_after')
                if code == 429 and type(retry) is int:
                    raise TelegramError('Telegram rate limit; delivery deferred', retry_after=max(1, retry))
                # Never include the URL, token, or server-supplied text in errors.
                raise TelegramError(f'Telegram API rejected request (code {int(code)})', uncertain=int(code) >= 500)
            return data['result']
        except TelegramError:
            raise
        except Exception:
            raise TelegramError('Telegram request failed; delivery outcome may be unknown', uncertain=True) from None

    def send_document(self, payload: dict, name: str, content: bytes):
        from .attachments import MAX_BYTES, filename
        if not 0 < len(content) <= MAX_BYTES:
            raise ValueError('Telegram attachments must contain 1 byte to 10 MiB')
        boundary = 'squad' + uuid.uuid4().hex
        fields = []
        for key, value in payload.items():
            if key not in {'chat_id', 'caption', 'message_thread_id', 'reply_parameters'}:
                raise ValueError('Unsupported Telegram file option')
            encoded = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
            fields.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{encoded}\r\n'.encode())
        fields.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename(name)}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode())
        body = b''.join(fields) + content + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(f'https://api.telegram.org/bot{self._token}/sendDocument', data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
        return self._request(req, 60)

    def download(self, file_id: str):
        from .attachments import MAX_BYTES
        if not isinstance(file_id, str) or not re.fullmatch(r'[A-Za-z0-9_=-]{1,500}', file_id):
            raise ValueError('Invalid Telegram file ID')
        info = self.request('getFile', {'file_id': file_id})
        if not isinstance(info, dict):
            raise TelegramError('Telegram returned invalid file metadata')
        size = info.get('file_size')
        if size is not None and (type(size) is not int or not 0 < size <= MAX_BYTES):
            raise ValueError('Telegram attachment exceeds Squad\'s 10 MiB limit or is empty')
        path = info.get('file_path', '')
        if (not isinstance(path, str) or len(path) > 1000 or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
                or any(part in {'', '.', '..'} for part in path.split('/'))):
            raise TelegramError('Telegram returned an invalid file path')
        try:
            with self.opener.open(f'https://api.telegram.org/file/bot{self._token}/{path}', timeout=30) as response:
                content = response.read(MAX_BYTES + 1)
        except Exception:
            raise TelegramError('Telegram file download failed; please resend the attachment') from None
        if not 0 < len(content) <= MAX_BYTES or (size is not None and len(content) != size):
            raise ValueError('Telegram attachment size is invalid or exceeds 10 MiB')
        return content


def config_location(root: Path) -> dict:
    root = root.resolve()
    return {'project_root': str(root), 'config_file': str(root / store.SQUAD_DIR / store.CONFIG_NAME)}


def listen_command(root: Path, with_workers: bool = False) -> str:
    return 'squad telegram listen --root ' + shlex.quote(str(root.resolve())) + (' --with-workers' if with_workers else '')


def settings(root: Path) -> dict:
    cfg = dict(store.load_config(root).get('telegram') or {})
    try:
        return validate_settings(cfg)
    except ValueError as exc:
        path = config_location(root)['config_file']
        command = 'squad telegram configure --root ' + shlex.quote(str(root.resolve()))
        raise ValueError(f'{exc}. Selected config: {path}. '
                         f'Configure this project with: {command} --chat-id CHAT_ID --user-id USER_ID') from None


def validate_settings(cfg: dict) -> dict:
    chat = cfg.get('chat_id')
    users = cfg.get('allowed_user_ids', [])
    if type(chat) is not int or chat == 0:
        raise ValueError('Configure Telegram with a numeric --chat-id first')
    if not isinstance(users, list) or not users or any(type(u) is not int or u <= 0 for u in users):
        raise ValueError('Telegram requires at least one explicit allowed user ID')
    env = cfg.setdefault('token_env', 'SQUAD_TELEGRAM_BOT_TOKEN')
    if not isinstance(env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env):
        raise ValueError('Invalid Telegram token environment variable name')
    if cfg.get('default_agent'):
        identifier(cfg['default_agent'])
    if type(cfg.get('allow_tasks', False)) is not bool:
        raise ValueError('telegram.allow_tasks must be boolean')
    if type(cfg.get('auto_discover_agents', False)) is not bool:
        raise ValueError('telegram.auto_discover_agents must be boolean')
    bounded_int(cfg.setdefault('task_budget', 6000), 'task_budget', 512, 100000)
    bounded_int(cfg.setdefault('max_pending_tasks', 5), 'max_pending_tasks', 1, 100)
    from .telegram_chat import validate
    validate(cfg)
    return cfg


def client(cfg: dict) -> BotAPI:
    token = os.environ.get(cfg['token_env'], '')
    if not token:
        raise ValueError(f'Set {cfg["token_env"]} in the environment; never put tokens in config.json')
    return BotAPI(token)


def chunks(text: str, limit: int = 4000):
    """Stay below Telegram's message limit, including astral Unicode characters."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Message must contain text')
    if len(text) > 60000:
        raise ValueError('Message too long; send an artifact reference')
    buf, used = [], 0
    for c in text:
        size = len(c.encode('utf-16-le')) // 2
        if used + size > limit:
            yield ''.join(buf)
            buf, used = [], 0
        buf.append(c)
        used += size
    if buf:
        yield ''.join(buf)


HELP = ('Squad commands:\n/status — task counts\n/tasks — recent task IDs\n'
        '/agents — group agent aliases\n/ask ALIAS question — chat with an agent\n'
        '/say AGENT message — send agent mail\n/task AGENT goal — queue read-only work (if enabled)\n'
        '/help — this help\nAttach a file with @ALIAS question as its caption, or reply to an agent with a file (10 MiB limit).\n'
        'Plain text goes to the configured default agent, or the human mailbox. '
        'Workers run separately; messages do not execute shell commands.')


class Bridge:
    def __init__(self, root: Path, cfg: dict, api: BotAPI, *, live_config: bool = False):
        self.root, self.cfg, self.api = root, cfg, api
        self.live_config = live_config
        self.broker = Broker(root)
        self.key = f'{api.bot_id}:{cfg["chat_id"]}'
        self.username = ''
        with self.broker.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS telegram_state(bridge TEXT, name TEXT, value INTEGER,
                    PRIMARY KEY(bridge,name));
                CREATE TABLE IF NOT EXISTS telegram_inbox(bridge TEXT, update_id INTEGER, payload TEXT,
                    state TEXT NOT NULL, PRIMARY KEY(bridge,update_id));
                CREATE TABLE IF NOT EXISTS telegram_outbox(bridge TEXT, delivery_id TEXT, text TEXT,
                    state TEXT NOT NULL DEFAULT 'pending', available REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', PRIMARY KEY(bridge,delivery_id));
                CREATE TABLE IF NOT EXISTS telegram_turns(bridge TEXT, update_id INTEGER, agent TEXT,
                    alias TEXT, task_id TEXT UNIQUE REFERENCES tasks(id), message_id INTEGER,
                    thread_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER, body TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0, reply_task TEXT, PRIMARY KEY(bridge,update_id,agent));
                CREATE TABLE IF NOT EXISTS telegram_chat_replies(bridge TEXT, message_id INTEGER,
                    thread_id INTEGER, agent TEXT, task_id TEXT, PRIMARY KEY(bridge,message_id));
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'route' not in {row['name'] for row in db.execute('PRAGMA table_info(telegram_outbox)')}:
                db.execute("ALTER TABLE telegram_outbox ADD COLUMN route TEXT NOT NULL DEFAULT '{}'")
            if 'reply_task' not in {row['name'] for row in db.execute('PRAGMA table_info(telegram_turns)')}:
                db.execute('ALTER TABLE telegram_turns ADD COLUMN reply_task TEXT')
            if 'attachments' not in {row['name'] for row in db.execute('PRAGMA table_info(telegram_turns)')}:
                db.execute("ALTER TABLE telegram_turns ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            last = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            db.execute('INSERT OR IGNORE INTO telegram_state VALUES(?,?,?)', (self.key, 'events', last))

    def _get(self, db, name):
        row = db.execute('SELECT value FROM telegram_state WHERE bridge=? AND name=?', (self.key, name)).fetchone()
        return row[0] if row else 0

    def _set(self, db, name, value):
        db.execute('INSERT INTO telegram_state VALUES(?,?,?) ON CONFLICT(bridge,name) DO UPDATE SET value=excluded.value',
                   (self.key, name, value))

    def _queue(self, db, delivery_id, text, route=None):
        for i, part in enumerate(chunks(text)):
            db.execute('INSERT OR IGNORE INTO telegram_outbox(bridge,delivery_id,text,route) VALUES(?,?,?,?)',
                       (self.key, f'{delivery_id}:{i}', part, json.dumps(route or {})))

    def queue(self, delivery_id, text, route=None):
        with self.broker.db() as db:
            self._queue(db, delivery_id, text, route)

    def recover(self):
        with workspace_lock(self.root, 'telegram-delivery'):
            self._recover()

    def _recover(self):
        with self.broker.db() as db:
            db.execute("UPDATE telegram_outbox SET state='uncertain',error='Interrupted during send; inspect chat before retry' WHERE bridge=? AND state='sending'", (self.key,))
            interrupted = db.execute("SELECT update_id FROM telegram_inbox WHERE bridge=? AND state='processing'", (self.key,)).fetchall()
            for row in interrupted:
                if db.execute('SELECT 1 FROM telegram_turns WHERE bridge=? AND update_id=?', (self.key, row[0])).fetchone():
                    db.execute("UPDATE telegram_inbox SET state='done' WHERE bridge=? AND update_id=?", (self.key, row[0]))
                    continue
                self._queue(db, f'interrupted:{row[0]}', f'Update {row[0]} was interrupted. Inspect Squad tasks/mail before resending; it was not replayed.')
            db.execute("UPDATE telegram_inbox SET state='interrupted' WHERE bridge=? AND state='processing'", (self.key,))

    def authorized(self, message):
        from .telegram_files import has_media
        return (isinstance(message, dict)
                and (message.get('chat') or {}).get('id') == self.cfg['chat_id']
                and (message.get('from') or {}).get('id') in self.cfg['allowed_user_ids']
                and not (message.get('from') or {}).get('is_bot', False)
                and (isinstance(message.get('text'), str) or has_media(message)))

    def receive(self, timeout=20):
        with self.broker.db() as db:
            offset = self._get(db, 'offset')
        updates = self.api.request('getUpdates', {'offset': offset, 'timeout': timeout,
                    'limit': 50, 'allowed_updates': ['message']}, timeout=timeout + 10)
        # Discovery can finish while long polling. Apply aliases before routing
        # the returned messages, and stop on scope changes before advancing cursors.
        self.reload_settings()
        if not isinstance(updates, list):
            raise TelegramError('Invalid Telegram update batch')
        for update in updates:
            uid = update.get('update_id') if isinstance(update, dict) else None
            if type(uid) is not int or uid < offset:
                continue
            message = update.get('message')
            with self.broker.db() as db:
                if self.authorized(message):
                    db.execute('INSERT OR IGNORE INTO telegram_inbox VALUES(?,?,?,?)',
                               (self.key, uid, json.dumps(message), 'pending'))
                self._set(db, 'offset', max(self._get(db, 'offset'), uid + 1))
        self.process_inbox()

    def process_inbox(self):
        with self.broker.db() as db:
            rows = db.execute("SELECT update_id,payload FROM telegram_inbox WHERE bridge=? AND state='pending' ORDER BY update_id LIMIT 50", (self.key,)).fetchall()
        for row in rows:
            with self.broker.db() as db:
                changed = db.execute("UPDATE telegram_inbox SET state='processing' WHERE bridge=? AND update_id=? AND state='pending'", (self.key, row[0])).rowcount
            if not changed:
                continue
            try:
                message = json.loads(row[1])
                response = self.handle(message, row[0]) if self.authorized(message) else None
            except ValueError as exc:
                response = f'Command rejected: {exc}'
            except (TelegramError, OSError, TypeError):
                response = 'The attachment could not be received. Check the local listener and resend it; no new task was queued.'
            with self.broker.db() as db:
                if response:
                    message = json.loads(row[1])
                    route = {'reply_to': message.get('message_id'), 'thread_id': message.get('message_thread_id', 0)} if self.cfg.get('allow_chat') else None
                    self._queue(db, f'reply:{row[0]}', response, route)
                db.execute("UPDATE telegram_inbox SET state='done' WHERE bridge=? AND update_id=?", (self.key, row[0]))

    def handle(self, message, update_id):
        from .telegram_files import has_media
        media = has_media(message)
        text = (message.get('text') or message.get('caption') or '').strip()
        if not text and not media:
            return None
        message = {**message, 'text': text}
        head, _, rest = text.partition(' ')
        cmd, _, mention = head.partition('@')
        if cmd in {'/approve', '/cancel'}:
            from .telegram_actions import decide
            if mention and mention.lower() != (self.username or '').lower():
                return None
            return decide(self, message, rest, cmd == '/approve')
        if text.startswith('/') and mention and mention.lower() != self.username.lower():
            return None
        if media:
            if not self.cfg.get('allow_chat'):
                return 'Enable agent chat locally with squad telegram setup before sending attachments.'
            if text.startswith('/') and cmd != '/ask':
                return 'For attachments, use @ALIAS question or /ask ALIAS question in the caption.'
            from .telegram_chat import route
            return route(self, message, update_id, explicit=rest if cmd == '/ask' else None)
        if cmd in {'/help', '/start'}:
            return HELP
        if cmd == '/agents':
            from .telegram_chat import roster
            return roster(self)
        if cmd == '/ask':
            from .telegram_chat import route
            return route(self, message, update_id, explicit=rest)
        if cmd == '/status':
            with self.broker.db() as db:
                rows = db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state').fetchall()
            return 'Squad tasks: ' + (', '.join(f'{r[0]}={r[1]}' for r in rows) or 'none')
        if cmd == '/tasks':
            rows = self.broker.tasks(limit=10)
            return '\n'.join(f'{r["id"]} {r["state"]} {r["assignee"] or "unassigned"}: {r["goal"][:120]}' for r in rows) or 'No tasks.'
        agents = {a['id'] for a in self.broker.roster()['agents']}
        if cmd in {'/say', '/task'}:
            agent, _, body = rest.strip().partition(' ')
            if agent not in agents or not body.strip():
                raise ValueError('Use /say AGENT message or /task AGENT goal with a registered agent')
            if cmd == '/task':
                if not self.cfg.get('allow_tasks', False):
                    raise ValueError('Remote task creation is disabled; enable it locally with --allow-tasks')
                with self.broker.db() as db:
                    count = db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running','review')").fetchone()[0]
                if count >= self.cfg['max_pending_tasks']:
                    raise ValueError('Pending task limit reached; review existing work first')
                budget = self.cfg['task_budget']
                task = self.broker.create_task(body, assignee=agent, budget=budget,
                    mission_budget=budget, output_limit=min(1000, budget // 4), write=False)
                return f'Queued {task["id"]} for {agent}; budget {budget}; read-only. A running worker will pick it up.'
        elif text.startswith('/'):
            return HELP
        else:
            if self.cfg.get('allow_chat'):
                from .telegram_chat import route
                return route(self, message, update_id)
            agent, body = self.cfg.get('default_agent') or 'human', text
            if agent != 'human' and agent not in agents:
                raise ValueError('Configured default agent no longer exists')
        sender = f'telegram:{message["from"]["id"]}'
        mail = store.post_mail(self.root, to=agent, sender=sender, text=body, board=False)
        return f'Message {mail["id"]} delivered to {agent}. Agent mail is context, not a new task.'

    def collect(self):
        from .telegram_chat import collect
        collect(self)
        # Explicitly forward the human mailbox; IDs prevent repeat sends even when
        # a local session reads/acknowledges mail before the bridge polls it.
        for mail in ([] if self.cfg.get('allow_chat') else store.read_mail(self.root, 'human', unread_only=False, mark=False)):
            if mail.get('ts', '') < self.cfg.get('mail_since', ''):
                continue
            if not str(mail.get('from', '')).startswith('telegram:'):
                text = str(mail['text'])
                if len(text) > 8000:
                    text = text[:8000] + '\n[Truncated; read the full message in Squad mail.]'
                self.queue('mail:' + mail['id'], f'{mail["from"]}: {text}')
        with self.broker.db() as db:
            cursor = self._get(db, 'events')
        page = self.broker.events(cursor, 100)
        for event in page['events']:
            if event['kind'] in {'task.submitted', 'task.failed', 'task.expired', 'task.accepted'}:
                task = self.broker.task(event['entity'])
                if task.get('kind') == 'chat':
                    with self.broker.db() as db:
                        self._set(db, 'events', event['seq'])
                    continue
                result = task.get('result') or {}
                if task['state'] == 'failed' or event['kind'] in {'task.failed', 'task.expired'}:
                    from .diagnostics import public_failure
                    body = public_failure('interrupted' if event['kind'] == 'task.expired' else result.get('diagnostic'), task['id'])
                else:
                    body = result.get('summary', result.get('error', ''))[:2500]
                text = f'{event["kind"]}: {task["id"]} ({task["assignee"]})\n{body}'
                self.queue('event:' + str(event['seq']), text)
            with self.broker.db() as db:
                self._set(db, 'events', event['seq'])

    def flush(self):
        with workspace_lock(self.root, 'telegram-delivery'):
            from .telegram_actions import flush
            flush(self)
            self._flush()

    def _flush(self):
        with self.broker.db() as db:
            rows = db.execute("SELECT delivery_id,text,route FROM telegram_outbox WHERE bridge=? AND state='pending' AND available<=? ORDER BY rowid LIMIT 20", (self.key, time.time())).fetchall()
        for row in rows:
            with self.broker.db() as db:
                changed = db.execute("UPDATE telegram_outbox SET state='sending' WHERE bridge=? AND delivery_id=? AND state='pending'", (self.key, row[0])).rowcount
            if not changed:
                continue
            state, error, available = 'sent', '', 0
            sent, route = None, json.loads(row[2])
            try:
                payload = {'chat_id': self.cfg['chat_id'], 'text': row[1],
                           'link_preview_options': {'is_disabled': True}}
                if route.get('reply_to'):
                    payload['reply_parameters'] = {'message_id': route['reply_to'], 'allow_sending_without_reply': True}
                if route.get('thread_id'):
                    payload['message_thread_id'] = route['thread_id']
                if route.get('file'):
                    from .attachments import read_saved
                    from .telegram_files import caption
                    payload.pop('text'); payload.pop('link_preview_options')
                    payload['caption'] = caption(row[1])
                    info = route['file']
                    sent = self.api.send_document(payload, info['name'], read_saved(self.root, info))
                else:
                    sent = self.api.request('sendMessage', payload)
                if (route.get('agent') or route.get('file')) and (not isinstance(sent, dict) or type(sent.get('message_id')) is not int):
                    raise TelegramError('Telegram send returned no message ID', uncertain=True)
            except TelegramError as exc:
                error = str(exc)
                if exc.retry_after is not None:
                    state, available = 'pending', time.time() + exc.retry_after
                else:
                    state = 'uncertain' if exc.uncertain else 'failed'
            except (ValueError, OSError):
                state, error = 'failed', 'Attachment validation failed; check the staged file or caption locally'
            with self.broker.db() as db:
                db.execute('UPDATE telegram_outbox SET state=?,error=?,available=? WHERE bridge=? AND delivery_id=?',
                           (state, error, available, self.key, row[0]))
                if state == 'sent' and route.get('agent'):
                    db.execute('INSERT OR IGNORE INTO telegram_chat_replies VALUES(?,?,?,?,?)',
                        (self.key, sent['message_id'], route.get('thread_id', 0), route['agent'], route['task_id']))
                if available:
                    db.execute("UPDATE telegram_outbox SET available=MAX(available,?) WHERE bridge=? AND state='pending'", (available, self.key))
            if state != 'sent':
                break

    def deliveries(self):
        with self.broker.db() as db:
            return [dict(r) for r in db.execute('SELECT delivery_id,state,error FROM telegram_outbox WHERE bridge=? ORDER BY rowid DESC LIMIT 50', (self.key,))]

    def reload_settings(self):
        if not self.live_config:
            return
        latest = settings(self.root)
        # A different group/token/user scope requires a deliberate restart. Do not
        # deliver queued responses after an authorization change in the same loop.
        if any(latest.get(key) != self.cfg.get(key) for key in ('chat_id', 'token_env', 'allowed_user_ids')):
            raise ValueError('Telegram group, token variable or allowed users changed. Restart the listener to apply the new scope.')
        self.cfg = latest

    def listen(self, once=False, timeout=20, with_workers=False):
        if with_workers:
            if once or not self.cfg.get('allow_chat'):
                raise ValueError('--with-workers needs continuous listening and configured group chat')
            # Own the listener lock before starting workers, to prevent a second
            # competing listener from creating a redundant worker fleet.
            with workspace_lock(self.root, 'telegram', blocking=False):
                from .telegram_chat import workers
                with workers(self.root, self.cfg['agent_aliases']) as check:
                    return self._listen(once, min(timeout, 2), check)
        return self._listen(once, timeout)

    def _listen(self, once=False, timeout=20, check_workers=None):
        bounded_int(timeout, 'poll timeout', 0, 50)
        with workspace_lock(self.root, 'telegram', blocking=False):
            info = self.api.request('getMe', {})
            self.username = info['username']
            if self.cfg.get('allow_chat') and not info.get('can_read_all_group_messages'):
                import sys
                print('Group aliases need BotFather /setprivacy disabled (or group admin status). '
                      'Privacy-on fallback: /ask@' + self.username + ' ALIAS question.', file=sys.stderr)
            if self.api.request('getWebhookInfo', {}).get('url'):
                raise ValueError('This bot has a webhook. Remove it explicitly or use a separate bot for Squad.')
            self.recover()
            self.process_inbox()
            while True:
                self.reload_settings()
                if check_workers:
                    check_workers(self.cfg.get('agent_aliases', {}))
                try:
                    self.collect()
                    self.flush()
                    self.receive(timeout)
                    self.collect()
                    self.flush()
                except TelegramError as exc:
                    if once or (not exc.uncertain and exc.retry_after is None):
                        raise
                    time.sleep(min(exc.retry_after or 3, 50))
                if once:
                    return self.deliveries()
                if timeout == 0:
                    time.sleep(1)

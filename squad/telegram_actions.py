"""Engine-independent Telegram delivery proposals, approvals and dispatch.

Model output cannot authorize a send. Approval is bound to the requesting user,
source chat/topic, immutable bytes and destination. MCP runs only at dispatch.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import mimetypes
import os
import re
import stat
import time
from pathlib import Path
from . import attachments, store


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS telegram_actions(
      id TEXT PRIMARY KEY, bridge TEXT NOT NULL, task_id TEXT NOT NULL,
      user_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'proposed',
      created REAL NOT NULL, error TEXT NOT NULL DEFAULT '')''')


def recipient(root, alias, chat_id, server=None):
    from .locking import workspace_lock
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', alias) or alias == 'here':
        raise ValueError('Use a lowercase recipient alias; here is reserved')
    if type(chat_id) is not int or chat_id == 0:
        raise ValueError('A numeric Telegram chat ID is required')
    with workspace_lock(root):
        cfg = store.load_config(root)
        target = {'chat_id': chat_id, 'transport': 'mcp' if server else 'bot'}
        if server:
            target['server'] = server
            check_target(cfg, target, 'send_message')
        cfg.setdefault('telegram', {}).setdefault('recipients', {})[alias] = target
        store.save_config(root, cfg)
    return {'alias': alias, **target}


def check_target(config, target, tool):
    if type(target.get('chat_id')) is not int or target['chat_id'] == 0:
        raise ValueError('Invalid recipient chat ID')
    if target.get('transport') == 'bot':
        return
    if target.get('transport') != 'mcp':
        raise ValueError('Unknown delivery transport')
    server = config.get('mcp_servers', {}).get(target.get('server'), {})
    if (server.get('integration') != 'chigwell/telegram-mcp'
            or target['chat_id'] not in server.get('allowed_chat_ids', [])
            or tool not in server.get('allowed_tools', [])):
        raise ValueError('Recipient or send tool is not permitted by the configured Telegram MCP server')


def project_bytes(root, raw):
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ValueError('Specify a project file path')
    root = root.resolve()
    path = Path(raw)
    if path.is_absolute():
        path = path.relative_to(root)
    if not path.parts or any(p in {'.', '..'} or p.startswith('.') for p in path.parts):
        raise ValueError('Only ordinary project files may be sent; hidden paths are excluded')
    # Walk with directory FDs to reject symlink traversal and replacement races.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Send a regular project file without hard links')
            content = stream.read(attachments.MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not 0 < len(content) <= attachments.MAX_BYTES:
        raise ValueError('File must contain 1 byte to 10 MiB')
    return str(path), content


def instruction(root):
    names = sorted(store.load_config(root).get('telegram', {}).get('recipients', {}))
    return ('\nTelegram delivery capability: when the user requests sending a project file or a message, '
        'return ONLY JSON {"reply":"brief reply", "telegram_actions":[{"to":"here",'
        '"file":"relative/project/path.mp4","text":"caption"}]}. '
        'For a text-only send omit file and supply text. Maximum 4 actions. '
        'Destinations are here (this group/topic) or these registered recipient aliases: '
        + json.dumps(names) + '. Use exact known paths, never invent a file or recipient. '
        'If the path is unknown, inspect using available reading tools or ask the user. '
        'Files must be ordinary non-hidden files inside this project, at most 10 MiB each. '
        'Squad stages the bytes and asks the requesting user to approve the exact send. '
        'Do not send through another messaging tool or claim delivery. '
        'Delivery receipts appear in subsequent conversation context. For ordinary replies, return plain text.\n')


def prepare(bridge, row, answer, db):
    stripped = answer.strip()
    if stripped.startswith('```json') and stripped.endswith('```'):
        stripped = stripped[7:-3].strip()
    if not stripped.startswith('{'):
        return answer
    try:
        data = json.loads(stripped)
    except ValueError:
        return 'The agent returned an incomplete delivery request. Ask it to retry with an exact file path or recipient.'
    if not isinstance(data, dict) or 'telegram_actions' not in data:
        return answer
    actions = data['telegram_actions']
    if not isinstance(actions, list) or not 1 <= len(actions) <= 4 or not isinstance(data.get('reply', ''), str):
        return 'Invalid delivery request; no sends were prepared.'
    cfg = store.load_config(bridge.root)
    prepared = []
    try:
        for index, action in enumerate(actions):
            if not isinstance(action, dict) or set(action) - {'to', 'file', 'text'}:
                raise ValueError('Use only to, file and text in delivery actions')
            alias = action.get('to', 'here')
            if not isinstance(alias, str):
                raise ValueError('Invalid recipient alias')
            target = ({'chat_id': bridge.cfg['chat_id'], 'transport': 'bot'} if alias == 'here'
                      else cfg.get('telegram', {}).get('recipients', {}).get(alias))
            if not isinstance(target, dict):
                raise ValueError('Register the recipient locally first')
            target = dict(target)
            tool = 'send_file' if 'file' in action else 'send_message'
            check_target(cfg, target, tool)
            text = action.get('text', '')
            if not isinstance(text, str) or len(text.encode('utf-16-le')) // 2 > (1000 if tool == 'send_file' else 3000):
                raise ValueError('Delivery text is too long')
            if tool == 'send_message' and not text.strip():
                raise ValueError('Message text is empty')
            action_id = hashlib.sha256((bridge.key + ':' + row['task_id'] + ':' + str(index)).encode()).hexdigest()[:24]
            payload = {'alias': alias, 'target': target, 'text': text, 'tool': tool}
            if tool == 'send_file':
                relative, content = project_bytes(bridge.root, action['file'])
                payload['source'] = relative
                payload['file'] = attachments.save(bridge.root, 'telegram', bridge.key, 'action:' + action_id,
                    Path(relative).name, content, mimetypes.guess_type(relative)[0] or 'application/octet-stream')
            prepared.append((action_id, payload))
    except (ValueError, OSError, TypeError):
        return 'Could not prepare the delivery: check the registered recipient, permitted MCP tools and project file path/size. Nothing was sent.'
    previews = []
    for action_id, payload in prepared:
        db.execute('INSERT OR IGNORE INTO telegram_actions(id,bridge,task_id,user_id,thread_id,message_id,payload,created) VALUES(?,?,?,?,?,?,?,?)',
            (action_id, bridge.key, row['task_id'], row['user_id'], row['thread_id'], row['message_id'], json.dumps(payload), time.time()))
        target = payload['target']
        preview = f'To: {payload["alias"]} (chat {target["chat_id"]}, {target["transport"]})'
        if target.get('server'):
            preview += ' via account server ' + target['server']
        if 'file' in payload:
            preview += f'\nFile: {payload["source"]} ({payload["file"]["size"]} bytes; SHA-256 {payload["file"]["sha256"]})'
        preview += '\nText: ' + (payload['text'] or '(no caption)')
        previews.append(preview + f'\nApprove: /approve {action_id}\nCancel: /cancel {action_id}')
    return 'Delivery prepared; nothing sent yet. Approval expires in 1 hour.\n\n' + '\n\n'.join(previews)


def decide(bridge, message, action_id, approve):
    if not bridge.authorized(message):
        raise ValueError('Unauthorized approval')
    with bridge.broker.db() as db:
        schema(db)
        row = db.execute('SELECT * FROM telegram_actions WHERE id=? AND bridge=?', (action_id.strip(), bridge.key)).fetchone()
        if not row or row['user_id'] != message['from']['id'] or row['thread_id'] != message.get('message_thread_id', 0):
            return 'No delivery proposal for you in this topic.'
        if row['state'] != 'proposed':
            return 'Delivery state: ' + row['state'] + '. It was not submitted again.'
        state = 'approved' if approve else 'cancelled'
        if time.time() - row['created'] > 3600:
            state = 'expired'
        db.execute('UPDATE telegram_actions SET state=? WHERE id=? AND state=\'proposed\'', (state, row['id']))
    return 'Delivery ' + state + '.'


async def mcp_send(root, cfg, payload, thread):
    from .mcp_bridge import ToolHub
    server_name = payload['target']['server']
    config = copy.deepcopy(cfg)
    # Limit this session to the one approved operation and staged file directory.
    server = config['mcp_servers'][server_name]
    server['allowed_tools'] = [payload['tool']]
    args = {'chat_id': payload['target']['chat_id']}
    if payload['tool'] == 'send_file':
        attachments.read_saved(root, payload['file'])
        path = (root / payload['file']['path']).resolve()
        server['file_roots'] = [str(path.parent)]
        server.setdefault('env', {})['TELEGRAM_ALLOWED_ROOTS'] = str(path.parent)
        args.update(file_path=str(path), caption=payload['text'])
    else:
        args['message'] = payload['text']
    async with ToolHub(root, config, [server_name], write=True, timeout=60) as hub:
        tool = next(item for item in hub.catalog if item['name'] == payload['tool'] and item['kind'] == 'tool')
        result = await hub.call(tool['alias'], args)
    # Upstream reports many errors as plain text rather than isError.
    text = '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')
    expected = 'File sent to chat ' if payload['tool'] == 'send_file' else 'Message sent'
    if result.get('isError') or not text.startswith(expected):
        raise RuntimeError('MCP delivery was not acknowledged; inspect destination before retrying')


def flush(bridge):
    from .telegram import TelegramError
    with bridge.broker.db() as db:
        schema(db)
        # A crash after remote acceptance must never cause an automatic repeat.
        db.execute("UPDATE telegram_actions SET state='uncertain',error='Interrupted during delivery' WHERE bridge=? AND state='sending'", (bridge.key,))
        rows = db.execute("SELECT * FROM telegram_actions WHERE bridge=? AND state='approved' ORDER BY created LIMIT 10", (bridge.key,)).fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        state, error = 'sent', ''
        started = False
        try:
            cfg = store.load_config(bridge.root)
            expected = ({'chat_id': bridge.cfg['chat_id'], 'transport': 'bot'} if payload['alias'] == 'here'
                        else cfg.get('telegram', {}).get('recipients', {}).get(payload['alias']))
            if expected != payload['target'] or row['user_id'] not in bridge.cfg['allowed_user_ids']:
                raise ValueError('Recipient or sender authorization changed')
            check_target(cfg, payload['target'], payload['tool'])
            content = attachments.read_saved(bridge.root, payload['file']) if 'file' in payload else None
            with bridge.broker.db() as db:
                if not db.execute("UPDATE telegram_actions SET state='sending' WHERE id=? AND state='approved'", (row['id'],)).rowcount:
                    continue
            if payload['target']['transport'] == 'mcp':
                started = True
                asyncio.run(mcp_send(bridge.root, cfg, payload, row['thread_id']))
            else:
                request = {'chat_id': payload['target']['chat_id']}
                if payload['alias'] == 'here':
                    if row['thread_id']: request['message_thread_id'] = row['thread_id']
                    request['reply_parameters'] = {'message_id': row['message_id'], 'allow_sending_without_reply': True}
                if content is not None:
                    request['caption'] = payload['text']
                    started = True
                    sent = bridge.api.send_document(request, payload['file']['name'], content)
                else:
                    request['text'] = payload['text']
                    started = True
                    sent = bridge.api.request('sendMessage', request)
                if not isinstance(sent, dict) or type(sent.get('message_id')) is not int:
                    raise RuntimeError('No remote message acknowledgement')
        except (ValueError, OSError):
            state = 'uncertain' if started else 'failed'
            error = ('Delivery was not confirmed; inspect the destination before retrying.' if started
                     else 'Local delivery validation failed; prepare a new request.')
        except TelegramError as exc:
            state = 'uncertain' if exc.uncertain else 'failed'
            error = 'Telegram did not acknowledge delivery. Inspect the destination before retrying.'
        except Exception:
            state, error = 'uncertain', 'MCP/transport delivery was not confirmed. Inspect the destination before retrying.'
        with bridge.broker.db() as db:
            db.execute('UPDATE telegram_actions SET state=?,error=? WHERE id=?', (state, error, row['id']))
            bridge._queue(db, 'action-receipt:' + row['id'], f'Delivery {row["id"]}: {state}. {error}',
                          {'thread_id': row['thread_id'], 'reply_to': row['message_id']})


def receipts(broker, task_id):
    with broker.db() as db:
        schema(db)
        row = db.execute('SELECT bridge,thread_id,user_id FROM telegram_turns WHERE task_id=?', (task_id,)).fetchone()
        if not row: return []
        return [dict(r) for r in db.execute('SELECT id,state,error FROM telegram_actions WHERE bridge=? AND thread_id=? AND user_id=? ORDER BY created DESC LIMIT 8', tuple(row))]

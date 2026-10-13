"""Telegram attachment selection and private staging; no arbitrary remote file reads."""
from __future__ import annotations
import mimetypes
import json
from . import attachments

KINDS = ('document', 'photo', 'audio', 'voice', 'video', 'animation', 'video_note')


def has_media(message):
    return any(isinstance(message.get(k), (dict, list)) and message[k] for k in KINDS)


def receive(bridge, message, update_id):
    kind = next((k for k in KINDS if message.get(k)), None)
    if not kind:
        return []
    item = message[kind]
    if kind == 'photo':
        if not isinstance(item, list) or not item or any(not isinstance(p, dict) for p in item):
            raise ValueError('Invalid Telegram photo metadata')
        item = max(item, key=lambda p: int(p.get('width', 0)) * int(p.get('height', 0)))
    if not isinstance(item, dict):
        raise ValueError('Invalid Telegram attachment metadata')
    size = item.get('file_size')
    if size is not None and (type(size) is not int or not 0 < size <= attachments.MAX_BYTES):
        raise ValueError('Telegram attachments must contain 1 byte to 10 MiB')
    content = bridge.api.download(item.get('file_id'))
    if size is not None and len(content) != size:
        raise ValueError('Telegram attachment size does not match its metadata')
    mime = item.get('mime_type') or {'photo': 'image/jpeg', 'voice': 'audio/ogg', 'video_note': 'video/mp4'}.get(kind, 'application/octet-stream')
    if not isinstance(mime, str):
        raise ValueError('Invalid Telegram attachment MIME type')
    name = item.get('file_name') or kind + (mimetypes.guess_extension(mime) or '.bin')
    scope = bridge.key + ':' + str(message.get('message_thread_id', 0))
    return [attachments.save(bridge.root, 'telegram', scope, str(update_id), name, content, mime)]


def caption(text):
    if not isinstance(text, str) or len(text.encode('utf-16-le')) // 2 > 1000:
        raise ValueError('Keep Telegram file captions within 1000 UTF-16 characters')
    return text


def stage(bridge, path, delivery_id, text='', thread_id=0, reply_to=None):
    from .broker import bounded_int
    caption(text)
    bounded_int(thread_id, 'thread_id', 0, 2**53 - 1)
    if reply_to is not None:
        bounded_int(reply_to, 'reply_to', 1, 2**53 - 1)
    name, content = attachments.local_file(path)
    info = attachments.save(bridge.root, 'telegram', bridge.key + ':' + str(thread_id), delivery_id,
                            name, content, mimetypes.guess_type(name)[0] or 'application/octet-stream')
    route = {'file': info, 'thread_id': thread_id, 'reply_to': reply_to}
    with bridge.broker.db() as db:
        db.execute('INSERT OR IGNORE INTO telegram_outbox(bridge,delivery_id,text,route) VALUES(?,?,?,?)',
                   (bridge.key, delivery_id + ':0', text, json.dumps(route)))

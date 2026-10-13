"""Bounded channel attachments; never execute files or unpack archives."""
from __future__ import annotations
import hashlib
import os
import re
import stat
from pathlib import Path
from .locking import serialized

MAX_BYTES = 10 * 1024 * 1024
STORE_BYTES = 100 * 1024 * 1024


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def filename(value: str) -> str:
    value = re.sub(r'[^A-Za-z0-9._-]', '_', str(value).replace('\\', '/').split('/')[-1])[:100]
    return value.lstrip('.') or 'attachment.bin'


def private_directory(root: Path, parts: list[str]) -> Path:
    path = root.resolve()
    for part in ['.squad', 'attachments', *parts]:
        path = path / part
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if path.is_symlink() or not path.is_dir():
            raise ValueError('Attachment directory must be a real directory inside the project')
    return path


@serialized
def save(root: Path, channel: str, scope: str, message_id: str, name: str, content: bytes, mime: str) -> dict:
    if channel not in {'email', 'whatsapp', 'telegram'} or not 0 < len(content) <= MAX_BYTES:
        raise ValueError('Attachment must contain 1 byte to 10 MiB')
    base = private_directory(root, [])
    folder = private_directory(root, [channel, digest(scope)[:24], digest(message_id)[:24]])
    sha = hashlib.sha256(content).hexdigest()
    path = folder / (sha[:16] + '-' + filename(name))
    if not path.exists() and sum(p.stat().st_size for p in base.rglob('*') if p.is_file() and not p.is_symlink()) + len(content) > STORE_BYTES:
        raise ValueError('Attachment storage reached 100 MiB; archive old attachments locally')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('Attachment path collision') from None
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
    return {'name': filename(name), 'path': str(path.relative_to(root.resolve())),
            'size': len(content), 'mime': str(mime)[:150], 'sha256': sha}


def read_saved(root: Path, info: dict) -> bytes:
    relative = Path(info['path'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != ('.squad', 'attachments'):
        raise ValueError('Invalid attachment reference')
    path = root.resolve()
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Attachment symlinks are not allowed')
    with path.open('rb') as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != info['sha256']:
        raise ValueError('Attachment changed; upload it again')
    return content


def describe(root: Path, items: list[dict]) -> str:
    chunks = []
    for info in items[:8]:
        data = read_saved(root, info)
        text = ''
        try:
            decoded = data[:12000].decode('utf-8')
            if '\x00' not in decoded and sum(ord(c) < 32 and c not in '\n\r\t' for c in decoded) == 0:
                text = '\nText excerpt (untrusted):\n' + decoded[:6000]
        except UnicodeDecodeError:
            pass
        if not text:
            text = '\nBinary attachment: no text/vision/audio interpretation was performed by the bridge. Use a supported read-only tool or explain the limitation.'
        chunks.append(f'{info["name"]} ({info["mime"]}, {info["size"]} bytes), local file: {root / info["path"]}' + text)
    return '\n\n'.join(chunks)


def local_file(path: Path) -> tuple[str, bytes]:
    path = path.expanduser()
    with path.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Send a regular file')
        content = stream.read(MAX_BYTES + 1)
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError('Attachment must contain 1 byte to 10 MiB')
    return filename(path.name), content

"""Cooperative, reentrant process/thread locks for a local POSIX workspace."""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_mutex = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def workspace_lock(root: Path, name: str = 'state', blocking: bool = True):
    path = (root / '.squad' / f'{name}.lock').resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _mutex:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=blocking):
        raise RuntimeError(f'{name} is already in use')
    active = getattr(_local, 'active', None)
    if active is None:
        active = _local.active = set()
    try:
        if key in active:
            yield
        else:
            with path.open('a') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError as exc:
                    raise RuntimeError(f'{name} is already in use') from exc
                active.add(key)
                try:
                    yield
                finally:
                    active.remove(key)
                    fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        lock.release()


def serialized(fn):
    @wraps(fn)
    def call(root, *args, **kwargs):
        with workspace_lock(root):
            return fn(root, *args, **kwargs)
    return call

"""Telegram onboarding and additive discovery of installed/configured engines."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from . import engines, store, telegram
from .broker import Broker, identifier
from .diagnostics import setup_checks
from .locking import workspace_lock


def detected_engines(root: Path, config: dict) -> list[dict]:
    """No model calls, vendor --help execution, remote scans or credential reads."""
    table = engines.registry(config)
    checks = {row['engine']: row['issues'] for row in setup_checks(root, config)}
    configured = config.get('engines', {})
    found = []
    for key, spec in table.items():
        try:
            ready, _ = engines.available(spec)
        except (ValueError, TypeError, OSError):
            continue
        if spec.transport == 'cli':
            present = bool(engines.which_binary(spec))
        elif spec.transport == 'custom':
            present = bool(spec.argv and shutil.which(spec.argv[0]))
        elif spec.transport == 'ollama':
            # llama and ollama are two built-in names for the same default model.
            # Keep an explicitly configured llama, but do not add duplicate defaults.
            present = key in configured or (key == 'ollama' and bool(shutil.which('ollama')))
        elif spec.transport == 'openai':
            present = ready
        else:
            present = False
        if present:
            issues = checks.get(key, [])
            if not ready and 'authentication' not in issues:
                issues = [*issues, 'authentication']
            found.append({'engine': key, 'transport': spec.transport, 'issues': issues,
                          'read_only_supported': engines.read_only_supported(spec, configured.get(key, {})),
                          'availability': 'installed' if spec.transport in ('cli', 'custom') else 'configured; endpoint/model unverified'})
    return sorted(found, key=lambda row: row['engine'])


def _alias(key: str, used: dict) -> str:
    base = re.sub('[^a-z0-9_]', '_', key.lower())
    if not base or not base[0].isalpha():
        base = 'agent_' + base
    base = base[:32]
    name, suffix = base, 2
    while name in used:
        tail = '_' + str(suffix)
        name = base[:32-len(tail)] + tail
        suffix += 1
    return name


def _agent_id(key: str, used: dict, channel: str = 'telegram') -> str:
    base = channel + '.' + key
    if len(base) > 90:
        base = base[:80] + '-' + hashlib.sha256(key.encode()).hexdigest()[:8]
    name, suffix = base, 2
    while name in used:
        name = base + '-' + str(suffix)
        suffix += 1
    return identifier(name)


def _roster(root: Path) -> dict:
    path = root / '.squad/coordination.sqlite3'
    if not path.is_file():
        return {'companies': [], 'agents': []}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {'companies': [dict(r) for r in db.execute('SELECT * FROM companies')],
                'agents': [dict(r) for r in db.execute('SELECT * FROM agents')]}


def _plan(root: Path, config: dict, roster: dict, channel: str = 'telegram') -> dict:
    cfg = config[channel]
    aliases = dict(cfg.get('agent_aliases', {}))
    agents = {a['id']: a for a in roster['agents']}
    new_agents, new_aliases, reused, skipped = [], {}, [], []
    detected = detected_engines(root, config)
    for item in detected:
        key = item['engine']
        existing_aliases = [name for name, agent in aliases.items() if agents.get(agent, {}).get('engine') == key]
        if existing_aliases:
            item['aliases'] = existing_aliases
            continue
        if len(aliases) >= 16:
            skipped.append({'engine': key, 'reason': '16-alias limit reached; existing bindings were preserved'})
            continue
        candidates = sorted(a['id'] for a in agents.values() if a['engine'] == key)
        if candidates:
            preferred = channel + '.' + key
            agent = preferred if preferred in candidates else candidates[0]
            reused.append(agent)
        else:
            agent = _agent_id(key, agents, channel)
            entry = {'id': agent, 'engine': key, 'company': channel, 'capabilities': []}
            agents[agent] = entry
            new_agents.append(entry)
        alias = _alias(key, aliases)
        aliases[alias] = agent
        new_aliases[alias] = agent
        item['aliases'] = [alias]
    return {'detected': detected, 'added_agents': new_agents, 'added_aliases': new_aliases,
            'reused_agents': reused, 'agent_aliases': aliases, 'skipped': skipped}


def _sync(root: Path, config: dict, *, dry_run: bool) -> dict:
    # Validate the group and user scope before registering any agents.
    telegram.validate_settings(dict(config['telegram']))
    plan = _plan(root, config, _roster(root))
    cfg = dict(config['telegram'])
    cfg['agent_aliases'] = plan['agent_aliases']
    cfg['allow_chat'] = bool(plan['agent_aliases'])
    cfg.setdefault('auto_discover_agents', True)
    telegram.validate_settings(cfg)
    if not dry_run:
        store.init_workspace(root)
        broker = Broker(root)
        # Never update an existing agent/company. Commit new roster entries first:
        # if JSON saving fails, the next discovery safely reuses those entries.
        with broker.db() as db:
            if plan['added_agents']:
                db.execute('INSERT OR IGNORE INTO companies(id,goal) VALUES(?,?)',
                           ('telegram', 'Agents available for Telegram conversations'))
            for agent in plan['added_agents']:
                db.execute('INSERT INTO agents(id,company,engine,capabilities) VALUES(?,?,?,?)',
                           (agent['id'], agent['company'], agent['engine'], json.dumps(agent['capabilities'])))
        config['telegram'] = cfg
        store.save_config(root, config)
    return {**telegram.config_location(root), **plan, 'dry_run': dry_run,
            'allow_chat': cfg['allow_chat'], 'auto_discover_agents': cfg['auto_discover_agents'],
            'chat_id': cfg['chat_id'], 'allowed_user_ids': cfg['allowed_user_ids'],
            'token_env': cfg['token_env'], 'token_set': bool(os.environ.get(cfg['token_env'])),
            'next': telegram.listen_command(root, bool(cfg['allow_chat'])),
            'note': 'Discovery does not test login/model access or change permissions. A running managed listener reloads new aliases.'}


def discover(root: Path, *, dry_run: bool = False) -> dict:
    if dry_run:
        config = store.load_config(root)
        config['telegram'] = telegram.settings(root)
        return _sync(root, config, dry_run=True)
    with workspace_lock(root):
        config = store.load_config(root)
        config['telegram'] = telegram.settings(root)
        return _sync(root, config, dry_run=False)


def setup(root: Path, *, chat_id: int | None = None, user_ids: list[int] | None = None,
          token_env: str | None = None, allow_tasks: bool | None = None,
          interactive: bool | None = None) -> dict:
    interactive = sys.stdin.isatty() if interactive is None else interactive
    current = dict(store.load_config(root).get('telegram') or {})
    if chat_id is None and type(current.get('chat_id')) is not int:
        if not interactive:
            raise ValueError('Telegram setup needs --chat-id and --user-id, or saved Telegram settings')
        chat_id = int(input('Telegram group chat ID (negative for a group): ').strip())
    if user_ids is None and not current.get('allowed_user_ids'):
        if not interactive:
            raise ValueError('Telegram setup needs --user-id, or saved allowed_user_ids')
        raw = input('Allowed Telegram user IDs (comma separated): ').strip()
        user_ids = [int(value) for value in re.split(r'[,\s]+', raw) if value]
    # Prompts happen outside the lock; merge with the latest settings afterward.
    with workspace_lock(root):
        config = store.load_config(root)
        cfg = dict(config.get('telegram') or {})
        if chat_id is not None:
            if chat_id != cfg.get('chat_id'):
                cfg['mail_since'] = store.utc_now()
            cfg['chat_id'] = chat_id
        if user_ids is not None:
            cfg['allowed_user_ids'] = sorted(set(user_ids))
        if token_env is not None:
            cfg['token_env'] = token_env
        if allow_tasks is not None:
            cfg['allow_tasks'] = allow_tasks
        cfg.setdefault('mail_since', store.utc_now())
        cfg.setdefault('auto_discover_agents', True)
        config['telegram'] = cfg
        return _sync(root, config, dry_run=False)

"""Opt-in chigwell/telegram-mcp preset and local chat/size policy."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import engines, store
from .broker import identifier
from .locking import workspace_lock

UPSTREAM = 'https://github.com/chigwell/telegram-mcp'
REVISION = '81ad14bd076d17babd8be2425965235b0c6b9e26'
INTEGRATION = 'chigwell/telegram-mcp'
READ_TOOLS = ('get_chat', 'get_messages', 'search_messages', 'get_message_context')
SEND_TOOLS = ('send_message', 'reply_to_message')
FILE_TOOLS = ('send_file',)
CREDENTIAL_ENV = ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH',
                  'TELEGRAM_SESSION_STRING', 'TELEGRAM_SESSION_NAME')


def configure(root: Path, checkout: Path, selected: list[str], chats: list[int],
              allow_send: bool = False, name: str = 'telegram-account', uv: str = 'uv', *, allow_files: bool = False) -> dict:
    identifier(name)
    checkout = checkout.expanduser().resolve()
    for path in ('main.py', 'pyproject.toml', 'session_string_generator.py', 'telegram_mcp/runner.py'):
        if not (checkout / path).is_file():
            raise ValueError(f'Expected a chigwell/telegram-mcp checkout: missing {path}')
    if 'github.com/chigwell/telegram-mcp' not in (checkout / 'pyproject.toml').read_text():
        raise ValueError('Checkout metadata does not identify chigwell/telegram-mcp')
    executable = shutil.which(uv)
    if not executable:
        raise ValueError('uv is not installed; install uv, then run uv sync in the Telegram MCP checkout')
    revision = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=10)
    if revision.returncode:
        raise ValueError('Use a Git checkout of chigwell/telegram-mcp')
    with workspace_lock(root):
        cfg = store.load_config(root)
        selected = list(dict.fromkeys(selected))
        if not selected:
            raise ValueError('Select at least one engine')
        for engine in selected:
            identifier(engine)
            spec = engines.resolve(engine, cfg)
            if spec.transport not in {'openai', 'ollama'} and spec.key != 'codex':
                raise ValueError('Automatic attachment supports codex, OpenAI-compatible and Ollama engines; use mcp proxy-config for other MCP clients')
        chats = sorted(set(chats or [cfg.get('telegram', {}).get('chat_id')]))
        if not chats or any(type(chat) is not int or chat == 0 for chat in chats):
            raise ValueError('Specify --chat-id, or configure the bot bridge chat first')
        old = cfg.get('mcp_servers', {}).get(name)
        if old and old.get('integration') != INTEGRATION:
            raise ValueError('Server name already belongs to another integration; choose --name')
        server = {
            'integration': INTEGRATION, 'upstream': UPSTREAM,
            'revision': revision.stdout.strip(), 'reviewed_revision': REVISION,
            'transport': 'stdio', 'command': executable,
            'args': ['--directory', str(checkout), 'run', '--frozen', '--no-sync', 'main.py'],
            'env_allow': list(CREDENTIAL_ENV),
            'env': {'TELEGRAM_EXPOSED_TOOLS': 'read-only' + ('+' + ','.join(SEND_TOOLS) if allow_send else ''),
                    'TELEGRAM_ALLOWED_CHAT_IDS': ','.join(str(chat) for chat in chats),
                    'TELEGRAM_TRANSCRIBE': 'off', 'TELEGRAM_EVENT_FEED': '0',
                    'TELEGRAM_SESSION_LOCK': 'exclusive'},
            'allowed_tools': list(READ_TOOLS + (SEND_TOOLS if allow_send else ()) + (FILE_TOOLS if allow_files else ())),
            'read_only_tools': list(READ_TOOLS), 'allowed_chat_ids': chats,
            'file_roots': [str(root.resolve())],
        }
        cfg.setdefault('mcp_servers', {})[name] = server
        for engine in selected:
            settings = cfg.setdefault('engines', {}).setdefault(engine, {})
            servers = settings.setdefault('mcp_servers', [])
            if name not in servers:
                servers.append(name)
        store.save_config(root, cfg)
    return {'server': name, 'engines': selected, 'chat_ids': chats, 'allow_send': allow_send,
            'revision': server['revision'], 'matches_reviewed_revision': server['revision'] == REVISION,
            'next': 'Authenticate in the upstream checkout, then run squad mcp inspect --server ' + name}


def status(root: Path, name: str) -> dict:
    cfg = store.load_config(root)
    server = cfg.get('mcp_servers', {}).get(name, {})
    if server.get('integration') != INTEGRATION:
        raise ValueError('Telegram MCP is not configured under this name')
    return {'server': name, 'upstream': UPSTREAM, 'revision': server.get('revision'),
            'chat_ids': server.get('allowed_chat_ids', []),
            'tools': server.get('allowed_tools', []),
            'engines': [key for key, value in cfg.get('engines', {}).items()
                        if name in value.get('mcp_servers', [])],
            'authentication': 'Run mcp inspect to check the upstream session; status does not log in or read secrets.'}


def launch_env(cfg: dict, write: bool) -> dict:
    """Clamp the upstream tool surface even if the parent environment enables writes."""
    env = dict(cfg.get('env', {}))
    if cfg.get('integration') == INTEGRATION:
        if set(cfg.get('allowed_tools', [])) - set(READ_TOOLS + SEND_TOOLS + FILE_TOOLS):
            raise ValueError('Telegram MCP preset contains unsupported tools')
        env['TELEGRAM_EXPOSED_TOOLS'] = 'read-only'
        sends = [tool for tool in SEND_TOOLS + FILE_TOOLS if tool in cfg.get('allowed_tools', [])]
        if write and sends:
            env['TELEGRAM_EXPOSED_TOOLS'] += '+' + ','.join(sends)
        chats = cfg.get('allowed_chat_ids')
        if not isinstance(chats, list) or not chats or any(type(x) is not int or x == 0 for x in chats):
            raise ValueError('Telegram MCP needs a nonempty numeric chat allowlist')
        env['TELEGRAM_ALLOWED_CHAT_IDS'] = ','.join(map(str, chats))
        if 'send_file' in sends:
            roots = cfg.get('file_roots', [])
            if not roots or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
                raise ValueError('Telegram file sends require explicit absolute file_roots')
            env['TELEGRAM_ALLOWED_ROOTS'] = __import__('os').pathsep.join(roots)
        env.update(TELEGRAM_TRANSCRIBE='off', TELEGRAM_EVENT_FEED='0', TELEGRAM_SESSION_LOCK='exclusive')
    return env


def validate_call(cfg: dict, tool: str, arguments: dict) -> None:
    if cfg.get('integration') != INTEGRATION:
        return
    if tool not in READ_TOOLS + SEND_TOOLS + FILE_TOOLS:
        raise ValueError('Tool is outside the Telegram MCP preset')
    chat = arguments.get('chat_id')
    if isinstance(chat, str) and chat.lstrip('-').isdigit():
        chat = int(chat)
    if type(chat) is not int or chat not in cfg.get('allowed_chat_ids', []):
        raise ValueError('Telegram chat is not in the configured allowlist')
    if tool == 'send_file':
        from .telegram_actions import project_bytes
        raw = arguments.get('file_path')
        if not isinstance(raw, str) or arguments.get('schedule_date') is not None:
            raise ValueError('Send one local file now; arrays and scheduling are not enabled')
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError('Telegram MCP requires an absolute file path')
        matched = False
        for root in cfg.get('file_roots', []):
            if path.is_relative_to(Path(root)):
                project_bytes(Path(root), str(path)); matched = True; break
        if not matched:
            raise ValueError('File is outside the configured Telegram file roots')
    if arguments.get('account') not in (None, 'default'):
        raise ValueError('This preset is bound to the default Telegram account')
    for key, default, maximum in (('page', 1, 10000), ('page_size', 20, 50),
                                  ('limit', 20, 50), ('context_size', 3, 10)):
        value = arguments.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{key} must be between 1 and {maximum}')
    for key in ('message', 'text', 'query'):
        if key in arguments and (not isinstance(arguments[key], str) or len(arguments[key]) > 4000):
            raise ValueError(f'{key} must be a string of at most 4000 characters')


def bounded_result(cfg: dict, result: dict) -> dict:
    if cfg.get('integration') != INTEGRATION:
        return result
    # Preserve short structured results, but never send a whole history to a model.
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) <= 12000:
        return result
    return {'content': [{'type': 'text', 'text': serialized[:11500] +
                         '\n[Truncated by Squad. Request a smaller page or narrower search.]'}],
            'isError': bool(result.get('isError'))}

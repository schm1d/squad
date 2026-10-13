"""Antigravity's native tool allowlist for Squad read-only invocations.

`--mode plan` is an instruction prefix, not an access boundary. Select a
dedicated native agent with only documented file-reading tools instead.
https://antigravity.google/docs/subagents/
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


def name(root: Path) -> str:
    digest = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
    return 'squad-reader-v1-' + digest


def definition(root: Path) -> str:
    return f'''---
name: {name(root)}
description: Read-only Squad conversation and code inspection.
tools:
  - view_file
  - grep_search
mainAgent: true
subagent: false
commandExecutionPolicy: "off"
mcpServers: []
skills: []
plugins: []
---
Answer the Squad request using its supplied context and the available reading tools.
Report unavailable information honestly. Do not claim to have run commands or edited files.
'''


def validate_args(extra: list[str]) -> None:
    """Prevent extra CLI options from selecting a more powerful agent/session."""
    permitted = {'--model', '--effort', '--print-timeout'}
    index = 0
    while index < len(extra):
        flag, equal, value = extra[index].partition('=')
        if flag not in permitted:
            raise ValueError('Unsupported read-only agy option: ' + flag)
        if not equal:
            index += 1
            value = extra[index] if index < len(extra) else ''
        if not value or value.startswith('-'):
            raise ValueError('Missing value for read-only agy option: ' + flag)
        index += 1


def check_cli(binary: str) -> None:
    """Check local flag support without starting an authenticated conversation."""
    result = subprocess.run([binary, '--help'], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, errors='replace', timeout=5)
    help_text = result.stdout + result.stderr
    required = ('--agent', '--disable-slash-commands', '--mode', '--output-format')
    if result.returncode or any(flag not in help_text for flag in required):
        raise ValueError('Installed agy CLI lacks the required --agent/--disable-slash-commands flags; run agy update and check agy --help')


def prepare(root: Path) -> Path:
    """Create an additive project agent; never overwrite an existing definition."""
    root = root.resolve()
    agent_name = name(root)
    # Avoid an obvious global/name collision. Local configuration is trusted,
    # but Squad never changes a user's global agent or permission settings.
    global_agents = Path.home() / '.gemini/config/agents'
    for candidate in (global_agents / (agent_name + '.md'), global_agents / agent_name):
        if candidate.exists() or candidate.is_symlink():
            raise ValueError('Reserved Squad agy agent name already exists globally; rename that local custom agent')
    directory = root
    for part in ('.agents', 'agents'):
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError('Squad agy profile directory must not be a symlink')
    alternate = directory / agent_name
    if alternate.exists() or alternate.is_symlink():
        raise ValueError('Reserved Squad agy agent name already has a directory definition')
    target = directory / (agent_name + '.md')
    expected = definition(root).encode()
    fd, temporary = tempfile.mkstemp(prefix='.squad-reader-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(expected)
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
        raise ValueError('Squad agy read-only profile was modified; inspect it locally before restoring the generated definition')
    return target

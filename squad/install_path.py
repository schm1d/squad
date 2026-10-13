"""Explicit, idempotent shell PATH configuration; never runs at import/pip time."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

START = '# >>> squad PATH >>>'
END = '# <<< squad PATH <<<'


def configure_path(bin_dir: Path, shell: str = 'auto', home: Path | None = None) -> list[str]:
    home = home or Path.home()
    if shell == 'auto':
        shell = Path(os.environ.get('SHELL', '')).name
    if shell not in {'bash', 'zsh', 'fish'}:
        raise ValueError('Choose --shell bash, zsh or fish; PATH was not changed')
    path = str(bin_dir.expanduser().resolve())
    if '\n' in path or '\r' in path:
        raise ValueError('PATH directory cannot contain newlines')
    quoted = shlex.quote(path)
    if shell == 'fish':
        # Fish single quotes use backslash escapes, not POSIX close/open quotes.
        quoted = "'" + path.replace('\\', '\\\\').replace("'", "\\'") + "'"
        targets = [home / '.config/fish/conf.d/squad-path.fish']
        body = f'fish_add_path --move --path {quoted}'
    else:
        targets = [Path(os.environ.get('ZDOTDIR') or home) / '.zshrc'] if shell == 'zsh' else [home / '.bashrc', home / ('.bash_profile' if (home / '.bash_profile').exists() else '.profile')]
        body = f'case ":$PATH:" in\n  *:{quoted}:*) ;;\n  *) export PATH={quoted}:"$PATH" ;;\nesac'
    block = f'{START}\n{body}\n{END}'
    changes = []
    # Validate every target before changing any of them.
    prepared = []
    for target in targets:
        old = target.read_text(encoding='utf-8') if target.exists() else ''
        if old.count(START) != old.count(END) or old.count(START) > 1:
            raise ValueError(f'Malformed Squad PATH block in {target}; inspect it manually')
        if START in old:
            start, end = old.index(START), old.index(END) + len(END)
            if end < start:
                raise ValueError(f'Malformed Squad PATH block in {target}')
            new = old[:start] + block + old[end:]
        else:
            new = old + ('\n' if old and not old.endswith('\n') else '') + '\n' + block + '\n'
        prepared.append((target, old, new))
    for target, old, new in prepared:
        if old != new:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding='utf-8')
            changes.append(str(target))
    return changes

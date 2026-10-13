#!/usr/bin/env python3
"""Install Squad in an isolated environment, optionally adding its command to PATH."""
import argparse
import ast
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import venv
from datetime import datetime, timezone
from pathlib import Path

from squad.install_path import configure_path
from squad.project_check import print_project_check
from squad.diagnostics import print_setup_checks


def launcher_state(path):
    """Snapshot the directory entry, including dangling links, without running it."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)


def is_squad_launcher(path):
    """Recognize pip/pipx console scripts by their entry point, not their location."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return False
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, SyntaxError, RuntimeError):
        return False
    entry_names = {alias.asname or alias.name for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module == 'squad.cli'
                   for alias in node.names if alias.name == 'main'}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in entry_names:
            return True
        # Older setuptools-generated wrappers use load_entry_point.
        if isinstance(node.func, ast.Name) and node.func.id == 'load_entry_point' and len(node.args) >= 3:
            args = node.args[:3]
            if all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in args):
                distribution, group, command = (arg.value for arg in args)
                if (re.match(r'^squad[-_]team[-_]mode(?:\s|[<>=!~\[]|$)', distribution, re.I)
                        and group == 'console_scripts' and command == 'squad'):
                    return True
    return False


def launcher_kind(link, executable):
    state = launcher_state(link)
    if state is None:
        return 'missing'
    if not (stat.S_ISREG(state[2]) or stat.S_ISLNK(state[2])):
        return 'unsupported'
    if link.is_symlink():
        try:
            if link.resolve() == executable.resolve():
                return 'current'
        except (OSError, RuntimeError):
            pass
    return 'squad' if is_squad_launcher(link) else 'unknown'


def publish_launcher(link, executable, expected_state):
    """Back up an old command and atomically publish the newly installed one."""
    if launcher_state(link) != expected_state:
        raise ValueError(f'{link} changed during installation; rerun the installer')
    if not executable.is_file():
        raise ValueError(f'Installation did not create {executable}; existing command was preserved')
    if launcher_kind(link, executable) == 'current':
        return None
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f'.squad-install-{uuid.uuid4().hex}')
    backup = None
    try:
        temporary.symlink_to(executable)
        if expected_state is not None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = link.with_name(f'squad.backup-{stamp}-{uuid.uuid4().hex[:8]}')
            if link.is_symlink():
                backup.symlink_to(os.readlink(link))
            else:
                shutil.copy2(link, backup)
        if launcher_state(link) != expected_state:
            raise ValueError(f'{link} changed during installation; existing command was preserved')
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/squad')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--shell', choices=['auto', 'bash', 'zsh', 'fish'], default='auto')
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--add-to-path', dest='path', action='store_true')
    flags.add_argument('--no-add-to-path', dest='path', action='store_false')
    parser.set_defaults(path=None)
    parser.add_argument('--without-mcp', action='store_true', help='Install only the dependency-free coordinator')
    parser.add_argument('--force', action='store_true', help='Back up and replace an unrecognized or broken squad launcher')
    parser.add_argument('--project', type=Path, default=None,
                        help='Project for checks and optional messaging setup (default: discover from current directory)')
    onboarding = parser.add_mutually_exclusive_group()
    onboarding.add_argument('--setup-telegram', dest='setup_telegram', action='store_true', help='Configure Telegram and discover agents after installing')
    onboarding.add_argument('--no-setup-telegram', dest='setup_telegram', action='store_false')
    parser.set_defaults(setup_telegram=None)
    parser.add_argument('--telegram-chat-id', type=int)
    parser.add_argument('--telegram-user-id', type=int, action='append')
    parser.add_argument('--telegram-token-env')
    parser.add_argument('--setup-email', action='store_true', help='Configure SMTP/IMAP agent conversations after installation')
    parser.add_argument('--setup-whatsapp', action='store_true', help='Configure WhatsApp Business Cloud API agent conversations after installation')
    args = parser.parse_args(argv)
    if os.name != 'posix':
        parser.error('This release supports Linux and macOS')
    if args.path is None:
        args.path = sys.stdin.isatty() and input('Add squad to your shell PATH? [y/N] ').strip().lower() in {'y', 'yes'}
    if args.setup_telegram is None:
        args.setup_telegram = sys.stdin.isatty() and input('Set up Telegram and discover agents for a project? [y/N] ').strip().lower() in {'y', 'yes'}
    if not args.setup_telegram and any(value is not None for value in (args.telegram_chat_id, args.telegram_user_id, args.telegram_token_env)):
        parser.error('Telegram options require --setup-telegram')
    if (args.setup_telegram or args.setup_email or args.setup_whatsapp) and args.project is None:
        if not sys.stdin.isatty():
            parser.error('Channel setup requires --project in noninteractive installations')
        selected = input('Project folder where agents will work: ').strip()
        if not selected:
            parser.error('Enter a project folder or use --project')
        args.project = Path(selected)
    prefix, bin_dir = args.prefix.expanduser().resolve(), args.bin_dir.expanduser().resolve()
    executable = prefix / 'venv/bin/squad'
    link = bin_dir / 'squad'
    previous_state = launcher_state(link)
    if args.path:
        kind = launcher_kind(link, executable)
        if kind == 'unsupported':
            parser.error(f'{link} is not a regular file or symlink; choose another --bin-dir')
        if kind == 'unknown' and not args.force:
            parser.error(f'{link} is not a recognized Squad launcher; use --force to back it up and replace it, or choose --bin-dir')
    shell = Path(os.environ.get('SHELL', '')).name if args.shell == 'auto' else args.shell
    if args.path and shell not in {'bash', 'zsh', 'fish'}:
        parser.error('Choose --shell bash, zsh or fish')
    if args.project is not None and not args.project.expanduser().is_dir():
        parser.error('--project must name an existing project directory')
    project = print_project_check(args.project if args.project is not None else Path.cwd(), discover=args.project is None)
    print_setup_checks(Path(project['path']))
    print('Project warnings do not prevent installation or use of other engines.')
    venv.EnvBuilder(with_pip=True).create(prefix / 'venv')
    python = prefix / 'venv/bin/python'
    source = str(Path(__file__).resolve().parent) + ('' if args.without_mcp else '[mcp]')
    subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', source], check=True)
    if args.path:
        backup = publish_launcher(link, executable, previous_state)
        changed = configure_path(bin_dir, shell)
        print('Squad is installed. Open a new terminal to use squad.')
        if backup is not None:
            print(f'Previous launcher backed up to {backup}')
        for path in changed:
            print(f'Updated {path}')
    else:
        print(f'Squad is installed at {executable}. Shell PATH was not changed.')
    if args.setup_telegram:
        from squad.onboarding import setup
        import json
        try:
            report = setup(Path(project['path']), chat_id=args.telegram_chat_id,
                           user_ids=args.telegram_user_id, token_env=args.telegram_token_env)
        except (ValueError, OSError, RuntimeError) as exc:
            import shlex
            print(f'Squad was installed, but Telegram setup did not finish: {exc}', file=sys.stderr)
            print('Run squad telegram setup --root ' + shlex.quote(str(project['path'])), file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2))
        if not report['token_set']:
            print(f'Set {report["token_env"]} in your environment before starting the listener. The bot token was not saved in JSON.')
    for channel in ('email', 'whatsapp'):
        if getattr(args, 'setup_' + channel):
            from squad.channel_cli import setup
            import json
            try:
                print(json.dumps(setup(Path(project['path']), channel), indent=2))
            except (ValueError, OSError, RuntimeError) as exc:
                import shlex
                print(f'Squad was installed, but {channel} setup did not finish: {exc}', file=sys.stderr)
                print(f'Run squad {channel} setup --root ' + shlex.quote(str(project['path'])), file=sys.stderr)
                return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        raise SystemExit(1)

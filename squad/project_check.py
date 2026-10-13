"""Read-only project checks shared by installation and workspace setup."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .store import find_root


def check_project(start: Path, *, discover: bool = True) -> dict:
    """Check the directory Squad will use; never initialize Git or change trust."""
    root = find_root(start.expanduser()) if discover else start.expanduser().resolve()
    result = {'path': str(root), 'status': '', 'detail': '', 'hint': ''}

    def report(status: str, detail: str, hint: str = '') -> dict:
        return dict(result, status=status, detail=detail, hint=hint)

    if not root.is_dir():
        return report('not-directory', 'Project directory does not exist.',
                      'Choose an existing project folder with --project PATH (installer) or --root PATH (Squad).')
    if root == Path.home().resolve() or root == Path(root.anchor):
        return report('broad-directory', 'Select a specific project folder, rather than your home or filesystem root.',
                      'Use --project PATH (installer) or --root PATH (Squad). Do not initialize Git here.')
    git = shutil.which('git')
    if not git:
        return report('git-unavailable', 'Git is not on PATH; the project check could not run.',
                      'Install Git, then run squad doctor --root ' + shlex.quote(str(root)))
    # Inspect the on-disk project, not a repository selected by ambient GIT_DIR,
    # GIT_WORK_TREE or injected Git configuration environment variables.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['LC_ALL'] = 'C'
    try:
        probe = subprocess.run([git, '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
                               capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return report('git-error', 'Git could not inspect this project.',
                      'Check locally with: git -C ' + shlex.quote(str(root)) + ' status')
    if probe.returncode == 0:
        if probe.stdout.strip() == 'true':
            return report('git-worktree', 'Git working tree found (Codex repository prerequisite).')
        return report('not-worktree', 'This is a Git directory without a working tree.',
                      'Select the project working tree with --project PATH (installer) or --root PATH (Squad).')
    marker = (root / '.git').exists() or (root / '.git').is_symlink()
    if not marker and 'not a git repository' in probe.stderr.lower():
        return report('no-git', 'No Git working tree found. Codex may reject tasks with "Not inside a trusted directory".',
                      'If this is your intended project, initialize it with: git -C '
                      + shlex.quote(str(root)) + ' init\n'
                      'Or choose an existing repository with --project PATH (installer) or --root PATH (Squad).')
    return report('git-error', 'Git could not validate the project; check repository access or ownership.',
                  'Inspect the error locally with: git -C ' + shlex.quote(str(root)) + ' status')


def print_project_check(start: Path, *, discover: bool = True) -> dict:
    result = check_project(start, discover=discover)
    print('Project check: ' + result['path'])
    print(('  OK: ' if result['status'] == 'git-worktree' else '  WARNING: ') + result['detail'])
    if result['hint']:
        print('  ' + result['hint'].replace('\n', '\n  '))
    print('  This checks Git only; Codex login, trust configuration and model access are not tested.')
    return result

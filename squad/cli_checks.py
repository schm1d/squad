"""Offline CLI prerequisites. These checks never connect to runtime sockets."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def grok_socket_paths() -> list[Path]:
    # Matches Grok's system/rootless Docker, Podman, containerd and D-Bus paths.
    # These are OS endpoints, not user project paths.
    paths = [Path(prefix) / suffix for prefix in ('/run', '/var/run')
             for suffix in ('docker.sock', 'podman/podman.sock', 'containerd/containerd.sock',
                            'dbus/system_bus_socket', 'systemd/private')]
    runtime = Path('/run/user') / str(os.getuid())
    paths += [runtime / suffix for suffix in ('docker.sock', 'podman/podman.sock',
                                              'containerd/containerd.sock', 'bus', 'systemd/private')]
    paths += [Path.home() / '.docker' / suffix / 'docker.sock' for suffix in ('desktop', 'run')]
    return paths


def grok_sandbox_checks() -> list[str]:
    if not sys.platform.startswith('linux'):
        return []
    issues = []
    if not shutil.which('bwrap'):
        issues.append('grok_bwrap_missing')
    for path in grok_socket_paths():
        try:
            # Resolve only the parent, like Grok: a symlink endpoint is rejected.
            path.parent.resolve(strict=True).joinpath(path.name).lstat()
            if path.is_symlink():
                issues.append('grok_socket_unverified')
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError):
            issues.append('grok_socket_unverified')
    return list(dict.fromkeys(issues))

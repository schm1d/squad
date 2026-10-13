"""Reinstall regression coverage for pip scripts, old links, and failed upgrades."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install

PIP_LAUNCHER = '#!/usr/bin/python3\nfrom squad.cli import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


class ReinstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.link = self.bin / 'squad'
        self.prefix = self.root / 'app'
        self.executable = self.prefix / 'venv/bin/squad'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text('#!/bin/sh\nprintf "installed\\n"\n')
        self.executable.chmod(0o755)
        self.args = ['--prefix', str(self.prefix), '--bin-dir', str(self.bin), '--add-to-path', '--shell', 'bash']

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, extra=(), pip_side_effect=None):
        with patch('install.venv.EnvBuilder'), patch('install.subprocess.run', side_effect=pip_side_effect), \
             patch('install.print_project_check', return_value={'path': str(self.root)}), patch('install.print_setup_checks'), \
             patch('install.configure_path') as configure, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(self.args + list(extra)), 0)
            configure.assert_called_once()

    def assert_works(self):
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), self.executable)
        self.assertEqual(subprocess.check_output([str(self.link)], text=True), 'installed\n')

    def test_migrate_pip_console_script_then_reinstall_twice(self):
        self.link.write_text(PIP_LAUNCHER); self.link.chmod(0o755)
        self.run_install()
        self.assert_works()
        backup = list(self.bin.glob('squad.backup-*'))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_text(), PIP_LAUNCHER)
        self.assertEqual(backup[0].stat().st_mode & 0o777, 0o755)
        self.run_install(); self.run_install()
        self.assert_works()
        self.assertEqual(list(self.bin.glob('squad.backup-*')), backup)

    def test_upgrade_link_from_previous_prefix_preserves_old_installation(self):
        old = self.root / 'old/bin/squad'; old.parent.mkdir(parents=True)
        old.write_text(PIP_LAUNCHER)
        self.link.symlink_to('../old/bin/squad')
        self.run_install()
        self.assert_works()
        self.assertEqual(old.read_text(), PIP_LAUNCHER)
        backup = next(self.bin.glob('squad.backup-*'))
        self.assertTrue(backup.is_symlink())
        self.assertEqual(os.readlink(backup), '../old/bin/squad')

    def test_old_setuptools_entry_point_is_recognized(self):
        self.link.write_text("#!/usr/bin/python3\nfrom pkg_resources import load_entry_point\nload_entry_point('squad-team-mode==0.4.0', 'console_scripts', 'squad')()\n")
        self.run_install(); self.assert_works()

    def test_force_recovers_dangling_legacy_link_with_backup(self):
        self.link.symlink_to('../removed-install/bin/squad')
        self.run_install(['--force'])
        self.assert_works()
        backup = next(self.bin.glob('squad.backup-*'))
        self.assertEqual(os.readlink(backup), '../removed-install/bin/squad')

    def test_failed_install_leaves_previous_command_and_shell_untouched(self):
        self.link.write_text(PIP_LAUNCHER)
        with patch('install.venv.EnvBuilder'), patch('install.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['pip'])), \
             patch('install.print_project_check', return_value={'path': str(self.root)}), patch('install.print_setup_checks'), \
             patch('install.configure_path') as configure:
            with self.assertRaises(subprocess.CalledProcessError): install.main(self.args)
        configure.assert_not_called()
        self.assertFalse(self.link.is_symlink())
        self.assertEqual(self.link.read_text(), PIP_LAUNCHER)
        self.assertEqual(list(self.bin.glob('squad.backup-*')), [])

    def test_changed_command_during_install_is_not_overwritten(self):
        self.link.write_text(PIP_LAUNCHER)
        def changed(*args, **kwargs): self.link.write_text('replacement by another process')
        with self.assertRaisesRegex(ValueError, 'changed during installation'):
            self.run_install(pip_side_effect=changed)
        self.assertEqual(self.link.read_text(), 'replacement by another process')

    def test_force_does_not_replace_a_directory(self):
        self.link.mkdir()
        with patch('install.venv.EnvBuilder') as builder, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): install.main(self.args + ['--force'])
        builder.assert_not_called(); self.assertTrue(self.link.is_dir())


if __name__ == '__main__': unittest.main()

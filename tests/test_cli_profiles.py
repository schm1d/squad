"""Native read-only profiles and sandbox setup diagnostics; no vendor inference."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from squad import agy_profile, cli_checks, diagnostics, engines, store, telegram
from squad.broker import Broker
from squad.worker import work_once
from test_telegram_chat import GroupBot, incoming


GROK_ERROR = '''error: sandbox profile resolve failed: socket deny resolution failed: could not resolve runtime-socket deny path /run/podman/podman.sock: Permission denied (os error 13)
error: this sandbox could not enforce its deny list on Linux: the required bwrap plan could not be prepared; see the error above for the specific cause. Refusing to start with denied paths unprotected.'''


class NativeProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project with spaces'; self.root.mkdir()
        self.home = Path(self.tmp.name) / 'home'; self.home.mkdir()
        self.home_patch = patch('pathlib.Path.home', return_value=self.home); self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop(); self.tmp.cleanup()

    def test_agy_has_only_native_reading_tools_and_no_permission_bypass(self):
        cmd = engines.build_command(engines.BUILTINS['agy'], 'Hi', cwd=self.root, write=False)
        self.assertEqual(cmd[cmd.index('--agent') + 1], agy_profile.name(self.root))
        self.assertIn('--disable-slash-commands', cmd)
        self.assertNotIn('--dangerously-skip-permissions', cmd)
        self.assertFalse((self.root / '.agents').exists())  # construction/dry-run is pure
        target = agy_profile.prepare(self.root)
        self.assertEqual(target.read_text(), agy_profile.definition(self.root))
        text = target.read_text()
        self.assertIn('tools:\n  - view_file\n  - grep_search\n', text)
        self.assertNotIn('run_command', text); self.assertNotIn('replace_file_content', text)
        self.assertNotIn('invoke_subagent', text)
        self.assertIn('mcpServers: []', text)
        self.assertEqual(agy_profile.prepare(self.root), target)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_concurrent_prepare_is_atomic_and_modified_profile_is_not_overwritten(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(agy_profile.prepare, [self.root] * 12))
        self.assertEqual(len(set(paths)), 1)
        paths[0].write_text('tools: [run_command]')
        with self.assertRaisesRegex(ValueError, 'modified'):
            agy_profile.prepare(self.root)
        self.assertEqual(paths[0].read_text(), 'tools: [run_command]')

    def test_profile_symlink_and_name_collisions_fail_closed(self):
        external = self.home / 'agents'; external.mkdir()
        (self.root / '.agents').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            agy_profile.prepare(self.root)
        self.assertEqual(list(external.iterdir()), [])
        (self.root / '.agents').unlink()
        target = agy_profile.prepare(self.root); target.unlink()
        other = self.home / 'definition'; other.write_text(agy_profile.definition(self.root))
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'modified'):
            agy_profile.prepare(self.root)
        target.unlink()
        global_path = self.home / '.gemini/config/agents' / (agy_profile.name(self.root) + '.md')
        global_path.parent.mkdir(parents=True); global_path.write_text('private existing agent')
        with self.assertRaisesRegex(ValueError, 'globally'):
            agy_profile.prepare(self.root)
        self.assertEqual(global_path.read_text(), 'private existing agent')

    def test_read_only_args_cannot_select_other_agent_or_resume_or_enable_tools(self):
        for flags in (['--agent', 'self'], ['--dangerously-skip-permissions'], ['--mode=accept-edits'],
                      ['--continue'], ['--conversation', 'previous'], ['--project', 'other'],
                      ['--add-dir', '/tmp'], ['--disable-slash-commands=false'], ['--model', '--agent=other']):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                engines.build_command(engines.BUILTINS['agy'], 'Hi', cwd=self.root, write=False, extra_args=flags)
        agy_profile.validate_args(['--model', 'example', '--effort=low', '--print-timeout', '30s'])
        cmd = engines.build_command(engines.BUILTINS['agy'], 'Edit', cwd=self.root, write=True)
        self.assertIn('accept-edits', cmd); self.assertNotIn('--agent', cmd)
        self.assertFalse(engines.read_only_supported(engines.EngineSpec('other', 'Other', 'Google', 'cli', family='gemini'), {}))

    def test_help_probe_is_offline_and_rejects_old_cli(self):
        good = subprocess.CompletedProcess(['agy', '--help'], 0,
                '--agent --disable-slash-commands --mode --output-format', '')
        with patch('squad.agy_profile.subprocess.run', return_value=good) as run:
            agy_profile.check_cli('/bin/agy')
        self.assertEqual(run.call_args.args[0], ['/bin/agy', '--help'])
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        with patch('squad.agy_profile.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '--mode plan', '')):
            with self.assertRaisesRegex(ValueError, 'agy update'):
                agy_profile.check_cli('agy')

    def test_worker_checks_profile_and_cli_before_launch_no_fallback(self):
        proc = Mock(returncode=0); proc.communicate.return_value = ('Hello', '')
        with patch('squad.engines.available', return_value=(True, 'fixture')), \
             patch('squad.engines.which_binary', return_value='/fixture/agy'), \
             patch('squad.agy_profile.check_cli') as check, \
             patch('squad.engines.subprocess.Popen', return_value=proc) as launch:
            result = engines.run_engine('agy', 'Hi', cwd=self.root, write=False, timeout=5, dry_run=False)
            self.assertTrue(result['ok']); check.assert_called_once_with('/fixture/agy')
            target = agy_profile.prepare(self.root)
            self.assertIn(agy_profile.name(self.root), launch.call_args.args[0])
            launch.reset_mock(); check.reset_mock()
            result = engines.run_engine('agy', 'Hi', cwd=self.root, write=False, timeout=5, dry_run=False, cont=True)
            self.assertEqual(result['diagnostic'], 'read_only_policy'); launch.assert_not_called(); check.assert_not_called()
            target.write_text('changed')
            result = engines.run_engine('agy', 'Hi', cwd=self.root, write=False, timeout=5, dry_run=False)
            self.assertEqual(result['diagnostic'], 'read_only_policy'); launch.assert_not_called()
            check.side_effect = ValueError('missing supported flags')
            result = engines.run_engine('agy', 'Hi', cwd=self.root, write=False, timeout=5, dry_run=False)
            self.assertEqual(result['diagnostic'], 'cli_arguments'); launch.assert_not_called()

    def test_telegram_agy_alias_can_queue_and_worker_uses_native_profile(self):
        store.init_workspace(self.root)
        broker = Broker(self.root); broker.company('team'); broker.agent('telegram.agy', 'team', 'agy', [])
        cfg = store.load_config(self.root)
        cfg['telegram'] = telegram.validate_settings({'chat_id': -123, 'allowed_user_ids': [7],
            'allow_chat': True, 'agent_aliases': {'agy': 'telegram.agy'}})
        store.save_config(self.root, cfg)
        bridge = telegram.Bridge(self.root, cfg['telegram'], GroupBot())
        proc = Mock(returncode=0); proc.communicate.return_value = ('Yes, I can hear you.', '')
        with patch('squad.engines.available', return_value=(True, 'fixture')), \
             patch('squad.engines.which_binary', return_value='/fixture/agy'), \
             patch('squad.agy_profile.check_cli'), \
             patch('squad.engines.subprocess.Popen', return_value=proc) as launch:
            bridge.handle(incoming(1, '@agy can you hear me?')['message'], 1)
            task = work_once(self.root, 'telegram.agy', kind='chat')
        self.assertEqual(task['state'], 'done')
        self.assertIn('--agent', launch.call_args.args[0])
        self.assertEqual(agy_profile.prepare(self.root).read_text(), agy_profile.definition(self.root))

    def test_exact_grok_failure_and_cross_provider_sandbox_errors(self):
        for text in (GROK_ERROR, 'bwrap: Creating new namespace failed: Operation not permitted',
                     'failed to apply Landlock: Permission denied', 'sandbox-exec: sandbox_apply: Operation not permitted'):
            self.assertEqual(diagnostics.classify({'stderr': text}), 'sandbox_startup')
            self.assertIsNone(diagnostics.classify({'ok': True, 'stdout': text}))
        self.assertEqual(diagnostics.classify({'stderr': 'Permission denied writing foo'}), 'permission')
        message = diagnostics.public_failure('sandbox_startup', 'a'*32)
        self.assertIn('squad doctor', message); self.assertNotIn('/run/podman', message)

    def test_grok_failure_is_not_retried_without_sandbox(self):
        proc = Mock(returncode=1); proc.communicate.return_value = ('', GROK_ERROR)
        with patch('squad.engines.available', return_value=(True, 'fixture')), \
             patch('squad.engines.which_binary', return_value='/fixture/grok'), \
             patch('squad.engines.subprocess.Popen', return_value=proc) as launch:
            result = engines.run_engine('grok', 'Hi', cwd=self.root, write=False, timeout=5, dry_run=False)
        self.assertEqual(result['diagnostic'], 'sandbox_startup'); self.assertEqual(launch.call_count, 1)
        cmd = launch.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--sandbox')+1], 'read-only')
        self.assertEqual(result['stderr'], GROK_ERROR)

    def test_offline_setup_warns_about_protected_socket_without_opening_it(self):
        socket = self.root / 'podman.sock'; socket.write_text('fixture endpoint')
        with patch('squad.cli_checks.sys.platform', 'linux'), patch('squad.cli_checks.shutil.which', return_value=None), \
             patch('squad.cli_checks.grok_socket_paths', return_value=[socket]), \
             patch.object(Path, 'lstat', side_effect=PermissionError('protected socket')):
            self.assertEqual(cli_checks.grok_sandbox_checks(), ['grok_bwrap_missing', 'grok_socket_unverified'])
        with patch('squad.cli_checks.sys.platform', 'darwin'), patch('squad.cli_checks.grok_socket_paths') as paths:
            self.assertEqual(cli_checks.grok_sandbox_checks(), []); paths.assert_not_called()
        with patch('squad.engines.available', return_value=(True, 'fixture')), \
             patch('squad.cli_checks.grok_sandbox_checks', return_value=['grok_socket_unverified']), \
             patch('squad.engines.run_engine') as model:
            rows = {r['engine']: r for r in diagnostics.setup_checks(self.root, {})}
        self.assertIn('grok_socket_unverified', rows['grok']['issues'])
        self.assertNotIn('read_only_policy', rows['agy']['issues']); model.assert_not_called()
        out = io.StringIO()
        with patch('squad.diagnostics.setup_checks', return_value=[rows['grok'], rows['agy']]), contextlib.redirect_stdout(out):
            diagnostics.print_setup_checks(self.root, {})
        self.assertIn('keep the socket permissions and sandbox enabled', out.getvalue())
        self.assertEqual(socket.read_text(), 'fixture endpoint')


if __name__ == '__main__':
    unittest.main()

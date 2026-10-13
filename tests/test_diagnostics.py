"""Setup and failure handling across transports, without vendor model calls."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import install
from squad import cli, diagnostics, engines, project_check, store
from squad.broker import Broker
from squad.worker import work_once


class ProjectChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_uninitialized_then_repo_and_linked_worktree(self):
        root = self.root / 'project with spaces'; root.mkdir()
        result = project_check.check_project(root)
        self.assertEqual(result['status'], 'no-git')
        self.assertIn("'" + str(root) + "'", result['hint'])
        self.assertFalse((root / '.git').exists())
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        self.assertEqual(project_check.check_project(root)['status'], 'git-worktree')
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(root), 'worktree', 'add', '--detach', '-q', str(linked)], check=True)
        self.assertTrue((linked / '.git').is_file())
        self.assertEqual(project_check.check_project(linked)['status'], 'git-worktree')

    @unittest.skipUnless(shutil.which('git'), 'Git required')
    def test_actual_squad_root_and_ambient_git_override(self):
        store.init_workspace(self.root)
        child = self.root / 'src'; child.mkdir()
        elsewhere = self.root / 'other'; elsewhere.mkdir()
        subprocess.run(['git', 'init', '-q', str(elsewhere)], check=True)
        with patch.dict(os.environ, {'GIT_DIR': str(elsewhere / '.git'), 'GIT_WORK_TREE': str(elsewhere)}):
            result = project_check.check_project(child)
        self.assertEqual(result['path'], str(self.root))
        self.assertEqual(result['status'], 'no-git')

    def test_missing_git_timeout_and_home_are_actionable_without_mutation(self):
        with patch('squad.project_check.shutil.which', return_value=None):
            self.assertEqual(project_check.check_project(self.root)['status'], 'git-unavailable')
        with patch('squad.project_check.shutil.which', return_value='/bin/git'), \
             patch('squad.project_check.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 5)):
            self.assertEqual(project_check.check_project(self.root)['status'], 'git-error')
        with patch('squad.project_check.Path.home', return_value=self.root):
            result = project_check.check_project(self.root)
        self.assertEqual(result['status'], 'broad-directory')
        self.assertNotIn('git -C', result['hint'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_installer_checks_selected_project_before_pip_and_rejects_typo(self):
        events = []
        args = ['--no-add-to-path', '--prefix', str(self.root / 'install'), '--project', str(self.root)]
        def check(root, *, discover):
            self.assertFalse(discover)
            events.append(('check', root)); return {'path': str(root)}
        with patch('install.print_project_check', side_effect=check), patch('install.print_setup_checks') as checks, \
             patch('install.venv.EnvBuilder'), patch('install.subprocess.run', side_effect=lambda *a, **k: events.append(('pip', None))), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(args), 0)
        self.assertEqual(events, [('check', self.root), ('pip', None)])
        checks.assert_called_once_with(self.root)
        with patch('install.venv.EnvBuilder') as builder, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                install.main(args + ['--project', str(self.root / 'typo')])
        builder.assert_not_called()


class FailureDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_provider_categories_and_no_success_false_positives(self):
        cases = [
            ('codex', 'Not inside a trusted directory', 'workspace_trust'),
            ('claude', 'Invalid API key. Please run /login', 'authentication'),
            ('qwen', 'OAuth token has expired', 'authentication'),
            ('grok', 'unknown option --flag', 'cli_arguments'),
            ('kimi', 'insufficient credits', 'quota'),
            ('glm', 'rate_limit_exceeded', 'rate_limit'),
            ('deepseek', 'Permission denied', 'permission'),
            ('mistral', 'MCP server failed to start', 'mcp'),
            ('ollama', 'model "my-model" not found', 'model'),
            ('custom', 'Connection refused', 'connection'),
            ('openai', 'context window exceeded', 'context_budget'),
            ('other', 'something new and unexplained', 'unknown'),
        ]
        for engine, text, code in cases:
            with self.subTest(engine=engine):
                self.assertEqual(diagnostics.classify({'engine': engine, 'stderr': text}), code)
                self.assertIsNone(diagnostics.classify({'ok': True, 'stdout': text}))
                message = diagnostics.public_failure(code, 'a' * 32)
                self.assertIn('--logs', message)

    def test_real_custom_process_failure_and_empty_success(self):
        cfg = {'engines': {'fixture': {'transport': 'custom', 'argv': [sys.executable, '-c',
             'import sys; print("unknown option --fixture", file=sys.stderr); sys.exit(2)'],
             'allow_advisory_read_only': True}}}
        result = engines.run_engine('fixture', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertEqual(result['diagnostic'], 'cli_arguments'); self.assertEqual(result['returncode'], 2)
        cfg['engines']['fixture']['argv'][-1] = 'pass'
        result = engines.run_engine('fixture', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertFalse(result['ok']); self.assertEqual(result['diagnostic'], 'invalid_response')

    def test_quotations_in_echoed_policy_are_not_quota_errors(self):
        policy = 'Never invent evidence, measurements, quotations or completed work.'
        self.assertEqual(diagnostics.classify({'stderr': policy}), 'unknown')
        self.assertEqual(diagnostics.classify({'stderr': policy +
            '\nERROR: workspace routing discovery failed'}), 'connection')

    def test_explicit_quota_failures_remain_recognized(self):
        for message in ('insufficient_quota', 'quota_exceeded', 'Quota exceeded',
                        'You exceeded your current quota.', 'Quota has been exhausted',
                        'insufficient credits', 'credit balance depleted', 'billing limit reached'):
            with self.subTest(message=message):
                self.assertEqual(diagnostics.classify({'stderr': message}), 'quota')
        self.assertEqual(diagnostics.classify({'stderr': 'Checking quota settings'}), 'unknown')

    def test_startup_connection_errors_and_more_specific_causes(self):
        for message in ('Connection failed: error sending request',
                        'workspace routing discovery failed',
                        'http/request failed: error sending request for url (http://localhost/mcp)',
                        'Transport channel closed'):
            with self.subTest(message=message):
                self.assertEqual(diagnostics.classify({'stderr': message}), 'connection')
        self.assertEqual(diagnostics.classify({'stderr':
            'Connection failed: certificate verify failed'}), 'tls')
        self.assertEqual(diagnostics.classify({'http_status': 401, 'stderr':
            'workspace routing discovery failed'}), 'authentication')

    def test_engine_diagnosis_excludes_exact_prompt_but_retains_raw_log(self):
        prompt = 'Investigate quota exceeded, permission denied and timeout diagnostics.'
        raw = 'user\n' + prompt + '\nERROR: workspace routing discovery failed'
        for engine in ('codex', 'claude', 'grok', 'agy', 'custom'):
            with self.subTest(engine=engine), patch('squad.engines._run_engine', return_value={
                    'engine': engine, 'ok': False, 'stderr': raw, 'stdout': '', 'returncode': 1}):
                result = engines.run_engine(engine, prompt, cwd=self.root, write=False,
                                            timeout=5, dry_run=False)
                self.assertEqual(result['diagnostic'], 'connection')
                self.assertEqual(result['stderr'], raw)

    def test_every_builtin_cli_uses_shared_failure_handler(self):
        keys = [key for key, spec in engines.BUILTINS.items() if spec.transport == 'cli']
        cfg = {'engines': {key: {'allow_advisory_read_only': True} for key in keys}}
        for key in keys:
            proc = Mock(returncode=2); proc.communicate.return_value = ('', 'unknown option --fixture')
            with self.subTest(engine=key), patch('squad.engines.available', return_value=(True, 'fixture')), \
                 patch('squad.engines.which_binary', return_value='/fixture/' + key), \
                 patch('squad.agy_profile.check_cli'), \
                 patch('squad.engines.subprocess.Popen', return_value=proc):
                result = engines.run_engine(key, 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
            self.assertEqual(result['diagnostic'], 'cli_arguments')

    def test_http_status_and_connection_failures_preserved(self):
        cfg = {'engines': {'local': {'transport': 'openai', 'base_url': 'http://127.0.0.1:8081/v1', 'auth': 'local'}}}
        cases = [(401, 'authentication'), (403, 'permission'), (404, 'endpoint'), (429, 'rate_limit'), (503, 'provider_error')]
        for status, code in cases:
            with self.subTest(status=status), patch('squad.http_agent.request_json',
                 side_effect=urllib.error.HTTPError('http://127.0.0.1', status, 'fixture', {}, None)):
                result = engines.run_engine('local', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
            self.assertEqual(result['diagnostic'], code); self.assertEqual(result['http_status'], status)
        with patch('squad.http_agent.request_json', side_effect=urllib.error.URLError('Connection refused')):
            result = engines.run_engine('local', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertEqual(result['diagnostic'], 'connection')

    def test_provider_error_body_retains_quota_and_model_diagnosis_locally(self):
        for transport, status, body, expected in [
            ('openai', 429, b'{"error":{"code":"insufficient_quota","private":"SECRET"}}', 'quota'),
            ('ollama', 404, b'{"error":"model \\"missing\\" not found"}', 'model')]:
            cfg = {'engines': {'local': {'transport': transport, 'base_url': 'http://127.0.0.1:8081', 'auth': 'local'}}}
            error = urllib.error.HTTPError('http://127.0.0.1', status, 'fixture', {}, io.BytesIO(body))
            with patch('squad.http_agent.request_json', side_effect=error):
                result = engines.run_engine('local', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
            self.assertEqual(result['diagnostic'], expected)
            self.assertNotIn('SECRET', diagnostics.public_failure(result['diagnostic'], 'a' * 32))

    def test_mcp_startup_failure_and_missing_executable(self):
        cfg = {'engines': {'local': {'transport': 'openai', 'base_url': 'http://127.0.0.1:8081', 'mcp_servers': ['missing']}}}
        result = engines.run_engine('local', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertEqual(result['diagnostic'], 'mcp')
        cfg = {'engines': {'missing-cli': {'transport': 'custom', 'argv': ['missing-fixture-executable']}}}
        result = engines.run_engine('missing-cli', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertEqual(result['diagnostic'], 'missing_executable')

    def test_timeout_with_no_exception_message_and_invalid_engine(self):
        with patch('squad.engines._run_engine', side_effect=TimeoutError()):
            result = engines.run_engine('anything', 'hi', cwd=self.root, write=False, timeout=1, dry_run=False)
        self.assertEqual(result['diagnostic'], 'timeout')
        result = engines.run_engine('unknown-engine', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False)
        self.assertEqual(result['diagnostic'], 'configuration')

    def test_worker_keeps_local_stderr_and_log_command_resolves_exact_task(self):
        store.init_workspace(self.root)
        broker = Broker(self.root); broker.company('team'); broker.agent('a', 'team', 'claude', [])
        task = broker.create_task('inspect', assignee='a')
        raw = 'Invalid API key SECRET-FIXTURE'
        with patch('squad.engines.run_engine', return_value={'ok': False, 'engine': 'claude', 'stderr': raw, 'stdout': '', 'returncode': 1}):
            result = work_once(self.root, 'a')
        self.assertEqual(result['result']['diagnostic'], 'authentication')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['tasks', '--root', str(self.root), '--id', task['id'], '--logs'])
        log = json.loads(out.getvalue())['run_log']
        self.assertEqual(log['stderr'], raw)
        self.assertEqual(log['diagnostic'], 'authentication')
        with self.assertRaisesRegex(ValueError, 'inside'):
            diagnostics.task_log(self.root, {'result': {'artifact': '/tmp/outside.md'}})

    def test_offline_setup_checks_include_custom_http_and_do_not_invoke_engines(self):
        cfg = {'engines': {'custom-one': {'transport': 'custom', 'argv': ['missing-fixture-executable']},
                          'http-one': {'transport': 'openai', 'base_url': 'http://127.0.0.1:8081/v1', 'auth': 'local'},
                          'bad-mcp': {'transport': 'openai', 'base_url': 'http://127.0.0.1:8081/v1', 'mcp_servers': ['absent']}}}
        with patch('squad.engines.run_engine') as model:
            rows = {r['engine']: r for r in diagnostics.setup_checks(self.root, cfg)}
        model.assert_not_called()
        self.assertIn('missing_executable', rows['custom-one']['issues'])
        self.assertEqual(rows['http-one']['issues'], ['endpoint_unverified'])
        self.assertIn('mcp', rows['bad-mcp']['issues'])
        self.assertTrue(set(engines.BUILTINS).issubset(rows))

    def test_public_fallback_does_not_echo_injected_error(self):
        secret = 'PRIVATE token=123 /home/private https://secret.example/?key=123'
        for code in (secret, {'message': secret}, None):
            result = diagnostics.public_failure(code, secret)
            self.assertNotIn(secret, result)
            self.assertIn('[unknown]', result)


if __name__ == '__main__':
    unittest.main()

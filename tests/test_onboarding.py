"""Automatic agent registration, setup reuse and live alias synchronization."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import install
from squad import cli, onboarding, store, telegram, telegram_chat
from squad.broker import Broker
from test_telegram_chat import GroupBot, incoming


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.installed = {'codex', 'claude', 'grok', 'agy'}
        self.find = patch('shutil.which', side_effect=lambda name: '/fixture/' + name if name in self.installed else None)
        self.find.start()
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()

    def tearDown(self):
        self.env.stop(); self.find.stop(); self.tmp.cleanup()

    def setup_group(self):
        return onboarding.setup(self.root, chat_id=-123, user_ids=[7], interactive=False)

    def test_setup_registers_detected_engines_and_aliases_without_inference(self):
        with patch('squad.engines.run_engine') as model, patch('squad.telegram.client') as bot:
            result = self.setup_group()
        model.assert_not_called(); bot.assert_not_called()
        self.assertEqual(set(result['agent_aliases']), self.installed)
        self.assertEqual(len(Broker(self.root).roster()['agents']), 4)
        cfg = store.load_config(self.root)['telegram']
        self.assertTrue(cfg['allow_chat']); self.assertTrue(cfg['auto_discover_agents'])
        self.assertNotIn('allow_tasks', cfg)
        self.assertFalse(result['token_set'])
        self.assertTrue(next(x for x in result['detected'] if x['engine'] == 'agy')['read_only_supported'])

    def test_repeated_setup_and_discovery_preserve_agents_permissions_and_group_settings(self):
        store.init_workspace(self.root)
        broker = Broker(self.root); broker.company('telegram', 'Keep this goal')
        broker.agent('telegram.agent', 'telegram', 'codex', ['code'])
        cfg = store.load_config(self.root)
        cfg['telegram'] = {'chat_id': -123, 'allowed_user_ids': [7, 8], 'allow_tasks': True,
            'task_budget': 9000, 'default_agent': 'telegram.agent', 'agent_aliases': {'coder': 'telegram.agent'},
            'allow_chat': True}
        store.save_config(self.root, cfg)
        task = broker.create_task('Keep existing task', assignee='telegram.agent')
        with patch('builtins.input', side_effect=AssertionError('Saved IDs must not be requested again')):
            first = onboarding.setup(self.root, interactive=True)
        second = onboarding.discover(self.root)
        self.assertEqual(second['added_agents'], []); self.assertEqual(second['added_aliases'], {})
        self.assertEqual(first['agent_aliases']['coder'], 'telegram.agent')
        self.assertNotIn('codex', first['agent_aliases'])
        self.assertEqual(broker.roster()['companies'][0]['goal'], 'Keep this goal')
        agent = next(a for a in broker.roster()['agents'] if a['id'] == 'telegram.agent')
        self.assertEqual(agent['capabilities'], ['code'])
        self.assertEqual(broker.task(task['id'])['state'], 'queued')
        cfg = store.load_config(self.root)['telegram']
        self.assertEqual(cfg['allowed_user_ids'], [7, 8]); self.assertEqual(cfg['task_budget'], 9000)
        self.assertTrue(cfg['allow_tasks'])

    def test_new_installs_merge_without_duplicates_or_removing_old_aliases(self):
        initial = self.setup_group()
        self.installed.update({'qwen', 'deepseek', 'glm'})
        result = onboarding.discover(self.root)
        self.assertEqual(set(result['added_aliases']), {'qwen', 'deepseek', 'glm'})
        for alias, agent in initial['agent_aliases'].items():
            self.assertEqual(result['agent_aliases'][alias], agent)
        self.installed.remove('claude')
        result = onboarding.discover(self.root)
        self.assertIn('claude', result['agent_aliases'])
        self.assertEqual(result['added_agents'], [])

    def test_existing_names_and_alias_collisions_are_preserved(self):
        store.init_workspace(self.root)
        b = Broker(self.root); b.company('original')
        b.agent('telegram.codex', 'original', 'claude', [])
        cfg = store.load_config(self.root)
        cfg['telegram'] = {'chat_id': -123, 'allowed_user_ids': [7], 'agent_aliases': {'codex': 'telegram.codex'}, 'allow_chat': True}
        store.save_config(self.root, cfg)
        result = onboarding.discover(self.root)
        self.assertEqual(result['agent_aliases']['codex'], 'telegram.codex')
        self.assertEqual(result['agent_aliases']['codex_2'], 'telegram.codex-2')
        old = next(a for a in b.roster()['agents'] if a['id'] == 'telegram.codex')
        self.assertEqual((old['company'], old['engine']), ('original', 'claude'))

    def test_dry_run_has_no_mutations_and_then_apply_matches_preview(self):
        store.init_workspace(self.root)
        cfg = store.load_config(self.root); cfg['telegram'] = {'chat_id': -123, 'allowed_user_ids': [7]}
        store.save_config(self.root, cfg)
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = onboarding.discover(self.root, dry_run=True)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        applied = onboarding.discover(self.root)
        self.assertEqual(preview['agent_aliases'], applied['agent_aliases'])

    def test_http_custom_and_ollama_detection_does_not_add_duplicate_defaults(self):
        self.installed.update({'ollama', 'my-agent'})
        store.init_workspace(self.root)
        cfg = store.load_config(self.root)
        cfg['engines'] = {'local-qwen': {'transport': 'openai', 'base_url': 'http://127.0.0.1:8081/v1', 'auth': 'local'},
                          'custom-agent': {'transport': 'custom', 'argv': ['my-agent', '{prompt}']},
                          'missing-api': {'transport': 'openai', 'base_url': 'https://example.invalid/v1', 'api_key_env': 'MISSING_KEY'}}
        store.save_config(self.root, cfg)
        result = self.setup_group()
        aliases = result['agent_aliases']
        self.assertIn('ollama', aliases); self.assertNotIn('llama', aliases)
        self.assertIn('local_qwen', aliases); self.assertIn('custom_agent', aliases)
        self.assertNotIn('missing_api', aliases); self.assertNotIn('openai', aliases)

    def test_alias_limit_skips_new_entries_and_leaves_manual_bindings(self):
        store.init_workspace(self.root)
        b = Broker(self.root); b.company('manual'); b.agent('other', 'manual', 'other-engine', [])
        cfg = store.load_config(self.root)
        cfg['telegram'] = {'chat_id': -123, 'allowed_user_ids': [7], 'allow_chat': True,
                           'agent_aliases': {f'manual{i}': 'other' for i in range(16)}}
        store.save_config(self.root, cfg)
        result = onboarding.discover(self.root)
        self.assertEqual(result['added_agents'], []); self.assertEqual(len(result['skipped']), 4)
        self.assertEqual(len(result['agent_aliases']), 16)

    def test_failed_json_save_recovers_without_duplicate_agents(self):
        store.init_workspace(self.root)
        cfg = store.load_config(self.root); cfg['telegram'] = {'chat_id': -123, 'allowed_user_ids': [7]}
        store.save_config(self.root, cfg)
        before = (self.root / '.squad/config.json').read_bytes()
        with patch('squad.onboarding.store.save_config', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError): onboarding.discover(self.root)
        self.assertEqual((self.root / '.squad/config.json').read_bytes(), before)
        result = onboarding.discover(self.root)
        self.assertEqual(result['added_agents'], [])
        self.assertEqual(len(Broker(self.root).roster()['agents']), 4)
        self.assertEqual(len(result['added_aliases']), 4)

    def test_setup_prompts_only_missing_ids_and_never_saves_token(self):
        with patch('builtins.input', side_effect=['-123', '7, 8']) as prompt, \
             patch.dict(os.environ, {'SQUAD_TELEGRAM_BOT_TOKEN': '123:PRIVATE'}):
            result = onboarding.setup(self.root, interactive=True)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(result['allowed_user_ids'], [7, 8]); self.assertTrue(result['token_set'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('PRIVATE', (self.root / '.squad/config.json').read_text())

    def test_setup_without_engines_saves_group_for_later_discovery(self):
        self.installed.clear()
        result = self.setup_group()
        self.assertFalse(result['allow_chat']); self.assertNotIn('--with-workers', result['next'])
        self.installed.add('codex')
        self.assertTrue(onboarding.discover(self.root)['allow_chat'])

    def test_live_listener_reload_adds_workers_and_routes_new_alias_without_restart(self):
        self.setup_group()
        api = GroupBot()
        bridge = telegram.Bridge(self.root, telegram.settings(self.root), api, live_config=True)
        processes = []
        def new_process(*args, **kwargs):
            p = Mock(); p.poll.return_value = None; processes.append(p); return p
        with patch('squad.telegram_chat.subprocess.Popen', side_effect=new_process) as spawn:
            with telegram_chat.workers(self.root, bridge.cfg['agent_aliases']) as check:
                self.assertEqual(spawn.call_count, 4)
                self.installed.add('deepseek'); onboarding.discover(self.root)
                bridge.reload_settings(); check(bridge.cfg['agent_aliases']); check(bridge.cfg['agent_aliases'])
                self.assertEqual(spawn.call_count, 5)
                self.assertIn('deepseek', bridge.cfg['agent_aliases'])
                # Unsupported read-only CLI is named but cannot reserve a task or invoke a model.
                before = bridge.broker.budgets()
                response = bridge.handle(incoming(1, '@deepseek Hello')['message'], 1)
                self.assertIn('no model call was made', response)
                self.assertEqual(bridge.broker.budgets(), before)
                # The same agent becomes usable after explicit, user-controlled policy setup.
                cfg = store.load_config(self.root); cfg['engines']['deepseek'] = {'allow_advisory_read_only': True}
                store.save_config(self.root, cfg)
                response = bridge.handle(incoming(2, '@deepseek Hello')['message'], 2)
                self.assertIsNone(response)
                self.assertEqual(bridge.broker.tasks()[0]['assignee'], 'telegram.deepseek')
            for process in processes:
                process.send_signal.assert_called_once(); process.wait.assert_called_once()

    def test_live_reload_stops_on_authorization_change(self):
        self.setup_group()
        bridge = telegram.Bridge(self.root, telegram.settings(self.root), GroupBot(), live_config=True)
        cfg = store.load_config(self.root); cfg['telegram']['allowed_user_ids'] = [8]; store.save_config(self.root, cfg)
        with self.assertRaisesRegex(ValueError, 'Restart the listener'): bridge.reload_settings()
        self.assertEqual(bridge.cfg['allowed_user_ids'], [7])

    def test_discovery_during_poll_is_applied_before_routing_or_acknowledgement(self):
        self.installed = {'codex'}; self.setup_group()
        api = GroupBot(); bridge = telegram.Bridge(self.root, telegram.settings(self.root), api, live_config=True)
        def discover_while_polling(*args, **kwargs):
            self.installed.add('claude'); onboarding.discover(self.root)
            return [incoming(1, '@claude Hello')]
        with patch.object(api, 'request', side_effect=discover_while_polling):
            bridge.receive(timeout=0)
        self.assertEqual(bridge.broker.tasks()[0]['assignee'], 'telegram.claude')
        def change_users_while_polling(*args, **kwargs):
            cfg = store.load_config(self.root); cfg['telegram']['allowed_user_ids'] = [8]
            store.save_config(self.root, cfg)
            return [incoming(2, '@codex This must not be processed')]
        with patch.object(api, 'request', side_effect=change_users_while_polling):
            with self.assertRaisesRegex(ValueError, 'Restart the listener'):
                bridge.receive(timeout=0)
        with bridge.broker.db() as db:
            self.assertEqual(bridge._get(db, 'offset'), 2)
        self.assertEqual(len(bridge.broker.tasks()), 1)

    def test_parallel_discovery_registers_each_engine_once(self):
        self.installed.clear(); self.setup_group()
        self.installed.update({'codex', 'claude', 'grok'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: onboarding.discover(self.root), range(2)))
        self.assertEqual(sum(len(r['added_agents']) for r in results), 3)
        self.assertEqual(len(Broker(self.root).roster()['agents']), 3)
        self.assertEqual(len(telegram.settings(self.root)['agent_aliases']), 3)

    def test_manual_disable_or_alias_selection_turns_off_startup_discovery(self):
        self.setup_group()
        telegram_chat.configure(self.root, [], False)
        cfg = telegram.settings(self.root)
        self.assertFalse(cfg['auto_discover_agents']); self.assertFalse(cfg['allow_chat'])
        telegram_chat.configure(self.root, ['only=telegram.codex'], True)
        self.assertFalse(telegram.settings(self.root)['auto_discover_agents'])

    def test_cli_setup_and_discover_run_in_requested_project(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['telegram', 'setup', '--root', str(self.root), '--chat-id', '-123', '--user-id', '7']), 0)
        self.assertIn('codex', json.loads(out.getvalue())['agent_aliases'])
        self.installed.add('qwen')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['telegram', 'discover', '--root', str(self.root)]), 0)
        self.assertIn('qwen', json.loads(out.getvalue())['added_aliases'])


class InstallerOnboardingTests(unittest.TestCase):
    def test_interactive_install_collects_project_and_ids_then_registers_agents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('install.sys.stdin.isatty', return_value=True), \
                 patch('builtins.input', side_effect=['y', str(root), '-123', '7']) as prompt, \
                 patch('install.venv.EnvBuilder'), patch('install.subprocess.run'), \
                 patch('install.print_project_check', return_value={'path': str(root)}), patch('install.print_setup_checks'), \
                 patch('shutil.which', side_effect=lambda name: '/fixture/codex' if name == 'codex' else None), \
                 patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code = install.main(['--no-add-to-path', '--prefix', str(root/'prefix')])
            self.assertEqual(code, 0); self.assertEqual(prompt.call_count, 4)
            cfg = telegram.settings(root)
            self.assertEqual(cfg['chat_id'], -123); self.assertEqual(cfg['allowed_user_ids'], [7])
            self.assertEqual(cfg['agent_aliases'], {'codex': 'telegram.codex'})

    def test_setup_runs_after_successful_pip_and_uses_selected_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = []
            def pip(*a, **k): events.append('pip')
            def setup(project, **kwargs):
                events.append('setup'); self.assertEqual(project, root)
                self.assertEqual(kwargs['chat_id'], -123); self.assertEqual(kwargs['user_ids'], [7])
                return {'token_set': True}
            with patch('install.venv.EnvBuilder'), patch('install.subprocess.run', side_effect=pip), \
                 patch('install.print_project_check', return_value={'path': str(root)}), patch('install.print_setup_checks'), \
                 patch('squad.onboarding.setup', side_effect=setup), contextlib.redirect_stdout(io.StringIO()):
                code = install.main(['--no-add-to-path', '--prefix', str(root/'prefix'), '--project', str(root),
                                     '--setup-telegram', '--telegram-chat-id', '-123', '--telegram-user-id', '7'])
            self.assertEqual(code, 0); self.assertEqual(events, ['pip', 'setup'])

    def test_failed_install_never_changes_project_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('install.venv.EnvBuilder'), patch('install.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['pip'])), \
                 patch('install.print_project_check', return_value={'path': str(root)}), patch('install.print_setup_checks'), \
                 patch('squad.onboarding.setup') as setup, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(subprocess.CalledProcessError):
                    install.main(['--no-add-to-path', '--prefix', str(root/'prefix'), '--project', str(root), '--setup-telegram'])
            setup.assert_not_called()


class ManagedDiscoveryIntegration(unittest.TestCase):
    def test_running_listener_adds_real_worker_and_delivers_new_agent_reply(self):
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                response = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': request['model'] + ' answered'},
                                                   'finish_reason': 'stop'}],
                                       'usage': {'prompt_tokens': 30, 'completion_tokens': 10, 'total_tokens': 40}}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(response)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store.init_workspace(root)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                engine = {'transport': 'openai', 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'auth': 'local'}
                cfg = store.load_config(root); cfg['engines'] = {'first': {**engine, 'model': 'first'}}
                store.save_config(root, cfg)
                with patch('shutil.which', return_value=None), patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
                    onboarding.setup(root, chat_id=-123, user_ids=[7], interactive=False)
                class Bot(GroupBot):
                    def __init__(self):
                        super().__init__(); self.stage = 0; self.started = time.monotonic()
                    def request(self, method, payload, timeout=30):
                        if time.monotonic() - self.started > 12:
                            raise TimeoutError('Managed listener fixture timed out')
                        if method == 'getUpdates':
                            if len(self.sent) == 2:
                                raise KeyboardInterrupt()
                            if self.stage == 0:
                                self.stage = 1; return [incoming(1, '@first Hello')]
                            if self.stage == 2:
                                self.stage = 3; return [incoming(2, '@second Hello')]
                            time.sleep(0.05); return []
                        result = super().request(method, payload, timeout)
                        if method == 'sendMessage' and len(self.sent) == 1:
                            cfg = store.load_config(root); cfg['engines']['second'] = {**engine, 'model': 'second'}
                            store.save_config(root, cfg)
                            with patch('shutil.which', return_value=None), patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
                                onboarding.discover(root)
                            self.stage = 2
                        return result
                bot = Bot(); bridge = telegram.Bridge(root, telegram.settings(root), bot, live_config=True)
                with self.assertRaises(KeyboardInterrupt):
                    bridge.listen(timeout=1, with_workers=True)
                self.assertIn('first answered', bot.sent[0]['text'])
                self.assertIn('second answered', bot.sent[1]['text'])
                self.assertEqual({row['state'] for row in bridge.broker.tasks()}, {'done'})
                self.assertEqual(len(bridge.broker.tasks()), 2)
                self.assertEqual({row['state'] for row in bridge.deliveries()}, {'sent'})
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()

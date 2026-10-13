from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from squad import cli, quality, store, telegram
from squad.broker import Broker
from squad.install_path import configure_path, START
from squad.worker import work_once

PROJECT = Path(__file__).resolve().parents[1]
try:
    import mcp
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class FakeBot:
    bot_id = '1234'

    def __init__(self):
        self.updates, self.sent, self.calls = [], [], []
        self.error = None
        self.webhook = ''

    def request(self, method, payload, timeout=30):
        self.calls.append((method, payload))
        if method == 'getMe': return {'username': 'SquadFixtureBot'}
        if method == 'getWebhookInfo': return {'url': self.webhook}
        if method == 'getUpdates': return list(self.updates)
        if self.error: raise self.error
        self.sent.append(payload)
        return {'message_id': len(self.sent)}


def update(uid, text='hello', user=7, chat=9, bot=False):
    return {'update_id': uid, 'message': {'message_id': uid, 'chat': {'id': chat},
            'from': {'id': user, 'is_bot': bot}, 'text': text}}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.broker = Broker(self.root); self.broker.company('lab')
        self.broker.agent('builder', 'lab', 'ollama', ['code'])
        self.cfg = telegram.validate_settings({'chat_id': 9, 'allowed_user_ids': [7], 'default_agent': 'builder'})
        self.api = FakeBot()
        self.bridge = telegram.Bridge(self.root, self.cfg, self.api)

    def tearDown(self): self.tmp.cleanup()

    def test_allowlist_dedup_restart_and_ack_offset(self):
        self.api.updates = [update(1, user=8), update(2, chat=8), update(3, bot=True), update(4)]
        self.bridge.listen(once=True, timeout=0)
        self.assertEqual(len(store.read_mail(self.root, 'builder')), 1)
        self.assertEqual(len(self.api.sent), 1)
        restarted = telegram.Bridge(self.root, self.cfg, self.api)
        restarted.listen(once=True, timeout=0)
        self.assertEqual(len(store.read_mail(self.root, 'builder')), 1)
        self.assertEqual(len(self.api.sent), 1)
        polls = [p for m,p in self.api.calls if m == 'getUpdates']
        self.assertEqual(polls[-1]['offset'], 5)

    def test_task_opt_in_budget_and_no_execution(self):
        self.api.updates = [update(1, '/task builder Inspect the parser')]
        self.bridge.listen(once=True, timeout=0)
        self.assertEqual(self.broker.tasks(), [])
        self.cfg['allow_tasks'] = True
        self.api.updates = [update(2, '/task builder Inspect the parser')]
        self.bridge.listen(once=True, timeout=0)
        task = self.broker.tasks()[0]
        self.assertEqual(task['state'], 'queued'); self.assertEqual(task['write_allowed'], 0)
        self.assertEqual(task['budget'], 6000); self.assertEqual(task['assignee'], 'builder')
        self.cfg['max_pending_tasks'] = 1
        self.api.updates = [update(3, '/task builder More work')]
        self.bridge.listen(once=True, timeout=0)
        self.assertEqual(len(self.broker.tasks()), 1)

    def test_commands_other_bot_and_agent_target(self):
        self.api.updates = [update(1, '/say builder check the logs'),
                            update(2, '/say@OtherBot builder wrong'), update(3, '/status')]
        self.bridge.listen(once=True, timeout=0)
        self.assertEqual([m['text'] for m in store.read_mail(self.root, 'builder')], ['check the logs'])
        self.assertEqual(len(self.api.sent), 2)

    def test_messages_and_task_reports_flow_back_without_echo(self):
        store.post_mail(self.root, to='human', sender='builder', text='Checked parser.py; 4 cases passed')
        store.read_mail(self.root, 'human', mark=True)
        store.post_mail(self.root, to='human', sender='telegram:7', text='do not echo')
        task = self.broker.create_task('Inspect', assignee='builder')
        claim = self.broker.claim('builder')
        self.broker.complete(task['id'], 'builder', claim['claim_token'], 'Evidence ready', tokens=20)
        self.bridge.collect(); self.bridge.flush(); self.bridge.collect(); self.bridge.flush()
        self.assertEqual(len(self.api.sent), 2)
        self.assertIn('task.submitted', self.api.sent[1]['text'])

    def test_uncertain_delivery_not_replayed_and_explicit_retry(self):
        self.bridge.queue('m1', 'hello')
        self.api.error = telegram.TelegramError('ambiguous', uncertain=True)
        self.bridge.flush()
        self.assertEqual(self.bridge.deliveries()[0]['state'], 'uncertain')
        self.api.error = None
        self.bridge.recover(); self.bridge.flush()
        self.assertEqual(self.api.sent, [])
        config = store.load_config(self.root); config['telegram'] = self.cfg; store.save_config(self.root, config)
        with patch('squad.telegram.client', return_value=self.api), contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(['telegram', 'retry', 'm1:0', '--root', str(self.root)])
        self.assertEqual(result, 0); self.assertEqual(len(self.api.sent), 1)

    def test_rate_limit_defers_whole_queue(self):
        self.bridge.queue('m1', 'hello'); self.bridge.queue('m2', 'world')
        self.api.error = telegram.TelegramError('wait', retry_after=30)
        self.bridge.flush()
        self.api.error = None; self.bridge.flush()
        self.assertEqual(self.api.sent, [])
        with self.broker.db() as db:
            db.execute('UPDATE telegram_outbox SET available=0')
        self.bridge.flush()
        self.assertEqual(len(self.api.sent), 2)

    def test_interrupted_processing_is_not_reexecuted(self):
        with self.broker.db() as db:
            db.execute('INSERT INTO telegram_inbox VALUES(?,?,?,?)',
                (self.bridge.key, 1, json.dumps(update(1, '/task builder x')['message']), 'processing'))
        self.bridge.recover(); self.bridge.process_inbox(); self.bridge.flush()
        self.assertEqual(self.broker.tasks(), [])
        self.assertIn('not replayed', self.api.sent[0]['text'])

    def test_pending_inbox_recovers_before_next_network_poll(self):
        with self.broker.db() as db:
            db.execute('INSERT INTO telegram_inbox VALUES(?,?,?,?)',
                (self.bridge.key, 1, json.dumps(update(1)['message']), 'pending'))
        self.bridge.listen(once=True, timeout=0)
        self.assertEqual(len(store.read_mail(self.root, 'builder')), 1)

    def test_webhook_conflict_does_not_delete_webhook(self):
        self.api.webhook = 'https://example.org/other-bot'
        with self.assertRaises(ValueError): self.bridge.listen(once=True, timeout=0)
        self.assertNotIn('deleteWebhook', [m for m,p in self.api.calls])

    def test_config_fails_closed_and_token_never_saved(self):
        for cfg in ({}, {'chat_id': 9, 'allowed_user_ids': []}, {'chat_id': 9, 'allowed_user_ids': ['7']}):
            with self.assertRaises(ValueError): telegram.validate_settings(cfg)
        with patch.dict(os.environ, {'SQUAD_TELEGRAM_BOT_TOKEN': '123:PRIVATE'}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['telegram', 'configure', '--root', str(self.root), '--chat-id', '9', '--user-id', '7']), 0)
        self.assertNotIn('PRIVATE', out.getvalue())
        self.assertNotIn('PRIVATE', (self.root / '.squad/config.json').read_text())

    def test_identify_only_prints_candidate_ids_without_acknowledging(self):
        self.api.updates = [update(1, 'sensitive message'), update(2, 'another')]
        with patch('squad.telegram.client', return_value=self.api), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['telegram', 'identify', '--root', str(self.root)]), 0)
        value = json.loads(out.getvalue())
        self.assertEqual(value['candidates'], [{'chat_id': 9, 'user_id': 7}])
        self.assertNotIn('sensitive', out.getvalue())
        self.assertNotIn('offset', self.api.calls[0][1]); self.assertEqual(self.broker.tasks(), [])

    def test_configure_does_not_forward_older_mail(self):
        store.post_mail(self.root, to='human', sender='builder', text='old mail')
        self.cfg['mail_since'] = '9999-01-01T00:00:00Z'
        self.bridge.collect(); self.bridge.flush()
        self.assertEqual(self.api.sent, [])

    def test_unicode_chunk_boundaries_and_literal_formatting(self):
        text = '😀' * 4096 + '<literal>&_'
        parts = list(telegram.chunks(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 4000 for p in parts))
        self.bridge.queue('long', text); self.bridge.flush()
        self.assertNotIn('parse_mode', self.api.sent[0])


class TelegramWireTests(unittest.TestCase):
    def test_api_payload_and_redacted_errors(self):
        api = telegram.BotAPI('123:PRIVATE')
        reply = io.BytesIO(b'{"ok": true, "result": {"message_id": 1}}')
        with patch.object(api.opener, 'open', return_value=reply) as send:
            self.assertEqual(api.request('sendMessage', {'chat_id': 9, 'text': 'hello'})['message_id'], 1)
            req = send.call_args.args[0]
            self.assertEqual(req.get_method(), 'POST')
            self.assertEqual(json.loads(req.data)['chat_id'], 9)
        with patch.object(api.opener, 'open', side_effect=OSError('https://api.telegram.org/bot123:PRIVATE/sendMessage')):
            with self.assertRaises(telegram.TelegramError) as caught: api.request('sendMessage', {})
        self.assertNotIn('PRIVATE', str(caught.exception)); self.assertTrue(caught.exception.uncertain)

    def test_rate_limit_and_redirect(self):
        api = telegram.BotAPI('123:PRIVATE')
        reply = io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":10}}')
        with patch.object(api.opener, 'open', return_value=reply):
            with self.assertRaises(telegram.TelegramError) as caught: api.request('sendMessage', {})
        self.assertEqual(caught.exception.retry_after, 10)
        with self.assertRaises(telegram.TelegramError): telegram.NoRedirect().redirect_request(None)


class PathTests(unittest.TestCase):
    def test_bash_idempotent_preserves_content_and_shell_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); rc = home / '.bashrc'; rc.write_text('# existing\nexport CUSTOM=yes\n')
            target = home / "bin with spaces ' and $(touch SHOULD_NOT_EXIST)"
            self.assertEqual(len(configure_path(target, 'bash', home)), 2)
            self.assertEqual(configure_path(target, 'bash', home), [])
            self.assertEqual(rc.read_text().count(START), 1)
            code = 'source "$1"; source "$1"; printf "%s" "$PATH"'
            r = subprocess.run(['bash', '--noprofile', '--norc', '-c', code, 'bash', str(rc)],
                cwd=home, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True, check=True)
            self.assertEqual(r.stdout.split(':').count(str(target)), 1)
            self.assertFalse((home / 'SHOULD_NOT_EXIST').exists())
            self.assertIn('export CUSTOM=yes', rc.read_text())

    def test_zsh_zdotdir_and_fish_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict(os.environ, {'ZDOTDIR': str(home / 'zsh')}):
                configure_path(home / 'bin', 'zsh', home)
                self.assertTrue((home / 'zsh/.zshrc').exists())
            configure_path(home / "bin 'quoted'", 'fish', home)
            self.assertIn('fish_add_path', (home / '.config/fish/conf.d/squad-path.fish').read_text())

    def test_installer_opt_out_does_not_change_profile_or_create_link(self):
        import install
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch('install.venv.EnvBuilder') as builder, patch('install.subprocess.run') as pip, patch('install.configure_path') as profile, contextlib.redirect_stdout(io.StringIO()):
                install.main(['--prefix', str(home / 'install'), '--bin-dir', str(home / 'bin'), '--no-add-to-path'])
            profile.assert_not_called(); self.assertFalse((home / 'bin').exists())
            self.assertTrue(pip.call_args.args[0][-1].endswith('[mcp]'))

    def test_installer_will_not_replace_unrelated_launcher(self):
        import install
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); bin_dir = home / 'bin'; bin_dir.mkdir()
            launcher = bin_dir / 'squad'; launcher.write_text('unrelated program')
            with patch('install.venv.EnvBuilder') as builder, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    install.main(['--prefix',str(home / 'app'),'--bin-dir',str(bin_dir),'--add-to-path','--shell','bash'])
            builder.assert_not_called(); self.assertEqual(launcher.read_text(), 'unrelated program')


class QualityTests(unittest.TestCase):
    def test_upstream_license_and_policy_available(self):
        self.assertIn('Peter Yang', quality.upstream('LICENSE'))
        self.assertIn('portability test', quality.upstream())
        self.assertIn('Final read', quality.upstream('eval.md'))

    def test_checks_are_specific_and_do_not_edit_code(self):
        result = quality.inspect('Here\'s the thing: a game changer.\n```python\nleverage = 3\n```')
        self.assertEqual(len(result['writing_findings']), 2)
        self.assertTrue(result['passed'])
        self.assertFalse(quality.inspect('Done', handoff=True)['passed'])
        handoff = json.dumps({'summary':'Fixed parser.py', 'evidence':['parser.py change'],
            'verification':['python -m unittest: 3 passed'], 'risks':[], 'next_actions':[]})
        self.assertTrue(quality.inspect(handoff, handoff=True)['passed'])

    def test_strict_broker_blocks_incomplete_handoff_without_spending_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store.init_workspace(root)
            cfg = store.load_config(root); cfg['quality'] = {'mode':'strict'}; store.save_config(root, cfg)
            b = Broker(root); b.company('lab'); b.agent('a','lab','ollama',[])
            task = b.create_task('Inspect', assignee='a'); claim = b.claim('a')
            result = b.complete(task['id'], 'a', claim['claim_token'], 'Done', tokens=120)
            self.assertEqual(result['state'], 'failed')
            self.assertTrue(result['result']['quality']['blocked'])
            self.assertEqual(b.budgets()[0]['used'], 120)

    def test_worker_policy_mail_and_full_result_check_before_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store.init_workspace(root)
            cfg = store.load_config(root); cfg['quality'] = {'mode':'strict'}; store.save_config(root, cfg)
            b = Broker(root); b.company('lab'); b.agent('a','lab','ollama',[])
            b.create_task('Inspect', assignee='a')
            store.post_mail(root, to='a', sender='telegram:7', text='Check the parser', board=False)
            output = json.dumps({'summary':'Inspected parser', 'evidence':['parser.py: ' + 'x'*5000],
                'verification':['Manual review; tests unavailable in this fixture'], 'risks':[], 'next_actions':[]})
            with patch('squad.engines.run_engine', return_value={'ok':True,'stdout':output,'stderr':'','usage':{'total_tokens':120,'kind':'reported'}}) as run:
                result = work_once(root, 'a')
            self.assertIn('no-ai-slop', run.call_args.args[1]); self.assertIn('Check the parser', run.call_args.args[1])
            self.assertEqual(result['state'], 'review'); self.assertTrue(result['result']['quality']['passed'])


@unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
class MCPExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_streamable_http_tools_resource_and_prompt(self):
        import socket
        from squad.mcp_bridge import ToolHub
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable, str(PROJECT / 'tests/mcp_fixture.py'), '--http', str(port)],
                                    cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close(); await writer.wait_closed(); break
                    except OSError:
                        if proc.poll() is not None: self.fail('HTTP fixture exited before startup')
                        await asyncio.sleep(0.05)
                else: self.fail('HTTP fixture did not start')
                cfg = {'mcp_servers': {'http': {'transport':'streamable-http', 'url':f'http://127.0.0.1:{port}/mcp',
                    'allowed_tools':['add'], 'read_only_tools':['add'],
                    'allowed_resources':['fixture://notes'], 'allowed_prompts':['review_goal']}}}
                async with ToolHub(Path(tmp), cfg, ['http'], False, timeout=5) as hub:
                    aliases = {r['kind']: r['alias'] for r in hub.catalog}
                    self.assertIn('5', json.dumps(await hub.call(aliases['tool'], {'a':2,'b':3})))
                    self.assertIn('parser', json.dumps(await hub.call(aliases['resource'], {})))
                    self.assertIn('test-goal', json.dumps(await hub.call(aliases['prompt'], {'goal':'test-goal'})))
            finally:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)

    async def test_outgoing_resources_prompts_and_exact_allowlists(self):
        from squad.mcp_bridge import ToolHub
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'mcp_servers': {'fixture': {'command':sys.executable, 'args':[str(PROJECT / 'tests/mcp_fixture.py')],
                'allowed_resources':['fixture://notes'], 'allowed_prompts':['review_goal']}}}
            async with ToolHub(Path(tmp), cfg, ['fixture'], False) as hub:
                self.assertEqual(len(hub.schemas), 2)
                resource = next(r['alias'] for r in hub.catalog if r['kind'] == 'resource')
                prompt = next(r['alias'] for r in hub.catalog if r['kind'] == 'prompt')
                self.assertIn('existing parser', json.dumps(await hub.call(resource, {})))
                self.assertIn('Inspect evidence', json.dumps(await hub.call(prompt, {'goal':'parser'})))
                with self.assertRaises(Exception): await hub.call(resource, {'uri':'file:///etc/passwd'})
                with self.assertRaises(Exception): await hub.call(prompt, {})

    async def test_incoming_resources_prompts_and_strict_submission(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store.init_workspace(root)
            cfg = store.load_config(root); cfg['quality'] = {'mode':'strict'}; store.save_config(root,cfg)
            b = Broker(root); b.company('lab'); b.agent('a','lab','ollama',[])
            task = b.create_task('Inspect parser', assignee='a')
            params = StdioServerParameters(command=sys.executable, args=['-m','squad','mcp','--root',str(root),'--agent','a'], cwd=str(PROJECT))
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as s:
                    await s.initialize()
                    policy = await s.read_resource('squad://quality/no-ai-slop')
                    self.assertIn('portability test', policy.contents[0].text)
                    prompt = await s.get_prompt('squad_work', {'task_id': task['id']})
                    self.assertIn('Inspect parser', prompt.messages[0].content.text)
                    claim = (await s.call_tool('squad_claim', {})).structuredContent
                    result = await s.call_tool('squad_submit', {'task_id': task['id'], 'claim_token':claim['claim_token'],
                        'summary':'Inspected parser', 'evidence':['parser.py handles empty input'],
                        'verification':['Manual trace; unit tests not run'], 'tokens':50})
                    self.assertFalse(result.isError); self.assertEqual(result.structuredContent['state'], 'review')

    async def test_insecure_remote_mcp_url_is_rejected(self):
        from squad.mcp_bridge import ToolHub
        cfg = {'mcp_servers': {'bad': {'transport':'streamable-http','url':'http://remote.example/mcp', 'allowed_tools':['read']}}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                async with ToolHub(Path(tmp), cfg, ['bad'], False): pass


if __name__ == '__main__': unittest.main()

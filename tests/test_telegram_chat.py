from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from squad import cli, store, telegram, telegram_chat
from squad.broker import Broker
from squad.worker import work_once


class GroupBot:
    bot_id = '1234'

    def __init__(self):
        self.updates, self.sent = [], []
        self.error = None

    def request(self, method, payload, timeout=30):
        if method == 'getMe': return {'username': 'SquadTestBot', 'can_read_all_group_messages': True}
        if method == 'getWebhookInfo': return {}
        if method == 'getUpdates': return self.updates
        if self.error: raise self.error
        self.sent.append(payload)
        return {'message_id': 10000 + len(self.sent)}


def incoming(uid, text, user=7, chat=-123, thread=0, reply=None, bot=False):
    message = {'message_id': uid, 'chat': {'id': chat, 'type': 'supergroup'},
               'from': {'id': user, 'is_bot': bot}, 'text': text}
    if thread: message['message_thread_id'] = thread
    if reply: message['reply_to_message'] = {'message_id': reply}
    return {'update_id': uid, 'message': message}


def model_reply(engine, prompt, **kwargs):
    return {'engine': engine, 'ok': True, 'stdout': f'{engine}: Here is my answer.', 'stderr': '',
            'command': ['fixture'], 'returncode': 0, 'usage': {'kind': 'reported', 'total_tokens': 100}}


class GroupChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.broker = Broker(self.root); self.broker.company('team')
        self.broker.agent('telegram.agent', 'team', 'codex', [])
        self.broker.agent('local.agent', 'team', 'ollama', [])
        self.cfg = telegram.validate_settings({'chat_id': -123, 'allowed_user_ids': [7, 8],
            'allow_chat': True, 'agent_aliases': {'codex': 'telegram.agent', 'qwen': 'local.agent'}})
        cfg = store.load_config(self.root); cfg['telegram'] = self.cfg; cfg['quality'] = {'mode': 'strict'}
        store.save_config(self.root, cfg)
        self.api = GroupBot(); self.bridge = telegram.Bridge(self.root, self.cfg, self.api)

    def tearDown(self): self.tmp.cleanup()

    def receive(self, *messages):
        self.api.updates = list(messages)
        self.bridge.receive(timeout=0)

    def complete(self, agent='telegram.agent'):
        with patch('squad.engines.run_engine', side_effect=model_reply) as run:
            item = work_once(self.root, agent, kind='chat')
        self.bridge.collect(); self.bridge.flush()
        return item, run

    def test_mention_worker_natural_reply_and_reply_continuation(self):
        self.receive(incoming(1, '@codex Can we discuss the parser?', thread=11))
        self.assertEqual(self.api.sent, [])
        task = self.broker.tasks()[0]
        self.assertEqual(task['kind'], 'chat'); self.assertFalse(task['write_allowed'])
        result, run = self.complete()
        self.assertEqual(result['state'], 'done')
        self.assertFalse(result['result']['quality']['blocked'])
        self.assertNotIn('Return JSON', run.call_args.args[1])
        sent = self.api.sent[-1]
        self.assertIn('[codex · telegram.agent]', sent['text'])
        self.assertNotIn('task.submitted', sent['text'])
        self.assertEqual(sent['message_thread_id'], 11)
        self.assertEqual(sent['reply_parameters']['message_id'], 1)
        self.bridge = telegram.Bridge(self.root, self.cfg, self.api)
        self.receive(incoming(2, 'Can you explain that further?', thread=11, reply=10001))
        second, run = self.complete()
        self.assertEqual(second['state'], 'done')
        self.assertIn('Here is my answer.', run.call_args.args[1])
        self.assertEqual(len(self.api.sent), 2)

    def test_failed_chat_has_safe_diagnosis_and_persistent_local_log(self):
        self.receive(incoming(1, '@codex Are you available?'))
        secret = 'Not inside a trusted directory; PRIVATE-CREDENTIAL-FIXTURE'
        with patch('squad.engines.run_engine', return_value={'ok': False, 'stdout': '', 'stderr': secret, 'returncode': 1}):
            task = work_once(self.root, 'telegram.agent', kind='chat')
        self.bridge = telegram.Bridge(self.root, self.cfg, self.api)
        self.bridge.collect(); self.bridge.flush()
        text = self.api.sent[-1]['text']
        self.assertIn('[workspace_trust]', text)
        self.assertIn(task['id'] + ' --logs', text)
        self.assertNotIn('PRIVATE-CREDENTIAL', text)
        log = (self.root / task['result']['artifact']).with_suffix('.json')
        self.assertEqual(json.loads(log.read_text())['stderr'], secret)

    def test_engine_task_failure_notification_does_not_forward_stderr(self):
        task = self.broker.create_task('Inspect locally', assignee='local.agent')
        secret = 'Unauthorized PRIVATE-CREDENTIAL-FIXTURE'
        with patch('squad.engines.run_engine', return_value={'ok': False, 'stdout': '', 'stderr': secret, 'returncode': 1}):
            work_once(self.root, 'local.agent')
        # A queued event may describe an earlier state than the task now has.
        with self.broker.db() as db:
            self.broker._event(db, 'task.submitted', task['id'])
        self.bridge.collect(); self.bridge.flush()
        text = '\n'.join(message['text'] for message in self.api.sent)
        self.assertIn('task.failed', text)
        self.assertIn('[authentication]', text)
        self.assertNotIn('PRIVATE-CREDENTIAL', text)

    def test_multiple_agents_dedup_and_explicit_override_of_reply_target(self):
        self.receive(incoming(1, '@codex @qwen @CODEX Compare your approaches.'))
        self.assertEqual(len(self.broker.tasks()), 2)
        _, codex = self.complete()
        _, local = self.complete('local.agent')
        self.assertEqual(codex.call_args.args[0], 'codex')
        self.assertEqual(local.call_args.args[0], 'ollama')
        self.receive(incoming(2, '@qwen Expand this.', reply=10001))
        queued = self.broker.tasks(state='queued')
        self.assertEqual([t['assignee'] for t in queued], ['local.agent'])

    def test_reply_reference_survives_history_window_and_cross_agent_handoff(self):
        cfg = store.load_config(self.root); cfg['telegram']['chat_context_turns'] = 0
        store.save_config(self.root, cfg)
        self.receive(incoming(1, '@codex ORIGINAL-QUESTION'))
        self.complete()
        self.receive(incoming(2, '@qwen Review this answer.', reply=10001))
        _, run = self.complete('local.agent')
        self.assertIn('ORIGINAL-QUESTION', run.call_args.args[1])
        self.assertIn('codex: Here is my answer.', run.call_args.args[1])

    def test_authorization_loops_and_ordinary_group_chatter(self):
        self.receive(incoming(1, '@codex ignored', user=99), incoming(2, '@codex ignored', chat=-999),
                     incoming(3, '@codex ignored', bot=True), incoming(4, 'Unrelated group conversation'),
                     incoming(5, 'Email somebody@codex.example'))
        self.assertEqual(self.broker.tasks(), [])
        self.assertEqual(store.read_mail(self.root, 'human'), [])
        self.assertEqual(self.api.sent, [])

    def test_topic_context_and_private_mail_are_isolated(self):
        store.post_mail(self.root, to='telegram.agent', sender='human', text='PRIVATE-AGENT-MAIL', board=False)
        store.post_mail(self.root, to='human', sender='local.agent', text='PRIVATE-HUMAN-MAIL', board=False)
        self.receive(incoming(1, '@codex TOPIC-ONE-SECRET', thread=11))
        self.complete()
        self.receive(incoming(2, '@codex Topic two question', thread=22))
        _, run = self.complete()
        prompt = run.call_args.args[1]
        self.assertNotIn('TOPIC-ONE-SECRET', prompt)
        self.assertNotIn('PRIVATE-', prompt)
        self.assertFalse(any('PRIVATE-' in item['text'] for item in self.api.sent))
        self.receive(incoming(3, 'Wrong topic reply', thread=22, reply=10001))
        self.assertEqual(len(self.broker.tasks()), 2)

    def test_replay_crash_and_outbox_dedup(self):
        update = incoming(1, '@codex Answer once')
        self.receive(update)
        self.bridge.handle(update['message'], 1)
        self.assertEqual(len(self.broker.tasks()), 1)
        with self.broker.db() as db:
            db.execute("UPDATE telegram_inbox SET state='processing'")
        self.bridge.recover()
        self.complete()
        self.bridge.collect(); self.bridge.flush(); self.receive(update)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(len(self.broker.tasks()), 1)

    def test_pending_limit_atomic_multi_mentions_and_conversation_does_not_fill_reviews(self):
        self.cfg['max_pending_tasks'] = 1
        self.receive(incoming(1, '@codex @qwen Too many at once'))
        self.assertEqual(self.broker.tasks(), [])
        self.assertEqual(self.broker.budgets(), [])
        for uid in range(2, 9):
            self.receive(incoming(uid, '@codex Keep chatting'))
            result, _ = self.complete()
            self.assertEqual(result['state'], 'done')
        self.assertEqual(self.broker.tasks(state='review'), [])
        self.assertEqual(len(self.broker.tasks()), 7)

    def test_chat_worker_does_not_claim_coding_job_and_chat_cannot_delegate(self):
        ordinary = self.broker.create_task('Engineering review', assignee='telegram.agent')
        self.receive(incoming(1, '@codex Hello'))
        chat, _ = self.complete()
        self.assertEqual(chat['kind'], 'chat')
        self.assertEqual(self.broker.task(ordinary['id'])['state'], 'queued')
        self.receive(incoming(2, '@codex Another question'))
        pending = next(t for t in self.broker.tasks(state='queued') if t['kind'] == 'chat')
        with self.assertRaises(ValueError):
            self.broker.create_task('Delegate', requester='telegram.agent', parent=pending['id'])

    def test_config_cli_preserves_bot_and_aliases_across_reconfigure(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['telegram', 'chat', 'configure', '--agent', 'coder=telegram.agent',
                                      '--root', str(self.root)]), 0)
            self.assertEqual(cli.main(['telegram', 'configure', '--chat-id', '-123', '--user-id', '7',
                                      '--root', str(self.root)]), 0)
        cfg = store.load_config(self.root)['telegram']
        self.assertTrue(cfg['allow_chat'])
        self.assertEqual(cfg['agent_aliases'], {'coder': 'telegram.agent'})
        with self.assertRaises(ValueError):
            telegram_chat.configure(self.root, ['same=telegram.agent', 'same=local.agent'], True)

    def test_privacy_friendly_ask_and_agents_commands(self):
        self.bridge.username = 'SquadTestBot'
        self.receive(incoming(1, '/ask@OtherBot codex ignore'), incoming(2, '/ask@SquadTestBot codex Hello'))
        self.assertEqual(len(self.broker.tasks()), 1)
        self.assertEqual(self.broker.tasks()[0]['goal'], 'Hello')
        self.receive(incoming(3, '/agents'))
        self.bridge.flush()
        self.assertIn('@codex', self.api.sent[-1]['text'])

    def test_failure_is_readable_and_uncertain_send_not_replayed(self):
        self.receive(incoming(1, '@codex Hello'))
        with patch('squad.engines.run_engine', return_value={'ok': False, 'stdout': '',
                  'stderr': 'secret=DO-NOT-POST', 'returncode': 1, 'command': []}):
            result = work_once(self.root, 'telegram.agent', kind='chat')
        self.assertEqual(result['state'], 'failed')
        self.bridge.collect()
        self.api.error = telegram.TelegramError('ambiguous', uncertain=True)
        self.bridge.flush(); self.api.error = None
        self.bridge.recover(); self.bridge.collect(); self.bridge.flush()
        self.assertEqual(self.api.sent, [])
        with self.broker.db() as db:
            text = db.execute('SELECT text FROM telegram_outbox').fetchone()[0]
        self.assertNotIn('DO-NOT-POST', text)
        self.assertIn('Error [unknown]', text)
        self.assertIn('--logs', text)

    def test_account_mcp_not_needed_for_group_reply(self):
        cfg = store.load_config(self.root)
        cfg['engines'] = {'codex': {'mcp_servers': ['telegram-account', 'repository']}}
        cfg['mcp_servers'] = {'telegram-account': {'integration': 'chigwell/telegram-mcp'}, 'repository': {}}
        store.save_config(self.root, cfg)
        self.receive(incoming(1, '@codex Hello'))
        _, run = self.complete()
        self.assertEqual(run.call_args.kwargs['config']['engines']['codex']['mcp_servers'], ['repository'])

    def test_supervisor_owns_unique_chat_only_workers_and_stops_them(self):
        process = Mock(); process.poll.return_value = None
        with patch('squad.telegram_chat.subprocess.Popen', return_value=process) as spawn:
            with telegram_chat.workers(self.root, {'codex': 'telegram.agent', 'coder': 'telegram.agent'}) as check:
                check()
            self.assertEqual(spawn.call_count, 1)
            self.assertIn('--chat-only', spawn.call_args.args[0])
            self.assertNotIn('--allow-write', spawn.call_args.args[0])
            process.send_signal.assert_called_once()
            process.wait.assert_called_once()

    def test_old_workspace_migrates_without_losing_tasks_or_pending_delivery(self):
        ordinary = self.broker.create_task('Existing engineering task', assignee='telegram.agent')
        self.bridge.queue('old-message', 'Pending before upgrade')
        with self.broker.db() as db:
            db.execute('ALTER TABLE tasks DROP COLUMN kind')
            db.execute('ALTER TABLE telegram_outbox DROP COLUMN route')
            db.execute('DROP TABLE telegram_chat_replies')
            db.execute('DROP TABLE telegram_turns')
        rebuilt = telegram.Bridge(self.root, self.cfg, self.api)
        self.assertEqual(rebuilt.broker.task(ordinary['id'])['kind'], 'task')
        self.assertEqual(rebuilt.broker.task(ordinary['id'])['state'], 'queued')
        rebuilt.flush()
        self.assertEqual(self.api.sent[0]['text'], 'Pending before upgrade')


class GroupHTTPTests(unittest.TestCase):
    def test_real_worker_process_http_model_and_group_reply(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.seen.append(request)
                body = json.dumps({'choices': [{'message': {'role': 'assistant',
                    'content': 'The fixture can read your question.'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store.init_workspace(root)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.seen = []
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                cfg = store.load_config(root)
                cfg['engines'] = {'chat-http': {'transport': 'openai', 'model': 'fixture',
                    'base_url': f'http://127.0.0.1:{server.server_port}/v1'}}
                cfg['quality'] = {'mode': 'strict'}
                cfg['telegram'] = telegram.validate_settings({'chat_id': -123, 'allowed_user_ids': [7],
                    'allow_chat': True, 'agent_aliases': {'fixture': 'http.agent'}})
                store.save_config(root, cfg)
                broker = Broker(root); broker.company('team'); broker.agent('http.agent', 'team', 'chat-http', [])
                api = GroupBot(); bridge = telegram.Bridge(root, cfg['telegram'], api)
                api.updates = [incoming(1, '@fixture Can you read my question?')]
                bridge.listen(once=True, timeout=0)
                result = subprocess.run([sys.executable, '-m', 'squad', 'worker', '--agent', 'http.agent',
                    '--chat-only', '--root', str(root)], cwd=str(Path(__file__).resolve().parents[1]),
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['state'], 'done')
                bridge.collect(); bridge.flush()
                self.assertEqual(len(api.sent), 1)
                self.assertIn('The fixture can read your question.', api.sent[0]['text'])
                self.assertEqual(len(server.seen), 1)
                self.assertEqual(server.seen[0]['model'], 'fixture')
                self.assertIn('Return only the reply text', server.seen[0]['messages'][0]['content'])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()

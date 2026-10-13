import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from squad import store, telegram, telegram_actions as actions, telegram_mcp
from squad.broker import Broker
from squad.worker import work_once
from test_telegram_chat import GroupBot, incoming


class ActionBot(GroupBot):
    def __init__(self):
        super().__init__(); self.documents = []
    def send_document(self, payload, name, content):
        self.documents.append((payload, name, content))
        return {'message_id': 999}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        store.init_workspace(self.root)
        self.broker = Broker(self.root); self.broker.company('team'); self.broker.agent('reader', 'team', 'codex', [])
        cfg = store.load_config(self.root)
        cfg['telegram'] = telegram.validate_settings({'chat_id': -123, 'allowed_user_ids': [7, 8],
            'allow_chat': True, 'agent_aliases': {'codex': 'reader'}})
        store.save_config(self.root, cfg)
        self.api = ActionBot(); self.bridge = telegram.Bridge(self.root, cfg['telegram'], self.api)
        self.source = self.root / 'walking video.mp4'; self.source.write_bytes(b'video bytes')
        self.row = {'task_id': 'a'*32, 'user_id': 7, 'thread_id': 5, 'message_id': 10}

    def tearDown(self): self.temp.cleanup()

    def propose(self, action):
        with self.broker.db() as db:
            actions.schema(db)
            result = actions.prepare(self.bridge, self.row, json.dumps({'reply': 'prepared', 'telegram_actions': [action]}), db)
        return result

    def items(self):
        with self.broker.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM telegram_actions')]

    def approve(self, action_id, user=7, thread=5):
        return self.bridge.handle(incoming(90, '/approve ' + action_id, user=user, thread=thread)['message'], 90)

    def test_file_approval_exact_bytes_topic_and_duplicate_protection(self):
        preview = self.propose({'to': 'here', 'file': self.source.name, 'text': 'Walking review'})
        self.assertIn('walking video.mp4', preview); self.assertIn('SHA-256', preview)
        action_id = self.items()[0]['id']
        self.bridge.flush(); self.assertEqual(self.api.documents, [])
        self.approve(action_id, user=8); self.approve(action_id, thread=6)
        self.assertEqual(self.items()[0]['state'], 'proposed')
        self.source.write_bytes(b'changed after preview')
        self.approve(action_id); self.approve(action_id); self.bridge.flush(); self.bridge.flush()
        self.assertEqual(len(self.api.documents), 1)
        payload, name, content = self.api.documents[0]
        self.assertEqual(content, b'video bytes'); self.assertEqual(payload['message_thread_id'], 5)
        self.assertEqual(payload['chat_id'], -123); self.assertEqual(payload['reply_parameters']['message_id'], 10)
        self.assertEqual(self.items()[0]['state'], 'sent')
        self.assertTrue(any('sent.' in m['text'] for m in self.api.sent))

    def test_dm_registered_destination_and_changed_recipient_revokes(self):
        actions.recipient(self.root, 'friend', 77)
        self.propose({'to': 'friend', 'text': 'Hello'})
        action_id = self.items()[0]['id']; self.approve(action_id); self.bridge.flush()
        sends = [m for m in self.api.sent if m['chat_id'] == 77]
        self.assertEqual(len(sends), 1); self.assertNotIn('reply_parameters', sends[0])
        self.row['task_id'] = 'b'*32
        self.propose({'to': 'friend', 'text': 'Next'})
        action_id = self.items()[1]['id']; self.approve(action_id)
        actions.recipient(self.root, 'friend', 88)
        self.bridge.flush(); self.assertEqual(self.items()[1]['state'], 'failed')
        self.assertFalse(any(m['chat_id'] == 88 for m in self.api.sent))

    def test_cancel_expiry_and_unauthorized_approval(self):
        self.propose({'file': self.source.name})
        action_id = self.items()[0]['id']
        with self.assertRaises(ValueError): self.approve(action_id, user=999)
        with self.broker.db() as db: db.execute('UPDATE telegram_actions SET created=?', (time.time()-4000,))
        self.approve(action_id); self.assertEqual(self.items()[0]['state'], 'expired')
        self.row['task_id'] = 'b'*32; self.propose({'text': 'Hello'})
        action_id = self.items()[1]['id']
        msg = incoming(91, '/cancel ' + action_id, thread=5)['message']
        self.bridge.handle(msg, 91); self.approve(action_id)
        self.assertEqual(self.items()[1]['state'], 'cancelled'); self.bridge.flush()
        self.assertEqual(self.api.sent, [])

    def test_outside_hidden_symlink_hardlink_fifo_and_unknown_destination_rejected(self):
        (self.root / 'link.mp4').symlink_to(self.source)
        os.link(self.source, self.root / 'hard.mp4')
        os.mkfifo(self.root / 'pipe')
        for path in ('../outside', '/etc/passwd', '.squad/config.json', 'link.mp4', 'hard.mp4', 'pipe'):
            with self.subTest(path=path):
                self.assertIn('Nothing was sent', self.propose({'file': path}))
        self.assertIn('Nothing was sent', self.propose({'to': '@unknown', 'text': 'Hello'}))
        self.assertEqual(self.items(), [])

    def test_staged_tamper_and_interrupted_delivery_do_not_retry(self):
        self.propose({'file': self.source.name})
        row = self.items()[0]; payload = json.loads(row['payload'])
        self.approve(row['id']); (self.root / payload['file']['path']).write_bytes(b'tampered')
        self.bridge.flush(); self.assertEqual(self.items()[0]['state'], 'failed')
        self.assertEqual(self.api.documents, [])
        with self.broker.db() as db: db.execute("UPDATE telegram_actions SET state='sending'")
        self.bridge.flush(); self.assertEqual(self.items()[0]['state'], 'uncertain')
        self.approve(row['id']); self.bridge.flush(); self.assertEqual(self.api.documents, [])

    def test_route_worker_collect_and_receipt_context(self):
        self.bridge.handle(incoming(1, '@codex Send the walking video', thread=5)['message'], 1)
        response = json.dumps({'reply': 'Prepared', 'telegram_actions': [{'file': self.source.name}]})
        with patch('squad.engines.run_engine', return_value={'ok': True, 'stdout': response, 'stderr': '', 'engine': 'codex'}):
            task = work_once(self.root, 'reader', kind='chat')
        self.assertEqual(task['state'], 'done')
        self.bridge.collect(); self.bridge.collect(); self.assertEqual(len(self.items()), 1)
        self.approve(self.items()[0]['id']); self.bridge.flush()
        self.assertEqual(len(self.api.documents), 1)
        self.assertEqual(actions.receipts(self.broker, task['id'])[0]['state'], 'sent')

    def test_mcp_file_policy_and_offline_registration(self):
        cfg = {'integration': telegram_mcp.INTEGRATION, 'allowed_tools': ['send_file'],
               'allowed_chat_ids': [77], 'file_roots': [str(self.root)]}
        env = telegram_mcp.launch_env(cfg, True)
        self.assertIn('send_file', env['TELEGRAM_EXPOSED_TOOLS'])
        self.assertEqual(telegram_mcp.launch_env(cfg, False)['TELEGRAM_EXPOSED_TOOLS'], 'read-only')
        telegram_mcp.validate_call(cfg, 'send_file', {'chat_id': 77, 'file_path': str(self.source)})
        for changes in ({'chat_id': 88}, {'file_path': '/etc/passwd'}, {'file_path': [str(self.source)]}, {'schedule_date': 'tomorrow'}):
            with self.assertRaises(ValueError):
                telegram_mcp.validate_call(cfg, 'send_file', {'chat_id': 77, 'file_path': str(self.source), **changes})
        with self.assertRaises(ValueError): actions.recipient(self.root, 'friend', 77, 'missing')

    def test_actual_mcp_subprocess_sends_only_after_approval(self):
        fixture = self.root / 'fixture.py'
        fixture.write_text('''from pathlib import Path
from mcp.server.fastmcp import FastMCP
m = FastMCP("fixture")
@m.tool()
def send_file(chat_id: int, file_path: str, caption: str = "") -> str:
    assert chat_id == 77
    assert Path(file_path).read_bytes() == b"video bytes"
    return f"File sent to chat {chat_id} from {file_path}."
@m.tool()
def send_message(chat_id: int, message: str) -> str:
    return "Message sent successfully."
m.run()
''')
        cfg = store.load_config(self.root)
        cfg['mcp_servers'] = {'account': {'integration': telegram_mcp.INTEGRATION,
            'transport': 'stdio', 'command': sys.executable, 'args': [str(fixture)],
            'allowed_tools': ['send_message', 'send_file'], 'read_only_tools': [],
            'allowed_chat_ids': [77], 'file_roots': [str(self.root)]}}
        store.save_config(self.root, cfg)
        actions.recipient(self.root, 'friend', 77, 'account')
        self.propose({'to': 'friend', 'file': self.source.name})
        self.approve(self.items()[0]['id']); self.bridge.flush()
        self.assertEqual(self.items()[0]['state'], 'sent')
        self.assertEqual(self.api.documents, [])


if __name__ == '__main__': unittest.main()

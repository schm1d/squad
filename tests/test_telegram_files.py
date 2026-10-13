"""Telegram file upload/download, bounded worker context and delivery recovery."""
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
from unittest.mock import patch
from squad import attachments, cli, store, telegram, telegram_files
from squad.broker import Broker
from squad.worker import work_once
from test_telegram_chat import GroupBot, incoming, model_reply


class FileBot(GroupBot):
    def __init__(self):
        super().__init__(); self.files = {}; self.downloads = []; self.uploads = []
    def download(self, file_id):
        self.downloads.append(file_id)
        data = self.files[file_id]
        if isinstance(data, Exception): raise data
        return data
    def send_document(self, payload, name, content):
        self.uploads.append((payload, name, content))
        if self.error: raise self.error
        return {'message_id': 20000 + len(self.uploads)}


class TelegramFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.broker = Broker(self.root); self.broker.company('team')
        self.broker.agent('coder', 'team', 'codex', [])
        self.broker.agent('unsafe', 'team', 'qwen', [])
        self.cfg = telegram.validate_settings({'chat_id': -123, 'allowed_user_ids': [7], 'allow_chat': True,
            'agent_aliases': {'codex': 'coder', 'qwen': 'unsafe'}, 'default_agent': None})
        cfg = store.load_config(self.root); cfg['telegram'] = self.cfg; store.save_config(self.root, cfg)
        self.api = FileBot(); self.bridge = telegram.Bridge(self.root, self.cfg, self.api)
        self.bridge.username = 'SquadTestBot'
        self.api.files['doc'] = b'Parser fixture information'

    def tearDown(self): self.tmp.cleanup()

    def media(self, uid=1, caption='@codex Review this', **kwargs):
        update = incoming(uid, '', **kwargs); message = update['message']; message.pop('text')
        message['document'] = {'file_id': 'doc', 'file_name': '../../parser.txt', 'mime_type': 'text/plain',
                               'file_size': len(self.api.files['doc']) if isinstance(self.api.files['doc'], bytes) else 10}
        if caption is not None: message['caption'] = caption
        return update

    def receive(self, *updates):
        self.api.updates = list(updates); self.bridge.receive(timeout=0)

    def finish(self):
        with patch('squad.engines.run_engine', side_effect=model_reply) as engine:
            result = work_once(self.root, 'coder', kind='chat')
        self.bridge.collect(); self.bridge.flush()
        return result, engine

    def test_file_caption_routes_and_worker_sees_text_once(self):
        message = self.media(thread=11)
        self.receive(message); self.receive(message)
        self.assertEqual(len(self.broker.tasks()), 1); self.assertEqual(self.api.downloads, ['doc'])
        result, engine = self.finish()
        self.assertEqual(result['state'], 'done')
        self.assertIn('Parser fixture information', engine.call_args.args[1])
        self.assertIn('/.squad/attachments/telegram/', engine.call_args.args[1])
        self.assertEqual(self.api.sent[-1]['message_thread_id'], 11)
        with self.broker.db() as db:
            files = json.loads(db.execute('SELECT attachments FROM telegram_turns').fetchone()[0])
        self.assertEqual(files[0]['name'], 'parser.txt')
        self.assertFalse(result['write_allowed'])

    def test_private_caption_command_and_reply_attachment_selection(self):
        self.receive(self.media(caption='/ask@SquadTestBot codex Review the file'))
        self.finish()
        self.receive(self.media(2, caption=None, reply=10001))
        self.assertEqual(len(self.broker.tasks()), 2)
        result, engine = self.finish(); self.assertEqual(result['state'], 'done')
        self.assertIn('Parser fixture information', engine.call_args.args[1])

    def test_reply_to_answer_retains_original_attachment_and_topic_isolation(self):
        self.receive(self.media(thread=5)); self.finish()
        self.receive(incoming(2, 'Explain another part', thread=5, reply=10001))
        _, engine = self.finish(); self.assertIn('Parser fixture information', engine.call_args.args[1])
        self.receive(incoming(3, '@codex Separate topic', thread=6, reply=10001))
        _, engine = self.finish(); self.assertNotIn('Parser fixture information', engine.call_args.args[1])

    def test_unauthorized_bot_and_wrong_alias_do_not_download(self):
        self.receive(self.media(user=8), self.media(2, bot=True), self.media(3, chat=-999),
                     self.media(4, '@unknown hello'), self.media(5, '@qwen hello'),
                     self.media(6, '/ask@OtherBot codex hello'))
        self.assertEqual(self.api.downloads, []); self.assertEqual(self.broker.tasks(), [])

    def test_captionless_file_needs_reply_or_configured_default(self):
        self.receive(self.media(caption=None))
        self.assertEqual(self.api.downloads, []); self.assertEqual(self.broker.tasks(), [])
        self.cfg['default_agent'] = 'coder'
        self.receive(self.media(2, caption=None))
        self.assertEqual(self.broker.tasks()[0]['assignee'], 'coder')

    def test_largest_photo_selected_and_binary_limit_reported_honestly(self):
        update = self.media(); message = update['message']; message.pop('document')
        self.api.files['small'] = b'\x89PNG\x00'; self.api.files['large'] = b'\x89PNG\x00LARGE'
        message['photo'] = [{'file_id': 'small', 'width': 10, 'height': 10}, {'file_id': 'large', 'width': 100, 'height': 100}]
        self.receive(update)
        _, engine = self.finish()
        self.assertEqual(self.api.downloads, ['large'])
        self.assertIn('no text/vision/audio interpretation', engine.call_args.args[1])

    def test_oversized_attachment_and_download_error_queue_no_task(self):
        update = self.media(); update['message']['document']['file_size'] = attachments.MAX_BYTES + 1
        self.receive(update); self.assertEqual(self.api.downloads, [])
        self.api.files['doc'] = telegram.TelegramError('FIXTURE_TOKEN must remain local')
        self.receive(self.media(2))
        self.assertEqual(self.broker.tasks(), [])
        self.bridge.flush()
        self.assertNotIn('FIXTURE_TOKEN', json.dumps(self.api.sent))
        self.assertIn('attachment could not be received', self.api.sent[-1]['text'])

    def test_local_send_snapshots_file_and_preserves_topic_reply(self):
        source = self.root / 'report.pdf'; source.write_bytes(b'%PDF fixture')
        telegram_files.stage(self.bridge, source, 'outgoing', 'Report', thread_id=22, reply_to=41)
        source.write_bytes(b'changed original')
        self.bridge.flush()
        payload, name, content = self.api.uploads[0]
        self.assertEqual(content, b'%PDF fixture'); self.assertEqual(name, 'report.pdf')
        self.assertEqual(payload['caption'], 'Report'); self.assertEqual(payload['chat_id'], -123)
        self.assertEqual(payload['message_thread_id'], 22); self.assertEqual(payload['reply_parameters']['message_id'], 41)
        self.assertEqual(self.bridge.deliveries()[0]['state'], 'sent')

    def test_file_uncertain_not_retried_until_explicit_retry(self):
        source = self.root / 'file.txt'; source.write_text('one')
        telegram_files.stage(self.bridge, source, 'outgoing')
        self.api.error = telegram.TelegramError('ambiguous network result', uncertain=True)
        self.bridge.flush(); self.assertEqual(self.bridge.deliveries()[0]['state'], 'uncertain')
        self.api.error = None
        self.bridge.recover(); self.bridge.flush(); self.assertEqual(len(self.api.uploads), 1)
        with patch('squad.telegram.client', return_value=self.api), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['telegram', 'retry', 'outgoing:0', '--root', str(self.root)]), 0)
        self.assertEqual(len(self.api.uploads), 2)

    def test_changed_staged_file_fails_before_upload_and_cli_file_only_works(self):
        source = self.root / 'x.txt'; source.write_text('original')
        telegram_files.stage(self.bridge, source, 'changed')
        with self.broker.db() as db:
            info = json.loads(db.execute('SELECT route FROM telegram_outbox').fetchone()[0])['file']
        (self.root / info['path']).write_text('altered')
        self.bridge.flush(); self.assertEqual(self.api.uploads, [])
        self.assertEqual(self.bridge.deliveries()[0]['state'], 'failed')
        with patch('squad.telegram.client', return_value=self.api), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['telegram', 'send', '--file', str(source), '--root', str(self.root)]), 0)
        self.assertEqual(self.api.uploads[-1][0]['caption'], '')

    def test_caption_unicode_limit_is_validated_before_staging(self):
        source = self.root / 'x.txt'; source.write_text('x')
        with self.assertRaises(ValueError): telegram_files.stage(self.bridge, source, 'bad', '😀' * 501)
        self.assertEqual(self.bridge.deliveries(), [])
        self.assertFalse((self.root / '.squad/attachments').exists())

    def test_api_upload_multipart_and_download_path_checks(self):
        api = telegram.BotAPI('123:PRIVATE')
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":9}}')) as opened:
            result = api.send_document({'chat_id': -123, 'caption': 'hello', 'reply_parameters': {'message_id': 7}}, '../../f.txt', b'FILE CONTENT')
        self.assertEqual(result['message_id'], 9)
        request = opened.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/sendDocument'))
        self.assertIn(b'filename="f.txt"', request.data); self.assertIn(b'FILE CONTENT', request.data)
        self.assertIn(b'{"message_id": 7}', request.data)
        info = {'file_path': 'documents/file_1.txt', 'file_size': 4}
        with patch.object(api, 'request', return_value=info), patch.object(api.opener, 'open', return_value=io.BytesIO(b'data')) as opened:
            self.assertEqual(api.download('FILE_ID'), b'data')
            self.assertTrue(opened.call_args.args[0].endswith('/documents/file_1.txt'))
        for path in ['../file.txt', '/absolute/file', 'https://evil.example/file', 'documents/../file', 'documents/file?x=1']:
            with patch.object(api, 'request', return_value={'file_path': path}), patch.object(api.opener, 'open') as opened:
                with self.assertRaises(telegram.TelegramError): api.download('FILE_ID')
                opened.assert_not_called()
        with patch.object(api, 'request', return_value=info), patch.object(api.opener, 'open', side_effect=OSError('SECRET bot123:PRIVATE')):
            with self.assertRaises(telegram.TelegramError) as failure: api.download('FILE_ID')
            self.assertNotIn('PRIVATE', str(failure.exception))

    def test_legacy_database_gets_attachment_column_without_losing_turn(self):
        self.receive(incoming(1, '@codex First'))
        before = self.broker.tasks()[0]['id']
        with self.broker.db() as db: db.execute('ALTER TABLE telegram_turns DROP COLUMN attachments')
        rebuilt = telegram.Bridge(self.root, self.cfg, self.api)
        with rebuilt.broker.db() as db:
            row = db.execute('SELECT task_id,attachments FROM telegram_turns').fetchone()
        self.assertEqual(row['task_id'], before); self.assertEqual(row['attachments'], '[]')

    def test_upgraded_worker_can_finish_legacy_turn_before_listener_migration(self):
        self.receive(incoming(1, '@codex Queued before upgrade'))
        with self.broker.db() as db: db.execute('ALTER TABLE telegram_turns DROP COLUMN attachments')
        with patch('squad.engines.run_engine', side_effect=model_reply):
            task = work_once(self.root, 'coder', kind='chat')
        self.assertEqual(task['state'], 'done')

    def test_attachment_reaches_real_worker_process_and_http_model(self):
        seen = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'The file describes the parser fixture.'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = store.load_config(self.root)
            config['engines']['fixture'] = {'transport': 'openai', 'model': 'fixture', 'auth': 'local', 'base_url': f'http://127.0.0.1:{server.server_port}/v1'}
            store.save_config(self.root, config); self.broker.agent('coder', 'team', 'fixture', [])
            self.receive(self.media())
            result = subprocess.run([sys.executable, '-m', 'squad', 'worker', '--root', str(self.root), '--agent', 'coder', '--chat-only'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.broker.tasks()[0]['state'], 'done')
            self.assertIn('Parser fixture information', json.dumps(seen))
            self.bridge.collect(); self.bridge.flush()
            self.assertIn('file describes the parser fixture', self.api.sent[-1]['text'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()

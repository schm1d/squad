"""Email/WhatsApp routing, authentication, attachments and delivery recovery."""
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from squad import attachments, channel_cli, cli, email_bridge, messaging, store, whatsapp
from squad.broker import Broker
from squad.worker import work_once
from test_telegram_chat import model_reply


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.broker = Broker(self.root)
        self.broker.company('team')
        self.broker.agent('coder', 'team', 'codex', [])
        self.broker.agent('writer', 'team', 'claude', [])
        self.broker.agent('unsafe', 'team', 'kimi', [])
        self.email = email_bridge.validate({'address': 'bot@example.org', 'allowed_senders': ['human@example.org', 'other@example.org'],
            'imap_host': 'imap.example.org', 'smtp_host': 'smtp.example.org', 'authserv_id': 'mx.example.org',
            'agent_aliases': {'codex': 'coder', 'claude': 'writer', 'kimi': 'unsafe'}, 'auto_discover_agents': False})
        self.wa = whatsapp.validate({'phone_number_id': '123456', 'api_version': 'v25.0',
            'allowed_users': ['41791234567', '41797654321'], 'allowed_groups': ['fixture-group'],
            'agent_aliases': {'codex': 'coder', 'claude': 'writer', 'kimi': 'unsafe'}, 'auto_discover_agents': False})
        cfg = store.load_config(self.root); cfg.update(email=self.email, whatsapp=self.wa)
        store.save_config(self.root, cfg)
        self.mail = messaging.Bridge(self.root, 'email', self.email)
        self.chat = messaging.Bridge(self.root, 'whatsapp', self.wa)
        self.env = patch.dict(os.environ, {'SQUAD_EMAIL_PASSWORD': 'fixture', 'SQUAD_WHATSAPP_TOKEN': 'fixture',
            'SQUAD_WHATSAPP_APP_SECRET': 'fixture-secret', 'SQUAD_WHATSAPP_VERIFY_TOKEN': 'verify-fixture'})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def email_message(self, text='@codex Inspect the attachment', sender='human@example.org', mid='<one@example.org>', auth=True):
        msg = EmailMessage(); msg['From'] = sender; msg['To'] = 'bot@example.org'; msg['Message-ID'] = mid
        msg['Subject'] = 'Squad question'
        if auth:
            msg['Authentication-Results'] = 'mx.example.org; dmarc=pass header.from=example.org'
        msg.set_content(text)
        return msg

    def inbound(self, text='@codex Hello', mid='wamid.fixture', user='41791234567', group=None, **extra):
        msg = {'from': user, 'id': mid, 'type': 'text', 'timestamp': str(int(time.time())), 'text': {'body': text}, **extra}
        if group:
            msg['group_id'] = group
        return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
            'metadata': {'phone_number_id': '123456'}, 'messages': [msg]}}]}]}

    def finish(self, bridge, agent='coder'):
        with patch('squad.engines.run_engine', side_effect=model_reply) as engine:
            task = work_once(self.root, agent, kind='chat')
        bridge.collect()
        sent = []
        def send(text, route, key):
            sent.append((text, route)); return '<reply@example.org>' if bridge.channel == 'email' else 'wamid.reply'
        bridge.deliver(send)
        return task, engine, sent

    def test_email_attachment_routes_worker_and_reply_thread(self):
        msg = self.email_message(); msg.add_attachment(b'parser detail 123', maintype='text', subtype='plain', filename='../../source.txt')
        env = email_bridge.parse_message(self.mail, msg.as_bytes(), '1:1')
        self.assertEqual(env['attachments'][0]['name'], 'source.txt')
        self.mail.ingest(env); self.mail.ingest(env); self.mail.process()
        self.assertEqual(len(self.broker.tasks()), 1)
        task, model, sent = self.finish(self.mail)
        self.assertEqual(task['state'], 'done'); self.assertFalse(task['write_allowed'])
        self.assertIn('parser detail 123', model.call_args.args[1])
        self.assertEqual(sent[0][1]['to'], 'human@example.org')
        reply = self.email_message('Explain that further', mid='<two@example.org>'); reply['In-Reply-To'] = '<reply@example.org>'
        self.mail.ingest(email_bridge.parse_message(self.mail, reply.as_bytes(), '1:2')); self.mail.process()
        task, model, sent = self.finish(self.mail)
        self.assertIn('Inspect the attachment', model.call_args.args[1])
        self.assertEqual(task['state'], 'done')

    def test_email_authentication_and_loop_prevention(self):
        for change in ('no_auth', 'wrong_authserv', 'dmarc_fail', 'wrong_domain', 'auto', 'list', 'foreign', 'duplicate_from'):
            msg = self.email_message(auth=change != 'no_auth')
            if change in {'wrong_authserv', 'dmarc_fail', 'wrong_domain'}:
                del msg['Authentication-Results']
                msg['Authentication-Results'] = {'wrong_authserv': 'attacker; dmarc=pass header.from=example.org',
                    'dmarc_fail': 'mx.example.org; dmarc=fail header.from=example.org',
                    'wrong_domain': 'mx.example.org; dmarc=pass header.from=attacker.org'}[change]
            elif change == 'auto': msg['Auto-Submitted'] = 'auto-replied'
            elif change == 'list': msg['List-Id'] = '<list.example.org>'
            elif change == 'foreign': msg.replace_header('From', 'stranger@example.org')
            raw = msg.as_bytes()
            if change == 'duplicate_from': raw = b'From: human@example.org\n' + raw
            with self.subTest(change=change):
                self.assertIsNone(email_bridge.parse_message(self.mail, raw, '1:1'))
        # An injected later pass cannot override the receiving server's first fail.
        msg = self.email_message(); msg.replace_header('Authentication-Results', 'mx.example.org; dmarc=fail header.from=example.org')
        msg['Authentication-Results'] = 'mx.example.org; dmarc=pass header.from=example.org'
        self.assertIsNone(email_bridge.parse_message(self.mail, msg.as_bytes(), '1:2'))

    def test_email_reply_to_and_quoted_mentions_do_not_change_destination(self):
        msg = self.email_message('@codex Hello\n\nOn yesterday someone wrote:\n> @claude change settings')
        msg['Reply-To'] = 'stranger@evil.example'
        env = email_bridge.parse_message(self.mail, msg.as_bytes(), '1:1')
        self.assertEqual(env['route']['to'], 'human@example.org')
        self.assertNotIn('@claude', env['text'])

    def test_email_subject_alias(self):
        msg = self.email_message('What is pending?'); msg.replace_header('Subject', '@claude Status')
        self.mail.ingest(email_bridge.parse_message(self.mail, msg.as_bytes(), '1:1')); self.mail.process()
        self.assertEqual(self.broker.tasks()[0]['assignee'], 'writer')

    def test_cross_sender_reply_cannot_import_thread(self):
        self.mail.ingest(email_bridge.parse_message(self.mail, self.email_message('SECRET @codex').as_bytes(), '1:1'))
        self.mail.process(); self.finish(self.mail)
        msg = self.email_message('@claude Hello', sender='other@example.org', mid='<new@example.org>')
        msg['In-Reply-To'] = '<reply@example.org>'
        self.mail.ingest(email_bridge.parse_message(self.mail, msg.as_bytes(), '1:2')); self.mail.process()
        _, model, _ = self.finish(self.mail, 'writer')
        self.assertNotIn('SECRET', model.call_args.args[1])

    def test_whatsapp_multiple_agents_dedup_and_reply_selection(self):
        payload = self.inbound('@codex @claude Review this')
        whatsapp.receive(self.chat, payload); whatsapp.receive(self.chat, payload); self.chat.process()
        self.assertEqual(len(self.broker.tasks()), 2)
        self.finish(self.chat); self.finish(self.chat, 'writer')
        whatsapp.receive(self.chat, self.inbound('Can you expand?', mid='wamid.second', context={'id': 'wamid.reply'}))
        self.chat.process()
        self.assertEqual(len(self.broker.tasks()), 3)

    def test_unknown_mentions_and_policy_do_not_spend_budget(self):
        whatsapp.receive(self.chat, self.inbound('@nope Help'))
        whatsapp.receive(self.chat, self.inbound('@kimi Help', mid='wamid.kimi'))
        self.chat.process()
        self.assertEqual(self.broker.tasks(), [])
        self.assertEqual(len(self.chat.status()['outbox']), 2)

    def test_max_pending_is_atomic_for_multi_agent_message(self):
        self.chat.cfg['max_pending_tasks'] = 1
        whatsapp.receive(self.chat, self.inbound('@codex @claude Help')); self.chat.process()
        self.assertEqual(self.broker.tasks(), [])
        self.assertEqual(self.chat.status()['inbox'][0]['state'], 'failed')
        self.assertEqual(self.broker.budgets(), [])

    def test_groups_and_sender_allowlists_are_both_required(self):
        whatsapp.receive(self.chat, self.inbound('@codex ignored', user='49123456789'))
        whatsapp.receive(self.chat, self.inbound('@codex ignored', group='unapproved'))
        self.chat.process(); self.assertEqual(self.broker.tasks(), [])
        whatsapp.receive(self.chat, self.inbound('@codex GROUP_SECRET', group='fixture-group')); self.chat.process()
        self.finish(self.chat)
        whatsapp.receive(self.chat, self.inbound('@codex direct', mid='wamid.private', context={'id': 'wamid.reply'})); self.chat.process()
        _, model, _ = self.finish(self.chat)
        self.assertNotIn('GROUP_SECRET', model.call_args.args[1])

    def test_whatsapp_media_caption_and_worker_reference(self):
        payload = self.inbound(type='document', document={'id': '123', 'filename': '../code.txt', 'caption': '@claude Review'})
        whatsapp.receive(self.chat, payload)
        api = whatsapp.CloudAPI(self.wa)
        with patch.object(api, 'download', return_value=(b'content from document', 'text/plain')):
            self.chat.process(lambda env: api.prepare(self.chat, env))
        task, model, _ = self.finish(self.chat, 'writer')
        self.assertEqual(task['state'], 'done')
        self.assertIn('content from document', model.call_args.args[1])
        self.assertIn('/.squad/attachments/whatsapp/', model.call_args.args[1])

    def test_attachment_quota_integrity_binary_and_symlink(self):
        info = attachments.save(self.root, 'email', 'scope', 'id', '../a.txt', b'hello', 'text/plain')
        self.assertEqual(attachments.read_saved(self.root, info), b'hello')
        (self.root / info['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError): attachments.read_saved(self.root, info)
        binary = attachments.save(self.root, 'whatsapp', 'scope', 'id2', 'x.png', b'\x89PNG\x00', 'image/png')
        self.assertIn('no text/vision/audio interpretation', attachments.describe(self.root, [binary]))
        with patch.object(attachments, 'STORE_BYTES', 1):
            with self.assertRaises(ValueError): attachments.save(self.root, 'email', 'scope', 'id3', 'q.txt', b'xx', 'text/plain')
        path = self.root / binary['path']; path.unlink(); path.symlink_to(self.root / 'TEAM.md')
        with self.assertRaises(ValueError): attachments.read_saved(self.root, binary)

    def test_uncertain_send_is_not_repeated_after_restart(self):
        whatsapp.receive(self.chat, self.inbound('/agents')); self.chat.process()
        send = Mock(side_effect=messaging.DeliveryError('connection interrupted', uncertain=True))
        self.chat.deliver(send)
        new = messaging.Bridge(self.root, 'whatsapp', self.wa); new.deliver(send)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(new.status()['outbox'][0]['state'], 'uncertain')

    def test_interrupted_send_and_revoked_recipient(self):
        whatsapp.receive(self.chat, self.inbound('/agents')); self.chat.process()
        with self.chat.broker.db() as db: db.execute("UPDATE channel_outbox SET state='sending'")
        send = Mock(); self.chat.deliver(send); send.assert_not_called()
        self.assertEqual(self.chat.status()['outbox'][0]['state'], 'uncertain')
        whatsapp.receive(self.chat, self.inbound('/help', mid='wamid.two')); self.chat.process()
        self.chat.cfg['allowed_users'] = []
        self.chat.deliver(send); send.assert_not_called()
        self.assertEqual(self.chat.status()['outbox'][0]['error'], 'recipient_removed')

    def test_worker_failures_are_sanitized_for_both_channels(self):
        for bridge, env in [(self.mail, email_bridge.parse_message(self.mail, self.email_message().as_bytes(), '1:1'))]:
            bridge.ingest(env); bridge.process()
        whatsapp.receive(self.chat, self.inbound()); self.chat.process()
        for bridge in (self.mail, self.chat):
            with patch('squad.engines.run_engine', return_value={'ok': False, 'stdout': '', 'stderr': 'SECRET_CREDENTIAL authentication failed'}):
                work_once(self.root, 'coder', kind='chat')
            bridge.collect()
            with bridge.broker.db() as db:
                text = db.execute('SELECT text FROM channel_outbox WHERE bridge=?', (bridge.key,)).fetchone()['text']
            self.assertNotIn('SECRET_CREDENTIAL', text)
            self.assertIn('squad tasks', text)

    def test_signed_webhook_verification_persistence_and_replay(self):
        server = whatsapp.webhook_server(self.chat, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}/webhook'
        try:
            with urllib.request.urlopen(base + '?hub.mode=subscribe&hub.verify_token=verify-fixture&hub.challenge=123') as r:
                self.assertEqual(r.read(), b'123')
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + '?hub.mode=subscribe&hub.verify_token=wrong')
            self.assertEqual(err.exception.code, 403)
            body = json.dumps(self.inbound()).encode()
            req = urllib.request.Request(base, data=body, headers={'X-Hub-Signature-256': 'sha256=bad'})
            with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(req)
            self.assertEqual(self.chat.status()['inbox'], [])
            signature = 'sha256=' + hmac.new(b'fixture-secret', body, hashlib.sha256).hexdigest()
            for _ in range(2):
                req = urllib.request.Request(base, data=body, headers={'X-Hub-Signature-256': signature})
                with urllib.request.urlopen(req) as r: self.assertEqual(r.status, 200)
            self.assertEqual(len(self.chat.status()['inbox']), 1)
            self.assertEqual(self.broker.tasks(), [])  # No model work in the HTTP request.
            self.chat.process(); self.assertEqual(len(self.broker.tasks()), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_whatsapp_account_scope_and_status_callback(self):
        payload = self.inbound(); payload['entry'][0]['changes'][0]['value']['metadata']['phone_number_id'] = 'other'
        whatsapp.receive(self.chat, payload); self.assertEqual(self.chat.status()['inbox'], [])
        whatsapp.receive(self.chat, self.inbound()); self.chat.process(); self.finish(self.chat)
        payload = self.inbound(); value = payload['entry'][0]['changes'][0]['value']; value['messages'] = []
        for state in ('delivered', 'read', 'delivered'):
            value['statuses'] = [{'id': 'wamid.reply', 'status': state}]; whatsapp.receive(self.chat, payload)
        self.assertEqual(self.chat.status()['outbox'][0]['state'], 'read')

    def test_whatsapp_download_checks_host_size_and_hash(self):
        api = whatsapp.CloudAPI(self.wa); data = b'file'
        info = {'url': 'https://lookaside.fbsbx.com/whatsapp_business/attachments/?fixture=1', 'file_size': 4,
                'mime_type': 'text/plain', 'sha256': hashlib.sha256(data).hexdigest()}
        with patch.object(api, 'graph', return_value=info), patch.object(api, '_request', return_value=data) as download:
            self.assertEqual(api.download('123'), (data, 'text/plain'))
            for bad in ['https://evil.example/file', 'https://lookaside.fbsbx.com.evil.example/file', 'http://lookaside.fbsbx.com/file', 'https://user@lookaside.fbsbx.com/file']:
                info['url'] = bad
                with self.assertRaises(ValueError): api.download('123')
            self.assertEqual(download.call_count, 1)

    def test_whatsapp_send_text_and_file_and_closed_window(self):
        api = whatsapp.CloudAPI(self.wa)
        route = {'to': '41791234567', 'received_at': time.time(), 'reply_id': 'wamid.in'}
        with patch.object(api, 'graph', return_value={'messages': [{'id': 'wamid.sent'}]}) as request:
            self.assertEqual(api.send('hello', route, 'id'), 'wamid.sent')
            self.assertEqual(request.call_args.args[1]['context'], {'message_id': 'wamid.in'})
            route['received_at'] = 1
            with self.assertRaises(messaging.DeliveryError): api.send('hello', route, 'id')
            self.assertEqual(request.call_count, 1)
        info = attachments.save(self.root, 'whatsapp', 'scope', 'id', 'data.txt', b'data', 'text/plain')
        route.update(received_at=time.time(), root=str(self.root), files=[info])
        with patch.object(api, '_request', return_value={'id': '123'}) as upload, patch.object(api, 'graph', return_value={'messages': [{'id': 'wamid.sent'}]}) as request:
            api.send('caption', route, 'id')
            self.assertIn(b'name="messaging_product"', upload.call_args.args[1])
            self.assertEqual(request.call_args.args[1]['document']['filename'], 'data.txt')
        route.update(to='fixture-group', recipient_type='group', sender='41791234567')
        with patch.object(api, 'upload', return_value=('123', 'document')), patch.object(api, 'graph', return_value={'messages': [{'id': 'wamid.group'}]}) as request:
            api.send('caption', route, 'id'); self.assertEqual(request.call_args.args[1]['recipient_type'], 'group')

    def test_smtp_tls_reply_headers_and_attachment(self):
        info = attachments.save(self.root, 'email', 'scope', 'id', 'data.txt', b'file data', 'text/plain')
        smtp = Mock()
        with patch('smtplib.SMTP_SSL', return_value=smtp):
            mid = email_bridge.MailClient(self.email).send('answer', {'to': 'human@example.org', 'scope': 'human@example.org',
                'subject': 'question', 'reply_id': '<in@example.org>', 'files': [info], 'root': str(self.root)}, 'delivery')
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg['Auto-Submitted'], 'auto-replied')
        self.assertEqual(msg['In-Reply-To'], '<in@example.org>')
        self.assertEqual(msg['Message-ID'], mid)
        self.assertEqual(list(msg.iter_attachments())[0].get_payload(decode=True), b'file data')
        self.assertEqual(smtp.send_message.call_args.kwargs['to_addrs'], ['human@example.org'])

    def test_imap_first_start_uid_reset_and_peek_dedup(self):
        raw = self.email_message().as_bytes()
        conn = Mock(); conn.select.return_value = ('OK', [b'1'])
        validity, nextuid = [1], [10]
        conn.response.side_effect = lambda name: (name, [str(validity[0] if name == 'UIDVALIDITY' else nextuid[0]).encode()])
        def uid(command, *args):
            if command == 'search': return 'OK', [b'9 10']
            if args[1] == '(RFC822.SIZE)': return 'OK', [f'1 (UID 10 RFC822.SIZE {len(raw)})'.encode()]
            return 'OK', [(b'BODY[]', raw)]
        conn.uid.side_effect = uid
        api = email_bridge.MailClient(self.email)
        with patch('imaplib.IMAP4_SSL', return_value=conn):
            api.poll(self.mail); conn.uid.assert_not_called()
            nextuid[0] = 11; api.poll(self.mail); api.poll(self.mail)
            self.assertEqual(len(self.mail.status()['inbox']), 1)
            self.assertTrue(any(call.args[-1] == '(BODY.PEEK[])' for call in conn.uid.call_args_list))
            validity[0] = 2; nextuid[0] = 100; api.poll(self.mail)
            self.assertEqual(json.loads(self.mail.state('imap_cursor')), {'validity': 2, 'uid': 99})

    def test_discovery_reuses_agents_across_channels_and_dry_run(self):
        with patch('shutil.which', side_effect=lambda n: '/fixture/' + n if n in {'codex', 'claude', 'qwen'} else None), patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            before = (self.root / '.squad/config.json').read_bytes()
            preview = messaging.discover(self.root, 'whatsapp', dry_run=True)
            self.assertEqual(before, (self.root / '.squad/config.json').read_bytes())
            self.assertEqual(len(self.broker.roster()['agents']), 3)
            result = messaging.discover(self.root, 'whatsapp')
            mail = messaging.discover(self.root, 'email')
        self.assertEqual(result['agent_aliases']['codex'], 'coder')
        self.assertEqual(mail['agent_aliases']['qwen'], result['agent_aliases']['qwen'])
        self.assertEqual(result['agent_aliases'], preview['agent_aliases'])
        self.assertEqual(len(self.broker.roster()['agents']), 4)

    def test_live_alias_reload_and_scope_change_requires_restart(self):
        cfg = store.load_config(self.root); cfg['whatsapp']['agent_aliases']['new'] = 'writer'; store.save_config(self.root, cfg)
        self.chat.reload(); self.assertEqual(self.chat.cfg['agent_aliases']['new'], 'writer')
        cfg['whatsapp']['allowed_users'] = ['41791234567']; store.save_config(self.root, cfg)
        with self.assertRaisesRegex(ValueError, 'restart'): self.chat.reload()

    def test_managed_listeners_use_real_workers_and_http_model(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = []
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                response = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'The attached fixture says ready.'},
                    'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(response))); self.end_headers(); self.wfile.write(response)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            cfg = store.load_config(self.root)
            cfg['engines']['fixture'] = {'transport': 'openai', 'model': 'fixture', 'auth': 'local',
                'base_url': f'http://127.0.0.1:{server.server_port}/v1'}
            store.save_config(self.root, cfg)
            self.broker.agent('coder', 'team', 'fixture', [])
            original_sleep = time.sleep
            for bridge in (self.mail, self.chat):
                delivered, started, injected = [], time.monotonic(), []
                with contextlib.ExitStack() as stack:
                    api = Mock()
                    def send(text, route, delivery_id):
                        delivered.append((text, route)); return 'fixture-reply'
                    api.send.side_effect = send
                    if bridge.channel == 'email':
                        stack.enter_context(patch('squad.email_bridge.MailClient', return_value=api))
                        def poll(active):
                            if not injected:
                                msg = self.email_message('@codex Check this file', mid='<managed@example.org>')
                                msg.add_attachment(b'fixture ready', maintype='text', subtype='plain', filename='input.txt')
                                active.ingest(email_bridge.parse_message(active, msg.as_bytes(), '1:20')); injected.append(True)
                        api.poll.side_effect = poll
                    else:
                        api.prepare.side_effect = lambda active, env: env
                        stack.enter_context(patch('squad.whatsapp.CloudAPI', return_value=api))
                        real_server = whatsapp.webhook_server
                        servers = []
                        def make(active, host, port):
                            instance = real_server(active, host, port); servers.append(instance); return instance
                        stack.enter_context(patch('squad.whatsapp.webhook_server', side_effect=make))
                    def tick(seconds):
                        if bridge.channel == 'whatsapp' and not injected:
                            body = json.dumps(self.inbound('@codex What is ready?', mid='wamid.managed')).encode()
                            signature = 'sha256=' + hmac.new(b'fixture-secret', body, hashlib.sha256).hexdigest()
                            req = urllib.request.Request(f'http://127.0.0.1:{servers[0].server_port}/webhook', data=body,
                                headers={'X-Hub-Signature-256': signature})
                            with urllib.request.urlopen(req, timeout=3) as response: self.assertEqual(response.status, 200)
                            injected.append(True)
                        if delivered: raise KeyboardInterrupt()
                        if time.monotonic() - started > 12: raise AssertionError('Managed channel listener fixture timed out')
                        original_sleep(0.05)
                    stack.enter_context(patch('squad.channel_cli.time.sleep', side_effect=tick))
                    with self.assertRaises(KeyboardInterrupt):
                        channel_cli._listen(bridge, SimpleNamespace(with_workers=True, host='127.0.0.1', port=0))
                self.assertEqual(len(delivered), 1)
                self.assertIn('fixture says ready', delivered[0][0])
            self.assertEqual(len(requests), 2)
            self.assertIn('fixture ready', json.dumps(requests[0]))
            self.assertEqual({t['state'] for t in self.broker.tasks()}, {'done'})
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_installer_offers_email_and_whatsapp_setup_after_pip(self):
        import install
        calls = []
        def installed(*args, **kwargs): calls.append('pip')
        def configured(root, channel):
            self.assertEqual(root, self.root); calls.append(channel); return {'configured': True}
        with patch('install.sys.stdin.isatty', return_value=False), patch('install.venv.EnvBuilder'), \
             patch('install.subprocess.run', side_effect=installed), patch('install.print_setup_checks'), \
             patch('install.print_project_check', return_value={'path': str(self.root)}), \
             patch('squad.channel_cli.setup', side_effect=configured), contextlib.redirect_stdout(io.StringIO()):
            result = install.main(['--no-add-to-path', '--no-setup-telegram', '--setup-email', '--setup-whatsapp',
                '--project', str(self.root), '--prefix', str(self.root / 'prefix')])
        self.assertEqual(result, 0)
        self.assertEqual(calls, ['pip', 'email', 'whatsapp'])

    def test_cli_root_at_every_level_and_setup_keeps_secrets_out_of_json(self):
        for command in (['--root', str(self.root), 'whatsapp', 'status'], ['whatsapp', '--root', str(self.root), 'status'],
                        ['whatsapp', 'status', '--root', str(self.root)], ['email', 'status', '--root', str(self.root)],
                        ['channels', 'status', '--root', str(self.root)]):
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(cli.main(command), 0)
        with patch('shutil.which', return_value=None), patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            result = channel_cli.setup(self.root, 'whatsapp', interactive=False)
        self.assertTrue(result['secrets_set']['SQUAD_WHATSAPP_TOKEN'])
        self.assertNotIn('fixture-secret', (self.root / '.squad/config.json').read_text())


if __name__ == '__main__': unittest.main()

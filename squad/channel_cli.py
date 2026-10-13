"""Command-line setup and supervised channel listeners."""
from __future__ import annotations
import json
import mimetypes
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from . import attachments, messaging, store
from .locking import workspace_lock


def add_parsers(sub, parent):
    for channel in messaging.CHANNELS:
        top = sub.add_parser(channel, help=f'{channel.title()} agent conversations and files', parents=[parent])
        commands = top.add_subparsers(dest='channel_command', required=True)
        setup_parser = commands.add_parser('setup', parents=[parent], help='Configure the channel and discover agents')
        setup_parser.add_argument('--agent', action='append', default=[])
        setup_parser.add_argument('--default-agent')
        if channel == 'email':
            for name in ['address', 'imap-host', 'smtp-host', 'authserv-id', 'password-env', 'imap-password-env', 'username', 'smtp-username', 'mailbox']:
                setup_parser.add_argument('--' + name)
            setup_parser.add_argument('--allow-sender', action='append', dest='allowed_senders')
            setup_parser.add_argument('--imap-port', type=int)
            setup_parser.add_argument('--smtp-port', type=int)
            setup_parser.add_argument('--smtp-security', choices=['tls', 'starttls'])
        else:
            for name in ['phone-number-id', 'api-version', 'token-env', 'app-secret-env', 'verify-token-env']:
                setup_parser.add_argument('--' + name)
            setup_parser.add_argument('--allow-user', action='append', dest='allowed_users')
            setup_parser.add_argument('--allow-group', action='append', dest='allowed_groups', help='Eligible Meta Groups API group ID; not a personal WhatsApp group')
        commands.add_parser('status', parents=[parent])
        discover = commands.add_parser('discover', parents=[parent])
        discover.add_argument('--dry-run', action='store_true')
        listen = commands.add_parser('listen', parents=[parent])
        listen.add_argument('--with-workers', action='store_true')
        if channel == 'whatsapp':
            listen.add_argument('--host', default='127.0.0.1', help='Bind behind an HTTPS reverse proxy')
            listen.add_argument('--port', type=int, default=8788)
        send = commands.add_parser('send', parents=[parent])
        send.add_argument('text', nargs='?', default='')
        send.add_argument('--to', required=True)
        send.add_argument('--file', type=Path, action='append', default=[])
        if channel == 'email':
            send.add_argument('--subject', default='Squad')
        else:
            send.add_argument('--group', action='store_true')
    top = sub.add_parser('channels', help='Manage all configured messaging channels', parents=[parent])
    commands = top.add_subparsers(dest='channel_command', required=True)
    discover = commands.add_parser('discover', parents=[parent], help='Add newly available agents to every configured channel')
    discover.add_argument('--dry-run', action='store_true')
    commands.add_parser('status', parents=[parent])


def setup(root, channel, updates=None, bindings=None, interactive=None):
    interactive = sys.stdin.isatty() if interactive is None else interactive
    updates = {k: v for k, v in (updates or {}).items() if v is not None}
    current = {**(store.load_config(root).get(channel) or {}), **updates}
    questions = ([('address', 'Bot email address'), ('imap_host', 'IMAP TLS host'), ('smtp_host', 'SMTP TLS host (port 465 by default)'),
                  ('authserv_id', 'Trusted Authentication-Results server ID (from your mailbox provider)'),
                  ('allowed_senders', 'Allowed human email addresses (comma separated)')]
        if channel == 'email' else [('phone_number_id', 'Meta WhatsApp phone-number ID'),
                  ('api_version', 'Supported Graph API version from your Meta dashboard (vNN.0)'),
                  ('allowed_users', 'Allowed WhatsApp phone numbers with country code (comma separated)')])
    for key, question in questions:
        if not current.get(key):
            if not interactive:
                option = {'allowed_users': 'allow-user', 'allowed_senders': 'allow-sender'}.get(key, key.replace('_', '-'))
                raise ValueError(f'{channel} setup requires --{option} or saved settings')
            value = input(question + ': ').strip()
            updates[key] = [x.strip() for x in value.split(',') if x.strip()] if key.startswith('allowed_') else value
    result = messaging.configure(root, channel, updates, bindings)
    result['secrets_set'] = {value: bool(__import__('os').environ.get(value)) for key, value in result.items() if key.endswith('_env')}
    return result


def _listen(bridge, args):
    from .telegram_chat import workers
    channel = bridge.channel
    server = thread = None
    check_context = workers(bridge.root, bridge.cfg['agent_aliases']) if args.with_workers else nullcontext(lambda *_: None)
    with workspace_lock(bridge.root, channel + '-listen', blocking=False), check_context as check:
        if channel == 'whatsapp':
            from .whatsapp import CloudAPI, webhook_server
            api = CloudAPI(bridge.cfg)
            server = webhook_server(bridge, args.host, args.port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f'WhatsApp webhook: http://{args.host}:{server.server_port}/webhook; expose it through your HTTPS reverse proxy.', file=sys.stderr)
        else:
            from .email_bridge import MailClient
            api = MailClient(bridge.cfg)
            print('Email listens for new mail after its first successful IMAP connection; existing inbox mail is skipped.', file=sys.stderr)
        try:
            next_poll = 0
            while True:
                bridge.reload()
                check(bridge.cfg['agent_aliases'])
                if channel == 'email' and time.monotonic() >= next_poll:
                    api.poll(bridge)
                    next_poll = time.monotonic() + 15
                bridge.process((lambda env: api.prepare(bridge, env)) if channel == 'whatsapp' else (lambda env: env))
                bridge.collect()
                bridge.deliver(api.send)
                time.sleep(1)
        finally:
            if server:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


def _send(bridge, args):
    channel = bridge.channel
    if not args.text.strip() and not args.file:
        raise ValueError('Provide text or --file PATH')
    if len(args.text) > 60000 or len(args.file) > (1 if channel == 'whatsapp' else 8):
        raise ValueError('Message or attachment count exceeds the channel limit')
    if channel == 'email':
        from .email_bridge import MailClient, address
        target = address(args.to)
        route = {'to': target, 'scope': target, 'subject': args.subject}
        api = MailClient(bridge.cfg)
    else:
        from .whatsapp import CloudAPI, phone
        target = args.to if args.group else phone(args.to)
        # Only a real authenticated inbound message opens a free-form reply window.
        # Reuse its real timestamp; do not manufacture a window for CLI sends.
        with bridge.broker.db() as db:
            envelopes = db.execute('SELECT payload FROM channel_inbox WHERE bridge=? ORDER BY created DESC', (bridge.key,))
            route = next((e['route'] for e in (json.loads(r['payload']) for r in envelopes)
                          if e['route']['to'] == target and e['route']['recipient_type'] == ('group' if args.group else 'individual')), None)
        if not route:
            raise ValueError('Receive an allowlisted WhatsApp message first; free-form sends need an active conversation')
        api = CloudAPI(bridge.cfg)
    if not messaging.module(channel).allowed_route(bridge.cfg, route):
        raise ValueError('Recipient is not allowlisted in this project')
    key = 'manual:' + uuid.uuid4().hex
    files, total = [], 0
    for path in args.file:
        name, content = attachments.local_file(path)
        total += len(content)
        if total > attachments.MAX_BYTES:
            raise ValueError('Attachments must total at most 10 MiB')
        info = attachments.save(bridge.root, channel, bridge.key + ':' + route['scope'], key,
                                name, content, mimetypes.guess_type(name)[0] or 'application/octet-stream')
        files.append(info)
    route.update(files=files, root=str(bridge.root))
    with bridge.broker.db() as db:
        bridge.queue(db, key, args.text, route)
    bridge.deliver(api.send)
    delivery = attachments.digest(bridge.key + ':' + key)
    result = next(row for row in bridge.status()['outbox'] if row['id'] == delivery)
    print(json.dumps(result, indent=2))
    return 0 if result['state'] == 'sent' else 2


def run(root, args):
    channel = args.cmd
    if channel == 'channels':
        configured = [c for c in ('telegram', *messaging.CHANNELS) if store.load_config(root).get(c)]
        result = {}
        for name in configured:
            if name == 'telegram':
                from . import onboarding, telegram
                result[name] = onboarding.discover(root, dry_run=args.dry_run) if args.channel_command == 'discover' else telegram.settings(root)
            else:
                result[name] = messaging.discover(root, name, args.dry_run) if args.channel_command == 'discover' else messaging.Bridge(root, name, messaging.settings(root, name)).status()
        print(json.dumps(result, indent=2)); return 0
    if args.channel_command == 'setup':
        updates = {k: v for k, v in vars(args).items() if k not in {'cmd', 'channel_command', 'root', 'agent'}}
        print(json.dumps(setup(root, channel, updates, args.agent), indent=2)); return 0
    if args.channel_command == 'discover':
        print(json.dumps(messaging.discover(root, channel, args.dry_run), indent=2)); return 0
    cfg = messaging.settings(root, channel)
    if args.channel_command == 'listen' and cfg['auto_discover_agents']:
        messaging.discover(root, channel)
        cfg = messaging.settings(root, channel)
    bridge = messaging.Bridge(root, channel, cfg)
    if args.channel_command == 'status':
        print(json.dumps(bridge.status(), indent=2)); return 0
    if args.channel_command == 'send':
        return _send(bridge, args)
    return _listen(bridge, args) or 0

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from squad import cli, engines, store, telegram_mcp as tm
from squad.mcp_bridge import ToolHub, proxy_config

try:
    import mcp
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / 'tests/telegram_mcp_fixture.py'


def fixture_config():
    return {'mcp_servers': {'telegram-account': {
        'integration': tm.INTEGRATION, 'command': sys.executable, 'args': [str(FIXTURE)],
        'allowed_tools': ['get_messages', 'send_message'], 'read_only_tools': ['get_messages'],
        'allowed_chat_ids': [-123], 'env_allow': ['TELEGRAM_API_HASH'],
        'env': {'TELEGRAM_EXPOSED_TOOLS': 'all'}}},
        'engines': {'codex': {'mcp_servers': ['telegram-account']}}}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.checkout = self.root / 'upstream checkout'; self.checkout.mkdir()
        for name in ('main.py', 'session_string_generator.py', 'telegram_mcp/runner.py'):
            p = self.checkout / name; p.parent.mkdir(exist_ok=True); p.write_text('')
        (self.checkout / 'pyproject.toml').write_text(tm.UPSTREAM)

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **kwargs):
        with patch('squad.telegram_mcp.shutil.which', return_value='/usr/bin/uv'), \
             patch('squad.telegram_mcp.subprocess.run', return_value=subprocess.CompletedProcess([], 0, tm.REVISION, '')):
            return tm.configure(self.root, self.checkout, ['codex'], [], **kwargs)

    def test_configure_merges_preserves_bridge_and_is_idempotent(self):
        cfg = store.load_config(self.root)
        cfg['telegram'] = {'chat_id': -123, 'token_env': 'BOT_SECRET'}
        cfg['engines'] = {'codex': {'mcp_servers': ['existing'], 'context_tokens': 9000}}
        cfg['mcp_servers'] = {'existing': {'allowed_tools': ['lookup']}}
        store.save_config(self.root, cfg)
        with patch.dict(os.environ, {'TELEGRAM_API_HASH': 'private-must-not-be-saved'}):
            self.configure(); self.configure()
        updated = store.load_config(self.root)
        self.assertEqual(updated['telegram'], cfg['telegram'])
        self.assertEqual(updated['engines']['codex']['mcp_servers'], ['existing', 'telegram-account'])
        self.assertEqual(updated['engines']['codex']['context_tokens'], 9000)
        self.assertNotIn('private-must-not-be-saved', json.dumps(updated))
        self.assertEqual(updated['mcp_servers']['telegram-account']['allowed_tools'], list(tm.READ_TOOLS))
        self.configure(allow_send=True)
        self.assertIn('send_message', store.load_config(self.root)['mcp_servers']['telegram-account']['allowed_tools'])

    def test_missing_chat_or_wrong_checkout_does_not_write_configuration(self):
        before = store.load_config(self.root)
        with self.assertRaises(ValueError): self.configure()
        (self.checkout / 'pyproject.toml').write_text('unrelated package')
        with self.assertRaises(ValueError): self.configure()
        self.assertEqual(store.load_config(self.root), before)

    def test_codex_proxy_config_roundtrips_toml_without_secret_values(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest('TOML round-trip check requires Python 3.11+')
        cfg = fixture_config()
        with patch('squad.engines.which_binary', return_value='/usr/bin/codex'), \
             patch.dict(os.environ, {'TELEGRAM_API_HASH': 'must-not-leak'}):
            cmd = engines.build_command(engines.BUILTINS['codex'], 'Read the latest message',
                                        cwd=self.root, write=False, config=cfg)
        settings = tomllib.loads('\n'.join(cmd[i+1] for i, value in enumerate(cmd[:-1]) if value == '-c'))
        proxy = settings['mcp_servers']['squad_tools']
        self.assertTrue(proxy['required'])
        self.assertIn('proxy', proxy['args'])
        self.assertNotIn('--allow-write', proxy['args'])
        self.assertEqual(proxy['env_vars'], ['TELEGRAM_API_HASH'])
        self.assertNotIn('must-not-leak', str(cmd))
        continued = engines.apply_session_flags(engines.BUILTINS['codex'], cmd, cmd[-1], resume=None, cont=True)
        self.assertEqual(continued[-3:], ['resume', '--last', cmd[-1]])
        self.assertIn('--allow-write', proxy_config(self.root, ['telegram-account'], True)['mcpServers']['squad-tools']['args'])

    def test_status_and_proxy_export_do_not_need_bot_token_or_login(self):
        store.save_config(self.root, fixture_config())
        for args in (['telegram', 'mcp', 'status'], ['mcp', 'proxy-config', '--server', 'telegram-account']):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli._main(args + ['--root', str(self.root)]), 0)
            self.assertIsInstance(json.loads(output.getvalue()), dict)


@unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
class TelegramMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.cfg = fixture_config()
        store.save_config(self.root, self.cfg)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_toolhub_chat_limits_env_and_write_filter(self):
        with patch.dict(os.environ, {'TELEGRAM_API_HASH': 'fixture-secret', 'SQUAD_UNRELATED_SECRET': 'not-for-server'}):
            async with ToolHub(self.root, self.cfg, ['telegram-account'], False) as hub:
                self.assertEqual([item['name'] for item in hub.catalog], ['get_messages'])
                name = hub.catalog[0]['alias']
                good = await hub.call(name, {'chat_id': '-123'})
                text = good['content'][0]['text']
                self.assertIn('read-only', text)
                self.assertIn('"credential_present": true', text)
                self.assertIn('"unrelated_key_present": false', text)
                for args in ({'chat_id': -999}, {'chat_id': '@someone'},
                             {'chat_id': -123, 'page_size': 500}, {'chat_id': -123, 'account': 'other'}):
                    with self.assertRaises(ValueError): await hub.call(name, args)
                large = await hub.call(name, {'chat_id': -123, 'page': 2})
                self.assertLess(len(json.dumps(large)), 12500)
                self.assertIn('Truncated', large['content'][0]['text'])
        self.assertFalse((self.root / 'telegram-fixture-sent').exists())

    async def test_send_requires_selection_and_write_permission(self):
        async with ToolHub(self.root, self.cfg, ['telegram-account'], True) as hub:
            send = next(item['alias'] for item in hub.catalog if item['name'] == 'send_message')
            with self.assertRaises(ValueError):
                await hub.call(send, {'chat_id': -999, 'message': 'no'})
            self.assertFalse((self.root / 'telegram-fixture-sent').exists())
            result = await hub.call(send, {'chat_id': -123, 'message': 'fixture only'})
            self.assertFalse(result.get('isError'))
            self.assertEqual((self.root / 'telegram-fixture-sent').read_text(), 'fixture only')

    async def test_real_proxy_stdio_catalog_calls_and_denials(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        proxy = proxy_config(self.root, ['telegram-account'])['mcpServers']['squad-tools']
        params = StdioServerParameters(**proxy, cwd=str(PROJECT))
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                self.assertEqual(len(tools), 1)
                self.assertIn('get_messages', tools[0].description)
                self.assertTrue(tools[0].annotations.readOnlyHint)
                result = await session.call_tool(tools[0].name, {'chat_id': -123})
                self.assertFalse(result.isError)
                denied = await session.call_tool(tools[0].name, {'chat_id': -999})
                self.assertTrue(denied.isError)
                missing = await session.call_tool('send_message', {'chat_id': -123, 'message': 'no'})
                self.assertTrue(missing.isError)
        self.assertFalse((self.root / 'telegram-fixture-sent').exists())


if __name__ == '__main__':
    unittest.main()

"""Cross-project configuration persistence and root-selection regressions."""
import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from squad import cli, store, telegram
from squad.broker import Broker


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.old = self.base / 'old project'; self.old.mkdir()
        self.project = self.base / 'Unreal Projects' / 'SbetVerse'; self.project.mkdir(parents=True)
        for root in (self.old, self.project):
            store.init_workspace(root)
            broker = Broker(root); broker.company('team'); broker.agent('telegram.agent', 'team', 'codex', [])

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with patch('squad.cli.Path.cwd', return_value=self.old), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(args), expected, err.getvalue())
        return (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def configure_args(self):
        return ['--chat-id', '-123', '--user-id', '7', '--default-agent', 'telegram.agent', '--allow-tasks']

    def test_root_survives_every_parser_position_and_only_target_json_changes(self):
        unchanged = (self.old / '.squad/config.json').read_bytes()
        positions = [
            ['--root', str(self.project), 'telegram', 'configure'],
            ['telegram', '--root', str(self.project), 'configure'],
            ['telegram', 'configure', '--root', str(self.project)],
        ]
        config = store.load_config(self.project); config['custom_marker'] = 'keep me'; store.save_config(self.project, config)
        for prefix in positions:
            with self.subTest(prefix=prefix):
                output, _ = self.command(prefix + self.configure_args())
                self.assertEqual(output['config_file'], str(self.project / '.squad/config.json'))
                self.assertEqual(output['project_root'], str(self.project))
                self.assertEqual(shlex.split(output['next'])[-1], str(self.project))
                self.assertEqual((self.old / '.squad/config.json').read_bytes(), unchanged)
                saved = json.loads((self.project / '.squad/config.json').read_text())
                self.assertEqual(saved['telegram']['chat_id'], -123)
                self.assertEqual(saved['custom_marker'], 'keep me')
                self.assertNotIn('config_file', saved['telegram'])

    def test_nested_chat_command_retains_root_and_emits_matching_listen_command(self):
        self.command(['telegram', 'configure', '--root', str(self.project)] + self.configure_args())
        for prefix in [
            ['--root', str(self.project), 'telegram', 'chat', 'configure'],
            ['telegram', '--root', str(self.project), 'chat', 'configure'],
            ['telegram', 'chat', '--root', str(self.project), 'configure'],
            ['telegram', 'chat', 'configure', '--root', str(self.project)],
        ]:
            with self.subTest(prefix=prefix):
                output, _ = self.command(prefix + ['--agent', 'codex=telegram.agent'])
                command = shlex.split(output['next'])
                self.assertEqual(command[command.index('--root') + 1], str(self.project))
                self.assertIn('--with-workers', command)
                self.assertTrue(telegram.settings(self.project)['allow_chat'])
                self.assertNotIn('telegram', store.load_config(self.old))

    def test_unconfigured_status_is_one_json_document_and_does_not_create_workspace(self):
        empty = self.base / 'empty'; empty.mkdir()
        output, _ = self.command(['telegram', 'status', '--root', str(empty)])
        self.assertFalse(output['configured'])
        self.assertEqual(output['config_file'], str(empty / '.squad/config.json'))
        self.assertIn('--root', output['error'])
        self.assertFalse((empty / '.squad').exists())

    def test_status_includes_delivery_state_in_same_json_without_leaking_token(self):
        self.command(['telegram', 'configure', '--root', str(self.project)] + self.configure_args())
        bridge = telegram.Bridge(self.project, telegram.settings(self.project), type('API', (), {'bot_id': '123'})())
        bridge.queue('fixture', 'Pending message')
        with patch.dict(os.environ, {'SQUAD_TELEGRAM_BOT_TOKEN': '123:PRIVATE-TOKEN'}):
            output, _ = self.command(['telegram', 'status', '--root', str(self.project)])
        self.assertTrue(output['configured']); self.assertTrue(output['token_set'])
        self.assertEqual(output['deliveries'][0]['delivery_id'], 'fixture:0')
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(output))
        self.assertNotIn('PRIVATE-TOKEN', (self.project / '.squad/config.json').read_text())

    def test_missing_configuration_reports_selected_path_before_any_network_call(self):
        self.command(['telegram', 'configure'] + self.configure_args())
        with patch('squad.telegram.client') as client:
            _, error = self.command(['telegram', 'listen', '--root', str(self.project)], expected=2)
        client.assert_not_called()
        self.assertIn(str(self.project / '.squad/config.json'), error)
        self.assertIn(shlex.quote(str(self.project)), error)
        self.assertNotIn(str(self.old), error)

    def test_explicit_child_root_does_not_inherit_parent_configuration(self):
        self.command(['telegram', 'configure'] + self.configure_args())
        child = self.old / 'different project'; child.mkdir()
        output, _ = self.command(['telegram', 'status', '--root', str(child)])
        self.assertEqual(output['project_root'], str(child))
        self.assertFalse(output['configured'])
        self.assertFalse((child / '.squad').exists())

    def test_implicit_root_still_discovers_enclosing_workspace_and_typo_is_rejected(self):
        child = self.old / 'src'; child.mkdir()
        with patch('squad.cli.Path.cwd', return_value=child), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['telegram', 'status']), 0)
        self.assertEqual(json.loads(out.getvalue())['project_root'], str(self.old))
        typo = self.project / 'does-not-exist'
        _, error = self.command(['telegram', 'configure', '--root', str(typo)] + self.configure_args(), expected=2)
        self.assertIn('does not exist', error); self.assertFalse(typo.exists())

    def test_missing_agent_error_names_selected_workspace(self):
        root = self.base / 'no agents'; root.mkdir()
        _, error = self.command(['telegram', 'configure', '--root', str(root)] + self.configure_args(), expected=2)
        self.assertIn('squad roster --root', error)
        self.assertIn(str(root), error)
        self.assertNotIn('telegram', store.load_config(root))


if __name__ == '__main__':
    unittest.main()

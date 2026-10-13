"""Regression coverage for Codex exec option placement and permissions."""
import unittest
from pathlib import Path
from unittest.mock import patch

from squad.engines import BUILTINS, apply_session_flags, build_command


class CodexAdapterTests(unittest.TestCase):
    def command(self, prompt='Inspect this project', write=False):
        with patch('squad.engines.which_binary', return_value='/usr/bin/codex'):
            return build_command(BUILTINS['codex'], prompt,
                                 cwd=Path('/tmp/project with spaces'), write=write)

    def test_noninteractive_permissions(self):
        for write, sandbox in ((False, 'read-only'), (True, 'workspace-write')):
            with self.subTest(write=write):
                cmd = self.command(write=write)
                self.assertEqual(cmd[cmd.index('-c') + 1], 'approval_policy="never"')
                self.assertEqual(cmd[cmd.index('--sandbox') + 1], sandbox)
                self.assertEqual(cmd[cmd.index('-C') + 1], '/tmp/project with spaces')
                self.assertNotIn('--ask-for-approval', cmd)
                self.assertNotIn('--full-auto', cmd)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)

    def test_resume_keeps_exec_options_before_subcommand(self):
        for prompt in ('Inspect this project', 'exec', 'resume'):
            with self.subTest(prompt=prompt):
                original = self.command(prompt)
                cmd = apply_session_flags(BUILTINS['codex'], original, prompt,
                                          resume='session-123', cont=False)
                self.assertEqual(cmd[:-3], original[:-1])
                self.assertEqual(cmd[-3:], ['resume', 'session-123', prompt])

    def test_continue_selects_last_in_current_workspace(self):
        prompt = 'Continue the task'
        original = self.command(prompt, write=True)
        cmd = apply_session_flags(BUILTINS['codex'], original, prompt,
                                  resume=None, cont=True)
        self.assertEqual(cmd[:-3], original[:-1])
        self.assertEqual(cmd[-3:], ['resume', '--last', prompt])

    def test_new_session_unchanged(self):
        cmd = self.command()
        self.assertEqual(apply_session_flags(BUILTINS['codex'], cmd, cmd[-1],
                                            resume=None, cont=False), cmd)


if __name__ == '__main__':
    unittest.main()

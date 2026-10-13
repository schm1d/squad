from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from squad import cli, context, engines, playbooks, store
from squad.broker import Broker
from squad.locking import workspace_lock
from squad.worker import work_once


def claim_process(root, agent, queue):
    queue.put(Broker(Path(root)).claim(agent))


def append_process(root, number):
    for i in range(10):
        store.post_mail(Path(root), to='agent', sender='human', text=f'{number}:{i}')


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.b = Broker(self.root)
        self.b.company('local-lab', 'build and review')
        self.b.agent('local.builder', 'local-lab', 'ollama', ['code'])
        self.b.agent('local.reviewer', 'local-lab', 'ollama', ['review'])

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, **kw):
        return self.b.create_task('Review code with evidence', **kw)

    def submit(self, claim, ok=True, tokens=100):
        return self.b.complete(claim['id'], claim['assignee'], claim['claim_token'], 'Evidence', tokens=tokens, ok=ok)

    def test_concurrent_process_claim_is_unique(self):
        t = self.task(capabilities=['code'])
        ctx = multiprocessing.get_context('spawn')
        q = ctx.Queue()
        procs = [ctx.Process(target=claim_process, args=(str(self.root), 'local.builder', q)) for _ in range(4)]
        for p in procs: p.start()
        rows = [q.get(timeout=20) for p in procs]
        for p in procs:
            p.join(20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sum(x is not None for x in rows), 1)
        self.assertEqual(self.b.budgets()[0]['reserved'], t['budget'])

    def test_routing_capabilities_and_company(self):
        self.task(capabilities=['code'])
        self.assertIsNone(self.b.claim('local.reviewer'))
        self.assertIsNotNone(self.b.claim('local.builder'))

    def test_write_requires_worker_permission(self):
        self.task(write=True)
        self.assertIsNone(self.b.claim('local.builder'))
        self.assertIsNotNone(self.b.claim('local.builder', allow_write=True))

    def test_stale_token_and_double_completion(self):
        self.task()
        c = self.b.claim('local.builder')
        with self.assertRaises(ValueError):
            self.b.complete(c['id'], 'local.builder', 'wrong', 'Bad')
        self.submit(c)
        with self.assertRaises(ValueError): self.submit(c)
        self.assertNotIn('claim_token', self.b.task(c['id']))

    def test_review_dependencies_and_no_self_accept(self):
        parent = self.task(assignee='local.builder')
        a = self.task(parent=parent['id'], requester='local.builder', assignee='local.reviewer')
        b = self.task(parent=parent['id'], requester='local.builder', assignee='local.reviewer', dependencies=[a['id']])
        c = self.b.claim('local.reviewer')
        self.assertEqual(c['id'], a['id'])
        self.submit(c)
        self.assertIsNone(self.b.claim('local.reviewer'))
        with self.assertRaises(ValueError): self.b.review(a['id'], 'local.reviewer', True)
        self.b.review(a['id'], 'local.builder', True)
        self.assertEqual(self.b.claim('local.reviewer')['id'], b['id'])

    def test_mission_budget_is_atomic_across_agents(self):
        parent = self.task(assignee='local.builder', budget=6000, mission_budget=6000)
        self.task(parent=parent['id'], assignee='local.reviewer', budget=6000)
        self.assertIsNotNone(self.b.claim('local.builder'))
        self.assertIsNone(self.b.claim('local.reviewer'))

    def test_expired_claim_is_not_replayed(self):
        t = self.task()
        c = self.b.claim('local.builder')
        with self.b.db() as db:
            db.execute('UPDATE tasks SET lease_until=0 WHERE id=?', (t['id'],))
        self.assertIsNone(self.b.claim('local.builder'))
        self.assertEqual(self.b.task(t['id'])['state'], 'failed')
        self.assertEqual(self.b.budgets()[0]['used'], t['budget'])
        with self.assertRaises(ValueError): self.b.heartbeat(t['id'], 'local.builder', c['claim_token'])

    def test_delegation_limits_permissions_and_new_missions(self):
        parent = self.task(assignee='local.builder')
        with self.assertRaises(ValueError): self.task(requester='local.builder')
        with self.assertRaises(ValueError): self.task(parent=parent['id'], requester='local.builder', write=True)
        with self.assertRaises(ValueError): self.task(parent=parent['id'], requester='local.reviewer')
        with self.assertRaises(ValueError): self.task(parent=parent['id'], dependencies=[parent['id']])
        for i in range(8): self.task(parent=parent['id'])
        with self.assertRaises(ValueError): self.task(parent=parent['id'])

    def test_depth_bound(self):
        p = self.task()
        for i in range(4): p = self.task(parent=p['id'])
        with self.assertRaises(ValueError): self.task(parent=p['id'])

    def test_retry_limit(self):
        t = self.task()
        c = self.b.claim('local.builder'); self.submit(c, ok=False)
        self.b.review(t['id'], 'human', False)
        c = self.b.claim('local.builder'); self.submit(c, ok=False)
        with self.assertRaises(ValueError): self.b.review(t['id'], 'human', False)

    def test_accept_parent_requires_children(self):
        p = self.task(assignee='local.builder')
        self.task(parent=p['id'], assignee='local.reviewer')
        self.submit(self.b.claim('local.builder'))
        with self.assertRaises(ValueError): self.b.review(p['id'], 'human', True)

    def test_delta_event_cursor(self):
        t = self.task()
        page = self.b.events(limit=1)
        self.assertEqual(page['events'][0]['entity'], t['id'])
        self.assertEqual(self.b.events(page['next_cursor'])['events'], [])

    def test_artifact_escape_and_mail_traversal(self):
        self.task(); c = self.b.claim('local.builder')
        outside = self.root / 'secret.txt'; outside.write_text('private')
        link = self.root / '.squad/runs/link'; link.symlink_to(outside)
        with self.assertRaises(ValueError): self.b.complete(c['id'], 'local.builder', c['claim_token'], 'x', '.squad/runs/link')
        with self.assertRaises(ValueError): store.post_mail(self.root, to='../../escape', sender='a', text='bad')
        with self.assertRaises(ValueError): engines.registry({'engines': {'../../bad': {}}})

    def test_multiprocess_board_mail_no_lost_updates(self):
        ctx = multiprocessing.get_context('spawn')
        ps = [ctx.Process(target=append_process, args=(str(self.root), i)) for i in range(3)]
        for p in ps: p.start()
        for p in ps:
            p.join(20)
            self.assertEqual(p.exitcode, 0)
        mail = store.read_mail(self.root, 'agent')
        self.assertEqual(len(mail), 30)
        self.assertEqual(len({r['id'] for r in mail}), 30)
        self.assertEqual(store.read_board(self.root, 100000).count('say → agent'), 30)
        store.ack_mail(self.root, 'agent', [mail[0]['id']])
        self.assertEqual(len(store.read_mail(self.root, 'agent')), 29)

    def test_run_ids_and_artifact_names_unique(self):
        a = store.new_run(self.root, 'same', 'relay')
        b = store.new_run(self.root, 'same', 'relay')
        self.assertNotEqual(a, b)
        x = store.write_result(a, 'ollama', 'commander', {})
        y = store.write_result(a, 'ollama', 'commander', {})
        self.assertNotEqual(x, y)

    def test_corrupt_state_not_reset(self):
        (self.root / '.squad/state.json').write_text('{')
        with self.assertRaises(ValueError): store.load_state(self.root)

    def test_custom_command_executes_and_unavailable_fails(self):
        cfg = {'engines': {'test': {'transport':'custom', 'argv':[sys.executable, '-c', 'print("real result")'], 'allow_advisory_read_only': True}}}
        result = engines.run_engine('test', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stdout'], 'real result')
        cfg['engines']['test']['argv'][0] = 'squad_missing_binary_12345'
        result = engines.run_engine('test', 'hi', cwd=self.root, write=False, timeout=5, dry_run=False, config=cfg)
        self.assertFalse(result['ok']); self.assertFalse(result['dry_run'])

    def test_writer_mutex_and_board_lock_block_dispatch(self):
        cfg = {'engines': {'test': {'transport':'custom', 'argv':[sys.executable, '-c', 'print("x")']}}}
        entered = threading.Event(); release = threading.Event()
        def hold():
            with workspace_lock(self.root, 'writer'):
                entered.set(); release.wait(5)
        t = threading.Thread(target=hold); t.start(); entered.wait(5)
        try:
            r = engines.run_engine('test', 'x', cwd=self.root, write=True, timeout=1, dry_run=False, config=cfg)
            self.assertFalse(r['ok'])
        finally: release.set(); t.join()
        state = store.load_state(self.root); state['locks']['tree'] = {'holder':'someone'}; store.save_state(self.root, state)
        r = engines.run_engine('test', 'x', cwd=self.root, write=True, timeout=1, dry_run=False, config=cfg)
        self.assertFalse(r['ok'])

    def test_parallel_council_and_unique_relay_artifacts(self):
        barrier = threading.Barrier(3)
        def fake(engine, prompt, **kw):
            if 'Independent' in prompt: barrier.wait(timeout=5)
            return {'engine':engine,'ok':True,'dry_run':False,'stdout':'summary','stderr':'','returncode':0}
        with patch.object(engines, 'run_engine', side_effect=fake):
            r = playbooks.run_playbook(self.root, 'audit', 'council', write=False, dry_run=False)
        self.assertTrue(r['ok']); self.assertEqual(len(r['steps']), 4)
        with patch.object(engines, 'run_engine', return_value={'engine':'ollama','ok':True,'dry_run':False,'stdout':'summary'}):
            r = playbooks.run_playbook(self.root, 'audit', 'relay', write=False, dry_run=False, only='ollama')
        self.assertEqual(len({x['artifact'] for x in r['steps']}), 5)

    def test_failed_relay_halts_before_builder(self):
        with patch.object(engines, 'run_engine', return_value={'engine':'ollama','ok':False,'dry_run':False,'stdout':'','stderr':'failed'}) as mock:
            r = playbooks.run_playbook(self.root, 'audit', 'relay', write=True, dry_run=False, only='ollama')
        self.assertFalse(r['ok']); self.assertEqual(mock.call_count, 1)

    def test_run_budget_prevents_dispatch(self):
        cfg = store.load_config(self.root); cfg['run_token_budget'] = 100; store.save_config(self.root, cfg)
        with patch.object(engines, 'run_engine') as mock:
            r = playbooks.run_playbook(self.root, 'audit', 'solo', write=False, dry_run=False)
        self.assertFalse(r['ok']); mock.assert_not_called()

    def test_worker_reported_and_unknown_accounting(self):
        self.task(assignee='local.builder')
        fake = {'engine':'ollama','ok':True,'dry_run':False,'stdout':'{"summary":"Done"}', 'usage':{'total_tokens':100, 'kind':'reported'}}
        with patch.object(engines, 'run_engine', return_value=fake):
            r = work_once(self.root, 'local.builder')
        self.assertEqual(r['state'], 'review')
        self.assertEqual(self.b.budgets()[0]['used'], 100)
        self.assertTrue((self.root / r['result']['artifact']).exists())

    def test_cli_federation_smoke(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for args in [('roster',), ('tasks',), ('budgets',), ('events',)]:
                self.assertEqual(cli.main([*args, '--root', str(self.root)]), 0)

    def test_worker_watch_respects_task_limit(self):
        self.task(assignee='local.builder')
        fake = {'engine':'ollama','ok':True,'dry_run':False,'stdout':'done'}
        with patch.object(engines, 'run_engine', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['worker', '--root', str(self.root), '--agent', 'local.builder', '--watch', '--max-tasks', '1']), 0)
        self.assertEqual(self.b.tasks()[0]['state'], 'review')

    def test_context_bounds_unicode_and_required_goal(self):
        text = 'security🔒漢字' * 2000
        result = context.packet('KEEP THIS GOAL', [('evidence', text), ('board', text)], 300)
        self.assertLessEqual(context.estimate_tokens(result), 300)
        self.assertIn('KEEP THIS GOAL', result)
        self.assertIn('omitted', result)
        with self.assertRaises(ValueError): context.packet(text, [], 50)


if __name__ == '__main__': unittest.main()

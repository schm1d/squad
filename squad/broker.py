"""Durable local federation: companies, agents, bounded missions and leased tasks.

All clients are trusted processes of the same OS user; IDs are routing identities,
not a network authentication mechanism. SQLite transactions arbitrate claims.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', value):
        raise ValueError('ID must be 1–100 letters, digits, dots, colons, underscores or hyphens')
    return value


def bounded_int(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in [{low}, {high}]')
    return value


class Broker:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / '.squad' / 'coordination.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY, goal TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, company TEXT NOT NULL REFERENCES companies(id),
                    engine TEXT NOT NULL, capabilities TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS missions(id TEXT PRIMARY KEY, goal TEXT NOT NULL,
                    budget INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, mission TEXT NOT NULL REFERENCES missions(id),
                    parent TEXT REFERENCES tasks(id), depth INTEGER NOT NULL, goal TEXT NOT NULL,
                    requester TEXT NOT NULL, assignee TEXT REFERENCES agents(id), capabilities TEXT NOT NULL,
                    dependencies TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    write_allowed INTEGER NOT NULL DEFAULT 0, budget INTEGER NOT NULL, output_limit INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
                    lease_until REAL, claim_token TEXT, result TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    entity TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(state, created);
            ''')
            # Additive migration for existing workspaces. Chat turns share leases
            # and budgets, but are not engineering handoffs awaiting acceptance.
            db.execute('BEGIN IMMEDIATE')
            if 'kind' not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                db.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, kind, entity, data=None):
        db.execute('INSERT INTO events(kind,entity,data,created) VALUES(?,?,?,?)',
                   (kind, entity, json.dumps(data or {}), time.time()))

    def company(self, name: str, goal: str = ''):
        identifier(name)
        if len(goal) > 10000:
            raise ValueError('Company goal exceeds 10000 characters')
        with self.db() as db:
            db.execute('INSERT INTO companies VALUES(?,?) ON CONFLICT(id) DO UPDATE SET goal=excluded.goal', (name, goal))
        return {'id': name, 'goal': goal}

    def agent(self, name: str, company: str, engine: str, capabilities: list[str]):
        identifier(name)
        identifier(company)
        identifier(engine)
        capabilities = sorted({identifier(c) for c in capabilities})
        with self.db() as db:
            if not db.execute('SELECT 1 FROM companies WHERE id=?', (company,)).fetchone():
                raise ValueError('Register the company first')
            if db.execute("SELECT 1 FROM tasks WHERE assignee=? AND state='running'", (name,)).fetchone():
                raise ValueError('Cannot change an agent with a running task')
            db.execute('INSERT INTO agents VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET company=excluded.company, engine=excluded.engine, capabilities=excluded.capabilities',
                       (name, company, engine, json.dumps(capabilities)))
        return {'id': name, 'company': company, 'engine': engine, 'capabilities': capabilities}

    def roster(self):
        with self.db() as db:
            return {'companies': [dict(r) for r in db.execute('SELECT * FROM companies ORDER BY id')],
                    'agents': [{**dict(r), 'capabilities': json.loads(r['capabilities'])} for r in db.execute('SELECT * FROM agents ORDER BY id')]}

    def create_task(self, goal: str, requester: str = 'human', assignee: str | None = None,
                    capabilities: list[str] | None = None, dependencies: list[str] | None = None,
                    parent: str | None = None, budget: int = 12000, mission_budget: int = 60000,
                    output_limit: int = 2000, write: bool = False, *, kind: str = 'task', _db=None):
        identifier(requester)
        if not goal.strip() or len(goal) > 30000:
            raise ValueError('Task goal must contain 1–30000 characters')
        bounded_int(budget, 'budget', 256, 10000000)
        bounded_int(mission_budget, 'mission_budget', 256, 100000000)
        bounded_int(output_limit, 'output_limit', 64, budget - 1)
        caps = sorted({identifier(c) for c in capabilities or []})
        deps = sorted(set(dependencies or []))
        if len(deps) > 32:
            raise ValueError('At most 32 dependencies')
        if type(write) is not bool:
            raise ValueError('write must be boolean')
        if kind not in {'task', 'chat'}:
            raise ValueError('Unknown work kind')
        if kind == 'chat' and (write or parent or deps or requester != 'human' or not assignee):
            raise ValueError('Chat turns must be assigned, read-only human requests without dependencies')
        tid = uuid.uuid4().hex
        with (self.db() if _db is None else nullcontext(_db)) as db:
            if requester != 'human' and not db.execute('SELECT 1 FROM agents WHERE id=?', (requester,)).fetchone():
                raise ValueError('Unknown requester')
            if assignee and not db.execute('SELECT 1 FROM agents WHERE id=?', (assignee,)).fetchone():
                raise ValueError('Unknown assignee')
            depth = 0
            if parent:
                p = self._get(db, parent)
                if p['kind'] == 'chat':
                    raise ValueError('Chat turns cannot delegate background work')
                if requester != 'human' and requester != p['assignee']:
                    raise ValueError('Only the parent assignee or human may delegate children')
                if p['state'] not in ('queued', 'running', 'review'):
                    raise ValueError('Parent is no longer active')
                if write and not p['write_allowed']:
                    raise ValueError('Child cannot widen parent write permission')
                depth = p['depth'] + 1
                if depth > 4 or db.execute('SELECT COUNT(*) FROM tasks WHERE parent=?', (parent,)).fetchone()[0] >= 8:
                    raise ValueError('Delegation depth/child limit reached')
                mission = p['mission']
            else:
                # New top-level spending envelopes are human-controlled.
                if requester != 'human':
                    raise ValueError('Agents must delegate inside an existing parent mission')
                mission = uuid.uuid4().hex
                if budget > mission_budget:
                    raise ValueError('Task budget exceeds mission budget')
                db.execute('INSERT INTO missions(id,goal,budget) VALUES(?,?,?)', (mission, goal, mission_budget))
            if db.execute('SELECT COUNT(*) FROM tasks WHERE mission=?', (mission,)).fetchone()[0] >= 64:
                raise ValueError('Mission task limit reached')
            if budget > db.execute('SELECT budget FROM missions WHERE id=?', (mission,)).fetchone()[0]:
                raise ValueError('Task budget exceeds mission budget')
            for dep in deps:
                d = self._get(db, dep)
                if d['mission'] != mission:
                    raise ValueError('Dependencies must belong to the same mission')
                # Ancestor dependencies deadlock manual acceptance of parent work.
                ancestor = parent
                while ancestor:
                    if dep == ancestor:
                        raise ValueError('A child cannot depend on its ancestor')
                    ancestor = self._get(db, ancestor)['parent']
            db.execute('''INSERT INTO tasks(id,mission,parent,depth,goal,requester,assignee,capabilities,
                dependencies,write_allowed,budget,output_limit,created,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (tid, mission, parent, depth, goal, requester, assignee, json.dumps(caps), json.dumps(deps), int(write), budget, output_limit, time.time(), kind))
            self._event(db, 'task.created', tid, {'mission': mission})
            return self._public(self._get(db, tid))

    def _get(self, db, task_id):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown task')
        return row

    def _public(self, row):
        d = dict(row)
        d.pop('claim_token', None)
        for k in ('capabilities', 'dependencies', 'result'):
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def task(self, task_id: str):
        with self.db() as db:
            return self._public(self._get(db, task_id))

    def tasks(self, state: str | None = None, limit: int = 50):
        bounded_int(limit, 'limit', 1, 200)
        with self.db() as db:
            rows = db.execute('SELECT * FROM tasks WHERE (? IS NULL OR state=?) ORDER BY created DESC LIMIT ?', (state, state, limit))
            # Listing avoids resending full result bodies.
            return [{k: v for k, v in self._public(r).items() if k != 'result'} for r in rows]

    def _expire(self, db):
        for task in db.execute("SELECT * FROM tasks WHERE state='running' AND lease_until < ?", (time.time(),)).fetchall():
            # Unknown external side effects: do not automatically replay expired work.
            db.execute("UPDATE tasks SET state='failed', claim_token=NULL, lease_until=NULL, result=? WHERE id=?",
                       (json.dumps({'error': 'lease expired; inspect effects before retry', 'usage_unknown': True,
                                    'diagnostic': 'interrupted'}), task['id']))
            db.execute('UPDATE missions SET reserved=reserved-?,used=used+? WHERE id=?', (task['budget'], task['budget'], task['mission']))
            self._event(db, 'task.expired', task['id'])

    def claim(self, agent: str, lease_seconds: int = 960, allow_write: bool = False, kind: str | None = None):
        bounded_int(lease_seconds, 'lease_seconds', 10, 86400)
        with self.db() as db:
            a = db.execute('SELECT * FROM agents WHERE id=?', (agent,)).fetchone()
            if a is None:
                raise ValueError('Unknown agent')
            self._expire(db)
            if db.execute("SELECT 1 FROM tasks WHERE assignee=? AND state='running'", (agent,)).fetchone():
                return None
            for t in db.execute("SELECT * FROM tasks WHERE state='queued' AND (assignee IS NULL OR assignee=?) ORDER BY created", (agent,)).fetchall():
                if kind is not None and t['kind'] != kind:
                    continue
                if t['write_allowed'] and not allow_write:
                    continue
                if not set(json.loads(t['capabilities'])).issubset(json.loads(a['capabilities'])):
                    continue
                if any(self._get(db, dep)['state'] != 'done' for dep in json.loads(t['dependencies'])):
                    continue
                if t['attempts'] >= t['max_attempts']:
                    continue
                reserved = db.execute('UPDATE missions SET reserved=reserved+? WHERE id=? AND used+reserved+? <= budget',
                                      (t['budget'], t['mission'], t['budget'])).rowcount
                if not reserved:
                    continue
                token = secrets.token_hex(24)
                db.execute("UPDATE tasks SET state='running',assignee=?,claim_token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                           (agent, token, time.time() + lease_seconds, t['id']))
                self._event(db, 'task.claimed', t['id'], {'agent': agent})
                return {**self._public(self._get(db, t['id'])), 'claim_token': token, 'engine': a['engine']}
        return None

    def _owned(self, db, task_id, agent, token):
        t = self._get(db, task_id)
        if t['state'] != 'running' or t['assignee'] != agent or not secrets.compare_digest(t['claim_token'] or '', token):
            raise ValueError('Stale claim or wrong owner')
        if t['lease_until'] <= time.time():
            raise ValueError('Lease expired')
        return t

    def heartbeat(self, task_id: str, agent: str, token: str, lease_seconds: int = 960):
        bounded_int(lease_seconds, 'lease_seconds', 10, 86400)
        with self.db() as db:
            self._owned(db, task_id, agent, token)
            db.execute('UPDATE tasks SET lease_until=? WHERE id=?', (time.time() + lease_seconds, task_id))
        return {'ok': True}

    def complete(self, task_id: str, agent: str, token: str, summary: str, artifact: str = '',
                 tokens: int | None = None, measured: bool = False, ok: bool = True,
                 quality_report: dict | None = None, diagnostic: str | None = None):
        from . import quality, store
        if len(summary) > 12000:
            raise ValueError('Keep handoffs under 12000 characters; use an artifact for details')
        if artifact:
            path = (self.root / artifact).resolve()
            if not path.is_relative_to((self.root / '.squad' / 'runs').resolve()) or not path.is_file():
                raise ValueError('Artifact must be an existing file inside .squad/runs')
            artifact = str(path.relative_to(self.root))
        with self.db() as db:
            t = self._owned(db, task_id, agent, token)
            report = quality_report if quality_report is not None else quality.evaluate(
                summary, store.load_config(self.root), handoff=t['kind'] != 'chat')
            charged = t['budget'] if tokens is None else bounded_int(tokens, 'tokens', 0, 100000000)
            result = {'summary': summary, 'artifact': artifact, 'tokens': charged,
                      'usage_kind': 'reserved_unknown' if tokens is None else ('reported' if measured else 'estimated'),
                      'quality': report}
            if not ok:
                from .diagnostics import ERRORS
                result['diagnostic'] = diagnostic if isinstance(diagnostic, str) and diagnostic in ERRORS else 'unknown'
            if report.get('blocked'):
                ok = False
                result['error'] = 'Handoff quality gate failed; inspect quality.errors and resubmit through review/retry'
                if 'diagnostic' not in result:
                    result['diagnostic'] = 'quality_gate'
            if charged > t['budget']:
                ok = False
                result['error'] = 'Worker exceeded its token allowance'
                result['diagnostic'] = 'context_budget'
            db.execute('UPDATE missions SET reserved=reserved-?,used=used+? WHERE id=?', (t['budget'], charged, t['mission']))
            db.execute('UPDATE tasks SET state=?,result=?,claim_token=NULL,lease_until=NULL WHERE id=?',
                       (('done' if t['kind'] == 'chat' else 'review') if ok else 'failed', json.dumps(result), task_id))
            event = 'chat.replied' if t['kind'] == 'chat' else 'task.submitted'
            self._event(db, event if ok else 'task.failed', task_id)
        return self.task(task_id)

    def review(self, task_id: str, reviewer: str, accept: bool, note: str = ''):
        with self.db() as db:
            t = self._get(db, task_id)
            if reviewer != 'human' and (reviewer != t['requester'] or reviewer == t['assignee']):
                raise ValueError('Review belongs to the requester or human; no self-acceptance')
            if t['state'] not in ('review', 'failed') or (accept and t['state'] != 'review'):
                raise ValueError('Task is not reviewable in this state')
            if accept and db.execute("SELECT 1 FROM tasks WHERE parent=? AND state!='done'", (task_id,)).fetchone():
                raise ValueError('Accept all child tasks first')
            if not accept and t['attempts'] >= t['max_attempts']:
                raise ValueError('Retry limit reached; create a newly scoped task')
            state = 'done' if accept else 'queued'
            db.execute('UPDATE tasks SET state=? WHERE id=?', (state, task_id))
            self._event(db, 'task.accepted' if accept else 'task.requeued', task_id, {'reviewer': reviewer, 'note': note[:4000]})
        return self.task(task_id)

    def events(self, after: int = 0, limit: int = 50):
        bounded_int(after, 'after', 0, 2**63 - 1)
        bounded_int(limit, 'limit', 1, 200)
        with self.db() as db:
            rows = [{**dict(r), 'data': json.loads(r['data'])} for r in db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?', (after, limit))]
        return {'events': rows, 'next_cursor': rows[-1]['seq'] if rows else after}

    def budgets(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM missions ORDER BY rowid DESC LIMIT 100')]

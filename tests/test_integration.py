from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from squad import engines, http_agent, store
from squad.broker import Broker
from squad.worker import work_once

try:
    import mcp
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / 'tests/mcp_fixture.py'


class InferenceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((body, dict(self.headers)))
        reply = self.server.respond(body)
        payload = json.dumps(reply).encode()
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length', str(len(payload))); self.end_headers()
        self.wfile.write(payload)


def answer(text='answer', finish='stop', usage=True):
    value = {'choices':[{'message':{'role':'assistant','content':text},'finish_reason':finish}]}
    if usage: value['usage'] = {'prompt_tokens':100,'completion_tokens':10}
    return value


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        store.init_workspace(self.root)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), InferenceHandler)
        self.server.requests = []
        self.server.respond = lambda body: answer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.cfg = {'engines': {'local': {'transport':'openai','model':'fixture',
                    'base_url':f'http://127.0.0.1:{self.server.server_port}/v1','max_output_tokens':200}}}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def run_model(self, dry=False, write=False):
        return engines.run_engine('local', 'Compute 2 + 3 and cite the tool.', cwd=self.root, write=write,
                                   timeout=15, dry_run=dry, config=self.cfg)

    def test_actual_http_local_no_api_key_and_usage(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'do-not-leak-this-key'}, clear=False):
            r = self.run_model()
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['usage']['total_tokens'], 110)
        body, headers = self.server.requests[0]
        self.assertNotIn('Authorization', headers)
        self.assertEqual(body['max_tokens'], 200)

    def test_missing_provider_key_never_falls_back(self):
        spec = engines.spec_from_dict('remote', {'transport':'openai','base_url':'https://other.example/v1', 'api_key_env':'OTHER_KEY'})
        with patch.dict(os.environ, {'OPENAI_API_KEY':'must-not-use'}, clear=True):
            self.assertFalse(engines.available(spec)[0])

    def test_dry_run_and_budget_do_not_call_http(self):
        self.assertTrue(self.run_model(dry=True)['dry_run'])
        self.assertEqual(len(self.server.requests), 0)
        self.cfg['engines']['local']['token_budget'] = 20
        self.assertFalse(self.run_model()['ok'])
        self.assertEqual(len(self.server.requests), 0)

    def test_empty_and_truncated_responses_fail(self):
        for text, reason in [('', 'stop'), ('partial', 'length')]:
            self.server.respond = lambda body: answer(text, reason)
            self.assertFalse(self.run_model()['ok'])

    def test_missing_usage_is_marked_estimated(self):
        self.server.respond = lambda body: answer(usage=False)
        self.assertEqual(self.run_model()['usage']['kind'], 'estimated_or_mixed')

    def test_ollama_shape_and_counts(self):
        self.cfg['engines']['local']['transport'] = 'ollama'
        self.cfg['engines']['local']['base_url'] = f'http://127.0.0.1:{self.server.server_port}'
        self.server.respond = lambda body: {'message':{'role':'assistant','content':'ok'},'done':True,'done_reason':'stop','prompt_eval_count':12,'eval_count':4}
        r = self.run_model()
        self.assertTrue(r['ok'], r); self.assertEqual(r['usage']['total_tokens'], 16)
        self.assertEqual(self.server.requests[0][0]['options']['num_predict'], 200)

    def with_tools(self, write=False):
        self.cfg['engines']['local']['mcp_servers'] = ['fixture']
        self.cfg['mcp_servers'] = {'fixture': {'command':sys.executable,'args':[str(FIXTURE)],
            'allowed_tools':['add','mutate'], 'read_only_tools':['add']}}

    @unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
    def test_real_http_to_mcp_tool_roundtrip(self):
        self.with_tools()
        def respond(body):
            if body['messages'][-1]['role'] == 'tool':
                self.assertIn('5', body['messages'][-1]['content'])
                return answer('2 + 3 = 5')
            self.assertEqual(len(body['tools']), 1)
            name = body['tools'][0]['function']['name']
            return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
                {'id':'call-1','type':'function','function':{'name':name,'arguments':'{"a":2,"b":3}'}}]},'finish_reason':'tool_calls'}],
                'usage':{'prompt_tokens':120,'completion_tokens':30}}
        self.server.respond = respond
        r = self.run_model()
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['stdout'], '2 + 3 = 5'); self.assertEqual(r['tool_calls'], 1)
        self.assertEqual(r['usage']['total_tokens'], 260)
        self.assertTrue((self.root / r['tool_artifacts'][0]).exists())
        self.assertFalse((self.root / 'mutation-marker').exists())

    @unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
    def test_tool_schema_rejects_invalid_arguments(self):
        self.with_tools()
        def respond(body):
            name = body['tools'][0]['function']['name']
            return {'choices':[{'message':{'role':'assistant','content':'','tool_calls':[
                {'id':'a','type':'function','function':{'name':name,'arguments':'{"a":"wrong","b":3}'}}]}}]}
        self.server.respond = respond
        r = self.run_model()
        self.assertFalse(r['ok']); self.assertEqual(r['tool_calls'], 0)

    @unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
    def test_ollama_mcp_roundtrip_and_round_limit(self):
        self.with_tools()
        self.cfg['engines']['local']['transport'] = 'ollama'
        self.cfg['engines']['local']['base_url'] = f'http://127.0.0.1:{self.server.server_port}'
        def respond(body):
            if body['messages'][-1]['role'] == 'tool':
                self.assertIn('tool_name', body['messages'][-1])
                return {'message':{'content':'5'},'done':True,'prompt_eval_count':10,'eval_count':1}
            return {'message':{'content':'','tool_calls':[{'function':{'name':body['tools'][0]['function']['name'], 'arguments':{'a':2,'b':3}}}]},'done':True,'prompt_eval_count':10,'eval_count':5}
        self.server.respond = respond
        self.assertTrue(self.run_model()['ok'])
        self.cfg['engines']['local']['max_tool_rounds'] = 0
        self.assertFalse(self.run_model()['ok'])

    def test_worker_real_http_and_human_acceptance(self):
        store.save_config(self.root, self.cfg)
        broker = Broker(self.root); broker.company('lab'); broker.agent('a', 'lab', 'local', ['math'])
        task = broker.create_task('Compute 2+3', assignee='a')
        self.server.respond = lambda body: answer('{"summary":"5", "evidence":["2+3"]}')
        submitted = work_once(self.root, 'a')
        self.assertEqual(submitted['state'], 'review')
        self.assertEqual(broker.review(task['id'], 'human', True)['state'], 'done')
        self.assertEqual(broker.budgets()[0]['used'], 110)


@unittest.skipUnless(MCP_AVAILABLE, 'install .[mcp]')
class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_lifecycle_and_identity(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); b = Broker(root); b.company('lab'); b.agent('a','lab','ollama',['code'])
            task = b.create_task('Inspect code', assignee='a')
            params = StdioServerParameters(command=sys.executable, args=['-m','squad','mcp','--root',str(root),'--agent','a'], cwd=str(PROJECT))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertIn('squad_delegate', {t.name for t in tools.tools})
                    result = await session.call_tool('squad_claim', {})
                    self.assertFalse(result.isError)
                    claim = result.structuredContent
                    self.assertEqual(claim['id'], task['id'])
                    submitted = await session.call_tool('squad_submit', {'task_id':task['id'],'claim_token':claim['claim_token'],'summary':'Done','tokens':25})
                    self.assertFalse(submitted.isError)
                    denied = await session.call_tool('squad_review', {'task_id':task['id'],'accept':True})
                    self.assertTrue(denied.isError)
                    state = await session.call_tool('squad_task', {'task_id':task['id']})
                    self.assertNotIn('claim_token', state.structuredContent)
                    resources = await session.list_resources()
                    self.assertEqual(str(resources.resources[0].uri), 'squad://overview')
            self.assertEqual(b.task(task['id'])['state'], 'review')

    async def test_toolhub_write_policy_does_not_trust_annotation(self):
        from squad.mcp_bridge import ToolHub
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {'mcp_servers':{'fixture':{'command':sys.executable,'args':[str(FIXTURE)], 'allowed_tools':['add','mutate'], 'read_only_tools':['add']}}}
            async with ToolHub(root,cfg,['fixture'],False) as hub:
                self.assertEqual(len(hub.schemas), 1)
                with self.assertRaises(ValueError): await hub.call('mutate', {})
            async with ToolHub(root,cfg,['fixture'],True) as hub:
                name = next(k for k,v in hub.tools.items() if v[1].name == 'mutate')
                await hub.call(name,{})
            self.assertTrue((root / 'mutation-marker').exists())

if __name__ == '__main__': unittest.main()

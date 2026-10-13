"""Bounded HTTP inference and native tool-calling for OpenAI-compatible/Ollama APIs."""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .context import clip, estimate_tokens


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Inference redirects are disabled; configure the exact endpoint')


def request_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Inference response exceeds 8 MiB')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Inference endpoint returned an error or invalid response')
    if data.get('error'):
        raise ValueError('Inference endpoint returned an error: ' + json.dumps(data['error'], ensure_ascii=False)[:16000])
    return data


@asynccontextmanager
async def no_tools():
    yield None


async def run(spec, prompt: str, root: Path, config: dict, timeout: int, write: bool, dry_run: bool) -> dict:
    settings = config.get('engines', {}).get(spec.key, {})
    output_limit = int(settings.get('max_output_tokens', config.get('max_output_tokens', 2000)))
    context_limit = int(settings.get('context_tokens', config.get('context_tokens', 12000)))
    total_limit = int(settings.get('token_budget', config.get('token_budget', 30000)))
    rounds = int(settings.get('max_tool_rounds', 6))
    call_limit = int(settings.get('max_tool_calls', 16))
    if min(output_limit, context_limit, total_limit) <= 0 or not 0 <= rounds <= 32 or not 0 <= call_limit <= 64:
        raise ValueError('Invalid inference limits')
    model = spec.model or ('llama3.1' if spec.transport == 'ollama' else 'gpt-4.1-mini')
    base = spec.base_url or ('http://127.0.0.1:11434' if spec.transport == 'ollama' else os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'))
    url = base.rstrip('/') + ('/api/chat' if spec.transport == 'ollama' else '/chat/completions')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) endpoint without embedded credentials')
    headers = {'Content-Type': 'application/json'}
    if spec.transport == 'openai':
        # An explicitly named credential never falls back to another provider's key.
        env_name = spec.api_key_env or ('OPENAI_API_KEY' if parsed.hostname == 'api.openai.com' else 'SQUAD_API_KEY')
        api_key = os.environ.get(env_name, '')
        if api_key:
            if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise ValueError('Refusing to send API credentials over non-local plaintext HTTP')
            headers['Authorization'] = 'Bearer ' + api_key
    result = {'engine': spec.key, 'ok': False, 'dry_run': dry_run, 'command': ['POST', url],
              'stdout': '', 'stderr': '', 'returncode': 1,
              'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'kind': 'reported'},
              'tool_calls': 0, 'tool_artifacts': []}
    if dry_run:
        return {**result, 'ok': True, 'returncode': 0,
                'stdout': f'Would POST {url}; model={model}; max_output_tokens={output_limit}; MCP servers={settings.get("mcp_servers", [])}'}
    seen_calls = set()
    messages = [{'role': 'user', 'content': prompt}]
    deadline = time.monotonic() + timeout
    servers = settings.get('mcp_servers', [])
    if not isinstance(servers, list):
        raise ValueError('mcp_servers on an engine must be a list')
    if servers:
        from .mcp_bridge import ToolHub
        hub_context = ToolHub(root, config, servers, write, min(timeout, 60))
    else:
        hub_context = no_tools()
    hub_ready = not servers
    try:
        async with hub_context as hub:
            hub_ready = True
            schemas = hub.schemas if hub else []
            for turn in range(rounds + 1):
                payload = {'model': model, 'messages': messages, 'stream': False}
                if schemas:
                    payload['tools'] = schemas
                expected = estimate_tokens(json.dumps(payload, ensure_ascii=False)) + 32
                remaining = total_limit - result['usage']['total_tokens']
                available = min(output_limit, context_limit - expected, remaining - expected)
                if available < 64:
                    raise ValueError('Context or total token budget exhausted before next inference')
                if spec.transport == 'ollama':
                    payload['options'] = {'num_predict': available}
                else:
                    field = settings.get('output_token_field', 'max_tokens')
                    if field not in ('max_tokens', 'max_completion_tokens'):
                        raise ValueError('output_token_field must be max_tokens or max_completion_tokens')
                    payload[field] = available
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError('Inference deadline exceeded')
                data = await asyncio.to_thread(request_json, url, payload, headers, left)
                if spec.transport == 'ollama':
                    msg = data.get('message') or {}
                    inp, out = data.get('prompt_eval_count'), data.get('eval_count')
                    stopped = data.get('done_reason')
                else:
                    choices = data.get('choices') or []
                    if not choices:
                        raise ValueError('Response contains no choices')
                    msg = choices[0].get('message') or {}
                    usage = data.get('usage') or {}
                    inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
                    stopped = choices[0].get('finish_reason')
                if not isinstance(msg, dict):
                    raise ValueError('Invalid assistant message')
                if type(inp) is not int or type(out) is not int or min(inp, out) < 0:
                    inp, out = expected, estimate_tokens(json.dumps(msg))
                    result['usage']['kind'] = 'estimated_or_mixed'
                result['usage']['input_tokens'] += inp
                result['usage']['output_tokens'] += out
                result['usage']['total_tokens'] += inp + out
                if result['usage']['total_tokens'] > total_limit:
                    raise ValueError('Provider-reported usage exceeded token budget')
                calls = msg.get('tool_calls') or []
                if not calls:
                    text = msg.get('content') or ''
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError('Empty or unsupported assistant content')
                    result['stdout'] = text
                    if stopped in ('length', 'max_tokens', 'content_filter'):
                        raise ValueError(f'Incomplete response: finish_reason={stopped}')
                    return {**result, 'ok': True, 'returncode': 0}
                if not hub or turn >= rounds:
                    raise ValueError('Tool calls returned without tools enabled, or round limit reached')
                if len(calls) + result['tool_calls'] > call_limit:
                    raise ValueError('Tool call limit exceeded')
                if spec.transport == 'openai':
                    ids = [call.get('id') for call in calls]
                    if any(not isinstance(cid, str) or not cid or cid in seen_calls for cid in ids) or len(set(ids)) != len(ids):
                        raise ValueError('Missing or repeated tool call id')
                    seen_calls.update(ids)
                assistant = {'role': 'assistant', 'content': msg.get('content') or '', 'tool_calls': calls}
                if spec.transport == 'ollama' and msg.get('thinking'):
                    assistant['thinking'] = msg['thinking']
                messages.append(assistant)
                for index, call in enumerate(calls):
                    fn = call.get('function') or {}
                    args = fn.get('arguments') or {}
                    if isinstance(args, str):
                        args = json.loads(args)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError('Tool deadline exceeded')
                    # No automatic retry of tool calls: side effects may already have occurred.
                    tool_result = await asyncio.wait_for(hub.call(fn.get('name', ''), args), timeout=left)
                    result['tool_calls'] += 1
                    serialized = json.dumps(tool_result, ensure_ascii=False)
                    artifact_dir = root / '.squad' / 'runs' / 'tool-results'
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    artifact = artifact_dir / (uuid.uuid4().hex + '.json')
                    artifact.write_text(serialized, encoding='utf-8')
                    ref = str(artifact.relative_to(root))
                    result['tool_artifacts'].append(ref)
                    body = json.dumps({'untrusted_tool_result': clip(serialized, 800), 'artifact': ref})
                    if spec.transport == 'ollama':
                        messages.append({'role': 'tool', 'tool_name': fn['name'], 'content': body})
                    else:
                        if not call.get('id'):
                            raise ValueError('OpenAI-compatible tool call lacks id')
                        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': body})
            raise ValueError('No final answer within tool round limit')
    except Exception as exc:
        result['stderr'] = f'{type(exc).__name__}: {exc}'
        result['error_type'] = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            result['http_status'] = exc.code
            # Preserve bounded provider details locally (quota vs rate limits,
            # model vs route errors); public messages use only the diagnostic code.
            try:
                detail = exc.read(16000).decode('utf-8', errors='replace')
                if detail:
                    result['stderr'] += '\n' + detail
            except (OSError, AttributeError):
                pass
            finally:
                exc.close()
        if not hub_ready:
            result['diagnostic'] = 'mcp'
        return result

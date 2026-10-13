"""Provider-independent failure categories and safe, actionable public messages."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


# Only these fixed messages leave the machine. Never copy provider stderr,
# stdout, credentials, endpoint URLs or local paths into a failure notification.
ERRORS = {
    'workspace_trust': ('The engine rejected the project directory.', 'Check the project root and its Git/trust setup with squad doctor.'),
    'missing_executable': ('The engine command was not found.', 'Install its CLI and ensure it is on PATH for the user running Squad.'),
    'authentication': ('The engine could not authenticate.', 'Sign in locally to its CLI or check its configured API key environment variable.'),
    'permission': ('The engine was denied access.', 'Check account, file and sandbox permissions locally.'),
    'sandbox_startup': ('The engine could not start its sandbox.', 'Run squad doctor locally and inspect the task log for sandbox prerequisites. The request was not retried with weaker permissions.'),
    'cli_arguments': ('The installed CLI rejected Squad’s command arguments.', 'Check the CLI version and configured arguments against its local help.'),
    'connection': ('Squad could not reach the engine.', 'Check the configured endpoint, local model server and network connection.'),
    'tls': ('The secure connection could not be verified.', 'Check the endpoint, system clock and certificate configuration.'),
    'rate_limit': ('The engine is rate limited.', 'Wait for the provider limit to reset before submitting a new request.'),
    'quota': ('The engine reported an account or credit limit.', 'Check the provider account quota or billing locally.'),
    'model': ('The configured model is unavailable.', 'Check the model name, account access or local model installation.'),
    'endpoint': ('The API route or model was not found.', 'Check the base URL, API transport and configured model.'),
    'provider_error': ('The engine service returned an error.', 'Check provider availability and the local run log before retrying.'),
    'timeout': ('The engine exceeded its time limit.', 'Check whether work completed before retrying; adjust timeout_seconds if needed.'),
    'context_budget': ('The request exceeded a context, token or tool limit.', 'Reduce the request/context or adjust the relevant Squad limit.'),
    'mcp': ('An MCP connection or operation failed.', 'Check the selected MCP server, dependencies, credentials and tool permissions locally.'),
    'invalid_response': ('The engine returned an empty, incomplete or invalid answer.', 'Inspect the local run log and model/output settings.'),
    'read_only_policy': ('Squad could not establish read-only access for this CLI.', 'Inspect the local task log and engine/profile configuration with squad doctor; use an HTTP/MCP engine if native restrictions are unavailable.'),
    'workspace_locked': ('The workspace is locked by other work.', 'Coordinate with the lock owner before trying again.'),
    'configuration': ('The engine configuration is invalid.', 'Run squad doctor and inspect the engine configuration locally.'),
    'quality_gate': ('The result did not pass Squad’s handoff checks.', 'Inspect the task quality report before requesting rework.'),
    'interrupted': ('The worker was interrupted or its lease expired.', 'Inspect the local task state and any completed work before retrying.'),
    'unknown': ('The engine failed without a recognized diagnosis.', 'Inspect the local task/run log for the exact error.'),
}


def classify(result: dict, *, prompt: str | None = None) -> str | None:
    """Best-effort diagnosis only after failure; successful prose is never scanned."""
    if result.get('ok'):
        return None
    code = result.get('diagnostic')
    if isinstance(code, str) and code in ERRORS:
        return code
    # Bound scanning; keep both ends because CLIs often print the error last.
    raw = '\n'.join(str(result.get(field) or '') for field in ('stderr', 'error', 'stdout'))
    # Native CLIs can echo the entire request to stderr. Diagnose a copy with
    # that exact request removed, retaining the untouched output in local logs.
    raw = raw.replace('\r\n', '\n')
    if prompt:
        raw = raw.replace(prompt.replace('\r\n', '\n'), '')
    text = (raw[:16000] + raw[-16000:]).lower()
    kind = str(result.get('error_type', '')).lower()
    status = result.get('http_status')
    has = lambda *patterns: any(p in text for p in patterns)
    if has('not inside a trusted directory', 'untrusted directory', 'workspace is not trusted', 'trust this folder', 'dubious ownership'):
        return 'workspace_trust'
    if has('read-only enforcement is unverified'):
        return 'read_only_policy'
    # These are launch failures, not a denied tool operation or expired login.
    # Check before the broad permission match (EACCES/EPERM is often nested).
    if has('sandbox profile resolve failed', 'socket deny resolution failed',
           'could not resolve runtime-socket deny path', 'required bwrap plan could not be prepared',
           'refusing to start with denied paths unprotected',
           'bwrap: creating new namespace failed', 'bwrap: no permissions to create new namespace',
           'failed to apply landlock', 'sandbox-exec: sandbox_apply:'):
        return 'sandbox_startup'
    if has('workspace board locks', 'workspace lock', 'writer lock'):
        return 'workspace_locked'
    # Bare substring "quota" also matches "quotations" in the quality policy.
    # Require an explicit failure phrase or provider code instead of topic words.
    if has('insufficient_quota', 'quota_exceeded', 'insufficient credits', 'credit balance', 'billing limit') or re.search(
            r'\bquota\b[^\n.!?]{0,60}\b(exceeded|exhausted|depleted|reached)\b|'
            r'\b(exceeded|exhausted|depleted|insufficient)\b[^\n.!?]{0,60}\bquota\b', text):
        return 'quota'
    if status == 401 or has('unauthorized', 'authentication', 'not logged in', 'not authenticated', 'login required', 'please log in', 'please login', 'please sign in', 'invalid api key', 'incorrect api key', 'invalid_api_key', 'api key not found', 'api key is missing', 'missing api key', 'token expired', 'token has expired', 'could not load default credentials'):
        return 'authentication'
    if status == 429 or has('rate limit', 'rate_limit', 'too many requests'):
        return 'rate_limit'
    if has('model_not_found', 'unknown model', 'model not found') or re.search(r'model.{0,120}(not found|does not exist|not available)', text):
        return 'model'
    if status == 403 or kind == 'permissionerror' or has('permission denied', 'access denied', 'forbidden', 'operation not permitted'):
        return 'permission'
    if status == 404:
        return 'endpoint'
    if isinstance(status, int) and status >= 500:
        return 'provider_error'
    if has('unknown option', 'unrecognized argument', 'unexpected argument', 'unknown flag', 'no such option', 'invalid option'):
        return 'cli_arguments'
    if has('certificate verify failed', 'certificate_verify_failed', 'sslerror', 'tls handshake'):
        return 'tls'
    if kind in ('timeouterror', 'timeoutexpired') or result.get('returncode') == 124 or has('timed out', 'timeout', 'deadline exceeded'):
        return 'timeout'
    if has('connection refused', 'connection reset', 'name or service not known', 'temporary failure in name resolution', 'getaddrinfo', 'network is unreachable', 'urlerror', 'connectionerror', 'failed to connect', 'fetch failed',
           'connection failed', 'error sending request', 'workspace routing discovery failed', 'transport channel closed'):
        return 'connection'
    if has('mcp', 'toolhub'):
        return 'mcp'
    if kind == 'filenotfounderror' or has('not on path', 'command not found'):
        return 'missing_executable'
    if has('context', 'token budget', 'token allowance', 'tool call limit', 'tool round limit', 'round limit reached'):
        return 'context_budget'
    if has('empty', 'no choices', 'invalid response', 'invalid assistant', 'unsupported assistant', 'incomplete response', 'jsondecodeerror'):
        return 'invalid_response'
    if has('quality gate'):
        return 'quality_gate'
    if has('interrupted', 'lease expired'):
        return 'interrupted'
    if kind in ('keyerror', 'valueerror', 'typeerror') or has('unknown engine', 'invalid', 'unsupported transport'):
        return 'configuration'
    return 'unknown'


def public_failure(code: str | None, task_id: str) -> str:
    code = code if isinstance(code, str) and code in ERRORS else 'unknown'
    message, action = ERRORS[code]
    # Task IDs come from the broker; constrain even this small dynamic field.
    ref = task_id if re.fullmatch(r'[a-f0-9]{32}', task_id) else ''
    return f'Error [{code}]: {message}\n{action}' + (
        f'\nLocal details: squad tasks --id {ref} --logs' if ref else '')


def task_log(root: Path, task: dict) -> dict:
    """Read one task's local engine JSON, constrained to the run directory."""
    result = task.get('result') or {}
    artifact = result.get('artifact')
    if not artifact:
        return {'detail': 'No engine log was produced; inspect the task result.', 'diagnostic': result.get('diagnostic')}
    path = (root / artifact).with_suffix('.json').resolve()
    if not path.is_relative_to((root / '.squad' / 'runs').resolve()):
        raise ValueError('Run log must be inside .squad/runs')
    if not path.is_file():
        return {'detail': 'Engine JSON log is unavailable; inspect the task artifact.'}
    data = json.loads(path.read_text())
    return {'path': str(path.relative_to(root.resolve())),
            **{name: data.get(name) for name in ('engine', 'returncode', 'diagnostic', 'error_type', 'http_status', 'stderr')},
            'stdout': data.get('stdout', '') if not data.get('stderr') else ''}


def setup_checks(root: Path, config: dict) -> list[dict]:
    """Offline checks for all built-in/custom transports. No inference or login."""
    from . import engines
    from .project_check import check_project
    rows = []
    for key, spec in engines.registry(config).items():
        issues = []
        try:
            ready, _ = engines.available(spec)
        except (ValueError, TypeError, OSError):
            rows.append({'engine': key, 'transport': spec.transport, 'issues': ['configuration']})
            continue
        settings = config.get('engines', {}).get(key, {})
        if spec.transport in ('cli', 'custom'):
            if spec.transport == 'custom' and not spec.argv:
                issues.append('configuration')
            elif not ready:
                issues.append('missing_executable')
            if ready and spec.transport == 'cli' and engines.session_status(spec).startswith('no session'):
                issues.append('session_unverified')
            if ready and not engines.read_only_supported(spec, settings):
                issues.append('read_only_policy')
            if ready and spec.family == 'grok':
                from .cli_checks import grok_sandbox_checks
                issues.extend(grok_sandbox_checks())
            if ready and key == 'agy' and spec.transport == 'cli' and spec.family == 'gemini':
                issues.append('agy_profile_unverified')
            if key == 'codex' and check_project(root, discover=False)['status'] != 'git-worktree':
                issues.append('project_unverified')
        elif spec.transport in ('openai', 'ollama'):
            base = spec.base_url or ('http://127.0.0.1:11434' if spec.transport == 'ollama' else os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'))
            try:
                url = urlsplit(base)
                local = url.hostname in ('localhost', '127.0.0.1', '::1')
                if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                    issues.append('configuration')
                if spec.transport == 'openai':
                    env = spec.api_key_env or ('OPENAI_API_KEY' if url.hostname == 'api.openai.com' else 'SQUAD_API_KEY')
                    if not ready:
                        issues.append('authentication')
                    if os.environ.get(env) and url.scheme == 'http' and not local:
                        issues.append('configuration')
            except ValueError:
                issues.append('configuration')
            issues.append('endpoint_unverified')
        else:
            issues.append('configuration')
        selected = settings.get('mcp_servers', [])
        if not isinstance(selected, list) or any(not isinstance(name, str) or name not in config.get('mcp_servers', {}) for name in selected):
            issues.append('mcp')
        rows.append({'engine': key, 'transport': spec.transport, 'issues': list(dict.fromkeys(issues))})
    return rows


def print_setup_checks(root: Path, config: dict | None = None):
    from .store import load_config
    print('Engine setup checks (offline; unused engines may be unavailable):')
    try:
        rows = setup_checks(root, config if config is not None else load_config(root))
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        print('  WARNING: Could not inspect project engine configuration; run squad doctor after correcting it.')
        return
    notes = {
        'missing_executable': 'CLI not found on PATH',
        'authentication': 'API credential environment variable is unset',
        'configuration': 'invalid local engine configuration',
        'mcp': 'selected MCP server is missing or selection is invalid',
        'session_unverified': 'no session directory found; sign in locally if needed',
        'project_unverified': 'project Git/trust prerequisite unverified',
        'endpoint_unverified': 'endpoint/model access needs a real request',
        'grok_bwrap_missing': 'Grok Linux deny-path sandbox needs bubblewrap (bwrap); install it with your OS package manager',
        'grok_socket_unverified': 'Grok cannot inspect a runtime socket path; its sandbox may fail before model startup. Inspect the local task log, check grok update and retest; keep the socket permissions and sandbox enabled',
        'agy_profile_unverified': 'read-only chat uses a generated native agent (view_file, grep_search); worker checks required flags before launch. Installed CLI enforcement/login still needs local validation',
    }
    for row in rows:
        details = '; '.join(notes.get(code, ERRORS.get(code, ERRORS['unknown'])[0]).rstrip('.') for code in row['issues'])
        print(f'  {row["engine"]} ({row["transport"]}): {details or "local prerequisites found"}')
    print('  No login, network request or model call was made. Existing credentials may still be expired.')

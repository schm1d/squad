"""Engine adapters.

Built-in coding CLIs, plus OpenAI-compatible and Ollama transports so any
lab can sit on the same board: Qwen, Llama, DeepSeek, GLM, Mistral, Kimi, …
"""

from __future__ import annotations

import json
import asyncio
import signal
from contextlib import nullcontext
from urllib.parse import urlsplit

from .context import estimate_tokens
from .locking import workspace_lock
from .broker import identifier
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class EngineSpec:
    key: str
    display: str
    vendor: str
    transport: str  # cli | openai | ollama | custom
    binaries: tuple[str, ...] = ()
    default_role: str = "runner"
    strength: str = ""
    family: str = "other"
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    argv: tuple[str, ...] = ()  # custom transport: "{prompt}" "{cwd}" placeholders
    auth: str = "browser"  # browser | apikey | local
    session_dirs: tuple[str, ...] = ()


ROLES = ("commander", "scout", "builder", "runner")

DEFAULT_ROLE_MAP = {
    "commander": "claude",
    "scout": "grok",
    "builder": "codex",
    "runner": "agy",
}

# Where each CLI stashes a browser / subscription login. Presence is a hint, not proof.
SESSION_HOMES: dict[str, tuple[str, ...]] = {
    "grok": ("~/.grok",),
    "claude": ("~/.claude",),
    "codex": ("~/.codex",),
    "agy": ("~/.gemini", "~/.agy", "~/.config/antigravity"),
    "qwen": ("~/.qwen", "~/.qwen-code"),
    "kimi": ("~/.kimi",),
    "deepseek": ("~/.deepseek",),
    "glm": ("~/.zai", "~/.glm"),
    "mistral": ("~/.mistral",),
}

LOGIN_HINTS: dict[str, str] = {
    "grok": "Run `grok` once and finish the browser login. Session stays in ~/.grok. Headless box: `grok login --device-auth`.",
    "claude": "Run `claude` once and log in with your Anthropic / Claude.ai account. Squad does NOT pass --bare, so it reuses that subscription session. --bare would demand ANTHROPIC_API_KEY and ignore the browser login.",
    "codex": "Run `codex` once and log in with your ChatGPT / OpenAI account. `codex exec` reuses that session.",
    "agy": "Run `agy` once and sign in with Google. Cached credentials are reused. Same OS user + keyring (Linux needs a D-Bus session).",
    "qwen": "Run `qwen`, then `/auth` and pick Qwen OAuth.",
    "kimi": "Run `kimi` once and complete the platform login.",
    "deepseek": "Run `deepseek` once. Paste a key if it asks, or use the saved credential store.",
    "glm": "Run `zai` or `glm` once and sign in to Z.ai.",
    "mistral": "Run `mistral` once and log in, or set MISTRAL_API_KEY for the API preset.",
}

# First-party coding agents from the original four, plus the labs people asked for.
BUILTINS: dict[str, EngineSpec] = {
    "grok": EngineSpec(
        key="grok",
        display="Grok Build",
        vendor="xAI",
        transport="cli",
        binaries=("grok",),
        default_role="scout",
        family="grok",
        strength="live X/web search, fast parallel exploration, second opinions",
        auth="browser",
        session_dirs=("~/.grok",),
    ),
    "claude": EngineSpec(
        key="claude",
        display="Claude Code",
        vendor="Anthropic",
        transport="cli",
        binaries=("claude",),
        default_role="commander",
        family="claude",
        strength="architecture, hard refactors, synthesis, review judgment",
        auth="browser",
        session_dirs=("~/.claude",),
    ),
    "codex": EngineSpec(
        key="codex",
        display="Codex",
        vendor="OpenAI",
        transport="cli",
        binaries=("codex",),
        default_role="builder",
        family="openai",
        strength="focused implementation on a well-specified task",
        auth="browser",
        session_dirs=("~/.codex",),
    ),
    "agy": EngineSpec(
        key="agy",
        display="Antigravity (agy)",
        vendor="Google",
        transport="cli",
        binaries=("agy",),
        default_role="runner",
        family="gemini",
        strength="cheap/fast Gemini staff work: surveys, scoped edits, bulk review",
    ),
    "qwen": EngineSpec(
        key="qwen",
        display="Qwen Code",
        vendor="Alibaba / Qwen",
        transport="cli",
        binaries=("qwen",),
        default_role="builder",
        family="qwen",
        strength="open coding agent, strong multilingual + repo navigation",
    ),
    "kimi": EngineSpec(
        key="kimi",
        display="Kimi Code",
        vendor="Moonshot",
        transport="cli",
        binaries=("kimi",),
        default_role="scout",
        family="kimi",
        strength="long-context coding agent, web fetch, parallel subagents",
    ),
    "deepseek": EngineSpec(
        key="deepseek",
        display="DeepSeek",
        vendor="DeepSeek",
        transport="cli",
        binaries=("deepseek", "dsh", "deepcli"),
        default_role="builder",
        family="deepseek",
        strength="reasoned implementation, cheap high-volume coding",
    ),
    "glm": EngineSpec(
        key="glm",
        display="GLM",
        vendor="Zhipu / Z.ai",
        transport="cli",
        binaries=("zai", "glm-agent", "glm"),
        default_role="builder",
        family="glm",
        strength="open-weight coding model with Claude-compatible harnesses",
    ),
    "mistral": EngineSpec(
        key="mistral",
        display="Mistral",
        vendor="Mistral",
        transport="cli",
        binaries=("mistral", "mistral-cli"),
        default_role="runner",
        family="mistral",
        strength="fast European models, Codestral-class completion",
    ),
    "llama": EngineSpec(
        key="llama",
        display="Llama (Ollama)",
        vendor="Meta / local",
        transport="ollama",
        binaries=("ollama",),
        default_role="runner",
        family="llama",
        model="llama3.1",
        base_url="http://127.0.0.1:11434",
        strength="local Llama — no cloud, no vendor lock, you pick the weights",
    ),
    "ollama": EngineSpec(
        key="ollama",
        display="Ollama",
        vendor="local",
        transport="ollama",
        binaries=("ollama",),
        default_role="runner",
        family="local",
        model="llama3.1",
        base_url="http://127.0.0.1:11434",
        strength="any local GGUF/Ollama tag: Qwen, Llama, DeepSeek, Mistral, GLM, Kimi…",
    ),
    "openai": EngineSpec(
        key="openai",
        display="OpenAI-compatible",
        vendor="any",
        transport="openai",
        family="openai",
        model="gpt-4.1-mini",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        strength="generic /v1/chat/completions — point it at any lab",
    ),
}

# API / local presets you can `squad engines add <name>` without writing JSON by hand.
PRESETS: dict[str, dict[str, Any]] = {
    "qwen-api": {
        "display": "Qwen (DashScope compatible)",
        "vendor": "Alibaba / Qwen",
        "transport": "openai",
        "family": "qwen",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "strength": "hosted Qwen via OpenAI-compatible API",
        "default_role": "builder",
    },
    "kimi-api": {
        "display": "Kimi (Moonshot API)",
        "vendor": "Moonshot",
        "transport": "openai",
        "family": "kimi",
        "model": "kimi-k2",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "strength": "hosted Kimi via OpenAI-compatible API",
        "default_role": "scout",
    },
    "deepseek-api": {
        "display": "DeepSeek API",
        "vendor": "DeepSeek",
        "transport": "openai",
        "family": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "strength": "hosted DeepSeek via OpenAI-compatible API",
        "default_role": "builder",
    },
    "glm-api": {
        "display": "GLM (Z.ai API)",
        "vendor": "Zhipu / Z.ai",
        "transport": "openai",
        "family": "glm",
        "model": "glm-4.7",
        "base_url": "https://api.z.ai/api/paas/v4",
        "api_key_env": "ZAI_API_KEY",
        "strength": "hosted GLM via OpenAI-compatible coding/paas API",
        "default_role": "builder",
    },
    "mistral-api": {
        "display": "Mistral API",
        "vendor": "Mistral",
        "transport": "openai",
        "family": "mistral",
        "model": "mistral-large-latest",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "strength": "hosted Mistral / Codestral via OpenAI-compatible API",
        "default_role": "runner",
    },
    "llama-api": {
        "display": "Llama (OpenAI-compatible host)",
        "vendor": "Meta / host",
        "transport": "openai",
        "family": "llama",
        "model": "llama3.1",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": "LLAMA_API_KEY",
        "strength": "Llama behind any OpenAI-compatible server (vLLM, llama.cpp, Groq, Together…)",
        "default_role": "runner",
    },
}


# Back-compat alias used by older call sites.
ENGINES = BUILTINS


def spec_from_dict(key: str, data: dict[str, Any]) -> EngineSpec:
    identifier(key)
    return EngineSpec(
        key=key,
        display=str(data.get("display") or key),
        vendor=str(data.get("vendor") or "custom"),
        transport=str(data.get("transport") or "openai"),
        binaries=tuple(data.get("binaries") or ()),
        default_role=str(data.get("default_role") or "runner"),
        strength=str(data.get("strength") or ""),
        family=str(data.get("family") or "custom"),
        model=str(data.get("model") or ""),
        base_url=str(data.get("base_url") or ""),
        api_key_env=str(data.get("api_key_env") or ""),
        argv=tuple(data.get("argv") or ()),
        auth=str(data.get("auth") or ("apikey" if (data.get("transport") == "openai") else "browser")),
        session_dirs=tuple(data.get("session_dirs") or ()),
    )


def registry(config: dict[str, Any] | None = None) -> dict[str, EngineSpec]:
    """Built-ins plus anything declared under config['engines']."""
    out = dict(BUILTINS)
    extras = (config or {}).get("engines") or {}
    for key, data in extras.items():
        identifier(key)
        if not isinstance(data, dict):
            continue
        if key in out:
            # Overlay fields onto a builtin (e.g. change ollama model).
            base = out[key]
            out[key] = replace(
                base,
                display=str(data.get("display") or base.display),
                vendor=str(data.get("vendor") or base.vendor),
                transport=str(data.get("transport") or base.transport),
                binaries=tuple(data["binaries"]) if "binaries" in data else base.binaries,
                model=str(data.get("model") or base.model),
                base_url=str(data.get("base_url") or base.base_url),
                api_key_env=str(data.get("api_key_env") or base.api_key_env),
                argv=tuple(data["argv"]) if "argv" in data else base.argv,
                strength=str(data.get("strength") or base.strength),
                auth=str(data.get("auth") or base.auth),
            )
        else:
            out[key] = spec_from_dict(key, data)
    return out


def resolve(key: str, config: dict[str, Any] | None = None) -> EngineSpec:
    table = registry(config)
    if key not in table:
        known = ", ".join(sorted(table))
        raise KeyError(f"unknown engine {key!r}. Known: {known}")
    return table[key]


def which_binary(spec: EngineSpec) -> str | None:
    for name in spec.binaries:
        path = shutil.which(name)
        if path:
            return path
    return None


def which_engine(key: str, config: dict[str, Any] | None = None) -> str | None:
    spec = resolve(key, config)
    return which_binary(spec)


def session_status(spec: EngineSpec) -> str:
    """Hint whether a browser/subscription session looks present in $HOME."""
    homes = spec.session_dirs or SESSION_HOMES.get(spec.key) or SESSION_HOMES.get(spec.family) or ()
    found = []
    for raw in homes:
        path = Path(os.path.expanduser(raw))
        if path.exists():
            found.append(str(path))
    if found:
        return "session " + ", ".join(found)
    if spec.transport == "cli":
        return "no session dir yet — log in once in the vendor TUI"
    return ""


def available(spec: EngineSpec) -> tuple[bool, str]:
    """Return (ready?, how). Ready means we can actually dispatch."""
    if spec.transport == "cli":
        path = which_binary(spec)
        if not path:
            return False, "not on PATH"
        hint = session_status(spec)
        return True, f"{path}  ·  browser login  ·  {hint}"
    if spec.transport == "custom":
        if spec.argv:
            found = shutil.which(spec.argv[0])
            return bool(found), found or "custom command not found"
        path = which_binary(spec)
        return (bool(path), path or "no argv / binary")
    if spec.transport == "ollama":
        path = shutil.which("ollama")
        if path:
            return True, f"{path}  model={spec.model or 'llama3.1'}"
        # API-only still works if the daemon is up.
        return True, f"HTTP {spec.base_url or 'http://127.0.0.1:11434'}  model={spec.model or 'llama3.1'}"
    if spec.transport == "openai":
        where = spec.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        local = urlsplit(where).hostname in ("localhost", "127.0.0.1", "::1")
        env = spec.api_key_env or ("OPENAI_API_KEY" if urlsplit(where).hostname == "api.openai.com" else "SQUAD_API_KEY")
        has = bool(os.environ.get(env)) or local or spec.auth == "local"
        mark = "key set" if has else f"set {env}"
        return has, f"{where}  model={spec.model or '?'}  ({mark})"
    return False, f"unknown transport {spec.transport}"


def doctor(config: dict[str, Any] | None = None) -> list[dict]:
    rows = []
    for key, spec in registry(config).items():
        ready, detail = available(spec)
        rows.append(
            {
                "key": key,
                "display": spec.display,
                "vendor": spec.vendor,
                "family": spec.family,
                "role": spec.default_role,
                "transport": spec.transport,
                "ready": ready,
                "detail": detail,
                "strength": spec.strength,
                "custom": key not in BUILTINS,
                "auth": spec.auth if spec.transport != "openai" else "apikey",
                "session": session_status(spec) if spec.transport == "cli" else "",
            }
        )
    return rows


def apply_session_flags(spec: EngineSpec, cmd: list[str], prompt: str, *, resume: str | None, cont: bool) -> list[str]:
    """Attach vendor continue/resume flags so the next turn keeps conversation memory."""
    if not resume and not cont:
        return cmd
    name = Path(cmd[0]).name if cmd else spec.key
    if name == "codex" or spec.key == "codex":
        # build_command puts the prompt last. Keep exec options on their
        # parent command: resume does not accept options such as -C/--sandbox.
        return cmd[:-1] + ["resume", resume or "--last", prompt]

    if resume:
        if name == "agy" or spec.family == "gemini":
            flags = ["--conversation", resume]
        else:
            flags = ["--resume", resume]
    else:
        flags = ["-c"] if name == "kimi" else ["--continue"]
    return [cmd[0]] + flags + cmd[1:]


def build_command(
    spec: EngineSpec,
    prompt: str,
    *,
    cwd: Path,
    write: bool,
    extra_args: Sequence[str] | None = None,
    config: dict[str, Any] | None = None,
    resume: str | None = None,
    cont: bool = False,
) -> list[str]:
    extra = list(extra_args or [])
    key = spec.key if spec.key in BUILTINS else spec.family
    binary = (which_binary(spec) or (spec.binaries[0] if spec.binaries else spec.key))

    if spec.transport == "custom":
        mapped = [
            part.replace("{prompt}", prompt).replace("{cwd}", str(cwd)).replace("{model}", spec.model)
            for part in spec.argv
        ]
        if not mapped:
            raise KeyError(f"{spec.key} custom transport needs argv")
        return mapped + extra

    if spec.transport in {"openai", "ollama"}:
        # HTTP transports don't produce a shell command; caller uses run_http.
        return [spec.transport, spec.model or spec.key]

    # CLI transports
    if binary.endswith("grok") or spec.family == "grok":
        cmd = [
            binary, "--no-auto-update", "--no-alt-screen", "-p", prompt,
            "--cwd", str(cwd), "--output-format", "plain",
        ]
        cmd += ["--sandbox", "workspace", "--always-approve"] if write else ["--sandbox", "read-only"]
        return cmd + extra

    if binary.endswith("claude") or spec.family == "claude":
        # Do NOT pass --bare by default. --bare skips ~/.claude and the
        # browser/subscription login and demands ANTHROPIC_API_KEY.
        auth_cfg = ((config or {}).get("auth") or {})
        use_bare = bool(auth_cfg.get("claude_bare"))
        cmd = [binary]
        if use_bare:
            cmd.append("--bare")
        cmd += ["-p", prompt, "--output-format", "text"]
        if write:
            cmd += ["--allowedTools", "Read,Edit,Bash,Glob,Grep", "--permission-mode", "acceptEdits"]
        else:
            cmd += ["--allowedTools", "Read,Glob,Grep", "--max-turns", "12"]
        return cmd + extra

    if binary.endswith("codex") or spec.family == "openai" and spec.key == "codex":
        # exec accepts config overrides, not the interactive approval flag.
        # Do not combine never with --full-auto, which changes approval policy.
        cmd = [binary, "exec", "-C", str(cwd), "-c", 'approval_policy="never"',
               "--sandbox", "workspace-write" if write else "read-only"]
        selected = (config or {}).get('engines', {}).get(spec.key, {}).get('mcp_servers', [])
        if selected:
            from .mcp_bridge import proxy_config
            proxy = proxy_config(cwd, selected, write)['mcpServers']['squad-tools']
            # Pass only paths and server names on argv; credentials stay in env/.env.
            # JSON string/array literals are valid TOML for these ASCII config values.
            settings = {**proxy, 'enabled': True, 'required': True,
                        'startup_timeout_sec': 60, 'tool_timeout_sec': 60}
            settings['env_vars'] = sorted({name for server in selected
                for name in (config or {}).get('mcp_servers', {}).get(server, {}).get('env_allow', [])})
            for key, value in settings.items():
                cmd.extend(['-c', 'mcp_servers.squad_tools.' + key + '=' + json.dumps(value, ensure_ascii=False)])
        return cmd + extra + [prompt]

    if binary.endswith("agy") or spec.family == "gemini":
        cmd = [binary, "-p", prompt, "--output-format", "text"]
        cmd += ["--mode", "accept-edits"] if write else ["--mode", "plan"]
        if not write and spec.key == 'agy':
            from . import agy_profile
            agy_profile.validate_args(extra)
            cmd += ['--agent', agy_profile.name(cwd), '--disable-slash-commands']
        return cmd + extra

    if binary.endswith("qwen") or spec.family == "qwen":
        cmd = [binary, "-p", prompt, "--output-format", "text"]
        return cmd + extra

    if binary.endswith("kimi") or spec.family == "kimi":
        cmd = [binary, "-p", prompt]
        return cmd + extra

    if spec.family == "deepseek" or Path(binary).name in {"deepseek", "dsh", "deepcli"}:
        name = Path(binary).name
        if name in {"dsh"} or name.endswith("harness-cli"):
            cmd = [binary, "exec", prompt]
        else:
            cmd = [binary, "-p", prompt]
        return cmd + extra

    if spec.family == "glm" or Path(binary).name in {"zai", "glm-agent", "glm"}:
        name = Path(binary).name
        if name == "glm-agent":
            cmd = [binary, prompt]
            if not write:
                cmd = [binary, "--output-format", "text", prompt]
        else:
            cmd = [binary, "-p", prompt]
        return cmd + extra

    if spec.family == "mistral" or Path(binary).name.startswith("mistral"):
        return [binary, "chat", prompt] + extra

    # Generic CLI fallback used by user-registered binaries.
    return [binary, "-p", prompt] + extra


def _supports_flag(binary: str, flag: str) -> bool:
    # Best-effort; don't spawn --help on every call.
    return flag in {"--yolo", "--yes"}


def read_only_supported(spec: EngineSpec, settings: dict) -> bool:
    return (spec.transport in {'openai', 'ollama'} or spec.family in {'claude', 'grok'}
            or spec.key == 'codex' or (spec.key == 'agy' and spec.transport == 'cli' and spec.family == 'gemini')
            or bool(settings.get('allow_advisory_read_only', False)))


def run_engine(
    key: str, prompt: str, *, cwd: Path, write: bool, timeout: int, dry_run: bool,
    extra_args: Sequence[str] | None = None, config: dict[str, Any] | None = None,
    resume: str | None = None, cont: bool = False,
) -> dict:
    """Normalize failures from every transport while preserving raw local logs."""
    from .diagnostics import classify
    try:
        result = _run_engine(key, prompt, cwd=cwd, write=write, timeout=timeout,
                             dry_run=dry_run, extra_args=extra_args, config=config,
                             resume=resume, cont=cont)
    except Exception as exc:
        result = {'engine': key, 'ok': False, 'dry_run': dry_run, 'command': [],
                  'stdout': '', 'stderr': str(exc), 'returncode': 1,
                  'error_type': type(exc).__name__}
    if not dry_run and result.get('ok') and not str(result.get('stdout') or '').strip():
        result = {**result, 'ok': False, 'diagnostic': 'invalid_response'}
    code = classify(result, prompt=prompt)
    if code:
        result['diagnostic'] = code
    return result


def _run_engine(
    key: str, prompt: str, *, cwd: Path, write: bool, timeout: int, dry_run: bool,
    extra_args: Sequence[str] | None = None, config: dict[str, Any] | None = None,
    resume: str | None = None, cont: bool = False,
) -> dict:
    identifier(key)
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    spec = resolve(key, config)
    config = config or {}
    settings = config.get('engines', {}).get(key, {})
    ready, detail = available(spec)
    failure = {'engine': key, 'ok': False, 'dry_run': dry_run, 'command': [],
               'stdout': '', 'stderr': '', 'returncode': 1}
    if not cwd.is_dir():
        return {**failure, 'stderr': 'Project directory does not exist', 'diagnostic': 'configuration'}
    if not dry_run and not ready:
        code = 'authentication' if spec.transport == 'openai' else 'missing_executable'
        if spec.transport not in ('openai', 'cli', 'custom') or (spec.transport == 'custom' and not spec.argv):
            code = 'configuration'
        return {**failure, 'stderr': detail, 'diagnostic': code}
    if write and not dry_run:
        from .store import load_state
        if load_state(cwd).get('locks'):
            return {**failure, 'stderr': 'Workspace board locks are held; coordinate their release before writing'}
    lock = workspace_lock(cwd, 'writer', blocking=False) if write and not dry_run else nullcontext()
    try:
        with lock:
            if write and not dry_run and load_state(cwd).get("locks"):
                return {**failure, "stderr": "Workspace board locks are held"}
            if spec.transport in {'openai', 'ollama'}:
                if resume or cont:
                    return {**failure, 'stderr': 'HTTP session resume is unsupported; use explicit task artifacts'}
                from .http_agent import run
                async def limited():
                    return await asyncio.wait_for(run(spec, prompt, cwd, config, timeout, write, dry_run), timeout=timeout)
                return asyncio.run(limited())
            # Custom CLI arguments come exclusively from trusted workspace configuration.
            cmd = build_command(spec, prompt, cwd=cwd, write=write, extra_args=extra_args, config=config)
            cmd = apply_session_flags(spec, cmd, prompt, resume=resume, cont=cont)
            if dry_run:
                return {**failure, 'ok': True, 'returncode': 0, 'command': cmd,
                        'stdout': f'Would invoke {cmd[0]}; prompt stored separately; write={write}'}
            if not write and not read_only_supported(spec, settings):
                return {**failure, 'stderr': 'Read-only enforcement is unverified for this CLI. Use an isolated checkout and explicitly configure allow_advisory_read_only=true, or use HTTP/MCP.'}
            expected = estimate_tokens(prompt)
            if expected > int(settings.get('context_tokens', config.get('context_tokens', 12000))):
                return {**failure, 'stderr': 'Prompt exceeds configured context allowance'}
            if spec.key == 'agy' and spec.transport == 'cli' and spec.family == 'gemini' and not write:
                from . import agy_profile
                if resume or cont:
                    return {**failure, 'diagnostic': 'read_only_policy',
                            'stderr': 'Read-only agy requires a fresh restricted agent session. Squad supplies conversation history in the prompt.'}
                try:
                    agy_profile.check_cli(cmd[0])
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    return {**failure, 'diagnostic': 'cli_arguments', 'stderr': str(exc)}
                try:
                    agy_profile.prepare(cwd)
                except (OSError, ValueError, RuntimeError) as exc:
                    return {**failure, 'diagnostic': 'read_only_policy', 'stderr': str(exc)}
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors='replace', env=os.environ.copy(), start_new_session=True)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                if isinstance(exc, KeyboardInterrupt):
                    raise
                return {**failure, 'command': [cmd[0], '[arguments in prompt artifact]'], 'stdout': stdout,
                        'stderr': f'timed out after {timeout}s; process group terminated', 'returncode': 124}
            usage = {'input_tokens': expected, 'output_tokens': estimate_tokens(stdout),
                     'total_tokens': expected + estimate_tokens(stdout), 'kind': 'estimated_cli_visible_text'}
            return {**failure, 'ok': proc.returncode == 0, 'returncode': proc.returncode,
                    'command': [cmd[0], '[arguments in prompt artifact]'],
                    'stdout': stdout.strip(), 'stderr': stderr.strip(), 'usage': usage}
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        return {**failure, 'stderr': str(exc), 'error_type': type(exc).__name__}


def _extract_text(transport: str, data: dict) -> str:
    if transport == "ollama":
        msg = data.get("message") or {}
        return str(msg.get("content") or data.get("response") or "")
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or choices[0].get("text") or "")
    return str(data.get("output_text") or "")


def _shell_quote(part: str) -> str:
    if not part or any(ch.isspace() or ch in "\"'$&|;<>()`" for ch in part):
        return "'" + part.replace("'", "'\\''") + "'"
    return part

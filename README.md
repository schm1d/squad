# Squad 0.11.1 — companies of agents, one shared mission

Squad coordinates independent coding agents and local/hosted models through a
shared task broker, compact artifacts and MCP. Each **company** groups its agents;
each **agent** chooses an engine and capabilities. The user's mission supplies the
goal and token allowance. Companies are logical teams, not authenticated tenants.

The existing `run`, `assign`, board and mailbox commands remain available.
Python 3.10+ on Linux/macOS is required; cooperative file locks use `fcntl`.

## Email, WhatsApp and automatic agent setup

Configure once, then chat with aliases such as `@codex`, `@claude` and `@grok`.
Installer flags `--setup-telegram`, `--setup-email` and `--setup-whatsapp` configure
selected channels and discover available agents for your project. Existing agents
and aliases are reused. After adding another CLI, `squad channels discover` adds
it to every configured channel.

Email uses authenticated TLS SMTP/IMAP. WhatsApp uses Meta's Business Cloud API,
with a signed HTTPS webhook and explicitly allowed phone numbers. Both support
incoming attachments and local `send --file` commands. Supported binary analysis
depends on the selected agent's tools. WhatsApp personal-account/QR login is not
included; the opt-in group route requires an eligible Meta Groups API group.

Telegram also supports local `squad telegram send --file PATH` and incoming files
with an `@agent` caption or reply context (10 MiB per file). See
[Telegram files](docs/telegram-files.md) and
[email and WhatsApp setup, files and limits](docs/email-whatsapp.md).

## Install and verify

Clone the repository, then install from its root:

```bash
git clone https://github.com/schm1d/squad.git
cd squad
python3 install.py --add-to-path --project "/path/to/your/project"
```

The installer creates an isolated environment in `~/.local/share/squad/venv`,
installs Squad with MCP support, and asks whether to add `squad` to PATH. Choosing
yes adds a launcher in `~/.local/bin` and an idempotent shell configuration block.
Bash, Zsh (including `ZDOTDIR`), and Fish are supported. Open a new terminal after
installation. Re-running the installer upgrades or reinstalls Squad in place.
Existing Squad launchers created by pip/pipx or pointing at a previous install
location are backed up and replaced after installation succeeds. The old
installation's files and your project configuration are preserved.

The interactive installer also offers Telegram setup. Choose your actual project
folder, enter missing Telegram group/user IDs, and Squad discovers installed
engines, registers agents and merges their aliases. Existing saved IDs are reused.
For an existing project, request this directly:

```bash
python3 install.py --add-to-path \
  --project "/path/to/your/project" --setup-telegram
```

The bot token must be present in `SQUAD_TELEGRAM_BOT_TOKEN` (or your configured
environment variable) when the listener starts. Setup never writes tokens into
JSON. For unattended first setup, add `--telegram-chat-id CHAT_ID` and repeat
`--telegram-user-id USER_ID` for each authorized user. Without a terminal, Telegram
setup defaults to off; `--setup-telegram` requires an explicit `--project`.
`--no-setup-telegram` skips the interactive offer.

After installing another supported CLI, run this from your project:

```bash
squad telegram discover
```

It adds newly detected engines without replacing existing agents or aliases.
A running `listen --with-workers` session reloads the aliases and starts the new
workers. Setup also enables a scan at future listener startups. No automatic
periodic scanning or inference is performed. See [automatic setup](docs/telegram-onboarding.md).

The installer checks the project before installing. By default it checks your
current directory, resolving an enclosing Squad workspace the same way workers
do. If installing from Downloads or the package directory, select your actual
agent project explicitly:

```bash
python3 install.py --add-to-path --project "/path/to/your/project"
```

Checks cover Git/worktree prerequisites, CLI availability, missing API credential
environment variables, endpoint configuration, read-only policy and selected MCP
server names across built-in and custom engines. A missing session directory is
only a login hint. Checks run offline: they cannot prove that credentials, a model,
an endpoint or every CLI flag will work. Warnings do not block installation for
other engines. The installer does not initialize Git, change trust settings or
disable safeguards. `--project` selects the check/setup target, not a global worker root.
Continue running Squad from that project or pass `--root` to its commands.

`squad init` and `squad doctor` repeat the checks for the selected workspace.

On Linux, checks also flag missing Grok `bwrap` and inaccessible runtime socket
paths. These can prevent Grok's sandbox from starting even when its login works.
Squad reports these startup failures separately from account/file permissions.
See [Grok and agy troubleshooting](docs/grok-agy.md).

agy read-only chat now selects a generated project-local native agent permitting
only `view_file` and `grep_search`. The worker checks CLI flags before launch and
refuses modified/conflicting profiles or session/permission overrides. `--mode plan`
alone is not treated as enforcement. No separate Telegram alias configuration is
needed when automatic discovery is enabled.

## Project configuration and switching folders

Squad is installed once; settings are saved per project in `.squad/config.json`.
Telegram settings are under its `telegram` key. Agents, companies and tasks are in
`.squad/coordination.sqlite3`, so copying only config.json does not copy the roster.
The bot token stays in the named environment variable, not the JSON file.

Without `--root`, Squad discovers the nearest enclosing workspace from your
current directory. An explicit `--root` selects that exact folder. In 0.8.2 the
option works before or after subcommands, including nested Telegram commands.
Configure and listen must select the same project:

```bash
squad_project="/path/to/your/project"
squad telegram setup --root "$squad_project" \
  --chat-id YOUR_CHAT_ID --user-id YOUR_USER_ID
squad telegram status --root "$squad_project"
squad telegram listen --root "$squad_project" --with-workers
```

`setup` discovers and registers available engines automatically. Configuration commands
print `project_root`, `config_file` and, where applicable, a next command containing
the same root. These output metadata fields are not written into your settings.
`telegram status` returns one JSON document, including `configured`, the config
path, and delivery status; inspecting an unconfigured folder does not create a
workspace. Missing configuration errors identify the exact file being checked.
Changing projects does not reinstall Squad or move another project's configuration.

If an old launcher is broken or cannot be identified, explicitly replace it with:

```bash
python3 install.py --add-to-path --force
```

The previous launcher is retained beside it as `squad.backup-...`. Without
`--force`, an unrecognized command is left alone. A directory is never replaced.

For a noninteractive installation, choose explicitly:

```bash
python3 install.py --add-to-path --shell zsh
# Or retain the absolute executable and leave shell configuration alone:
python3 install.py --no-add-to-path
```

Without an interactive terminal, PATH changes default to off. `--without-mcp`
installs the coordinator without optional dependencies. Pip installs never edit
shell startup files. To remove a PATH change, delete its marked `squad PATH` block
from the reported startup files (or remove `squad-path.fish`) and remove the launcher.

For development and tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[mcp]'
python -m unittest discover -s tests -v
python -m squad --version
```

The core coordinator has no third-party runtime dependency. The `mcp` extra pins
the official MCP Python SDK maintenance line (`>=1.28,<2`) and JSON Schema
validation. This release was exercised with Python 3.12 and MCP SDK 1.30.0.

## Telegram group conversations

Use your existing Squad bot to talk to registered agents in a selected group.
Bind aliases such as `@codex` and `@qwen`, then run the listener and chat workers:

```bash
squad telegram setup
squad telegram listen --with-workers
```

Users can mention multiple agents or reply to a labeled agent response. Replies
use a bounded history from the same group/topic. These are routing aliases behind
one shared bot, not separate Telegram accounts. See the [group chat guide](docs/telegram-group-chat.md)
for group privacy settings, additional aliases, user allowlists and commands.

## Diagnose engine failures

All engine transports share failure reporting: built-in CLIs, custom commands,
OpenAI-compatible APIs and Ollama. Runtime failures are classified when evidence
supports it: project trust, authentication, command arguments, permissions,
connection/TLS, rate or credit limits, unavailable models, timeout, token/tool
limits, MCP and invalid responses. Unknown errors remain explicitly unknown;
successful answers are not scanned for error keywords.

Telegram failure replies show a fixed diagnosis, a suggested next step and the
task's local inspection command. Provider output, endpoint URLs, credentials and
local paths are not copied into these failure notifications. This also applies
to failed engineering-task notifications, not just group chat.

```bash
squad doctor
squad tasks --state failed
squad tasks --id TASK_ID --logs
```

`--logs` displays the exact task's local error details. Those local logs may contain
private provider output; review them before sharing. If failure occurred before a
run artifact existed, inspect the task result instead. Resolve the cause and send
a fresh mention/request; Squad does not automatically replay work or weaken its
permission settings. Failed/unknown runs retain the existing conservative token
accounting. Live authentication, remote model availability and Telegram delivery
still need verification on your own machine.

## Telegram account MCP integration

Use the optional [Telegram MCP setup guide](docs/telegram-mcp.md) to connect
[chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp). It adds chat
history/search and optional account sends alongside the existing bot bridge.
Codex, OpenAI-compatible and Ollama engines can attach automatically; other MCP
clients use `squad mcp proxy-config`. Access is limited to selected chat IDs.

## Telegram communication

Create a bot through Telegram's [BotFather](https://t.me/BotFather), keep its token
in an environment variable or your process supervisor's secret environment, and
send the bot `/start` from your Telegram account. Do not put the token in project
JSON, source control, command arguments, or messages to other agents.

The following commands run from your Squad project. `identify` shows numeric IDs
from pending messages to your bot without acknowledging them or executing commands:

```bash
# Set SQUAD_TELEGRAM_BOT_TOKEN using your shell/secret manager first.
squad telegram identify
squad telegram configure --chat-id 123456789 --user-id 123456789 \
  --default-agent local.builder
squad telegram listen
```

Replace the IDs with your own. Register `local.builder` first, or omit
`--default-agent` to receive plain messages in `squad mail --as human`.
Each received message must match BOTH the configured chat and an allowed user.
Repeat `--user-id` for another person you intend to authorize in that chat.
Bots and other senders are ignored. Use one listener per bot across all workspaces.

| In Telegram | Result |
|---|---|
| Plain text | Mail to the configured default agent, or `human` |
| `/say local.builder Check the parser` | Mail to that registered agent |
| `/status` | Counts of queued/running/review/failed/done tasks |
| `/tasks` | Recent task IDs, owners, goals, and states |
| `/task local.builder Inspect the parser` | A bounded read-only task, only when enabled |
| `/help` | Command reference |

To allow remote task creation, repeat `configure` with `--allow-tasks` and the
desired IDs/default agent. `--task-budget 6000` caps each mission and
`--max-pending-tasks 5` limits task creation while the workspace has pending work.
Separately start a worker, for example `squad worker --agent local.builder --watch`.
Worker watch limits and idle timeout still apply. The listener never launches a
model or runs Telegram text as a shell command. Remote tasks cannot grant writes,
approve results, change configuration, or increase their own budgets.

While the listener runs, human-directed agent replies and new task submissions,
failures, expirations, and acceptances are forwarded to the configured chat:

```bash
squad reply --as local.builder --to human 'Parser checks finished; see task result.'
squad telegram send 'Starting the review now.'
squad telegram status
```

The CLI setup starts forwarding mail from configuration time. A hand-written
config without `mail_since` also forwards existing human mail on first use.
Local mail reads do not suppress delivery, and incoming Telegram mail is not
echoed back. Workers include a bounded mailbox excerpt at their next dispatch;
they do not interrupt an already running model. Interactive/MCP agents can poll
and acknowledge mail explicitly. Mail alone does not create new work.

Telegram uses long polling, so no inbound web server is required. `listen --once`
performs one cycle. Updates, offsets, delivery records, and notification cursors
survive restarts in the workspace SQLite database. An existing webhook is reported
as a conflict and is never removed automatically.

Rate limits defer delivery. A timeout during sending can leave the outcome
unknown; it appears as `uncertain` in `telegram status` and is not silently resent.
After checking the chat, `squad telegram retry DELIVERY_ID` explicitly retries it
and may produce a duplicate. Interrupted command processing is recorded for
inspection rather than automatically creating another task. This provides durable
deduplication, not an exactly-once guarantee across Telegram and local storage.

## Work quality and no-ai-slop

Squad bundles the upstream [no-ai-slop](https://github.com/petergyang/no-ai-slop)
guide and checklist, pinned to commit `000650b156983f5159695b441477f4e63b25dc85`,
with its MIT license. It does not download changing instructions at runtime.
See `THIRD_PARTY_NOTICES.md` for attribution.

Workers, playbooks, assignment prompts, interactive briefs, and the MCP server
receive a compact adaptation. The complete writing guide is loaded on demand:

```bash
squad quality policy --full
squad quality check draft.md
squad quality configure --mode strict
squad quality check result.json --handoff
```

Successful task handoffs should have this shape:

```json
{
  "summary": "Fixed empty-input handling in parser.py.",
  "evidence": ["parser.py now returns an empty record list for an empty file."],
  "verification": ["python -m unittest tests.test_parser: 4 tests passed."],
  "risks": [],
  "next_actions": []
}
```

Describe only work actually performed. If verification could not run, state the
specific reason. `warn` is the compatibility default: completeness errors and
wording findings are recorded for review. `strict` blocks incomplete handoffs
from reaching successful submission and stops dependent playbook stages. `off`
disables policy injection and handoff checks. Strict mode does not launch extra
LLM calls or silently retry a job. Writing heuristics remain advisory; use
`quality check --strict` to make their findings affect that command's exit code.

These checks identify missing evidence fields and some wording patterns. They
cannot verify claims, determine authorship, or prove code correctness. Independent
acceptance remains necessary. Full worker check reports are stored as `quality.json`;
broker task results retain the report even if the handoff text is compacted.

## Two ways to coordinate

| Interface | Purpose |
|---|---|
| `squad run GOAL --mode council` | Independent parallel assessments followed by synthesis |
| `squad run GOAL --mode relay --write` | Investigate → plan → build → review → verdict; stop on failure |
| `squad task` + `squad worker` | Durable missions, company agents, claims, delegation, review and budgets |
| `squad mcp --agent AGENT` | Let an existing MCP-capable session join the durable task system |

Playbooks write run artifacts and board entries. They do **not** create broker
tasks; choose the task/worker interface when you need durable dependency tracking.

## Register companies and agents

From your project directory, after installing Squad:

```bash
squad init

# Local llama.cpp/vLLM server; set the model alias served by your endpoint.
squad engines add local-coder --transport openai \
  --base-url http://127.0.0.1:8081/v1 --model YOUR_MODEL_ALIAS

squad company local-lab --goal 'Efficient implementation and repository surveys'
squad company review-lab --goal 'Independent evidence-based review'
squad agent local.builder --company local-lab --engine local-coder --capability code
squad agent review.auditor --company review-lab --engine claude --capability review
squad roster

squad task 'Inspect the authentication flow; return evidence and a plan' \
  --assignee local.builder --budget 12000 --mission-budget 60000
squad worker --agent local.builder
squad tasks
# Use the returned task ID after inspecting the artifact:
squad review TASK_ID --accept
squad budgets
```

HTTP models need relevant text in their task or configured MCP tools to inspect
repository files. Registering an engine alone does not give it filesystem access.
Provider CLI agents use their own login and tool configuration.

A registered agent can delegate inside a user-created mission, for example:

```bash
squad task 'Review the proposed authentication changes' \
  --parent PARENT_TASK_ID --requester local.builder \
  --assignee review.auditor --capability review --budget 6000 --output-limit 1000
squad worker --agent review.auditor
```

The parent must belong to `local.builder`. Use `--depends-on CHILD_ID` to sequence
siblings in the same mission. Dependencies become ready only after acceptance.
An agent cannot accept its own work. Accept child results before the parent.

Run a worker for each agent in separate terminals to coordinate concurrently:

```bash
squad worker --agent local.builder --watch --max-tasks 20 --idle-timeout 60
squad worker --agent review.auditor --watch --max-tasks 20 --idle-timeout 60
```

Workers stop after the task limit or the idle timeout. Idle can mean that no task
matches, a dependency awaits acceptance, or the mission lacks available budget.
Use `tasks`, `budgets`, and `events` to inspect the reason.

## Connect an existing agent through MCP

In your MCP client's server settings, add a server for its registered identity.
Use absolute paths, and the Python executable in the environment where Squad and
its MCP extra are installed:

```json
{
  "mcpServers": {
    "squad": {
      "command": "/absolute/path/to/squad/.venv/bin/python",
      "args": ["-m", "squad", "mcp", "--root", "/absolute/path/to/project", "--agent", "review.auditor"]
    }
  }
}
```

Client configuration envelopes differ; the `command` and `args` above describe the
stdio server. Give each agent its own bound identity. Add `--allow-write` only if
that agent is intended to claim/delegate write-enabled tasks. This flag does not
add filesystem-writing tools to Squad's own MCP server.

Tools include task lookup, claim, heartbeat, delegation, submission, review,
mission budgets, event deltas, bounded artifact reads, messages and acknowledgement.
Resources include `squad://overview`, `squad://tasks/{task_id}`,
`squad://quality/policy`, and `squad://quality/no-ai-slop`. Prompts include
`squad_work(task_id)` and `squad_edit_writing(draft)`. The `squad_quality_check`
tool exposes the same explainable checks as the CLI. `squad_submit` accepts
`evidence`, `verification`, `risks`, and `next_actions` alongside its summary.
Coordination tool mutations update
Squad metadata; they do not launch another model. Separate workers execute tasks.

Generate the configuration for the currently installed Python and selected project:

```bash
squad mcp config --agent review.auditor
```

The server remains stdio-only and bound to a registered identity. Telegram replies
use `squad_message(to="human", text="...")`; the configured listener forwards them.

A useful instruction for a participating agent:

> Claim an eligible task, preserve its goal and permission limits, and heartbeat
> before the lease expires. Delegate scoped children when useful. Share concise
> evidence and artifact references. Submit for independent acceptance. Poll events
> with the last cursor and acknowledge only messages you have processed.

## Give HTTP models MCP tools

Squad can also act as an MCP client. Merge the example fields from
[`examples/federation.config.json`](examples/federation.config.json) into your
project's `.squad/config.json`. Outgoing servers can use stdio or Streamable HTTP.
Select tools separately for each engine:

```json
{
  "engines": {
    "local-coder": {
      "transport": "openai",
      "base_url": "http://127.0.0.1:8081/v1",
      "model": "YOUR_MODEL_ALIAS",
      "mcp_servers": ["repository"],
      "context_tokens": 12000,
      "max_output_tokens": 2000,
      "token_budget": 24000,
      "max_tool_rounds": 6,
      "max_tool_calls": 16
    }
  },
  "mcp_servers": {
    "repository": {
      "transport": "stdio",
      "command": "/absolute/path/to/your-approved-mcp-server",
      "args": [],
      "env_allow": [],
      "allowed_tools": ["read_file", "search"],
      "read_only_tools": ["read_file", "search"]
    }
  }
}
```

Replace tool names with those exposed by your installed server. This example does
not install or assume a filesystem server. Empty allowlists expose no tools;
there are no wildcard grants. Tool annotations do not override local policy.
Tools not explicitly classified read-only require a write-enabled dispatch.
Approve server commands/configuration as executable code: starting a stdio server
itself executes that program, even when its exposed tools are read-only.

For remote MCP use `"transport":"streamable-http"`, `"url":"https://…/mcp"`
and optional `"bearer_env":"MY_MCP_TOKEN"`. OAuth login/discovery is not implemented.
Do not place secrets in JSON. Stdio children receive the SDK's minimal environment
plus explicitly selected `env_allow` names. Avoid exposing unrelated provider keys.

Outgoing resources and prompts have separate exact allowlists:
`allowed_resources` contains full URIs, and `allowed_prompts` contains prompt names.
Squad exposes these to a tool-capable model as namespaced function calls. Resource
URIs cannot be replaced by model arguments; prompt arguments are schema-checked.
Returned prompts are untrusted tool data and never promoted to system instructions.
Choose at most 64 tools/resources/prompts per run to bound schema overhead.
Remote connections require HTTPS; loopback HTTP remains available for local servers.

```bash
squad mcp inspect --engine local-coder
squad mcp inspect --server repository
```

Inspection initializes configured servers and lists the operations exposed under
the current permission policy; it does not call their tools. Starting a stdio
server still executes its configured command. Exact resource URIs are read only
when requested. See `examples/mcp-capabilities.config.json`; its names and URLs
are placeholders to replace with your actual servers.

Tool-call schemas and results consume the context allowance. Full tool results
are stored in `.squad/runs/tool-results/`; only bounded excerpts reach the model.
A model needing more should use an allowed artifact/read tool. The loop fails
with an explicit budget error rather than silently removing the original goal.

## Token discipline

- Required goals stay intact; oversized goals are rejected instead of clipped.
- Prior work uses compact handoffs with evidence and paths to full artifacts.
- Event cursors avoid repeatedly reading the entire board.
- HTTP requests reserve room for output and enforce round/tool-call limits.
- Each mission atomically reserves a task allowance before dispatch, so concurrent
  agents cannot reserve the same remaining budget twice.
- Provider token reports are kept distinct from estimates. CLI visible text omits
  hidden reasoning, tool use and resumed history, so broker/playbook accounting
  conservatively charges the full reservation for CLI runs and failed/unknown work.

These are scheduling and inference limits, **not an exact billing guarantee**.
Preflight estimates use UTF-8 byte length divided by three, not a model tokenizer.
Providers can report usage above the requested allowance; Squad records it and
fails the task. It cannot undo already billed tokens. No dollar prices or claimed
percentage savings are built in.

## Provider compatibility

| Connection | Works with | Requirements and limits |
|---|---|---|
| Coding CLI | Existing vendor CLIs and custom commands | Vendor executable/login; flags vary by version |
| OpenAI-compatible | Hosted gateways, llama.cpp, vLLM and compatible services | Chat Completions; native function calling for MCP tools |
| Ollama | Models served by Ollama | `/api/chat`; selected model must support tools for MCP |
| Squad MCP server | MCP-capable agent hosts | A registered bound agent; stdio |

Local HTTP endpoints can work without API keys. For a remote keyless gateway set
`"auth":"local"` explicitly. Set `api_key_env` for each hosted provider; Squad
never substitutes an unrelated OpenAI key. Some compatible APIs require
`"output_token_field":"max_completion_tokens"` instead of `"max_tokens"`.
Anthropic/Gemini native HTTP APIs, A2A, arbitrary proprietary protocols and browser
sessions are not universal adapters. Use a supported CLI or a compatible gateway.

## Safety, persistence and recovery

The shared broker lives in `.squad/coordination.sqlite3`. State/mail updates use
process locks and atomic replacement where a file is rewritten. Runs and artifact
filenames have random suffixes. Read-only council/fanout stages run concurrently;
write dispatches share a single cooperative writer lock per workspace.

Claims carry an expiring lease and a private token. Expired work is marked failed
on the next claim attempt, charges its reservation, and is **not automatically
replayed**: inspect effects before `squad review TASK_ID --retry`. At most two
attempts are allowed. Delegation is capped at depth 4, 8 children per parent and
64 tasks per mission. These are local Squad tasks, not the MCP experimental Tasks
protocol.

This is a trusted local workspace, not a sandbox, multi-tenant service or remote
company authentication system. External MCP servers and vendor CLIs execute with
their own authority. Prompt labels cannot neutralize all prompt injection. For
unverified CLI read-only behavior, Squad refuses dispatch unless you explicitly
set `allow_advisory_read_only: true` on that engine and accept the advisory nature.
Use an isolated checkout/container for those CLIs. Existing Claude/Grok/Codex
restriction flags are retained; their live behavior requires your installed CLI's
validation. Squad no longer automatically adds `--yolo` or the agy permission
bypass flag. Other native permission modes remain engine-specific.

For writes, opt in both when creating the task (`--write`) and starting its worker
(`--allow-write`). HTTP/MCP write dispatches use the same workspace writer lock.
Independent checkouts and non-Squad processes are outside that lock. No automatic
worktree isolation, merge conflict resolution, cancellation/rollback, or hidden
vendor-token metering is promised in this release.

Read [`REVIEW.md`](REVIEW.md) for the concrete review and next architectural steps.


## Agent-requested Telegram messages and files (0.11.0)

Ask any registered agent to send a project file, for example
`@codex Send Docs/Testing/walking.mp4 here`. Squad stages its bytes and displays
the destination, path, size, hash and caption. Reply with the displayed
`/approve ID` to send, or `/cancel ID`. No additional setup is needed for the
current group. This approval does not grant the agent project editing permission.

Named recipients can use the bot or the configured Telegram account MCP server.
See [Telegram delivery setup](docs/telegram-actions.md) for commands, recipient
registration, transport identity, delivery receipts and limits.

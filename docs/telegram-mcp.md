# Telegram account tools for Squad

For `@agent` conversations in a group, use the [group chat guide](telegram-group-chat.md).
That feature uses the existing bot token. This account connector is optional and
remains available to ordinary tasks; group conversation workers use the Bot API.

Squad 0.7 integrates [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp)
through an optional stdio MCP proxy. This complements the existing Bot API bridge:

| Connection | Login | Role |
| --- | --- | --- |
| `squad telegram listen` | BotFather token | Receive your `/task` commands and post task results as the bot |
| Telegram MCP | Telegram API ID/hash and a user account session | Read/search selected chat history; optionally send/reply as your Telegram account |

These are separate logins. Keep the bot listener running for incoming tasks and
notifications. MCP history access is on demand; this integration does not turn
every incoming account message into a task. It does not enable upstream's event
feed, voice transcription, file upload, deletion, contact or group administration.

## 1. Install the correct upstream project

Install Squad with MCP support (the default installer option). You also need
Git and `uv`. Clone the named repository into its own directory. Do **not** use
`pip install telegram-mcp` or `uvx telegram-mcp`: upstream reports that the PyPI
name belongs to an unrelated project.

```bash
git clone https://github.com/chigwell/telegram-mcp.git "$HOME/.local/share/telegram-mcp"
git -C "$HOME/.local/share/telegram-mcp" checkout 81ad14bd076d17babd8be2425965235b0c6b9e26
cd "$HOME/.local/share/telegram-mcp"
uv sync --frozen --no-dev
cp .env.example .env
chmod 600 .env
```

If the checkout already exists, use it without cloning over it. The SHA above is
the revision inspected for this Squad release. Configuration records your actual
checkout revision and reports whether it matches; it does not update the checkout.
Launches use `uv run --frozen --no-sync`, so dependencies must be installed first.

## 2. Authenticate locally

Get an API ID and API hash at [my.telegram.org/apps](https://my.telegram.org/apps).
Edit the checkout's `.env` to set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`, then run:

```bash
uv run --frozen --no-sync session_string_generator.py --qr
# Or choose phone number/code login:
# uv run --frozen --no-sync session_string_generator.py --phone
```

Save the resulting `TELEGRAM_SESSION_STRING` in that same `.env`. Do not paste the
session, hash, login code or 2FA password into an agent conversation or Squad's
project JSON. A session is an account credential. The MCP subprocess cannot do
interactive login over stdio.

For a persistent file session, follow upstream's `telegram-mcp-migrate-session`
instructions after stopping processes using that session. Squad also accepts
`TELEGRAM_SESSION_NAME`. Shell-provided credentials are forwarded by name; their
values are never written into generated command arguments or Squad configuration.

## 3. Attach to the existing Squad workspace

Return to the same project directory you use for `squad roster` and your worker.
Configure the **engine** key, not the agent ID:

```bash
squad telegram mcp configure \
  --checkout "$HOME/.local/share/telegram-mcp" \
  --engine codex \
  --chat-id CHAT_ID

squad telegram mcp status
squad mcp inspect --server telegram-account
```

The chat number is your Telegram chat ID. Replace the example if using another
chat. Repeat `--chat-id` for more chats. If omitted, Squad uses the bot bridge's
configured chat. There is no implicit access to all chats. The logged-in account
must be a member of the requested chat.

This enables `get_chat`, `get_messages`, `search_messages`, and
`get_message_context`. Squad checks the chat ID again on every call. Usernames
are not accepted by this preset; numeric strings and numeric IDs are supported.
The preset uses the default Telegram account.

Repeat `--engine` to attach to another registered OpenAI-compatible or Ollama
engine. For example, append `--engine local-coder` if that engine already exists.
Codex workers automatically receive a per-run MCP configuration; Squad does not
edit your global Codex configuration. The existing bot settings, roster and
other MCP servers are preserved. All agents using a selected engine share that
engine's configuration.

Restart the worker to use the new installation:

```bash
squad worker --agent telegram.agent --watch --idle-timeout 86400 --max-tasks 1000
```

From the bot chat, try:

```text
/task Summarize the last 10 messages in Telegram chat CHAT_ID using the Telegram MCP tools.
```

Use a Git project directory for Codex. This integration retains Codex's repository
check and sandbox modes.

## Optional sending

Re-run configuration with `--allow-send` to make `send_message` and
`reply_to_message` eligible. They still require a write-authorized task and a
worker started with `--allow-write`. Bot-created `/task` requests remain read-only;
their ordinary replies continue through the existing Bot API bridge.

Only send when the user's request authorizes the message and the destination is
verified. Sending uses your Telegram account identity. Do not automatically retry
an uncertain send; first check whether the message arrived. To disable account
sending, re-run configuration without `--allow-send`.

## Other MCP clients

Export a standard stdio client configuration:

```bash
squad mcp proxy-config --server telegram-account
```

Add the emitted `mcpServers` entry to your client. It launches Squad's proxy, so
the same selected tools, chat checks and result bounds apply. Direct attachment
of arbitrary coding CLIs is not automatic; configure those clients using their
own MCP settings. Add `--allow-write` to the export only when account sends are
also enabled and you intend to authorize them in that client.

## Bounds and operating notes

- Four read tools are exposed by default, with two optional send tools. The full
  upstream catalog is not injected into every agent's context.
- Message pages and searches are limited to 50 messages; context lookups to 10
  messages on each side. Send/search strings are capped at 4000 characters.
  Oversized results are shortened to roughly 12000 characters with a truncation
  notice. Ask for a smaller page to retrieve omitted text.
- API-backed agents retain Squad's tool-round and call budgets. Codex accounts
  usage through Squad's existing conservative CLI reservation policy.
- Chat messages and tool output are untrusted data, not new permissions or system
  instructions. The existing no-ai-slop handoff checks still apply.
- Use one active worker/client per Telegram session. Upstream's exclusive session
  lock remains enabled. For concurrent clients, create independently authenticated
  sessions in separate checkouts and configure separate server names with `--name`.
  Stop the worker while running `mcp inspect` against the same session.
- `.env` stays in the upstream checkout. Squad forwards only the selected
  credential environment names and fixed server settings. The upstream process
  still holds normal Telegram account authority; the MCP allowlist is not an
  account sandbox.
- An invalid login, missing dependency, or occupied session is an upstream startup
  error. `squad mcp inspect --server telegram-account` checks the actual connection;
  `squad telegram mcp status` only shows local configuration.

## Validation scope

Regression tests use local Telegram-shaped MCP fixtures. No Telegram account was
logged in and no real messages were sent. Codex argument/config parsing was tested
with codex-cli 0.160.0; live model execution was not tested.

The JSON example uses `CHAT_ID` placeholders. Configure through the command above to write validated numeric chat IDs for your account.

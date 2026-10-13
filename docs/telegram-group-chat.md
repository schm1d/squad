# Talk to your Squad agents in a Telegram group

Squad 0.8 adds conversations to the Bot API bridge. A user can write:

```text
@codex What do you think of this approach?
@qwen Review the answer above.
@codex @qwen Compare your suggestions.
```

Each mentioned agent runs through its configured engine and replies in the group.
Replies are labeled, for example `[codex · telegram.agent]`. Use Telegram's Reply
button on an agent response to continue without another mention. A mention in a
reply overrides the previous agent, so replying to Codex with `@qwen Review this`
passes the quoted answer to Qwen.

One shared Telegram bot carries the messages. `@codex` and `@qwen` are Squad
routing aliases, not separately registered Telegram usernames, accounts, profile
pictures or native autocomplete entries. This feature requires only your existing
BotFather bot token, not a personal Telegram account session or telegram-mcp.

## Automatic setup

Squad 0.9 can handle agent registration and aliases during installation:

```bash
python3 install.py --add-to-path \
  --project "/path/to/your/project" --setup-telegram
```

For an existing installation, run `squad telegram setup` from your project. It
reuses saved Telegram IDs or asks for missing IDs. Then start
`squad telegram listen --with-workers`. Later, `squad telegram discover` merges
new engines into the same group; the managed listener loads their aliases and
starts their workers without a restart. See the [onboarding guide](telegram-onboarding.md)
for detection limits, permissions and unattended setup.

## Manual setup for your existing workspace

Stop the old listener/workers, extract the updated Squad package and reinstall:

```bash
python3 install.py --add-to-path --project "/path/to/your/project"
```

Return to your existing project folder, where `squad roster` lists your agents.
Reuse the Telegram group and user configuration already present. To configure a
different group or additional authorized participants:

```bash
squad telegram configure \
  --chat-id CHAT_ID \
  --user-id USER_ID \
  --default-agent telegram.agent
```

These are example IDs from the current setup. Use the intended Telegram group's
ID and the participating users' Telegram IDs; repeat `--user-id` for each person
who may invoke agents. `squad telegram identify` can discover IDs from messages
sent to the bot. Anonymous-admin/channel posts are not accepted as identified
human requests. `/task` remains separately opt-in through `--allow-tasks`.

Configuration is saved in `<project>/.squad/config.json`, under `telegram`.
If you run commands from different directories, pass the same `--root` to every
configure, chat configure and listen command. Version 0.8.2 prints the selected
project and JSON path after saving, and the listener announces the path it reads.
`telegram status --root PATH` also reports an unconfigured project without creating
or replacing its settings. The bot token remains an environment variable; the
agent roster remains in the project's database rather than the config JSON.

Enable an alias for the registered `telegram.agent`:

```bash
squad telegram chat configure --agent codex=telegram.agent
```

To expose more registered agents, provide all desired bindings in the same call:

```bash
# Only if local.agent already exists and has a working engine configuration:
squad telegram chat configure \
  --agent codex=telegram.agent \
  --agent qwen=local.agent
```

Explicit bindings replace the alias map. With no `--agent` flags, existing aliases
are retained, or aliases are generated from the roster on first setup. Dots and
other punctuation become underscores; collisions require explicit bindings.
An alias may target any registered Squad agent whose engine is ready. Local/hosted
model credentials and existing read-only engine restrictions still apply. Aliases
must begin with a letter and contain only lowercase letters, digits or underscores.
Explicit manual bindings and disabling chat turn off startup auto-discovery, so a
later scan does not undo your manual selection. You can still run
`squad telegram discover` explicitly to add more engines.

In BotFather, use `/setprivacy` to disable group privacy for this bot so it receives
free-text aliases. Alternatively, a bot with group-admin status receives ordinary
group messages. The bot must belong to the selected group and be allowed to send.
See Telegram's [official explanation](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

If privacy remains enabled, use a command explicitly addressed to your bot:

```text
/ask@YourActualBotUsername codex What do you think?
```

Replace `YourActualBotUsername` with the bot's Telegram username. This is different
from the Squad agent alias. `/agents` lists the available aliases and engines;
`/agents@YourActualBotUsername` is the privacy-friendly form.

Start the group and its workers together:

```bash
squad telegram listen --with-workers
```

This starts one read-only chat worker per distinct agent in the alias map. It only
claims chat turns, not queued engineering tasks. The existing standalone worker
command can also handle chat turns; use `squad worker --agent ID --chat-only --watch`
if supervising it separately. For a separately managed listener, use
`squad telegram listen --poll-timeout 2` for prompt reply delivery.

Do not run competing pollers for the same bot. The workspace listener lock prevents
duplicate listeners within one workspace. This release routes one configured group
per workspace; use a distinct bot/workspace for another concurrent group.

## Conversation behavior

- Only authorized human senders in the configured group can trigger replies.
  Bot messages and unrelated group chatter do not create work or receipts.
- Mention up to three agents in one message by default. Repeated aliases targeting
  the same agent create one turn. Agent-generated mentions do not automatically
  trigger other agents. Ask another agent to review a reply when you want a handoff.
- Each reply gets the current question and up to six earlier turns from the same
  group and Telegram topic. All authorized participants share that room context.
  Direct replies also carry a bounded copy of the specifically referenced answer,
  even when it falls outside that window. This is context passed by Squad, not an
  unbounded shared provider session.
- Group prompts exclude private Squad mail. Chat mode also stops automatically
  forwarding the general human mailbox into the group. Existing engineering task
  notifications remain available through the bridge.
- Chat turns use the existing task budget and pending limit (defaults: 6000 tokens
  per agent reply, at most five queued/running turns). At most 16 aliases can be
  configured. A successful conversation completes without an engineering acceptance
  gate; actual engineering tasks retain their review requirement.
- Replies are plain conversation text, with writing checks and honest uncertainty.
  They are not forced into JSON handoff envelopes. Chat workers cannot delegate
  child work or acquire file-write permission through a mention.
- The bot delivers answers once through its durable outbox. Chat workers do not
  use the optional chigwell Telegram account MCP connector for reply delivery;
  that connector remains available to ordinary tasks. Other selected read tools
  remain subject to the engine's normal policy.
- Sending interruptions remain `uncertain`; they are not silently retried.
  Check `squad telegram status` before explicitly retrying a delivery. Failed model
  runs can be retried with a fresh mention and a new budget.
- Keep the listener running. Ctrl+C stops the listener and chat workers it started.
  The combined session stops if a managed worker exits; restart it after resolving
  the cause. Worker sessions retain the existing 1000-turn/24-hour idle limits.

Use `squad telegram chat configure --context-turns 10` to adjust the history window
while retaining aliases. Version 0.9 listeners reload aliases and chat settings.
Changing the group, allowed users or token variable requires a listener restart.

To stop new conversational routing:

```bash
squad telegram chat disable
```

Version 0.9 listeners reload this setting; already queued turns remain in the broker.

## Troubleshooting engine failures

In 0.8.1, a failed turn identifies a recognized error category and suggests a local
fix instead of only saying the reply failed. For example, `workspace_trust` points
to project setup, `authentication` to CLI login/API credentials, and `connection`
to the server or network. The same reporting applies to any engine behind an alias.
Run the `squad tasks --id ... --logs` command shown in the message from your project
folder to inspect that exact run. Run `squad doctor` to check local prerequisites.

Failure messages use fixed text and never forward raw engine logs into the group.
Unknown errors point to the local log; they do not trigger automatic retries.
Setup checks run without inference and cannot validate live credentials or model
access. Reinstall and restart the existing listener/workers to use the new reporting.

## Test scope

Tests exercise mentions, multiple models, reply routing after restart, explicit
cross-agent replies, topic separation, duplicate recovery, limits, old-workspace
migration, natural replies under strict quality mode, worker ownership and loop
prevention. A real worker process talks to a local HTTP model fixture and returns
its answer through a simulated Bot API. Live Telegram delivery and vendor model
responses require verification with your own account and engine setup.

The JSON example uses `CHAT_ID` and `USER_ID` placeholders. Replace them with numeric IDs, or use `squad telegram setup` to collect and validate them.

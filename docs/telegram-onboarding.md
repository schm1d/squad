# Install once, discover agents as they become available

The installer can configure a project's Telegram group and agent aliases together:

```bash
python3 install.py --add-to-path \
  --project "/path/to/project" --setup-telegram
```

In an interactive terminal, ordinary installation offers this setup too. It asks
for a project folder if one was not supplied. Saved Telegram group/user IDs are
reused; missing IDs are requested. A project needs both its Telegram group ID and
an explicit list of allowed Telegram user IDs. These are Telegram IDs, not Squad
or model-provider identities. Use the existing `telegram identify` command if you
need to discover the IDs from messages sent to your bot.

For unattended first installation, pass `--telegram-chat-id CHAT_ID` and one or
more `--telegram-user-id USER_ID` flags. `--telegram-token-env ENV_NAME` selects a
different token variable. Squad does not ask for, print or store the actual bot
token during this flow. Set the configured environment variable before listening.
Task creation through `/task` remains separately opt-in; existing permissions are
preserved, and new setup does not enable write access or background delegation.

The installer completes package installation before changing the project's agent
or Telegram configuration. If setup fails afterward, it reports that Squad was
installed and provides a `telegram setup` command to finish the project setup.

## Already installed

From the project directory:

```bash
squad telegram setup
squad telegram listen --with-workers
```

You can also specify `--root` on either command. For first setup without prompts:

```bash
squad telegram setup --root "/path/to/project" \
  --chat-id YOUR_GROUP_ID --user-id YOUR_USER_ID
```

Setup automatically registers detected engines and creates aliases such as
`@codex`, `@claude` and `@grok`. An existing alias for the engine is reused; an
existing agent with that engine can be reused without changing its company or
capabilities. New agents join the `telegram` company. Existing company goals,
agents, capabilities, tasks, aliases, user allowlists, budgets and MCP settings
are preserved unless explicitly changed by setup arguments.

## After installing another AI

```bash
squad telegram discover
```

This scans again, adds newly available engines and merges their aliases. It does
not remove agents when a CLI is missing or alter previous bindings. Collisions get
a numbered name; existing aliases and IDs win. There is a 16-alias limit; extra
engines are reported as skipped. Inspect the proposed additions without writing:

```bash
squad telegram discover --dry-run
```

The managed listener reloads aliases and starts one worker per newly bound agent.
You do not need to restart it just to add an engine in an existing PATH directory.
An independently supervised listener also reloads aliases, but you must start any
new standalone workers yourself. Changes to the process environment (new PATH
directories or API keys), group ID, allowed users or token variable require a
listener restart. Existing child workers remain until the managed session ends,
so work already queued for removed aliases can finish.

Setup defaults `telegram.auto_discover_agents` to true, which enables discovery
at listener startup. It does not run a recurring background scanner. Explicit
manual alias selection (`telegram chat configure --agent ...`) or chat disable
sets this flag false, preventing startup discovery from undoing that choice.
You can still invoke discovery explicitly. Change the flag in project JSON if
you want to re-enable startup scanning later.

## What detection means

- Built-in CLI names and configured custom executable commands are discovered
  on PATH. Supported names include Codex, Claude, Grok, agy, Qwen, Kimi, DeepSeek,
  GLM and Mistral. Unknown harnesses need an engine definition first.
- Configured OpenAI-compatible/local endpoints can be registered, as can the
  built-in API engine when its required key variable is set. This does not probe
  endpoints or validate model/account access.
- Ollama is registered when its executable is found or an Ollama engine is
  explicitly configured. Discovery does not inventory installed model weights;
  select the right model in your engine settings. Duplicate default `llama` and
  `ollama` aliases are avoided.
- No model request, vendor login, CLI help command or remote scan is performed.
  Detection does not prove that a saved session is valid or the CLI accepts every
  argument. Runtime diagnostics remain available through `tasks --id ID --logs`.

## Read-only chat

Automatic registration never changes engine permissions. Detection reports whether
an adapter currently supports read-only execution. An alias for a CLI with
unverified enforcement appears in `/agents` as needing read-only setup. Mentioning
it explains the issue without creating a task, reserving tokens or calling a model.

This applies to the current agy/Qwen/GLM/DeepSeek and other unverified CLI adapters.
Use a configured HTTP/MCP engine or the documented isolated-checkout/advisory
policy when appropriate. Setup never silently enables advisory permissions or
removes a sandbox. After you explicitly configure the engine, the same alias can
be used; no re-registration is needed.

Configuration remains in the selected project's `.squad/config.json`. Agent and
task state remains in `.squad/coordination.sqlite3`. Discovery is idempotent and
serialized; concurrent setup/discovery does not duplicate agents. If configuration
saving fails after registration, another discovery reuses the registered agents.

# Grok and agy chat troubleshooting

## agy

Squad 0.10.2 uses Antigravity's documented native custom-agent tool allowlist.
For a read-only invocation, Squad creates a project-local file:

```
.agents/agents/squad-reader-v1-<project-hash>.md
```

It selects that name with `--agent`, permits only `view_file` and `grep_search`,
and supplies `--disable-slash-commands`. The profile has no shell, file-editing,
browser, subagent, plugin or MCP tools. This is a native tool boundary, not an
OS sandbox for the whole CLI. The CLI still has its normal runtime/cache/auth
behavior; trusted local hooks and vendor configuration retain their own authority.

`--mode plan` is kept for planning behavior, but is not the access boundary.
Antigravity's `--sandbox` controls terminal commands and gives the workspace
read/write access, so it is not a substitute for this read-only agent.

The definition is created automatically on the first actual read-only task.
Creation is additive and atomic; an existing modified definition, symlink or
reserved-name collision fails without being overwritten. Global settings are
never edited. Read-only sessions are fresh: Squad's prompt carries channel history
instead of resuming a possibly more powerful native session. Extra arguments may
select a model, effort or print timeout but cannot override the agent or permissions.

Before starting a conversation, the worker executes only `agy --help` to confirm
required flag support. Older CLIs fail with `cli_arguments`; run `agy update` and
review `agy --help` locally. Setup/doctor show that live enforcement/login still
requires validation on the installed version. The reported 1.2.15 help and the
official 1.2.16 binary's help expose the required flags. No authenticated agy model
call was performed during development; this is not a claim of end-to-end vendor
validation. If a future vendor release changes custom-agent behavior, stop that
worker until its adapter is checked.

Use your existing setup:

```bash
squad telegram listen --with-workers
```

Then send `@agy can you hear me?` in the configured group. Existing aliases and
settings are preserved. If you deliberately disabled auto discovery and never
registered agy, use `squad telegram discover` first.

## Grok: inaccessible Podman socket

The reported Grok 1.0.46 error is:

```
sandbox profile resolve failed: socket deny resolution failed:
could not resolve runtime-socket deny path /run/podman/podman.sock:
Permission denied (os error 13)
```

Grok tries to resolve runtime sockets so its Linux sandbox can deny access to
container daemons. On the reported host, it cannot inspect a protected path and
stops before producing a response. This is not evidence that the Telegram IDs,
project root or xAI login are wrong.

Squad now classifies it as `sandbox_startup`. Installer/init/doctor perform an
offline metadata check of known system and per-user runtime socket paths and look
for `bwrap`. They do not open or connect to sockets, change permissions, authenticate
or invoke a model. These checks are hints, not a complete test of Grok's namespace,
Landlock, managed policy or sandbox behavior. Warnings do not block other engines.

Local next steps:

```bash
grok update
grok --version
squad doctor
```

Use the package manager instead if it manages your Grok installation. Install
`bubblewrap` through your OS package manager only if doctor reports it missing.
Once an upstream update is installed, submit a new `@grok` message to test it.
The original log does not establish that bubblewrap is missing, and installing
it alone is not a verified repair for the protected socket error.

There is no confirmed upstream release fixing this error in this package's
validation. The inspected official source still returns errors for inaccessible
socket candidates. If an update gives the same error, retain the Grok version and
stderr for an upstream report and use another configured agent in the meantime.
Squad does not retry with `--sandbox off`, change Podman socket permissions, add
the user to a daemon group, or run Grok as root.

## Official references

- https://antigravity.google/docs/subagents/
- https://antigravity.google/docs/cli/modes/
- https://antigravity.google/docs/cli/headless/
- https://antigravity.google/docs/sandbox/
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-sandbox/src/runtime_sockets.rs
- https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md

The development checks cover adapter flags, local profile integrity, routing,
failure classification, preservation of restrictions and offline prerequisites.
They use local fixtures and make no paid model calls or real channel sends.

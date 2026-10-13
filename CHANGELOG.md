# 0.11.1

- Fix false quota failures caused by matching "quota" inside "quotations" in
  echoed quality instructions. Require explicit quota failure phrases/codes.
- Remove exact echoed requests from diagnostic input across engines while keeping
  raw local logs intact. Recognize request/transport/workspace-routing connection
  failures, preserving more specific authentication and TLS diagnoses.
- Add regression controls for the reported startup failure. Existing task logs
  are historical records; this update does not rewrite them, reset task attempts,
  change budget accounting, repair provider connectivity or bypass sandboxes.

# 0.11.0

- All Telegram chat engines can propose project-file uploads and text messages to
  the current chat or registered destinations using a shared output protocol.
- Bind approvals to the requesting user, group/topic, destination and staged bytes.
  Show previews, support /approve and /cancel, expire pending approvals in one hour,
  and retain durable delivery states and receipts for subsequent agent context.
- Dispatch approved account messages/files through chigwell/telegram-mcp, with an
  exact tool/chat allowlist and a staged-file-only root per MCP delivery session.
- Add telegram recipient and MCP --allow-files. Existing aliases, account settings,
  and bot configuration are preserved. No workspace write permission is needed for
  an approved upload. Interrupted or uncertain sends are not automatically retried.
- Limit exports to regular non-hidden project files, 10 MiB each, rejecting symlink,
  traversal, hardlink and special-file paths. Videos/images/audio use document uploads.

# 0.10.2

- Add an agy native read-only agent with an explicit `view_file` / `grep_search`
  tool list, selected automatically for read-only tasks and channel chat. Disable
  slash/skill expansion; check supported flags before inference; preserve existing
  global settings. Refuse profile collisions, tampering and resumed sessions.
- Classify Linux/macOS sandbox launch errors before generic permission failures.
  Preserve the exact stderr in local task logs and never retry with fewer restrictions.
- Extend existing installer, init and doctor checks with offline Grok bubblewrap
  and runtime-socket path checks. Reuse one read-only capability predicate across
  discovery, diagnostics and dispatch.
- Grok 1.0.46's reported inaccessible Podman socket failure remains an upstream
  startup problem. This release diagnoses it; it does not claim to repair Grok,
  alter daemon socket permissions or disable the sandbox.

# 0.10.1

- Add Telegram send --file with optional captions, forum topic and reply IDs.
  Stage immutable file bytes in the durable outbox and preserve uncertain-send recovery.
- Receive Telegram documents, photos, audio, voice, video and animation files;
  route captions or replies to the selected read-only agent. Reuse 10 MiB attachment
  limits, private storage, hash validation and bounded text excerpts.
- Add an attachment column without losing existing Telegram turns. Upgraded
  standalone workers also handle queued legacy text turns before listener migration.
- Keep reply-referenced files within the same chat/topic and reject unapproved
  users, bots, wrong-bot commands and unsupported agents before downloading files.

# 0.10.0

- Add email setup/status/discover/listen/send with TLS SMTP/IMAP, authenticated
  sender allowlists, threaded replies, loop suppression and persistent UID cursors.
- Add WhatsApp Business Cloud API setup/status/discover/listen/send, signed webhook
  verification, per-agent mentions/replies, delivery callbacks and opt-in group-ID
  routing for eligible Meta Groups API accounts. Personal WhatsApp login is not included.
- Receive bounded attachments and send files explicitly with --file. Sanitize paths,
  verify hashes, cap storage and include readable text excerpts in worker context.
  Binary interpretation depends on agent tools; no automatic OCR/transcription.
- Share durable routing, task/budget transactions and outbox recovery across the
  new channels. Preserve conversation boundaries and sanitize outward failures.
- Add installer --setup-email/--setup-whatsapp and channels discover/status. Reuse
  existing agents across channels; keep account secrets in environment variables.
- Keep Telegram behavior and migrations compatible. Generalize worker chat prompt
  dispatch while preserving the previous Telegram route as a fallback.

# 0.9.0

- Add optional Telegram onboarding to installation (--setup-telegram). Reuse saved
  IDs, prompt for missing project/group/user information, and register discovered
  engines and aliases after package installation succeeds.
- Add telegram setup and telegram discover [--dry-run]. Merge aliases, reuse
  existing agents, preserve company goals/permissions/tasks, and handle name
  collisions and repeated/concurrent discovery without destructive updates.
- Discover supported installed CLIs, configured custom commands and eligible
  API/Ollama definitions without inference or remote probing. Report readiness
  limitations and avoid duplicate default Ollama aliases.
- Setup enables discovery at listener startup. A running managed listener reloads
  aliases and starts newly needed workers; changes to group/user/token scope
  require restart. Manual alias selection/disable turns off startup discovery.
- Register unverified read-only CLI adapters with clear status. Mentions explain
  the missing setup without queuing work, reserving a budget or making a model
  call. Existing permissions and sandbox enforcement remain unchanged.

# 0.8.2

- Show project_root and config_file in Telegram configuration/status output and
  print the selected JSON path when the listener starts. Next commands retain
  that project path; missing settings and agents report the selected workspace.
- Fix nested argument parsers clearing --root from an earlier command position.
  Allow --root before or after subcommands and honor an explicit folder exactly;
  implicit selection still discovers the nearest enclosing Squad workspace.
- Return one JSON document from Telegram status, including deliveries and a
  configured flag. Inspecting an unconfigured project does not create its state.
- Keep Telegram settings in the existing project config.json, credentials in the
  environment, and agents/tasks in their existing database. No automatic migration
  or overwrite of another project's settings was added.

# 0.8.1

- Add offline project and engine checks to installation, init and doctor, with
  --project for selecting the installer's actual agent workspace. Support Git
  worktrees and warn about unsuitable roots without editing repositories/trust.
- Normalize failures across built-in CLI, custom, OpenAI-compatible and Ollama
  transports. Preserve HTTP status and bounded error details in local logs.
- Show safe diagnoses and corrective guidance for recognized runtime failures,
  including authentication, CLI flags, limits, network, model and MCP errors.
  Unknown errors stay unknown; successful replies are not scanned for keywords.
- Persist diagnostic categories in task results. Both chat and engineering-task
  Telegram failures use fixed public messages rather than raw provider output.
- Add tasks --id TASK_ID --logs for exact local error inspection. No automatic
  retries, account changes, Git initialization or permission bypasses.

# 0.8.0

- Group conversations: configurable @agent aliases, multiple agent mentions,
  /agents and /ask commands, natural replies and durable reply-to-agent routing.
- Bounded group/topic history and referenced-message context. Group prompts do
  not include private mail; bot messages cannot trigger automatic agent loops.
- Add `telegram listen --with-workers` to run configured chat workers together,
  plus `worker --chat-only` for separate supervisors.
- Chat turns share durable claims and token budgets but complete without an
  engineering review gate. Existing tasks keep their review workflow.
- Additive SQLite migration preserves existing work and pending deliveries.
  Chat creation and routing are transactional, with duplicate/restart recovery.
- Preserve existing bot configuration and MCP capabilities. Group reply delivery
  uses the Bot API, so chat turns do not launch the Telegram account MCP server.

# 0.7.0

- Add opt-in chigwell/telegram-mcp setup and status commands. Keep the Bot API
  listener, roster and unrelated MCP configuration intact.
- Attach Telegram account history/search to Codex and API/Ollama engines; expose
  the same outgoing tool policy to other MCP clients with a stdio proxy/export.
- Enforce numeric chat allowlists, small pages/searches and bounded responses.
  Account send/reply tools require both opt-in selection and write permission.
- Pass credentials by environment name or load the upstream checkout's private
  .env; generated configuration and argv contain no credential values.
- Record upstream revision; use a frozen checkout environment, retain exclusive
  session locking, and fail when selected upstream tools are absent.
- Codex receives MCP configuration per run, including resumed sessions, without
  changing its global config or removing its sandbox/repository checks.

# 0.6.2

- Fix Codex exec startup: use the supported approval_policy configuration override
  instead of the interactive --ask-for-approval flag. Preserve read-only and
  workspace-write sandbox modes without the conflicting --full-auto preset.
- Keep exec options before the resume subcommand for resumed/continued tasks.
- Add regression coverage for permissions, session routing, and literal prompts.

# 0.6.1

- Reinstallation now recognizes pip/pipx and legacy setuptools Squad launchers,
  including symlinks into a previous installation location.
- Existing launchers are backed up and atomically replaced after successful
  installation. Same-location reinstalls do not create redundant backups.
- Added `--force` for explicitly replacing an unrecognized or dangling launcher,
  while keeping a backup. Directories cannot be replaced.
- Failed package installation leaves the previous launcher and PATH files intact.
  A launcher changed by another process during installation is not overwritten.

# 0.6.0

- Telegram Bot API bridge: numeric chat/user allowlists, ID discovery, two-way
  agent mail, task status notifications, optional bounded read-only task creation,
  durable inbox/outbox/cursors, restart recovery, rate-limit handling, explicit
  retry for failed or uncertain sends, and UTF-16-aware message splitting.
- Interactive installer: isolated environment, MCP included by default, explicit
  optional PATH setup for Bash/Zsh/Fish, idempotent startup blocks, and protection
  against overwriting unrelated launchers. Plain pip installation does not edit PATH.
- MCP: outgoing allowlisted resources and prompts alongside tools; incoming task
  and quality resources, work/edit prompts, structured submission fields, quality
  check tool, generated client configuration, and capability inspection. Loopback
  HTTP bypasses environment proxies; remote HTTPS supports configured SOCKS proxies.
- Pinned upstream no-ai-slop guide, evaluation checklist, source revision, and MIT
  license. Compact policy injection avoids repeatedly loading the full documents.
- Explainable handoff checks with off/warn/strict modes, verification fields,
  independent-review preservation, and persisted quality reports. Strict checks
  stop incomplete submissions and dependent stages without additional model calls.
- Worker prompts include bounded agent-mail context. Message contents cannot
  grant permissions beyond the existing task.

This extends 0.5's task broker and provider adapters. Existing workspace data is
preserved; new Telegram tables are created on first bridge use. Quality checks
default to warn for compatibility. Configure strict mode to enforce handoff fields.

Telegram credentials, live provider credentials, and live remote services are not
included. Setup and the validation boundaries are documented in README.md and
validation/VALIDATION.md.

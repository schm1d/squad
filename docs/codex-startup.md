# Codex startup failures and task recovery

Squad 0.11.1 corrects diagnostic reporting for native startup failures. Earlier
versions could classify the word "quotations" in an echoed prompt as "quota".
The new classifier requires quota failure phrases/codes and removes the exact
request from diagnostic input while preserving raw local logs. Request failures,
closed transports and workspace routing discovery failures are classified as
connection problems unless a more specific authentication/TLS error is present.
This category identifies the failure type, not its underlying cause.

## Diagnose before spending a retry

Stop the dedicated task worker with Ctrl+C if it is watching the queue. A queued
task may be claimed immediately; requeueing does not prove that the previous
startup failure has been fixed. Leave other unrelated workers alone.

From the intended project directory, check the CLI outside Squad:

```bash
codex login status
codex exec -C "$PWD" -m gpt-6-astra \
  -c 'approval_policy="never"' --sandbox read-only \
  'Reply exactly OK. Do not call tools or change files.'
```

Use the model configured for the failed task; the example matches the reported
gpt-6-astra run. The second command makes a small model request outside the Squad
task budget. Login status identifies the local authentication state; it does not
prove that a model request will succeed. See the official CLI reference:
https://developers.openai.com/codex/cli/reference/

If the direct request fails, investigate its actual error before restarting
Squad. A workspace routing error alone does not distinguish account, service,
proxy, certificate or network causes. If it succeeds but Squad still fails,
compare the launching user, terminal/service environment, executable, model,
config and enabled MCP servers. Squad inherits its launching process environment.

Failed local MCP endpoints are separate prerequisites to check. Start the
intended servers and verify their configured endpoints when the task needs
them. Do not remove required Unreal tooling just to suppress a startup error.

## Resume the existing task

Inspect `squad tasks --id TASK_ID --logs`. If the task is already queued, do not
run review --retry again. Start its assigned worker with --allow-write only when
the task is authorized to modify the project. For the AGLS builder:

```bash
squad worker --agent agls.builder --allow-write --watch \
  --timeout 7200 --idle-timeout 86400 --max-tasks 20
```

An already running assigned worker will claim the queued task. The worker is
agent-scoped, not restricted to a single task ID. Existing failed result data
remains on a requeued task until the new attempt finishes.

`reserved_unknown` means Squad accounted for the full reserved task budget
because actual usage was unavailable. It is not a measurement of provider token
usage or a bill. Failed startup still consumes a task attempt in this release.
The update does not reset attempts, refund reservations, rewrite historical
logs, bypass sandbox policy, or make Telegram chat a write-enabled worker.

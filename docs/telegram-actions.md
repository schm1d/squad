# Agent-requested Telegram delivery

Squad 0.11.0 adds delivery proposals to every engine's Telegram conversation.
The agent identifies a project file or composes a message. Squad validates and
stages the action, previews it in the source group/topic, and sends it when the
requesting user replies `/approve ID`. `/cancel ID` discards a pending proposal.
Pending approvals expire after one hour. Another allowed group member cannot
approve your proposal, and approval in a different topic is rejected.

## Send a project file to the current group

Restart the upgraded listener and ask:

```
@codex Send Docs/Testing/walking.mp4 here.
@agy Send the report at Docs/Testing/Results.pdf here.
@claude Find the walking review video in this project and prepare it for sending here.
```

The file must exist. An engine with suitable reading/search tools can locate it;
otherwise supply the exact path. The agent cannot create/edit files during these
read-only chat turns. A path alone in normal prose does not cause an upload.

The preview contains the destination, original project-relative path, byte count,
SHA-256 and exact caption/message. Approval sends the staged snapshot, even if the
original file changes later. Changed staged bytes fail validation. Never interpret
the agent saying “sent” as delivery proof; use Squad's receipt.

There is no new setup for the existing group. Existing agent aliases and allowed
users remain in effect. Videos, photos, audio and other files are sent as Telegram
documents, not specialized video/audio messages. Maximum 10 MiB per file, four
proposals per response, and 100 MiB total retained attachment storage. Hidden paths,
symlinks, hard links, special files and paths outside the selected project are
excluded. Review content before approving; these path restrictions are not a
secret-content scanner.

## Register a destination once

From the selected project:

```bash
squad telegram recipient friend --chat-id 123456789
```

Replace the example number with the verified private chat ID of the intended
recipient. Do not infer identity from a display name. For bot delivery, that person
must first open your bot and press Start. Registering a recipient authorizes an
outbound destination; it does not add that person to the group's allowed users.
The current listener remains scoped to its configured group; it does not start
agent conversations in every recipient's DM inbox.

Then ask, for example:

```
@agy Prepare a direct message to friend: Hello, the review is ready.
@codex Send Docs/Testing/Results.pdf to friend with caption: Latest report.
```

Bot sends appear from the configured bot. Mentioning an arbitrary @username in
the group does not register it or authorize a DM. Recipient aliases are lowercase
letters/digits/underscores, starting with a letter; `here` is reserved for the
source group/topic. Recipient settings persist in `.squad/config.json` and are
read when the proposal is prepared and again before dispatch. Rebinding a recipient
invalidates already prepared proposals instead of redirecting their contents.

## Use chigwell/telegram-mcp for account delivery

Keep the existing authenticated checkout/session. Configure its permitted chats
and add file support (replace paths/IDs with your own):

```bash
squad telegram mcp configure \
  --checkout /path/to/telegram-mcp \
  --engine codex \
  --chat-id 123456789 \
  --allow-send --allow-files

squad telegram recipient friend --chat-id 123456789 \
  --mcp-server telegram-account
```

Include every intended account destination using repeated `--chat-id` options;
MCP configuration replaces that server's allowlist. `--engine` retains the existing
native MCP attachment behavior for Codex/HTTP engines. Approved chat proposals from
**any engine**, including agy, Claude and Grok, use the shared dispatcher and can
therefore use the registered MCP destination. Agents need not share native tool-call
formats. The dispatcher opens the MCP session only for the approved action.

These messages are sent as the Telegram user account authenticated in the MCP
session, not as the bot. The preview identifies the account server; verify which
account you authenticated before using it. Existing Telegram privacy/access rules
still apply. No new account login or token is performed by recipient registration.

For file dispatch the MCP session exposes only `send_file` and receives only the
staged file's containing directory as its allowed root. For text it exposes only
`send_message`. The model cannot change the chat ID, transport or approved content
at dispatch. Native chat workers do not receive direct account messaging tools:
doing so would bypass the shared proposal/approval path. Telegram MCP is actually
invoked by the delivery layer for registered MCP destinations; ordinary group
replies continue through the bot.

## Receipts and recovery

The bridge posts a receipt with the action ID and state: `sent`, `failed`, or
`uncertain`. The originating user/topic's recent action states are included in
later agent prompts. `squad telegram status` also lists action states locally.

Duplicate approval commands do not resubmit an action. An interrupted send becomes
`uncertain`; Squad does not automatically retry it. Inspect the destination before
asking for a new proposal, because the first send may already have arrived. A
definitive failure such as a blocked bot also requires a new proposal after its
cause is fixed. This does not promise exactly-once delivery across Telegram/network
failures. The old `telegram retry` command applies to the original bot outbox, not
to these approved actions.

## Validation and scope

Automated checks cover full group routing/worker/collector flow, immutable upload
bytes, sender/topic-bound approvals, cancellation/expiry, duplicate sends, recipient
changes, interrupted delivery, path restrictions, MCP file policy and a real local
MCP subprocess fixture. No real Telegram messages, recipient lookups, account logins
or paid model calls were made. Test the live provider/account on your installation.

This release implements project-file and text delivery. It does not expose every
upstream Telegram administration/contact/history tool, personal-account inbox
polling, scheduled sends, albums, or agent delivery actions on email/WhatsApp.
The existing email/WhatsApp integrations retain their previous behavior.

References: https://core.telegram.org/bots and the configured upstream checkout at
https://github.com/chigwell/telegram-mcp (see `squad telegram mcp status` for revision).

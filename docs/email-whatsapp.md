# Email and WhatsApp agent conversations

Squad 0.10 adds two-way email and WhatsApp Business Cloud API messaging. Both channels reuse the project roster and worker budgets. Type `@codex question`, `@claude question`, `/ask codex question`, or `/agents`. Replies carry the alias and registered agent ID. A reply to an agent's message continues with that agent. These aliases are text handled by Squad: the WhatsApp account remains one business number, not one WhatsApp account per agent.

The setup commands discover installed/configured engines and reuse existing agents, including agents registered for Telegram. New agents can be added to every configured channel with:

```bash
squad channels discover
squad channels status
```

Add `--root "/path/to/project"` when running outside your selected project. It works before or after subcommands. Configuration stays in that project's `.squad/config.json`; inbox, task and delivery records stay in its `.squad/coordination.sqlite3`. Tokens and passwords stay in environment variables. Executable defaults contain no personal project paths or account identifiers.

## Installation

From the extracted package's parent directory:

```bash
python3 install.py --add-to-path --project "/path/to/project" \
  --setup-telegram --setup-email --setup-whatsapp
```

Each selected channel asks for missing account settings and keeps saved values on reinstall. Installation does not contact messaging services or start listeners. You can configure channels later without reinstalling:

```bash
squad email setup
squad whatsapp setup
```

Noninteractive installation can reuse saved channel settings. For first-time unattended configuration, use the explicit `setup` options below. `--setup-email` and `--setup-whatsapp` are opt-in installer flags; the installer still offers Telegram interactively as before.

## Email

Use a dedicated bot mailbox with IMAP and SMTP access. Your human sender address must be different. Providers that disable password login need a supported app password; OAuth-only SMTP/IMAP is not implemented in this release.

```bash
squad email setup \
  --address squad@example.com \
  --imap-host imap.example.com \
  --smtp-host smtp.example.com \
  --authserv-id mx.example.com \
  --allow-sender you@example.com
```

`--authserv-id` is the trusted receiving server identifier in the mailbox provider's `Authentication-Results` header, not necessarily its IMAP hostname. For example, Gmail commonly stamps `mx.google.com`. Use your provider's actual value. Squad requires its first matching result to report DMARC pass for the From domain. Your provider must strip forged results claiming its own identifier. Plain From/Reply-To text is not authentication. Rejected/automatic messages appear as a cursor diagnostic in `squad email status` and never start agents.

Set `SQUAD_EMAIL_PASSWORD` in the listener's environment using your usual secret manager or a hidden terminal prompt. Squad stores only that variable's name. Separate IMAP/SMTP logins are supported with `--username`, `--smtp-username`, `--imap-password-env` and `--password-env`.

Defaults are IMAP TLS on 993 and SMTP TLS on 465. For submission on 587, add `--smtp-port 587 --smtp-security starttls`. There is no plaintext authentication option. `--mailbox` defaults to `INBOX`.

```bash
squad email listen --with-workers
```

The first successful IMAP connection establishes a cursor and **skips the existing inbox**. Send a fresh email afterward. A recreated mailbox also gets a new cursor. Messages are fetched with BODY.PEEK and are not marked read or deleted. Put `@codex` in the subject or body and attach files normally. Replies go only to the authenticated, allowlisted From address, never Reply-To or CC. Automatic messages, lists and delivery reports are ignored to prevent loops. Subject/body plain text is supported; HTML-only mail does not become executable content.

Local outbound mail, including files:

```bash
squad email send "Here is the report." --to you@example.com \
  --subject "Project report" --file ./report.pdf
squad email status
```

Recipients must be explicitly allowlisted; repeat `--allow-sender` during setup for multiple people. SMTP acceptance is recorded as `sent`; this does not prove inbox delivery.

## WhatsApp

This adapter uses **Meta's WhatsApp Business Cloud API**. It needs a registered business phone number, a Meta app, a supported Graph API version, and a public HTTPS webhook. It does not log in to a personal WhatsApp account via QR code, join existing personal groups, or use WhatsApp Web automation.

In your Meta app, obtain the phone-number ID (not the phone number itself), a token with `whatsapp_business_messaging` access, and the app secret. Choose a separate webhook verification token. Set these in the environment of the listener:

- `SQUAD_WHATSAPP_TOKEN`: API access token.
- `SQUAD_WHATSAPP_APP_SECRET`: Meta app secret, used to verify every webhook signature.
- `SQUAD_WHATSAPP_VERIFY_TOKEN`: your chosen secret for Meta's initial callback verification.

```bash
squad whatsapp setup \
  --phone-number-id YOUR_META_PHONE_NUMBER_ID \
  --api-version VERSION_FROM_YOUR_META_DASHBOARD \
  --allow-user +YOUR_COUNTRY_CODE_AND_NUMBER
squad whatsapp listen --with-workers
```

Replace the version placeholder with a supported value such as `v25.0`; no moving 'latest' API version is guessed. Repeat `--allow-user` for each authorized human. A phone number on this list can request read-only project conversations, so choose it deliberately.

The listener binds `127.0.0.1:8788` and serves `/webhook`. Put your HTTPS reverse proxy in front of it and enter `https://YOUR_HOST/webhook` as the Meta callback URL, with your verification token. Subscribe the app/account to the `messages` webhook field. Preserve the raw POST body and `X-Hub-Signature-256` header. Other paths are not served. Use `--host` and `--port` only when changing the local bind is needed; the built-in listener does not terminate TLS. Apply ordinary request-rate/connection limits at the public reverse proxy.

Send to the business number:

```text
/agents
@codex What is pending?
@claude Review the file attached to this message.
/ask grok Explain the tradeoff.
```

Put the alias/question in a document or image caption, or attach a file while replying to an agent's earlier message. Audio/video without a caption needs a reply context or a configured default agent. Optional `--default-agent REGISTERED_AGENT_ID` routes unmentioned messages; use an ID already listed by setup.

To send a file from the workstation:

```bash
squad whatsapp send "Here is the report." --to +YOUR_NUMBER --file ./report.pdf
squad whatsapp status
```

An allowlisted incoming WhatsApp message must have been received first. Free-form direct replies require the 24-hour customer-service window opened by a user message. Squad records a closed-window failure rather than inventing consent or falling back to a template. Approved-template sending is not implemented; use your Meta tooling for that workflow. Delivery/read/failure callbacks update local status where received.

### Groups

There is an opt-in route for **Meta Groups API** group IDs:

```bash
squad whatsapp setup --allow-group YOUR_CLOUD_API_GROUP_ID
```

The business must separately qualify for and provision the group through Meta. Both the group ID and the human sender's phone number must be allowlisted. Squad routes `group_id` webhook messages and sends with `recipient_type=group`. It does not create groups, invite people, or turn a personal group into a Cloud API group. Group routing is covered by protocol fixtures; it has not been verified against a live eligible Meta Groups API account. Check current Meta eligibility and webhook availability before relying on it.

## Attachments, permissions and costs

Incoming files are saved in the project under `.squad/attachments/CHANNEL/...`, with sanitized filenames, private file permissions and hash verification. Path traversal and attachment symlinks are rejected. Limits are 10 MiB per file and per email attachment batch, up to eight email attachments, and a 100 MiB local attachment store. Archive old attachments locally when it fills. Files are never executed or unpacked automatically. WhatsApp outbound images have a 5 MiB limit; supported outbound formats are TXT, PDF, Office documents, JPEG/PNG, and supported audio/video types. One file is sent per WhatsApp `send` command. Archives and arbitrary binaries are not advertised as WhatsApp-supported documents.

Readable UTF-8 file excerpts enter the bounded task context, together with an exact local file reference. Binary files remain available to a capable CLI/MCP tool. Squad does **not** automatically OCR images, transcribe voice notes, or add vision support to a text-only model. The worker prompt requires it to explain those limits honestly. File replies are explicit local `send --file` operations; agents do not autonomously upload arbitrary project files during read-only chat.

Conversation history is isolated by channel/account and email sender/thread or WhatsApp user/group. Each requested agent gets a separate bounded read-only chat task. Existing vendor MCP read-only filters still apply. Agents with no enforceable read-only harness are listed but cannot silently gain write permission; run `squad doctor` to address them. Discovery detects executables/configuration, not valid vendor login or usable model access. It does not install unknown harnesses or scan model weights.

Listeners scan on startup when `auto_discover_agents` is true and reload alias additions during operation. `squad channels discover` performs an additive scan on demand. Explicit `--agent alias=REGISTERED_AGENT` bindings select a manual alias set and disable automatic startup discovery for that channel. Removed executables do not delete saved agents. New environment variables or PATH directories require restarting listeners. Changes to account, allowlists or authentication settings also require restart.

Incoming provider IDs are deduplicated. Chat task creation, budget allocation and route records commit together. Replies use a durable outbox. Interrupted/ambiguous sends become `uncertain` and are not automatically repeated, since the recipient may already have received them. Inspect status before explicitly sending again. Worker failure replies use Squad's fixed public error descriptions; raw CLI/provider logs remain local and can be inspected with `squad tasks --id TASK_ID --logs`.

Run each desired channel listener in its own terminal or service. Managed listeners own and stop only the workers they start. Workers use the shared broker's atomic task claims, even if the same agent has workers under multiple listeners. Worker lifetime limits remain 1,000 turns/24-hour idle; supervise/restart for continuous use.

## API references and verification

- [Meta's Cloud API collection: messages](https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ba8d099d-007e-4b52-b9f2-3cf3c60e4fbc)
- [Meta's Cloud API collection: media](https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ecb27be5-4d27-4763-bbee-6a8002c04bf3)
- [Meta webhook verification reference](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/) (archived SDK reference; Squad does not depend on that SDK)
- [WhatsApp Business Messaging Policy](https://business.whatsapp.com/policy), including the reply window
- [Meta Groups API](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)

Automated tests use SMTP/IMAP fixtures, signed loopback webhooks, real Squad worker subprocesses and a local HTTP model fixture. No real email, WhatsApp account, public reverse proxy, vendor login or paid model was exercised. Complete a live round trip with your accounts after setup.

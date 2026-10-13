# Telegram files

Squad 0.10.1 adds native Bot API file sending and incoming attachments. It uses your saved Telegram bot, group/user allowlists and project agents. No separate Telegram MCP account or configuration step is required.

## Send from Squad to Telegram

Run in your configured project, or add `--root "/path/to/project"`:

```bash
squad telegram send --file ./report.pdf
squad telegram send "Here is the report." --file ./report.pdf
```

The file goes to this project's configured Telegram chat. Add `--thread-id TOPIC_ID` for a forum topic, or `--reply-to MESSAGE_ID` to reply to a specific Telegram message. One file is sent per command, as a document, preserving its bytes. File captions can contain up to 1,000 UTF-16 characters (some emoji count as two). Text-only sends still support normal message chunking.

Files are copied into Squad's private attachment storage before being queued. Changing the original file afterward does not change an outstanding send. If staged bytes are changed or removed, validation fails before upload. `squad telegram status` shows the delivery result. An interrupted or ambiguous send is not automatically repeated. After checking the group, explicitly retry if needed:

```bash
squad telegram retry DELIVERY_ID
```

The delivery ID is printed by `send`/`status`. Retrying an uncertain send can duplicate a file already delivered.

## Send from Telegram to an agent

Keep the listener running:

```bash
squad telegram listen --with-workers
```

Attach a file and put the agent in its caption:

```text
@codex Review the attached code.
@claude Summarize this document.
/ask codex What does this file contain?
```

Alternatively, reply to an agent's message with an attachment. A captionless attachment needs that reply context or a saved default agent that is bound to an alias. An attachment with no target prompts you to choose one. Agent chat must be enabled; legacy mailbox-only mode does not silently create file tasks.

Documents, photos, audio, voice notes, video, animations and video notes are accepted. For photos, Squad downloads the largest supplied photo size; to preserve an original image exactly, send it as a document in Telegram. Files are handled individually. Albums are not automatically combined into a single agent request, so give each file its own caption or reply context.

Only authorized users in the configured chat can submit attachments. Bot messages and commands addressed to another bot do not download files or start tasks. Agents without supported read-only access do not gain permissions from an attachment. Replies remain in the same forum topic, and replying to the agent's answer keeps a reference to the original file within that chat/topic.

## Limits and interpretation

Squad caps files at **10 MiB** and shares a **100 MiB** attachment store with email/WhatsApp. Files are stored under `.squad/attachments/telegram/` with sanitized names, private permissions and hash verification. Archive old attachments locally if the store fills. Filenames cannot choose arbitrary destination paths; files are never automatically executed or unpacked.

Readable UTF-8 excerpts and exact local paths enter the bounded worker context. Binary interpretation requires suitable tools in the selected agent. File transfer alone does not add PDF parsing, OCR, image understanding or audio transcription to a text-only model. Agents must explain missing support instead of claiming to have inspected unread content.

Sending files back is an explicit local `send --file` operation. Read-only conversational agents do not autonomously upload arbitrary project files.

The Telegram Bot API supports larger platform limits, but Squad deliberately uses its existing common attachment limits. See [Telegram's file FAQ](https://core.telegram.org/bots/faq#how-do-i-download-files) and [sendDocument](https://core.telegram.org/bots/api#senddocument).

Validation uses fake Bot API responses, multipart payload checks, database migration/recovery checks and a real worker subprocess calling a loopback model server. No real Telegram files were uploaded or downloaded during development.

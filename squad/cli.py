"""squad — open team mode for any coding LLM."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, engines, playbooks, store
from .locking import serialized
from .broker import Broker, identifier
from .project_check import print_project_check
from .diagnostics import print_setup_checks


BANNER = """
  squad  ·  open team mode
  any lab, one board, no shared chat
""".strip(
    "\n"
)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="squad",
        description="Team mode for Grok, Claude, Codex, agy, Qwen, Llama, DeepSeek, GLM, Mistral, Kimi, and any OpenAI-compatible or Ollama model.",
    )
    parser.add_argument("--version", action="version", version=f"squad {__version__}")
    parser.add_argument('--root', type=Path, default=argparse.SUPPRESS,
                        help='Use this exact project folder (default: discover from the current directory)')
    parent = argparse.ArgumentParser(add_help=False)
    # Nested parsers must not replace an explicitly supplied parent --root with
    # their own default. Accept the option before or after any subcommand.
    parent.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="Check which engines are ready", parents=[parent])

    p_init = sub.add_parser("init", help="Create .squad/ + TEAM.md in this repo", parents=[parent])

    p_run = sub.add_parser("run", help="Run a playbook against a goal", parents=[parent])
    p_run.add_argument("goal", help="What the team should do")
    p_run.add_argument(
        "--mode",
        choices=playbooks.PLAYBOOKS,
        default="council",
        help="council (default), relay, fanout, or solo",
    )
    p_run.add_argument("--write", action="store_true", help="Allow file edits (relay/solo)")
    p_run.add_argument("--dry-run", action="store_true", help="Print commands, do not call vendors")
    p_run.add_argument("--only", default=None, help="Force every role onto one engine key")
    p_run.add_argument("--timeout", type=int, default=None)

    p_assign = sub.add_parser("assign", help="Send one packet to one role", parents=[parent])
    p_assign.add_argument("goal")
    p_assign.add_argument("--role", choices=engines.ROLES, default="commander")
    p_assign.add_argument("--engine", default=None, help="Engine key (builtin, preset, or custom)")
    p_assign.add_argument("--write", action="store_true")
    p_assign.add_argument("--dry-run", action="store_true")
    p_assign.add_argument("--timeout", type=int, default=None)
    p_assign.add_argument("--continue", dest="cont", action="store_true", help="Resume the vendor's last session instead of starting a new chat")
    p_assign.add_argument("--session", default=None, help="Resume a specific vendor session id")

    p_board = sub.add_parser("board", help="Print the shared board", parents=[parent])

    p_roles = sub.add_parser("roles", help="Show or set role → engine map", parents=[parent])
    p_roles.add_argument("--set", nargs=2, metavar=("ROLE", "ENGINE"), action="append")

    p_eng = sub.add_parser("engines", help="List, add, or drop engines", parents=[parent])
    eng_sub = p_eng.add_subparsers(dest="eng_cmd")
    eng_sub.add_parser("list", help="List roster + presets (default)", parents=[parent])
    p_add = eng_sub.add_parser("add", help="Add a preset or a custom engine", parents=[parent])
    p_add.add_argument("name", help="Preset name or new engine key")
    p_add.add_argument("--transport", choices=("cli", "openai", "ollama", "custom"))
    p_add.add_argument("--model", default="")
    p_add.add_argument("--base-url", default="")
    p_add.add_argument("--api-key-env", default="")
    p_add.add_argument("--display", default="")
    p_add.add_argument("--vendor", default="")
    p_add.add_argument("--binary", action="append", default=[], help="CLI binary (repeatable)")
    p_add.add_argument("--argv", nargs="*", default=None, help="custom transport argv, use {prompt} {cwd} {model}")
    p_drop = eng_sub.add_parser("drop", help="Remove a custom engine from config", parents=[parent])
    p_drop.add_argument("name")

    p_auth = sub.add_parser(
        "auth",
        help="How each engine logs in — browser session vs API key",
        parents=[parent],
    )

    p_note = sub.add_parser("note", help="Append a board log entry without spawning an engine", parents=[parent])
    p_note.add_argument("text")
    p_note.add_argument("--by", default="human")
    p_note.add_argument("--heading", default="note")

    p_own = sub.add_parser("own", help="Claim or release a path/stream on the board", parents=[parent])
    p_own.add_argument("path")
    p_own.add_argument("--by", required=True)
    p_own.add_argument("--note", default="")
    p_own.add_argument("--drop", action="store_true")

    p_lock = sub.add_parser("lock", help="Take a named lock (tree, build, editor…)", parents=[parent])
    p_lock.add_argument("resource")
    p_lock.add_argument("--by", required=True)
    p_lock.add_argument("--why", default="")

    p_unlock = sub.add_parser("unlock", help="Release a named lock", parents=[parent])
    p_unlock.add_argument("resource")
    p_unlock.add_argument("--by", default="")

    p_brief = sub.add_parser("brief", help="Print a pasteable brief for an interactive agent", parents=[parent])
    p_brief.add_argument("--for", dest="for_engine", default="any")

    p_status = sub.add_parser("status", help="Ownership, locks, pending, inbox", parents=[parent])

    p_say = sub.add_parser("say", help="Drop a message into a live agent's mailbox", parents=[parent])
    p_say.add_argument("text")
    p_say.add_argument("--to", required=True, help="Role or engine: claude, codex, grok, agy, …")
    p_say.add_argument("--from", dest="sender", default="human")

    p_mail = sub.add_parser("mail", help="Read mailbox for a live session", parents=[parent])
    p_mail.add_argument("--as", dest="who", required=True)
    p_mail.add_argument("--all", action="store_true", help="Include already-read mail")
    p_mail.add_argument("--peek", action="store_true", help="Do not mark as read")

    p_reply = sub.add_parser("reply", help="Live agent replies: board note + optional mail back", parents=[parent])
    p_reply.add_argument("text")
    p_reply.add_argument("--as", dest="who", required=True)
    p_reply.add_argument("--to", default="human")

    p_company = sub.add_parser('company', help='Register a model company', parents=[parent])
    p_company.add_argument('name')
    p_company.add_argument('--goal', default='')
    p_agent = sub.add_parser('agent', help='Register an agent within a company', parents=[parent])
    p_agent.add_argument('name')
    p_agent.add_argument('--company', required=True)
    p_agent.add_argument('--engine', required=True)
    p_agent.add_argument('--capability', action='append', default=[])
    sub.add_parser('roster', help='Show companies and agents', parents=[parent])
    p_task = sub.add_parser('task', help='Create a user mission or child task', parents=[parent])
    p_task.add_argument('goal')
    p_task.add_argument('--assignee')
    p_task.add_argument('--parent')
    p_task.add_argument('--requester', default='human')
    p_task.add_argument('--depends-on', action='append', default=[])
    p_task.add_argument('--capability', action='append', default=[])
    p_task.add_argument('--budget', type=int, default=12000)
    p_task.add_argument('--mission-budget', type=int, default=60000)
    p_task.add_argument('--output-limit', type=int, default=2000)
    p_task.add_argument('--write', action='store_true')
    p_tasks = sub.add_parser('tasks', help='List durable tasks or inspect one', parents=[parent])
    p_tasks.add_argument('--id')
    p_tasks.add_argument('--state')
    p_tasks.add_argument('--limit', type=int, default=50)
    p_tasks.add_argument('--logs', action='store_true', help='Show local engine error details for --id TASK_ID')
    p_worker = sub.add_parser('worker', help='Execute one eligible task; repeat via your supervisor', parents=[parent])
    p_worker.add_argument('--agent', required=True)
    p_worker.add_argument('--allow-write', action='store_true')
    p_worker.add_argument('--timeout', type=int)
    p_worker.add_argument('--watch', action='store_true', help='Poll until task/idle limits are reached')
    p_worker.add_argument('--max-tasks', type=int, default=20)
    p_worker.add_argument('--idle-timeout', type=float, default=60)
    p_worker.add_argument('--poll-interval', type=float, default=2)
    p_worker.add_argument('--chat-only', action='store_true', help='Process group conversation turns only')
    p_review = sub.add_parser('review', help='Accept a submitted task or request bounded rework', parents=[parent])
    p_review.add_argument('task_id')
    p_review.add_argument('--by', default='human')
    verdict = p_review.add_mutually_exclusive_group(required=True)
    verdict.add_argument('--accept', action='store_true')
    verdict.add_argument('--retry', action='store_true')
    p_review.add_argument('--note', default='')
    p_events = sub.add_parser('events', help='Read events after a cursor', parents=[parent])
    p_events.add_argument('--after', type=int, default=0)
    p_events.add_argument('--limit', type=int, default=50)
    sub.add_parser('budgets', help='Show reserved and charged mission tokens', parents=[parent])
    p_mcp = sub.add_parser('mcp', help='Serve Squad over MCP stdio for a bound agent', parents=[parent])
    p_mcp.add_argument('action', nargs='?', choices=['serve', 'config', 'inspect', 'proxy', 'proxy-config'], default='serve')
    p_mcp.add_argument('--agent')
    p_mcp.add_argument('--allow-write', action='store_true')
    p_mcp.add_argument('--server', action='append', default=[], help='Configured outgoing server to inspect')
    p_mcp.add_argument('--engine', help='Inspect only this engine\'s selected MCP servers')

    p_tg = sub.add_parser('telegram', help='Configure/send/listen to the Telegram bridge', parents=[parent])
    tg = p_tg.add_subparsers(dest='telegram_cmd', required=True)
    p_tgsetup = tg.add_parser('setup', parents=[parent], help='Configure Telegram and discover available agents together')
    p_tgsetup.add_argument('--chat-id', type=int)
    p_tgsetup.add_argument('--user-id', type=int, action='append')
    p_tgsetup.add_argument('--token-env')
    p_tgsetup.add_argument('--allow-tasks', action='store_true', default=None)
    p_tgdiscover = tg.add_parser('discover', parents=[parent], help='Add newly installed/configured engines and merge their aliases')
    p_tgdiscover.add_argument('--dry-run', action='store_true', help='Preview discovery without changing the project')
    p_tgi = tg.add_parser('identify', parents=[parent], help='Show chat/user IDs from messages to your bot without acknowledging them')
    p_tgi.add_argument('--token-env', default='SQUAD_TELEGRAM_BOT_TOKEN')
    p_tgc = tg.add_parser('configure', parents=[parent])
    p_tgc.add_argument('--chat-id', type=int, required=True)
    p_tgc.add_argument('--user-id', type=int, action='append', required=True)
    p_tgc.add_argument('--token-env', default='SQUAD_TELEGRAM_BOT_TOKEN')
    p_tgc.add_argument('--default-agent')
    p_tgc.add_argument('--allow-tasks', action='store_true')
    p_tgc.add_argument('--task-budget', type=int, default=6000)
    p_tgc.add_argument('--max-pending-tasks', type=int, default=5)
    p_tgl = tg.add_parser('listen', parents=[parent])
    p_tgl.add_argument('--once', action='store_true')
    p_tgl.add_argument('--poll-timeout', type=int, default=20)
    p_tgl.add_argument('--with-workers', action='store_true', help='Run one read-only chat worker per configured agent')
    p_tgs = tg.add_parser('send', parents=[parent])
    p_tgs.add_argument('text', nargs='?', default='')
    p_tgs.add_argument('--file', type=Path, help='Send one local file as a Telegram document (10 MiB limit)')
    p_tgs.add_argument('--thread-id', type=int, default=0, help='Forum topic ID')
    p_tgs.add_argument('--reply-to', type=int, help='Telegram message ID to reply to')
    tg.add_parser('status', parents=[parent])
    p_tgrec = tg.add_parser('recipient', parents=[parent], help='Register a destination for agent-proposed deliveries')
    p_tgrec.add_argument('alias')
    p_tgrec.add_argument('--chat-id', type=int, required=True)
    p_tgrec.add_argument('--mcp-server', help='Use this configured Telegram account MCP server instead of the bot')
    p_tgr = tg.add_parser('retry', parents=[parent], help='Explicitly retry a failed/uncertain send; may duplicate a delivered message')
    p_tgr.add_argument('delivery_id')

    p_tgchat = tg.add_parser('chat', help='Configure group conversations and agent aliases', parents=[parent])
    tgc = p_tgchat.add_subparsers(dest='telegram_chat_cmd', required=True)
    tgcc = tgc.add_parser('configure', parents=[parent])
    tgcc.add_argument('--agent', action='append', default=[], metavar='ALIAS=AGENT_ID')
    tgcc.add_argument('--context-turns', type=int, default=6)
    tgc.add_parser('disable', parents=[parent])

    p_tgm = tg.add_parser('mcp', help='Connect chigwell/telegram-mcp (Telegram user account)', parents=[parent])
    tgm = p_tgm.add_subparsers(dest='telegram_mcp_cmd', required=True)
    tgm_config = tgm.add_parser('configure', parents=[parent])
    tgm_config.add_argument('--checkout', type=Path, required=True)
    tgm_config.add_argument('--engine', action='append', required=True)
    tgm_config.add_argument('--chat-id', type=int, action='append', default=[])
    tgm_config.add_argument('--allow-send', action='store_true', help='Enable send/reply for write-authorized tasks only')
    tgm_config.add_argument('--allow-files', action='store_true', help='Enable bounded project file sends through Telegram MCP')
    tgm_config.add_argument('--name', default='telegram-account')
    tgm_config.add_argument('--uv', default='uv')
    tgm_status = tgm.add_parser('status', parents=[parent])
    tgm_status.add_argument('--name', default='telegram-account')

    p_quality = sub.add_parser('quality', help='no-ai-slop policy and handoff checks', parents=[parent])
    qs = p_quality.add_subparsers(dest='quality_cmd', required=True)
    qp = qs.add_parser('policy', parents=[parent]); qp.add_argument('--full', action='store_true')
    qc = qs.add_parser('check', parents=[parent])
    qc.add_argument('file', type=Path)
    qc.add_argument('--handoff', action='store_true', help='Validate the structured task-result fields')
    qc.add_argument('--strict', action='store_true', help='Return nonzero for writing findings too')
    qcfg = qs.add_parser('configure', parents=[parent])
    qcfg.add_argument('--mode', choices=['off', 'warn', 'strict'], required=True)

    from .channel_cli import add_parsers
    add_parsers(sub, parent)
    args = parser.parse_args(argv)
    selected_root = getattr(args, 'root', None)
    root = selected_root.expanduser().resolve() if selected_root is not None else store.find_root(Path.cwd())
    if not root.is_dir() and args.cmd != 'init':
        raise ValueError(f'Project directory does not exist: {root}')
    if args.cmd == "doctor":
        return cmd_doctor(root)
    if args.cmd == 'telegram':
        return cmd_telegram(root, args)
    if args.cmd in {'email', 'whatsapp', 'channels'}:
        from .channel_cli import run
        return run(root, args)
    if args.cmd == 'quality':
        return cmd_quality(root, args)
    if args.cmd in {'company', 'agent', 'roster', 'task', 'tasks', 'worker', 'review', 'events', 'budgets', 'mcp'}:
        return cmd_federation(root, args)
    if args.cmd == "init":
        return cmd_init(root)
    if args.cmd == "run":
        return cmd_run(root, args)
    if args.cmd == "assign":
        return cmd_assign(root, args)
    if args.cmd == "board":
        return cmd_board(root)
    if args.cmd == "roles":
        return cmd_roles(root, args)
    if args.cmd == "engines":
        return cmd_engines(root, args)
    if args.cmd == "auth":
        return cmd_auth(root)
    if args.cmd == "note":
        return cmd_note(root, args)
    if args.cmd == "own":
        return cmd_own(root, args)
    if args.cmd == "lock":
        return cmd_lock(root, args)
    if args.cmd == "unlock":
        return cmd_unlock(root, args)
    if args.cmd == "brief":
        return cmd_brief(root, args)
    if args.cmd == "status":
        return cmd_status(root)
    if args.cmd == "say":
        return cmd_say(root, args)
    if args.cmd == "mail":
        return cmd_mail(root, args)
    if args.cmd == "reply":
        return cmd_reply(root, args)
    parser.error("unknown command")
    return 2


def cmd_doctor(root: Path) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    print(BANNER)
    print()
    rows = engines.doctor(cfg)
    width = max(len(r["display"]) for r in rows)
    ready_n = 0
    print("  roster")
    for r in rows:
        mark = "ok " if r["ready"] else "—  "
        extra = "  [custom]" if r["custom"] else ""
        print(
            f"  {mark} {r['display']:<{width}}  {r['key']:<14}  {r['transport']:<7}  {r['detail']}{extra}"
        )
        if r["ready"]:
            ready_n += 1
    print()
    print("  presets you can add: " + ", ".join(sorted(engines.PRESETS)))
    print()
    print(f"{ready_n}/{len(rows)} engines look callable.")
    print("CLI engines reuse the browser / subscription login already on this machine.")
    print("Run `squad auth` for the exact login command per vendor.")
    print("API presets still need an env key. Missing CLIs work with --dry-run.")
    print_project_check(root, discover=False)
    print_setup_checks(root, cfg)
    return 0


def _preflight(root: Path, cfg: dict, needed: set[str], write: bool) -> list[str]:
    warnings = []
    table = engines.registry(cfg)
    for key in sorted(needed):
        if key not in table:
            warnings.append(f"{key}: unknown engine")
            continue
        spec = table[key]
        ready, detail = engines.available(spec)
        if not ready:
            warnings.append(f"{key}: not ready ({detail})")
            continue
        if spec.transport == "cli":
            session = engines.session_status(spec)
            if "no session dir" in session:
                warnings.append(
                    f"{key}: binary on PATH but no login dir — run `{spec.binaries[0] if spec.binaries else key}` once and sign in"
                )
    state = store.load_state(root)
    if write and state.get("locks"):
        holders = ", ".join(
            f"{k}={v if isinstance(v, str) else v.get('holder')}"
            for k, v in state["locks"].items()
        )
        warnings.append(f"write requested while locks held: {holders}")
    inbox = store.squad_dir(root) / "inbox"
    if inbox.exists():
        pending = [p.name for p in inbox.iterdir() if p.is_file()]
        if pending:
            warnings.append(f"{len(pending)} unmerged inbox note(s) in .squad/inbox/")
    return warnings


def cmd_note(root: Path, args) -> int:
    store.init_workspace(root)
    path = store.append_board(root, f"{args.heading} / {args.by}", args.text)
    print(f"appended → {path}")
    return 0


@serialized
def cmd_own(root: Path, args) -> int:
    store.init_workspace(root)
    state = store.load_state(root)
    if args.drop:
        state["ownership"].pop(args.path, None)
    else:
        state["ownership"][args.path] = {
            "owner": args.by,
            "note": args.note,
            "since": store.utc_now(),
        }
    store.save_state(root, state)
    store.append_board(
        root,
        f"own / {args.by}",
        f"{'dropped' if args.drop else 'claimed'} `{args.path}`" + (f" — {args.note}" if args.note else ""),
    )
    print(json.dumps(state["ownership"], indent=2))
    return 0


@serialized
def cmd_lock(root: Path, args) -> int:
    from .locking import workspace_lock
    with workspace_lock(root, "writer", blocking=False):
        return _cmd_lock(root, args)


def _cmd_lock(root: Path, args) -> int:
    store.init_workspace(root)
    state = store.load_state(root)
    existing = state["locks"].get(args.resource)
    if existing:
        holder = existing if isinstance(existing, str) else existing.get("holder")
        if holder and holder != args.by:
            print(f"lock {args.resource} already held by {holder}", file=sys.stderr)
            return 1
    state["locks"][args.resource] = {
        "holder": args.by,
        "since": store.utc_now(),
        "why": args.why,
    }
    store.save_state(root, state)
    store.append_board(root, f"lock / {args.by}", f"`{args.resource}` — {args.why or 'no reason'}")
    print(json.dumps(state["locks"], indent=2))
    return 0


@serialized
def cmd_unlock(root: Path, args) -> int:
    store.init_workspace(root)
    state = store.load_state(root)
    existing = state["locks"].get(args.resource)
    if not existing:
        print(f"no lock on {args.resource}", file=sys.stderr)
        return 1
    holder = existing if isinstance(existing, str) else existing.get("holder")
    if args.by and holder and args.by != holder:
        print(f"lock held by {holder}, not {args.by}", file=sys.stderr)
        return 1
    state["locks"].pop(args.resource)
    store.save_state(root, state)
    store.append_board(root, f"unlock / {args.by or holder}", f"`{args.resource}`")
    print(json.dumps(state["locks"], indent=2))
    return 0


def cmd_status(root: Path) -> int:
    store.init_workspace(root)
    store.refresh_status_block(root)
    state = store.load_state(root)
    print(json.dumps(state, indent=2))
    inbox = store.squad_dir(root) / "inbox"
    if inbox.exists():
        files = sorted(p.name for p in inbox.iterdir() if p.is_file())
        if files:
            print("\ninbox:")
            for name in files:
                print(f"  {name}")
    return 0


def cmd_brief(root: Path, args) -> int:
    from . import quality
    store.init_workspace(root)
    cfg = store.load_config(root)
    who = args.for_engine
    unread = store.read_mail(root, who, unread_only=True, mark=False)
    print(
        "\n".join(
            [
                f"You are the {who} agent on this repo.",
                "You are in a live interactive session. Stay in it.",
                "Shared memory is .squad/BOARD.md, .squad/state.json, and .squad/mail/.",
                f"Every turn start with: squad mail --as {who}",
                "Then read the status block at the top of .squad/BOARD.md.",
                f"Unread mail waiting: {len(unread)}",
                "When the human (or another agent) wants you, they run: squad say --to " + who + " '…'",
                "Reply in-session, then: squad reply --as " + who + " --to human '…'",
                "Claim work: squad own <path> --by " + who + " --note '…'",
                "Before a write or Unreal build: squad lock tree --by " + who + " --why '…'",
                "When you stop: squad unlock tree --by " + who + " && squad note --by " + who + " 'handoff: …'",
                "Do not edit files owned by someone else unless the board records a handoff.",
                "squad assign --continue talks to your last *vendor* session. It still is not this chat unless you read the mail.",
                f"Role map: {json.dumps(cfg.get('roles') or {})}",
                quality.policy(cfg),
            ]
        )
    )
    return 0


def cmd_say(root: Path, args) -> int:
    store.init_workspace(root)
    msg = store.post_mail(root, to=args.to, sender=args.sender, text=args.text)
    print(json.dumps(msg, indent=2))
    print(f"\nLive {args.to} session: run  squad mail --as {args.to}")
    return 0


def cmd_mail(root: Path, args) -> int:
    store.init_workspace(root)
    rows = store.read_mail(
        root,
        args.who,
        unread_only=not args.all,
        mark=not args.peek,
    )
    if not rows:
        print(f"no {'mail' if args.all else 'unread mail'} for {args.who}")
        return 0
    for item in rows:
        print(f"[{item.get('ts')}] {item.get('from')} → {item.get('to')}")
        print(item.get("text") or "")
        print("---")
    print(f"{len(rows)} message(s). Reply with: squad reply --as {args.who} '…'")
    return 0


def cmd_reply(root: Path, args) -> int:
    store.init_workspace(root)
    store.append_board(root, f"reply / {args.who} → {args.to}", args.text)
    if args.to and args.to != "board":
        store.post_mail(root, to=args.to, sender=args.who, text=args.text, board=False)
    print("recorded")
    return 0


def cmd_auth(root: Path) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    mode = ((cfg.get("auth") or {}).get("mode")) or "browser"
    print(BANNER)
    print()
    print(f"auth mode: {mode}")
    print("squad runs as you, with your $HOME. It does not open a new browser.")
    print("Log in once in each vendor TUI; after that, `squad run` reuses the session.")
    print()
    for spec in engines.registry(cfg).values():
        if spec.transport != "cli":
            continue
        ready, detail = engines.available(spec)
        hint = engines.LOGIN_HINTS.get(spec.key) or engines.LOGIN_HINTS.get(spec.family) or ""
        mark = "ok" if ready else "—"
        print(f"  {mark}  {spec.key:<10}  {detail}")
        if hint:
            print(f"      {hint}")
        print()
    print("Do not set claude_bare=true unless you intend to use ANTHROPIC_API_KEY.")
    print("CI / SSH without a keyring: use each vendor's device-code login, not a fresh browser.")
    return 0


def cmd_init(root: Path) -> int:
    paths = store.init_workspace(root)
    print(f"Initialized squad in {root}")
    for key, path in paths.items():
        print(f"  {key}: {path}")
    print_project_check(root, discover=False)
    print_setup_checks(root)
    return 0


def cmd_run(root: Path, args) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    table = engines.registry(cfg)
    if args.only and args.only not in table:
        print(f"unknown engine: {args.only}", file=sys.stderr)
        return 2
    dry = bool(args.dry_run)
    needed = set(cfg["roles"].values())
    if args.only:
        needed = {args.only}
    if args.mode == "fanout":
        needed = {args.only or cfg["roles"][r] for r in ("scout", "runner", "commander")}
    if args.mode == "solo":
        needed = {args.only or cfg["roles"]["commander"]}
    write = bool(args.write)
    if write and args.mode in {"council", "fanout"}:
        print("council/fanout stay read-only; ignoring --write", file=sys.stderr)
        write = False
    warns = _preflight(root, cfg, needed, write)
    for w in warns:
        print(f"preflight: {w}", file=sys.stderr)
    if not dry:
        ready = {r["key"] for r in engines.doctor(cfg) if r["ready"]}
        missing = sorted(needed - ready)
        if missing:
            print(
                f"Engines not ready: {', '.join(missing)}. "
                "Re-run with --dry-run, install the CLI, or log in (`squad auth`).",
                file=sys.stderr,
            )
            return 1
    print(BANNER)
    print()
    print(f"root   {root}")
    print(f"mode   {args.mode}")
    print(f"write  {write}")
    print(f"dry    {dry}")
    print(f"goal   {args.goal}")
    print()
    summary = playbooks.run_playbook(
        root,
        args.goal,
        args.mode,
        write=write,
        dry_run=dry,
        only=args.only,
        timeout=args.timeout,
    )
    print(json.dumps(summary, indent=2))
    print()
    print(f"artifacts: {summary['path']}")
    print("board:     ", store.squad_dir(root) / store.BOARD_NAME)
    return 0 if summary["ok"] else 1


def cmd_assign(root: Path, args) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    engine = args.engine or cfg["roles"][args.role]
    try:
        spec = engines.resolve(engine, cfg)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2
    ready, _ = engines.available(spec)
    dry = bool(args.dry_run)
    if not dry and not ready:
        print(f"Engine {engine} is not ready; use --dry-run explicitly for a preview", file=sys.stderr)
        return 1
    for w in _preflight(root, cfg, {engine}, bool(args.write)):
        print(f"preflight: {w}", file=sys.stderr)
    run_dir = store.new_run(root, args.goal, f"assign:{args.role}")
    prompt = playbooks.packet_prompt(
        role=args.role,
        engine=engine,
        goal=args.goal,
        mode="assign",
        write=bool(args.write),
        board=store.read_board(root),
        extra="Handle only this assignment.",
        config=cfg,
    )
    (run_dir / f"{args.role}-{engine}.prompt.md").write_text(prompt + "\n", encoding="utf-8")
    result = engines.run_engine(
        engine,
        prompt,
        cwd=root,
        write=bool(args.write),
        timeout=args.timeout or int(cfg.get("timeout_seconds") or 900),
        dry_run=dry,
        config=cfg,
        resume=args.session,
        cont=bool(args.cont),
    )
    if not dry:
        from . import quality
        result['quality'] = quality.evaluate(result.get('stdout') or '', cfg)
        if result['quality']['blocked']:
            result.update(ok=False, returncode=1, stderr='Handoff quality gate failed; see quality.errors')
    if args.session:
        store.remember_session(root, engine, args.session)
    path = store.write_result(run_dir, engine, args.role, result)
    path.with_suffix('.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    store.append_board(
        root,
        f"assign {args.role}/{engine}",
        f"Run `{run_dir.name}`; ok={result['ok']}; dry_run={dry}; artifact={path.relative_to(root)}",
    )
    print(result.get("stdout") or result.get("stderr") or "")
    if (result.get('quality') or {}).get('blocked'):
        print(json.dumps(result['quality'], indent=2), file=sys.stderr)
    print(f"\nartifact: {path}")
    return 0 if result.get("ok") else 1


def cmd_board(root: Path) -> int:
    text = store.read_board(root, tail_chars=100_000)
    if not text:
        print("No board yet. Run `squad init`.")
        return 1
    print(text)
    return 0


@serialized
def cmd_roles(root: Path, args) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    table = engines.registry(cfg)
    if args.set:
        roles = dict(cfg["roles"])
        for role, engine in args.set:
            if role not in engines.ROLES:
                print(f"unknown role: {role}", file=sys.stderr)
                return 2
            if engine not in table:
                print(f"unknown engine: {engine}", file=sys.stderr)
                return 2
            roles[role] = engine
        cfg["roles"] = roles
        store.save_config(root, cfg)
    print(json.dumps(cfg["roles"], indent=2))
    return 0


@serialized
def cmd_engines(root: Path, args) -> int:
    store.init_workspace(root)
    cfg = store.load_config(root)
    action = args.eng_cmd or "list"
    if action == "list":
        return _list_engines(cfg)
    if action == "add":
        return _add_engine(root, cfg, args)
    if action == "drop":
        extras = dict(cfg.get("engines") or {})
        if args.name not in extras:
            print(f"{args.name} is not a custom engine in this config", file=sys.stderr)
            return 1
        extras.pop(args.name)
        cfg["engines"] = extras
        store.save_config(root, cfg)
        print(f"dropped {args.name}")
        return 0
    return _list_engines(cfg)


def _list_engines(cfg: dict) -> int:
    print("built-in / configured")
    for row in engines.doctor(cfg):
        flag = "ready" if row["ready"] else "wait"
        print(f"  {row['key']:<16} {flag:<5}  {row['transport']:<7}  {row['display']}")
    print()
    print("presets (`squad engines add NAME`)")
    for name, preset in engines.PRESETS.items():
        print(
            f"  {name:<16} {preset['transport']:<7}  {preset['display']}  "
            f"({preset.get('api_key_env') or preset.get('model')})"
        )
    print()
    print("Any other lab:  squad engines add NAME --transport openai --base-url URL --model ID --api-key-env ENV")
    print("Local weights:  squad engines add NAME --transport ollama --model qwen2.5-coder")
    return 0


def _add_engine(root: Path, cfg: dict, args) -> int:
    extras = dict(cfg.get("engines") or {})
    name = identifier(args.name)
    if name in engines.PRESETS and not any(
        [args.transport, args.model, args.base_url, args.api_key_env, args.binary, args.argv]
    ):
        extras[name] = dict(engines.PRESETS[name])
        cfg["engines"] = extras
        store.save_config(root, cfg)
        print(f"added preset {name}")
        print(json.dumps(extras[name], indent=2))
        return 0

    if name in engines.PRESETS:
        data = dict(engines.PRESETS[name])
    elif name in engines.BUILTINS:
        spec = engines.BUILTINS[name]
        data = {
            "display": spec.display,
            "vendor": spec.vendor,
            "transport": spec.transport,
            "family": spec.family,
            "model": spec.model,
            "base_url": spec.base_url,
            "api_key_env": spec.api_key_env,
        }
    else:
        data = {
            "display": args.display or name,
            "vendor": args.vendor or "custom",
            "transport": args.transport or "openai",
            "family": "custom",
        }

    if args.transport:
        data["transport"] = args.transport
    if args.model:
        data["model"] = args.model
    if args.base_url:
        data["base_url"] = args.base_url
    if args.api_key_env:
        data["api_key_env"] = args.api_key_env
    if args.display:
        data["display"] = args.display
    if args.vendor:
        data["vendor"] = args.vendor
    if args.binary:
        data["binaries"] = list(args.binary)
    if args.argv is not None:
        data["argv"] = list(args.argv)
        data["transport"] = data.get("transport") or "custom"

    extras[name] = data
    cfg["engines"] = extras
    store.save_config(root, cfg)
    print(f"added {name}")
    print(json.dumps(data, indent=2))
    return 0


def cmd_federation(root: Path, args) -> int:
    broker = Broker(root)
    if args.cmd == 'mcp':
        from .mcp_bridge import create_server, ToolHub, proxy_config, serve_proxy
        if args.action in {'inspect', 'proxy', 'proxy-config'}:
            import asyncio
            cfg = store.load_config(root)
            selected = args.server or (cfg.get('engines', {}).get(args.engine, {}).get('mcp_servers', []) if args.engine else list(cfg.get('mcp_servers', {})))
            if not selected:
                raise ValueError('No MCP servers selected; configure mcp_servers and the engine allowlists')
            if args.action == 'proxy-config':
                for name in selected:
                    if name not in cfg.get('mcp_servers', {}):
                        raise ValueError(f'Unknown MCP server: {name}')
                print(json.dumps(proxy_config(root, selected, args.allow_write), indent=2))
                return 0
            if args.action == 'proxy':
                asyncio.run(serve_proxy(root, cfg, selected, args.allow_write))
                return 0
            async def inspect_servers():
                async with ToolHub(root, cfg, selected, args.allow_write) as hub:
                    return {'selected': selected, 'operations': hub.catalog,
                            'note': 'Only configured and permitted operations are exposed. Remote OAuth is not implemented.'}
            print(json.dumps(asyncio.run(inspect_servers()), indent=2))
            return 0
        if not args.agent:
            raise ValueError('MCP serve/config requires --agent')
        if args.action == 'config':
            if args.agent not in {a['id'] for a in broker.roster()['agents']}:
                raise ValueError('Register this agent first')
            arguments = ['-m', 'squad', 'mcp', '--root', str(root.resolve()), '--agent', args.agent]
            if args.allow_write:
                arguments.append('--allow-write')
            print(json.dumps({'mcpServers': {'squad': {'command': sys.executable, 'args': arguments}}}, indent=2))
            return 0
        create_server(root, args.agent, args.allow_write).run(transport='stdio')
        return 0
    if args.cmd == 'company':
        result = broker.company(args.name, args.goal)
    elif args.cmd == 'agent':
        engines.resolve(args.engine, store.load_config(root))
        result = broker.agent(args.name, args.company, args.engine, args.capability)
    elif args.cmd == 'roster':
        result = broker.roster()
    elif args.cmd == 'task':
        result = broker.create_task(args.goal, requester=args.requester, assignee=args.assignee,
                                    capabilities=args.capability, dependencies=args.depends_on,
                                    parent=args.parent, budget=args.budget, mission_budget=args.mission_budget,
                                    output_limit=args.output_limit, write=args.write)
    elif args.cmd == 'tasks':
        if args.logs and not args.id:
            raise ValueError('--logs requires --id TASK_ID')
        result = broker.task(args.id) if args.id else broker.tasks(args.state, args.limit)
        if args.logs:
            from .diagnostics import task_log
            result = {**result, 'run_log': task_log(root, result)}
    elif args.cmd == 'worker':
        from .worker import work_once
        if args.watch:
            import time
            if not 1 <= args.max_tasks <= 1000 or not 0.1 <= args.poll_interval <= 60 or not 0 < args.idle_timeout <= 86400:
                raise ValueError('Invalid worker polling limits')
            completed = 0
            idle_since = time.monotonic()
            failed = False
            while completed < args.max_tasks:
                item = work_once(root, args.agent, args.allow_write, args.timeout, kind='chat' if args.chat_only else None)
                if item.get('idle'):
                    if time.monotonic() - idle_since >= args.idle_timeout:
                        break
                    time.sleep(min(args.poll_interval, args.idle_timeout))
                    continue
                print(json.dumps(item), flush=True)
                failed = failed or item.get('state') == 'failed'
                completed += 1
                idle_since = time.monotonic()
            return 1 if failed else 0
        result = work_once(root, args.agent, args.allow_write, args.timeout, kind='chat' if args.chat_only else None)
    elif args.cmd == 'review':
        result = broker.review(args.task_id, args.by, args.accept, args.note)
    elif args.cmd == 'events':
        result = broker.events(args.after, args.limit)
    else:
        result = broker.budgets()
    print(json.dumps(result, indent=2))
    return 1 if isinstance(result, dict) and result.get('state') == 'failed' else 0


def cmd_quality(root: Path, args) -> int:
    from . import quality
    if args.quality_cmd == 'policy':
        print(quality.upstream() + '\n\n' + quality.upstream('eval.md') if args.full else quality.POLICY)
        return 0
    if args.quality_cmd == 'configure':
        cfg = store.load_config(root)
        cfg['quality'] = {'mode': args.mode}
        store.save_config(root, cfg)
        print(f'Quality mode: {args.mode}')
        return 0
    if args.file.stat().st_size > 2_000_000:
        raise ValueError('Quality input exceeds 2 MB')
    report = quality.inspect(args.file.read_text(encoding='utf-8'), args.handoff)
    print(json.dumps(report, indent=2))
    return int(not report['passed'] or (args.strict and bool(report['writing_findings'])))


def cmd_telegram(root: Path, args) -> int:
    import uuid
    from . import telegram
    from .locking import workspace_lock
    if args.telegram_cmd == 'recipient':
        from .telegram_actions import recipient
        print(json.dumps(recipient(root, args.alias, args.chat_id, args.mcp_server), indent=2))
        return 0
    if args.telegram_cmd == 'setup':
        from .onboarding import setup
        print(json.dumps(setup(root, chat_id=args.chat_id, user_ids=args.user_id,
                               token_env=args.token_env, allow_tasks=args.allow_tasks), indent=2))
        return 0
    if args.telegram_cmd == 'discover':
        from .onboarding import discover
        print(json.dumps(discover(root, dry_run=args.dry_run), indent=2))
        return 0
    if args.telegram_cmd != 'status':
        store.init_workspace(root)
    if args.telegram_cmd == 'chat':
        from .telegram_chat import configure
        print(json.dumps({**configure(root, getattr(args, 'agent', []),
            args.telegram_chat_cmd == 'configure', getattr(args, 'context_turns', 6)),
            **telegram.config_location(root)}, indent=2))
        return 0
    if args.telegram_cmd == 'mcp':
        from . import telegram_mcp
        if args.telegram_mcp_cmd == 'configure':
            result = telegram_mcp.configure(root, args.checkout, args.engine, args.chat_id,
                                            args.allow_send, args.name, args.uv, allow_files=args.allow_files)
        else:
            result = telegram_mcp.status(root, args.name)
        print(json.dumps({**result, **telegram.config_location(root)}, indent=2))
        return 0
    if args.telegram_cmd == 'identify':
        api = telegram.client({'token_env': args.token_env})
        updates = api.request('getUpdates', {'timeout': 0, 'limit': 100, 'allowed_updates': ['message']})
        candidates = set()
        for update in updates:
            message = update.get('message') or {}
            chat, sender = message.get('chat') or {}, message.get('from') or {}
            if type(chat.get('id')) is int and type(sender.get('id')) is int:
                candidates.add((chat['id'], sender['id']))
        print(json.dumps({'candidates': [{'chat_id': c, 'user_id': u} for c,u in sorted(candidates)],
            'next': 'Send your bot /start, run identify, then configure your own chat/user IDs. Stop other pollers while identifying.'}, indent=2))
        return 0
    if args.telegram_cmd == 'configure':
        proposed = {'chat_id': args.chat_id, 'allowed_user_ids': sorted(set(args.user_id)),
                    'token_env': args.token_env, 'default_agent': args.default_agent,
                    'allow_tasks': args.allow_tasks, 'task_budget': args.task_budget,
                    'max_pending_tasks': args.max_pending_tasks, 'mail_since': store.utc_now()}
        if args.default_agent and args.default_agent not in {a['id'] for a in Broker(root).roster()['agents']}:
            import shlex
            raise ValueError(f'Agent {args.default_agent} is not registered in {root}. '
                             f'Inspect this project with: squad roster --root {shlex.quote(str(root))}')
        # Validate before writing any configuration; no credentials needed here.
        telegram.validate_settings(proposed)
        with workspace_lock(root):
            cfg = store.load_config(root)
            for key in ('allow_chat', 'agent_aliases', 'chat_context_turns', 'chat_max_mentions', 'auto_discover_agents', 'recipients'):
                if key in cfg.get('telegram', {}):
                    proposed[key] = cfg['telegram'][key]
            telegram.validate_settings(proposed)
            cfg['telegram'] = proposed
            store.save_config(root, cfg)
        print(json.dumps({**proposed, **telegram.config_location(root),
                          'next': telegram.listen_command(root, proposed.get('allow_chat', False))}, indent=2))
        return 0
    if args.telegram_cmd == 'status':
        import os
        try:
            cfg = telegram.settings(root)
        except ValueError as exc:
            print(json.dumps({**telegram.config_location(root), 'configured': False,
                              'error': str(exc), 'deliveries': []}, indent=2))
            return 0
        deliveries = []
        actions = []
        # Return one JSON document, including delivery state, without a bot token.
        database = root / '.squad' / 'coordination.sqlite3'
        if database.is_file():
            import sqlite3
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_outbox'").fetchone():
                    deliveries = [dict(r) for r in db.execute('SELECT delivery_id,state,error FROM telegram_outbox ORDER BY rowid DESC LIMIT 50')]
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_actions'").fetchone():
                    actions = [dict(r) for r in db.execute('SELECT id,state,error FROM telegram_actions ORDER BY created DESC LIMIT 50')]
        print(json.dumps({**cfg, **telegram.config_location(root), 'configured': True,
                          'token_set': bool(os.environ.get(cfg['token_env'])), 'deliveries': deliveries, 'actions': actions}, indent=2))
        return 0
    cfg = telegram.settings(root)
    if args.telegram_cmd == 'listen':
        if cfg.get('auto_discover_agents'):
            from .onboarding import discover
            report = discover(root)
            if report['added_aliases']:
                print('Discovered Telegram aliases: ' + ', '.join('@' + alias for alias in report['added_aliases']), file=sys.stderr)
            cfg = telegram.settings(root)
        print('Telegram configuration: ' + telegram.config_location(root)['config_file'], file=sys.stderr)
    bridge = telegram.Bridge(root, cfg, telegram.client(cfg), live_config=args.telegram_cmd == 'listen')
    if args.telegram_cmd == 'listen':
        try:
            result = bridge.listen(args.once, args.poll_timeout, args.with_workers)
        except KeyboardInterrupt:
            print('Telegram listener stopped.', file=sys.stderr)
            return 130
        print(json.dumps(result, indent=2))
        return int(any(d['state'] in {'failed', 'uncertain'} for d in result))
    with workspace_lock(root, 'telegram-send', blocking=False):
        if args.telegram_cmd == 'send':
            delivery_id = 'manual:' + uuid.uuid4().hex
            if args.file:
                from .telegram_files import stage
                stage(bridge, args.file, delivery_id, args.text, args.thread_id, args.reply_to)
            else:
                from .broker import bounded_int
                bounded_int(args.thread_id, 'thread_id', 0, 2**53 - 1)
                if args.reply_to is not None:
                    bounded_int(args.reply_to, 'reply_to', 1, 2**53 - 1)
                bridge.queue(delivery_id, args.text, {'thread_id': args.thread_id, 'reply_to': args.reply_to})
        else:
            delivery_id = args.delivery_id
            with bridge.broker.db() as db:
                changed = db.execute("UPDATE telegram_outbox SET state='pending',available=0,error='' WHERE bridge=? AND delivery_id=? AND state IN ('failed','uncertain')", (bridge.key, delivery_id)).rowcount
            if not changed:
                raise ValueError('Delivery is not failed/uncertain; inspect telegram status')
        bridge.flush()
        result = [d for d in bridge.deliveries() if d['delivery_id'] == delivery_id or d['delivery_id'].startswith(delivery_id + ':')]
        print(json.dumps(result, indent=2))
        return int(not result or any(d['state'] != 'sent' for d in result))


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        print(f'squad: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

"""Telegram-shaped stdio fixture; never connects to Telegram."""
import json
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP('telegram-fixture')


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_messages(chat_id: int | str, page: int = 1, page_size: int = 20,
                 account: str | None = None) -> str:
    if page == 2:
        return 'x' * 20000
    return json.dumps({'chat': chat_id, 'messages': ['fixture message'],
                      'mode': os.getenv('TELEGRAM_EXPOSED_TOOLS'),
                      'credential_present': bool(os.getenv('TELEGRAM_API_HASH')),
                      'unrelated_key_present': bool(os.getenv('SQUAD_UNRELATED_SECRET'))})


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def send_message(chat_id: int | str, message: str) -> str:
    """Misleading read annotation: Squad's own write policy must win."""
    Path('telegram-fixture-sent').write_text(message)
    return 'fixture sent'


@server.tool()
def delete_chat(chat_id: int) -> str:
    raise AssertionError('This unselected tool must never be called')


if __name__ == '__main__':
    server.run(transport='stdio')

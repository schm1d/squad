"""Real stdio MCP fixture; only launched by integration tests."""
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP('fixture')

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> dict:
    """Add two integers."""
    return {'sum': a + b}

@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def mutate() -> str:
    """Intentionally misleading annotation: local policy must still block it."""
    Path('mutation-marker').write_text('mutated')
    return 'mutated'

@server.resource('fixture://notes')
def notes() -> str:
    return 'Repository notes: use the existing parser.'

@server.prompt()
def review_goal(goal: str) -> str:
    return 'Inspect evidence for: ' + goal

if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--http':
        server.settings.port = int(sys.argv[2])
        server.run(transport='streamable-http')
    else:
        server.run(transport='stdio')

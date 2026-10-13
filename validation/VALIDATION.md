[VALIDATION.md](https://github.com/user-attachments/files/33363448/VALIDATION.md)
# Repository import validation — 2026-10-08

- Source release: Squad 0.11.1; source archive SHA-256:
  `789760b0a2e15b9ea9383993c2d5fef370136cf03bea9fefb6b5fdbb104f4930`.
- All Python application, installer and test files match that release byte for byte.
- Third-party no-ai-slop rules and license match the release byte for byte.
- Repository changes: installation paths/documentation, root MIT license matching
  existing package metadata, ignore rules, and removal of demo workspace state and
  historical development logs/patches. Runtime behavior is unchanged.
- Python 3.12 isolated environment: `python -m pip install -e '.[mcp]'`
  succeeded with MCP SDK 1.30.0.
- `python -m unittest discover -s tests -v`: 203 tests passed, no skips,
  17.411 seconds, exit 0. Full output: `validation/unittest.txt`.
- `python -m squad --version`: `squad 0.11.1`, exit 0.
- Python AST parsing passed. A bounded scan found no GitHub/OpenAI/Telegram token
  patterns or private-key headers. This is not a comprehensive security audit.
- No live model calls, real Telegram/WhatsApp/email sends, or Unreal runs were made.

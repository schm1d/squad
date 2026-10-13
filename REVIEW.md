# Squad 0.11.1 repository import

This repository contains the application, installer, tests, examples and docs
from the released Squad 0.11.1 package. Its Python source and test files are
unchanged. Installation examples use the repository root.

The release archive's demonstration .squad workspace and historical development
patches/logs are excluded. Project state, environment files and Telegram session
files are ignored. Keep each user's actual project state outside this checkout.
Third-party no-ai-slop documents and their original license are preserved. The
root MIT license matches the existing pyproject.toml license declaration.

The original release validation reported 203 passing tests. See
validation/VALIDATION.md for verification of this prepared repository.

Known limitations remain documented in README.md and docs/. Importing the source
does not resolve native engine authentication, MCP approvals, service startup,
Unreal project identity, or automatic mission review/continuation.

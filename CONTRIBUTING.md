# Contributing

Keep changes inside the repository and never use real customer media, credentials, account identifiers or remote task IDs as fixtures.

## Setup

| Platform | Bootstrap | No-backend Doctor |
| --- | --- | --- |
| macOS / Linux preview | `./install` | `.venv/bin/python tools/doctor.py --backend none` |
| native Windows 11 x64 | `.\install.cmd` | `.\.venv\Scripts\python.exe .\tools\doctor.py --backend none` |

Windows development uses the native `.cmd` entrypoints. WSL/Git Bash and a permanent PowerShell execution-policy change are not prerequisites.

## Validation

| Platform | Full suite |
| --- | --- |
| macOS / Linux preview | `./video-replacer-test` |
| native Windows 11 x64 | `.\video-replacer-test.cmd` |

Also run `git diff --check` from the Git client used for the change.

Tests must remain deterministic and must not call Codex inference, video backends, TOS, account endpoints or paid operations. Use local fakes for media and process boundaries. New workflow behavior needs a failing regression test before implementation.

## Pull requests

- Explain the user-visible contract change and its safety impact.
- Preserve the explicit paid Gate and external recovery ledger.
- Update `README.md`, `ARCHITECTURE.md`, the operator skill or workflow contract only when their public behavior changes.
- Keep secrets in the process environment and add only placeholders to `.env.example`.
- Do not enable a persistent watcher or background service by default.

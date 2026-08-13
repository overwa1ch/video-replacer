# First-Run Setup

Complete setup immediately as part of the same Agent task that downloads or opens the repository. Do not defer required environment work to the first video job. The user may authorize a specific system or network change and complete provider login in the provider's own protected interface. Do not make the user run commands, edit JSON, edit env files, choose an internal profile, or troubleshoot the installation.

## 1. Establish the complete local runtime

From `REPO_ROOT`, detect the platform and architecture. The implementation support targets are macOS Apple Silicon and native Windows 11 x64; Linux remains preview. Reject an architecture mismatch instead of silently changing execution environments. On Windows, use native `.cmd` entrypoints and Windows paths, including when `REPO_ROOT` contains spaces or Chinese characters. Windows READY requires OpenAI's official modern native standalone `codex.exe` package; do not install or rely on npm `codex.cmd` / `codex.ps1` wrappers. Follow the current official Windows Codex installation method after obtaining any required system/network approval.

Do not accept a bare `.exe` name or suffix as provenance. Before the repository invokes Windows Codex for `--version`, node login, Doctor/model checks or a production prompt turn, its verifier must resolve the canonical `packages/standalone/releases/<version>-x86_64-pc-windows-msvc/` package, validate the plain-file/directory layout and `codex-package.json`, and derive the exact package version without executing it. It must then fetch only `https://releases.openai.com/codex/releases/<version>/release.json`, require the matching release tag and exactly one `codex-x86_64-pc-windows-msvc.exe` asset, and compare that asset's SHA-256 with a fresh hash of the actual `bin/codex.exe`. A legacy/nonofficial layout, changed or reparse-linked package entry, tag/asset/digest mismatch, offline host, or unavailable exact manifest fails closed before execution. Install or repair the current official modern standalone package instead of bypassing this Gate. Never pin a single current version or digest as a permanent trust rule.

Do not switch to WSL/Git Bash or permanently change PowerShell execution policy. The outer repository Agent may use Codex's official `windows.sandbox="elevated"` mode; that name identifies a Codex sandbox and does not authorize running the whole Agent as an OS administrator. The later inner prompt process uses `CODEX_EXEC_SERVER_URL=none` and the stable external node-only file-auth `CODEX_HOME`; it does not depend on a sandbox execution environment or initialize a second elevated sandbox/home. A one-time administrator approval may initialize the outer sandbox. A trusted system package manager may install missing Python 3.12, Node.js 20+, Git, FFmpeg, or Codex CLI only after the Agent explains the exact system/network change and obtains any required approval. Keep any separate UAC/administrator installer narrowly scoped, end it after the install, and continue inside the configured Codex sandbox.

Install the repository runtime and pinned local dependency needed by `mosaic_required`, then run the no-backend Doctor:

| Platform | Runtime install | Local Doctor |
| --- | --- | --- |
| macOS Apple Silicon / Linux preview | `./install --with-mosaic` | `.venv/bin/python tools/doctor.py --backend none --check-mosaic` |
| native Windows 11 x64 | `.\install.cmd --with-mosaic` | `.\.venv\Scripts\python.exe .\tools\doctor.py --backend none --check-mosaic` |

Resolve every failed required check yourself when it is safe and authorized. Never disable a check to reach READY. After the dedicated login below, Doctor must prove that the node home's authenticated Codex account exposes the workflow's fixed model through the reviewed CLI interface.

## 2. Configure the stable prompt-node Codex identity

Prompt-node authentication is separate from the outer conversation Agent's working home. The workflow owns `$VIDEO_REPLACER_STATE_DIR/codex-node-home/`, one stable external, instruction-free `CODEX_HOME` with strict file-based authentication and workflow lock bookkeeping. It must contain no `AGENTS.md`, rules, skills, plugins, MCP configuration, hooks or other user configuration. Do not copy the outer Agent's `auth.json` on every run and do not point production at the outer Agent's ordinary `CODEX_HOME`. Its `auth.json` must pass the bounded strict ChatGPT file-auth schema before Doctor or production uses it.

On Windows, every entrypoint uses the same state resolver. It obtains the real current user's `FOLDERID_LocalAppData` through the Known Folder API and fixes the state root at `LocalAppData/video-replacer`; environment-supplied `LOCALAPPDATA` cannot select a parent. `VIDEO_REPLACER_STATE_DIR` may be absent or canonically restate that exact path and cannot redirect it. This home must reside below that root on a local fixed drive. Reject redirects, UNC, mapped-drive, non-fixed-drive and reparse-point paths. The node-home directory, `auth.json` and `.video-replacer-auth.lock` must be owned by the current user and use inheritance-protected DACLs containing only current-user and `SYSTEM` full-control allow ACEs. Setup applies these ACLs and every credential-bearing path validates the live native owner/DACL. POSIX retains the documented absolute override and XDG behavior.

When the node home is not authenticated, the Agent starts its dedicated login through the platform launcher:

| Platform | Node Codex login |
| --- | --- |
| macOS Apple Silicon / Linux preview | `./video-replacer setup login-codex-node` |
| native Windows 11 x64 | `.\video-replacer.cmd setup login-codex-node` |

The Windows launcher completes the official package/digest provenance Gate before it starts the Codex login process. Wait while the user completes only the Codex account authorization in Codex's protected interface. Do not ask the user to run the command, copy a token, paste a credential, or edit the home. When an explicit login encounters a malformed stale regular `auth.json`, the launcher first locks the exact home and independently validates its path, structure and permission/ACL boundary, then rechecks the file's stable identity before removing only that auth file. A valid auth remains for Codex to replace or revoke. A link, nonregular auth, unsafe home or identity change fails before the subprocess starts. After login, strict file-auth validation and login status must pass. Doctor must validate login status and model availability by pointing at this exact stable home; production uses the same path. All prompt-node Codex processes are serialized so concurrent Jobs cannot race while refreshing or rewriting the shared file-auth tokens.

Doctor must also attest the real local request construction under the same stable home lock. It runs the exact production prompt command twice with the same home, a synthetic image and loopback Responses providers. Phase one uses `requires_openai_auth=false` and records required check `codex-wire`; require an attached image, empty top-level tools, no nonempty nested/additional tools, no multi-agent host hints and no authorization/API-key header. Phase two uses `requires_openai_auth=true` and records required check `codex-file-auth-wire`; require the same zero-tool image surface and exactly one Bearer handshake, then compare its in-memory SHA-256 with the strictly validated stable `auth.json` `access_token` digest through `hmac.compare_digest`. Retain and log neither raw token nor header value. Each local mock captures one model request and returns HTTP 418 before a model response or inference. These final-wire checks expose managed/system MCP, hook, tool or Agent-instruction reinjection and auth-store divergence; any mismatch blocks READY. They do not attest unrelated process network activity.

## 3. Configure the durable backend

The first public durable READY lane is the reviewed project-local Dreamina CLI. An existing CLI may prove that the account is already authorized, but it cannot become the persisted workflow binary:

- Explain that the next action downloads the pinned official Dreamina binary into ignored `.video-replacer/bin/`, obtain approval, then run `.venv/bin/python tools/install_dreamina.py` on macOS or `.\.venv\Scripts\python.exe .\tools\install_dreamina.py` on Windows. This installer verifies the repository-reviewed SHA-256 and does not execute the provider's remote shell installer, modify PATH, or install a global skill.
- When the project-local binary does not already see an authenticated account, start login only through the launcher's `setup login-dreamina` subcommand, represented as `[LAUNCHER, "setup", "login-dreamina"]`, and wait while the user completes OAuth/device authorization in Dreamina's own protected interface. The setup command verifies the pinned binary and launches it with the credential-minimized environment; never execute `dreamina` or `dreamina.exe` directly.
- Do not ask the user for profile IDs, endpoints, JSON fields, shell exports, commands, or credentials in chat.

The Agent owns configuration work. The user owns system-change consent and provider account consent. The Ark adapter is internal development and test code until a reviewed cross-session credential broker and no-cost account/model probe exist; it is not selectable by the public Skill or launcher, and environment-only Ark secrets must not be written as first-run READY.

## 4. Prove live READY

Choose the reviewed Dreamina profile matching the configured CLI and run its no-task online verification through the current platform launcher:

| Platform | Verify | Live status |
| --- | --- | --- |
| macOS Apple Silicon / Linux preview | `./video-replacer setup verify --profile dreamina_cli_seedance_2_5` | `./video-replacer setup status --json` |
| native Windows 11 x64 | `.\video-replacer.cmd setup verify --profile dreamina_cli_seedance_2_5` | `.\video-replacer.cmd setup status --json` |

Use `dreamina_cli_seedance_2_0` only when the available account or CLI requires that reviewed lane. Verification writes `.video-replacer/setup.json`. It contains the profile, code-contract digest, canonical non-secret tool paths and identities, timestamp, capabilities, and secret-free check statuses. On Windows, the Codex tool identity includes the just-proved `official_sha256` and `official_release_tag`; these are evidence for that exact installed release, not a permanent allowlist.

Finish only when the platform's live-status command performs current checks and returns `ready: true`.

`setup status` rechecks the reviewed binary hashes and interfaces, the production node `CODEX_HOME` strict file-auth login and model access, both zero-tool wire phases, Dreamina login, CLI model surface, non-basic account level and checked local prerequisites. On Windows, every live status validates the modern Codex package again, refetches the exact-version manifest from `releases.openai.com`, rehashes the actual `codex.exe`, and compares the resulting release tag/digest with setup identity before any Codex execution. It does not trust the `.exe` suffix or the recorded digest alone; loss of network proof returns `ready: false`. Doctor does not create a temporary auth copy for this proof; it checks the same stable external home that production will use. Both captured model requests carry a synthetic image to loopback and stop at HTTP 418 before a model response or inference; the authenticated phase only proves the local Bearer/file-auth match in memory. This wire evidence does not attest unrelated process network activity and contains no video-backend or paid command. Dreamina exposes no no-cost real-generation entitlement probe, so READY does not claim that a paid Seedance task has been executed successfully. READY also does not run the real sampled-frame prompt turn or prove that a particular batch will prepare successfully. A removed CLI, expired node login, changed tool or changed setup contract returns `ready: false`; repair it before finishing. Report the selected backend in ordinary language. Do not start a watcher during setup. If setup began while handling a replacement request, resume that request after READY.

---
name: video-replacer
description: "Install, configure, and operate repository-local video replacement jobs. Use when the user downloads, sets up, repairs, or first runs the repository, or asks to prepare, replace, mask faces in, generate references for, submit, resume, or track video tasks. In the same download task, make the clone live READY with its Agent-configured durable backend and checked local prerequisites; then use the repository launcher to build exact schema-v3 batches with backend_profile, privacy_mode, and the explicit paid Gate."
---

# Video Replacer Operator

Treat the Git repository containing this skill as the complete project. The conversation Agent owns intake and user intent; the workflow owns media analysis, prompt writing, optional mosaic processing, deterministic Gates, remote submission, recovery, and download.

## Resolve and verify the repository

Find the nearest ancestor containing `tools/video_batch_orchestrator.mjs` and the current platform's launcher; call it `REPO_ROOT`. Resolve `LAUNCHER` once and invoke it directly as an argument array so spaces and non-ASCII path characters remain one path:

- native Windows 11 x64: `REPO_ROOT\video-replacer.cmd`
- macOS Apple Silicon and Linux preview: `REPO_ROOT/video-replacer`

Do not substitute WSL or Git Bash on Windows, and do not ask for a permanent PowerShell execution-policy change. Use:

- runtime: `REPO_ROOT/workspace/video-loop/`
- outputs: `REPO_ROOT/outputs/video-replacements/`
- reusable local references: `REPO_ROOT/assets/reference-images/`
- external recovery ledger: the platform state directory resolved by the workflow

When the user's Agent task downloads or opens the repository for installation, proceed directly into setup; do not wait for a video request. Run `[LAUNCHER, "setup", "status", "--json"]` when the repository runtime exists. If it is missing or not live-ready, read [references/first-run-setup.md](references/first-run-setup.md) and complete that procedure in the same Agent task, including `[LAUNCHER, "setup", "login-codex-node"]` when the stable node-only strict-file-auth home is not authenticated. The user only completes provider account authorization; the Agent owns the command and repair work. Do not create a batch until setup reports `ready: true`. Do not install a global copy of this skill.

On native Windows, never infer Codex trust from the `.exe` suffix. Before any repository-triggered Codex execution, the setup/workflow Gate must validate the modern standalone package layout and `codex-package.json`, resolve that package's exact version, fetch only its exact `https://releases.openai.com/codex/releases/<version>/release.json`, and match the actual binary SHA-256 to the single `codex-x86_64-pc-windows-msvc.exe` asset. A nonofficial or legacy layout, mismatched tag/asset/digest, or unavailable online provenance is a fail-closed setup repair; do not invoke the binary, bypass the launcher, or substitute a hard-coded version/digest.

Run normal workflow commands only as:

```text
[LAUNCHER, <command>, ...arguments]
```

## Operate one batch

Read [references/workflow-operator.md](references/workflow-operator.md) and follow it exactly.

1. Read the verified backend profile with `[LAUNCHER, "setup", "profile"]`; use it as the batch-level `backend_profile`. Resolve every Job's source, requested changes, ordered references, and `privacy_mode`. Never infer `privacy_mode: none` from silence; if the user did not say whether the outgoing video needs face masking, ask one concise privacy question before creating the batch. Ask only when a binding or privacy instruction is genuinely ambiguous.
2. Prefer a user-supplied reference or an exact match from `assets/reference-images/catalog.json`. Generate a missing reference only when the user explicitly asks; read [references/static-asset-prompt-templates.md](references/static-asset-prompt-templates.md), then [references/reference-image-generation.md](references/reference-image-generation.md).
3. Build a new clean batch with copied regular files, `requirements.txt`, schema-v3 `job-bindings.json`, and `PAUSE`.
4. Set each Job's `privacy_mode` to `none` or `mosaic_required`. Read [references/face-anonymization.md](references/face-anonymization.md) for face or head masking.
5. Run `once` until the batch is indexed under `needs-input/`, then run `prepare <batch>` and wait on that same process.
6. For local preparation, report the prepared state and stop with `PAUSE` present.
7. For generation, require the user's explicit approval for this exact prepared batch, then run one `[LAUNCHER, "--confirm-paid", "submit-prepared", <batch>]` command and wait for it to exit.
8. Report the batch path, per-Job bindings, selected profile, completed files and task IDs, or exact blockers.

## Stable boundaries

- Use direct one-off commands. Do not start a persistent watcher for a conversation task.
- `prepare` is local-only: it does not upload media, stage Ark/TOS objects, or create a paid task.
- Keep `PAUSE` throughout explicit preparation and submission. The explicit `submit-prepared` command is the current-batch payment approval.
- Treat another batch, semantic variant, or semantic retry as a new paid decision.
- Preserve source media, references, prompts, outputs, and task state outside Git-tracked paths.
- Keep credentials in the process environment or the backend CLI's own credential store. Never write them to `.env`, batches, manifests, logs, or chat output.
- Leave prompt-node construction to the deterministic parent. It samples the source locally with trusted FFmpeg, attaches only timestamped frames, ordered references and structured Job JSON, and starts one Codex turn with `CODEX_EXEC_SERVER_URL=none`; that turn has no execution/filesystem tools or discovery and returns only a structured prompt string. The parent validates it and alone writes `prompt.txt`.
- Prompt turns use the setup-managed stable external, instruction-free, strict-file-auth `CODEX_HOME` shared with Doctor. It contains no `AGENTS.md`, rules, skills, plugins, MCP configuration or hooks. Do not copy the outer Agent's auth into per-run homes, add user configuration, or create a second Windows elevated sandbox/home. Explicit login may clear only a malformed regular `auth.json` after the locked home passes independent structure/ACL and stable file-identity checks; preserve valid auth and fail closed on unsafe, link or nonregular states.
- On Windows, the shared state resolver must obtain the real current user's `FOLDERID_LocalAppData` through the Known Folder API and fix state at `LocalAppData/video-replacer`. Ignore environment-supplied `LOCALAPPDATA`; accept `VIDEO_REPLACER_STATE_DIR` only when it canonically restates that root. Require a local fixed-drive node home and reject redirects, UNC, mapped-drive, non-fixed-drive and reparse paths. The home, `auth.json` and auth lock must be owned by the current user and use inheritance-protected DACLs with only current-user and `SYSTEM` full-control ACEs.
- Require Doctor's two local zero-tool wire phases before READY. Under one stable auth lock, both use the exact production command, same home, synthetic image and loopback Responses provider. The `requires_openai_auth=false` capture is required check `codex-wire` and requires empty top-level/additional tools, no multi-agent hints, an image and no auth header. The `requires_openai_auth=true` capture is required check `codex-file-auth-wire`; it requires the same surface and an authenticated handshake whose Bearer matches strictly validated stable `auth.json` by in-memory SHA-256 plus `hmac.compare_digest`. Raw token/header values are not retained or logged. Each captured model request must route to loopback and stop at HTTP 418 before a model response or inference. Any managed/system MCP, hook, tool or agent-hint reinjection is a setup failure. This evidence does not attest unrelated process network activity; the phases use synthetic media and contain no video-backend or paid command.
- All prompt-node Codex processes are serialized even when multiple Jobs are eligible. Do not bypass this lock or introduce prompt-node concurrency; the shared file-auth refresh tokens must have one writer.
- On Windows, preserve the pre-execution Codex provenance Gate for node login, Doctor, live status and every production prompt turn. Setup identity records the proved `official_sha256` and `official_release_tag`; each live status refetches the exact-version official manifest and rehashes the binary instead of trusting the recorded value.
- Treat old prompt-pipeline state as recovery evidence. Rebuild a partial or unknown flow as a new schema-v3 batch.
- Never write caller-selected compression fields, file limits, FFmpeg parameters, or final upload paths. The workflow owns them.
- `mosaic_required` is best-effort processing, not a guarantee of anonymity. Do not describe it as verified de-identification.
- Resume a recorded task ID when submission state is uncertain. Create another paid task only with fresh explicit authority.

# Workflow Operator Contract

This reference defines how the conversation Agent prepares and controls one Video Replacer batch. Parent-owned FFmpeg sampling, one sampled-frame/reference prompt turn, optional mosaic processing, deterministic Gates, preflight, submission, download, and terminal routing belong to the workflow.

Use the `LAUNCHER` resolved by the parent Skill: `REPO_ROOT\video-replacer.cmd` on native Windows 11 x64, and `REPO_ROOT/video-replacer` on macOS Apple Silicon or Linux preview. Invoke it as an argument array; do not rebuild a path-containing command string.

## 1. Build a clean intake

Require `[LAUNCHER, "setup", "status", "--json"]` to pass its live checks and report `ready: true`, then read the configured profile with `[LAUNCHER, "setup", "profile"]`. Live checks include `codex-wire`, `codex-file-auth-wire` and strict file-auth validation under the stable node-home lock. On Windows they also resolve the real current user's `FOLDERID_LocalAppData/video-replacer`, reject environment/state redirects and enforce the native fixed-drive/DACL boundary, then refetch the exact-version official Codex manifest and rehash the actual binary. A failed or offline provenance/attestation check is a setup repair, never a reason to trust an old record or bypass the launcher. Use the exact profile value as the batch-level `backend_profile`; do not ask the user to choose or type an internal profile ID.

Choose a new filesystem-safe batch name. Create a hidden staging directory outside the visible `inbox/`, then add:

```text
<batch>/
├── videos/
│   ├── 001-<semantic-source-name>.mp4
│   └── 002-<semantic-source-name>.mp4
├── replacements/
│   └── <semantic-reference-name>.<ext>
├── requirements.txt
├── job-bindings.json
└── PAUSE
```

Accept copied regular files with unique basenames. Prefix source filenames with their intended natural order so the workflow's `V###` assignment is deterministic. Write one requirement line per Job containing the requested change and user-prioritized invariants.

Use schema v3 for every new batch. Copy the one verified `backend_profile` into the whole batch:

```json
{
  "schema_version": 3,
  "batch_id": "<batch>",
  "backend_profile": "dreamina_cli_seedance_2_5",
  "jobs": [
    {
      "id": "V001",
      "privacy_mode": "none",
      "references": [
        {
          "relative_path": "replacements/<file>",
          "semantic_name": "<natural noun phrase>"
        }
      ]
    }
  ]
}
```

The conversation Agent writes only `schema_version`, `batch_id`, `backend_profile`, every Job's `id`, `privacy_mode`, `references`, `requirements.txt`, and copied media. Do not write `should_compress`, `compression_policy`, size limits, current video size, FFmpeg parameters, or a final upload path. Compression is a fixed workflow behavior, not an Agent option.

| `backend_profile` | Adapter | Hard limit | Compression target |
| --- | --- | ---: | ---: |
| `dreamina_cli_seedance_2_5` | 即梦 CLI / Seedance 2.5 | 200,000,000 bytes | 190,000,000 bytes |
| `dreamina_cli_seedance_2_0` | 即梦 CLI / Seedance 2.0 | 50,000,000 bytes | 47,000,000 bytes |

The public operator uses only the profile returned by first-run setup. Ark remains an internal development adapter and must not be selected by the public Skill or root launcher until it gains a persistent credential broker and complete no-cost capability probe.

Set `privacy_mode` from the user's instruction:

- `none`: upload the ordinary workflow-selected source input.
- `mosaic_required`: the workflow must produce and upload a separate mosaic video.

The field must reflect an explicit user choice. When the user has not said whether the outgoing video needs face masking, ask before creating the batch; silence is not authorization to use `none`.

List references in the required `@图片1…N` order. Give each reference a distinct natural noun phrase that works in a fluent sentence. Include every Job exactly once; use an empty `references` array when it needs none.

After an optional mosaic, the workflow examines the actual file that would be uploaded. A compatible file at or below the hard limit is reused without re-encoding. Otherwise it first tries a lossless remux; if that is still too large, it may make up to three H.264/AAC two-pass re-encoding attempts. Every attempt is checked against its real final byte size, profile media constraints, and a complete local FFmpeg decode. Failure blocks the Job before upload. The original and mosaic files stay in place; a changed upload copy is written as `source-upload-ready.mp4` with `upload-preparation.json`.

After all files are closed and stable, atomically rename the staging directory into `REPO_ROOT/workspace/video-loop/inbox/<batch>`.

## 2. Index and verify bindings

Run `[LAUNCHER, "once"]`. Wait for the batch to appear at `needs-input/<batch>`, then verify that `batch-index.json` maps every expected source to the intended `V###` ID. Run `[LAUNCHER, "check", <batch>]` and resolve deterministic input errors before preparation.

`once` and `watch` are shadow-only commands. They never upload media or create paid tasks.

## 3. Prepare locally

Run:

```text
[LAUNCHER, "prepare", <batch>]
```

Keep the process/session and wait until it exits. For `mosaic_required`, the workflow invokes its own fixed tool and accepts the result only when the command succeeds and the output file exists. Failure blocks that Job before upload. The workflow does not fall back to the original video.

The deterministic parent reads the current source video with trusted local FFmpeg and creates a bounded chronological, uniform-timestamp frame set beginning at the opening frame. Supported backend sources are at most 30 seconds, for which the current cadence is 0.75 seconds; the bounded policy spreads samples across an unexpectedly longer input. The isolated Codex turn receives only those attached frames, the ordered bound reference images and structured Job JSON; the source video itself is not attached. With `CODEX_EXEC_SERVER_URL=none`, the turn has no execution/filesystem tools, returns the prompt in its structured result, and cannot write a file. It uses the same setup-managed stable external, instruction-free, strict-file-auth `CODEX_HOME` whose zero-tool request and authenticated local handshake Doctor attested; this home has no `AGENTS.md`, rules, skills, plugins, MCP configuration or hooks. On Windows the parent repeats official package/release/digest verification and validates the fixed-drive/private-DACL home boundary immediately before executing Codex. Prompt-node Codex processes run one at a time even when the parent prepares multiple Jobs, preventing concurrent refresh-token writes. The parent validates the structured result and writes `prompt.txt`. No source/reference analysis artifact is handed to another model node, and no stage scores, approves, rejects or reviews the reference images.

`prepare` keeps `PAUSE`, performs no backend upload, creates no remote task, and returns every eligible Job as locally prepared or blocked. It writes the local upload-preparation evidence before probe and the submission plan.

## 4. Submit only when explicitly authorized

After the user explicitly approves this batch, run exactly once:

```text
[LAUNCHER, "--confirm-paid", "submit-prepared", <batch>]
```

The authorization is limited to the current batch, frozen flow, selected profile, eligible Job set, final video SHA-256, upload-preparation manifest SHA-256, and preflight/plan evidence. Changing the video, preparation evidence, manifest, or `backend_profile` requires a new `prepare` and fresh approval. Wait on the same process through submission, task polling, and download.

Before creating any payment checkpoint, the control plane validates every Job's frozen preparation result and submission plan as a batch. An old or unknown prompt-pipeline flow may only reuse a fully prepared batch; rebuild every partial old flow as a new schema-v3 batch.

If the session is lost, inspect `status --json`, `loop-state.json`, and the external request ledger. Resume a recorded task ID. An uncertain submission remains blocked until the ledger establishes a safe recovery path.

## 5. Brief handoff

Report:

- batch path and terminal directory;
- each Job's source, ordered semantic references, and `privacy_mode`;
- selected `backend_profile` and local upload-preparation action when present;
- local-prepared, `COMPLETED`, or blocked status;
- final video path and task ID when present;
- exact blocker for blocked Jobs;
- `PAUSE` state when stopping after local preparation.

Do not ask an Agent or person to review media, and do not make quality, identity-consistency, or mosaic-coverage judgments.

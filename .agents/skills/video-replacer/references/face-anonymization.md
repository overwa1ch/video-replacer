# Face Mosaic Field Contract

Read this reference when the user requests masking, mosaic, blur, or anonymization of a face or head in a source video.

The Agent records the choice in the Job entry of schema-v3 `job-bindings.json`:

```json
{
  "id": "V001",
  "privacy_mode": "mosaic_required",
  "references": []
}
```

Use `privacy_mode: "none"` only after the user explicitly says masking is not required. When the user is silent, ask before creating the batch. Natural-language wording does not control workflow behavior; the explicit field does.

`backend_profile` belongs once at the batch top level. The Agent does not add compression or upload-size fields to this Job entry.

During `prepare`, the deterministic parent keeps the indexed original and uses trusted local FFmpeg to produce the sampled-frame evidence for the prompt turn. The source video itself is not attached to that turn. The workflow separately invokes `tools/privacy/face_mosaic.py` and selects the mosaic file as the provisional upload input, then runs the fixed local upload-preparation size gate before probe. The masking step checks only that the command succeeds and the output file exists.

This mode is best-effort obfuscation. It does not prove complete face coverage or irreversible de-identification, and mosaic patterns can retain recoverable visual information. Use it only when that limitation matches the user's intent; do not describe the result as verified anonymity.

If the command fails or the output file is absent, the Job becomes blocked before upload. The original video is never used as a fallback for `mosaic_required`.

No Agent, human, or automatic stage reviews mosaic coverage or visual quality. Report only the selected mode, its best-effort limitation, the output path when available, or the processing blocker.

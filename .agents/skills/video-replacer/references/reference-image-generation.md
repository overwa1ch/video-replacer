# Reference Image Generation

Read this reference only when the user explicitly requests a missing reference image and the prompt is frozen from `static-asset-prompt-templates.md` or explicitly approved by the user.

## Capability check

Use the current Codex surface's image-generation capability when it is available. This is optional and is not installed by this repository. If the surface cannot generate or download images, report that exact blocker and ask the user to provide the reference file; do not substitute web search results or an unrelated image.

## Generation contract

1. Freeze the prompt, subject, layout, and any user-supplied base image before generation.
2. Generate only the named prerequisite. Do not add extra people, brands, props, text, or scenes.
3. Save the downloaded regular file into the new batch's `replacements/` directory with a semantic filename.
4. Save a sibling JSON provenance record containing the exact prompt, model when reported, generation time, source-input filenames, local filename, byte size, format, and SHA-256. Do not store account identifiers, cookies, tokens, or signed URLs.
5. Bind the image in the final `@图片N` order and include its hash in replacement preflight.
6. Do not ask an Agent to score or approve the generated image. Report the saved asset or the exact generation blocker.

A vehicle-interior grid requires a base image of that vehicle. A location grid requires a base image of that location. Stop and request the missing base rather than inventing it.

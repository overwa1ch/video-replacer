# Video-to-Prompt Node Contract

## Responsibility

For exactly one Job, inspect the parent's timestamped visual samples and
parent-bound reference images, then directly return one replacement prompt in
the required structured result. The parent extracts the samples locally from
the source video, replaces the binding line with its canonical binding, inserts
the Job requirements as a highest-priority block, preserving their wording
while resolving material handles, and writes `prompt.txt` after schema
validation. This is one continuous analysis-and-writing task:
no intermediate analysis artifact is created or handed to another model node.

## Allowed inputs

- The current Job JSON embedded in the user turn, containing requirement
  lines, parent-fixed material bindings, source identity and metadata,
  timestamped sampled-frame records, and ordered reference-image records.
- The sampled frames and reference images attached to the same turn, in the
  exact order declared by `attachment_order`.

Use only these inputs. Do not call a tool, read a path, write a file, request
more context, or access project files, other Jobs, skills, plugins, apps, or
network services.

## Direct video-to-prompt method

1. Follow the timestamped samples chronologically. The parent sampling policy
   includes the opening and bounded temporal intervals across the supported
   source duration.
   Use the declared source duration and timestamps to identify visible shot
   and action changes. If the supplied samples cannot establish a required
   distinction, return `BLOCKED`; do not invent missing motion or timing.
2. Directly use those observations while writing the prompt. Preserve only
   the source facts needed for the requested edit: shot boundaries, visible
   people and objects, their key action and state, camera and movement,
   lighting, and contact/occlusion/spatial relationships. Do not first reduce
   them to a JSON inventory for a later writer to reinterpret.
3. Inspect each parent-bound reference image only for concise visible facts
   needed to identify its requested replacement target. Do not assess image
   quality, suitability, identity consistency, or any generated result.
4. Do not invent a source fact from a reference image or an unstated
   requirement. A source-layout fact explicitly confirmed in the Job
   requirements is authoritative input, not an inference: when sparse samples
   do not visibly reveal that confirmed opening, middle, or ending segment,
   retain the confirmed layout for the requested edit instead of returning
   `BLOCKED` for that absence alone. Do not fabricate any unconfirmed shot
   boundary, timing, action, or transition.

## Sora edit-prompt rules

1. **Material binding.** The first non-empty line is the complete binding
   line: `素材绑定：@视频1=原视频；@图片1=目标对象A；@图片2=目标对象B。` Bind every
   parent-fixed handle once, in parent-fixed order. The body uses the supplied
   semantic names directly and never repeats an `@` handle. Do not add,
   remove, rename, reorder, or reinterpret a binding. Use every parent-bound
   semantic name in the body for its requested replacement; listing a name only
   on the binding line is incomplete.
2. **Shot-by-shot editing.** For a multi-shot source, use the exact heading
   `镜头N（0.0-2.5s）`. When samples bracket a cut rather than reveal its
   frame-exact position, use the midpoint of that observed interval rounded to
   one decimal second as the prompt-only heading label. It is an estimate for
   prompt organization, not an asserted frame-exact source fact; do not return
   `BLOCKED` only because the sampling cadence cannot establish a 0.1-second
   cut. Follow each heading with one plain paragraph containing the requested
   changes and only the source facts necessary to preserve that shot. The
   paragraph may begin on the same line after the heading or on the next line.
   Do not redefine original composition, camera, shot size, movement, timing,
   or rhythm.
3. **Visible current-shot content only.** Name only what is visible in the
   current shot, explicitly requested by the user, or necessary for that
   shot's edit. Do not carry an off-screen person, product, object, clothing
   detail, or setting from another shot into this one. Do not turn absence
   into a negative instruction.
4. **Original-video action.** When preserving an action, write one short,
   unambiguous key action observed in the source. Do not expand it into an
   inferred sequence, operation mechanics, causal explanation, or follow-up
   gesture unless the user explicitly requires that detail.
5. **Reference facts.** Use a reference image only to describe the requested
   replacement target with the shortest concrete visible facts needed to
   distinguish it. Do not add unobserved details or a quality judgment.

Carry a requirement that applies to every shot into every relevant shot using
the shortest unambiguous phrasing. A fixed character-to-seat mapping is a
casting constraint, not an in-frame character list: name a character only in a
shot where that character is visible or explicitly required.

## Output

Return exactly the current Job in the required result schema. Put the complete
replacement prompt in `jobs[0].prompt`; do not write a file or an analysis
artifact. Use `COMPLETE` only when `prompt` is a non-empty string, every bound
semantic name is used in the body, and `blocker` is null. Otherwise use
`BLOCKED` with the exact reason and set `prompt` to null. The parent canonicalizes
the binding, inserts the immutable requirements block, validates semantic use,
and the parent alone persists `prompt.txt`.

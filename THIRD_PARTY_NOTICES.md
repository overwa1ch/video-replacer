# Third-party components

Video Replacer is licensed under Apache-2.0. It integrates with, but does not redistribute, these external components:

- OpenAI Codex CLI — installed and authenticated separately under OpenAI's applicable terms.
- Dreamina CLI — official external binary installed and authenticated separately under ByteDance/Jianying's applicable terms.
- OpenScrub — optional Apache-2.0 dependency installed from the pinned upstream Git commit listed in `tools/privacy/openscrub-requirements.lock.txt`.
- Volcengine TOS Python SDK — optional Apache-2.0 dependency pinned in `tools/ark-tos-requirements.lock.txt`.
- imageio-ffmpeg — BSD-2-Clause dependency pinned in `requirements-core.lock.txt`; it supplies an FFmpeg executable when a system FFmpeg is unavailable.
- FFmpeg — supplied by the system or the imageio-ffmpeg package and governed by the license configuration of that executable (LGPL and/or GPL components).

If a future release bundles any dependency binary, wheel, model or source tree, that release must include the dependency's full license and an updated software bill of materials.

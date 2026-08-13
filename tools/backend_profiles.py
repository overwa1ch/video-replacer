"""Reviewed backend profiles for Video Replacer upload preparation.

The conversation agent may select only a profile id in ``job-bindings.json``.
Everything that affects media preparation or remote transport is fixed here,
instead of being supplied by a batch, prompt, or environment-controlled path.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, FrozenSet, Optional


class BackendProfileError(ValueError):
    """Raised when a batch asks for an unavailable backend profile."""


@dataclass(frozen=True)
class BackendProfile:
    """Stable, reviewable capabilities for one backend + model combination."""

    profile_id: str
    adapter_name: str
    transport: str
    model_version: Optional[str]
    model_environment_variable: Optional[str]
    limit_bytes: int
    target_bytes: int
    allowed_containers: FrozenSet[str]
    allowed_input_video_codecs: FrozenSet[str]
    allowed_input_audio_codecs: FrozenSet[str]
    output_container: str
    output_video_codec: str
    output_audio_codec: str
    min_duration_seconds: float
    max_duration_seconds: float
    min_width: int
    max_width: int
    min_height: int
    max_height: int
    min_pixels: int
    max_pixels: int
    min_aspect_ratio: float
    max_aspect_ratio: float
    min_fps: Optional[float]
    max_fps: Optional[float]
    # Empty means the upstream product accepts arbitrary dimensions within the
    # numeric bounds.  Ark Seedance 2.5 documents a narrower 480p/720p
    # reference-video contract, which is expressed here rather than in a
    # caller-provided batch setting.
    allowed_reference_heights: FrozenSet[int]
    constraints_source: str

    def resolved_model_version(self, environment: Optional[Dict[str, str]] = None) -> str:
        """Return the reviewed model id, optionally supplied through one env key.

        Ark model identifiers are account/region rollout dependent.  A profile
        intentionally permits only its single documented environment variable;
        the value is never persisted to a batch or printed by the parent.
        """

        if self.model_version:
            return self.model_version
        if not self.model_environment_variable:
            raise BackendProfileError(f"{self.profile_id} 缺少模型版本配置")
        source = os.environ if environment is None else environment
        value = str(source.get(self.model_environment_variable, "")).strip()
        if not value:
            raise BackendProfileError(
                f"{self.profile_id} 需要环境变量 {self.model_environment_variable}"
            )
        # Model ids are identifiers, not free-form command text.  This is not
        # passed through a shell, but limiting the shape prevents accidental
        # secrets or malformed configuration entering manifests.
        if not all(char.isalnum() or char in "._-" for char in value):
            raise BackendProfileError(f"{self.model_environment_variable} 格式无效")
        return value

    def public_constraints(self) -> Dict[str, object]:
        """A secret-free representation safe to bind into manifests."""

        value = asdict(self)
        value.pop("model_environment_variable", None)
        value["allowed_containers"] = sorted(self.allowed_containers)
        value["allowed_input_video_codecs"] = sorted(
            self.allowed_input_video_codecs
        )
        value["allowed_input_audio_codecs"] = sorted(
            self.allowed_input_audio_codecs
        )
        value["allowed_reference_heights"] = sorted(
            self.allowed_reference_heights
        )
        value["model_version"] = self.model_version or "environment-configured"
        return value

    @property
    def constraints_digest(self) -> str:
        canonical = json.dumps(
            self.public_constraints(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


# Limits are deliberately in decimal bytes because the upstream product uses
# MB, not MiB.  The target leaves the product safety margin requested by the
# workflow contract.
BACKEND_PROFILES: Dict[str, BackendProfile] = {
    "dreamina_cli_seedance_2_5": BackendProfile(
        profile_id="dreamina_cli_seedance_2_5",
        adapter_name="dreamina_video.py",
        transport="dreamina_cli_local_upload",
        model_version="seedance2.5",
        model_environment_variable=None,
        limit_bytes=200_000_000,
        target_bytes=190_000_000,
        allowed_containers=frozenset({"mp4", "mov"}),
        allowed_input_video_codecs=frozenset({"h264", "hevc"}),
        allowed_input_audio_codecs=frozenset({"aac", "mp3"}),
        output_container="mp4",
        output_video_codec="h264",
        output_audio_codec="aac",
        min_duration_seconds=2.0,
        max_duration_seconds=30.0,
        min_width=300,
        max_width=6000,
        min_height=300,
        max_height=6000,
        min_pixels=409_600,
        max_pixels=8_295_044,
        min_aspect_ratio=0.4,
        max_aspect_ratio=2.5,
        min_fps=24.0,
        max_fps=60.0,
        allowed_reference_heights=frozenset(),
        constraints_source="dreamina-web-seedance-2.5-2026-08-12",
    ),
    "dreamina_cli_seedance_2_0": BackendProfile(
        profile_id="dreamina_cli_seedance_2_0",
        adapter_name="dreamina_video.py",
        transport="dreamina_cli_local_upload",
        model_version="seedance2.0",
        model_environment_variable=None,
        limit_bytes=50_000_000,
        target_bytes=47_000_000,
        allowed_containers=frozenset({"mp4", "mov"}),
        allowed_input_video_codecs=frozenset({"h264", "hevc"}),
        allowed_input_audio_codecs=frozenset({"aac", "mp3"}),
        output_container="mp4",
        output_video_codec="h264",
        output_audio_codec="aac",
        min_duration_seconds=2.0,
        max_duration_seconds=15.0,
        min_width=200,
        max_width=2160,
        min_height=200,
        max_height=2160,
        # The documented 2.0 edge bounds are 200–2160px.  200×200 is the
        # smallest technically valid frame, so keep the derived pixel floor
        # positive for the common upload-constraint validator.
        min_pixels=40_000,
        max_pixels=8_295_044,
        min_aspect_ratio=0.4,
        max_aspect_ratio=2.5,
        min_fps=None,
        max_fps=None,
        allowed_reference_heights=frozenset(),
        constraints_source="dreamina-web-seedance-2.0-2026-08-12",
    ),
    "volcengine_ark_seedance_2_5": BackendProfile(
        profile_id="volcengine_ark_seedance_2_5",
        adapter_name="ark_video.py",
        transport="volcengine_ark_tos_presigned_url",
        # The exact Ark model deployment id is region/account scoped.  Requiring
        # it from one named environment variable makes an unsupported rollout
        # fail before any upload or paid task instead of guessing a model id.
        model_version=None,
        model_environment_variable="VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID",
        limit_bytes=200_000_000,
        target_bytes=190_000_000,
        allowed_containers=frozenset({"mp4", "mov"}),
        allowed_input_video_codecs=frozenset({"h264", "hevc"}),
        allowed_input_audio_codecs=frozenset({"aac", "mp3"}),
        output_container="mp4",
        output_video_codec="h264",
        output_audio_codec="aac",
        min_duration_seconds=4.0,
        max_duration_seconds=30.0,
        min_width=300,
        max_width=6000,
        min_height=300,
        max_height=6000,
        min_pixels=409_600,
        max_pixels=8_295_044,
        min_aspect_ratio=0.4,
        max_aspect_ratio=2.5,
        min_fps=24.0,
        max_fps=60.0,
        allowed_reference_heights=frozenset({480, 720}),
        constraints_source="volcengine-ark-seedance-2.5-2026-08-11",
    ),
}


def get_backend_profile(profile_id: object) -> BackendProfile:
    value = str(profile_id or "").strip()
    profile = BACKEND_PROFILES.get(value)
    if profile is None:
        allowed = "、".join(sorted(BACKEND_PROFILES))
        raise BackendProfileError(f"未知 backend_profile {value!r}；只允许：{allowed}")
    return profile

#!/usr/bin/env python3
"""Volcengine Ark adapter for the Video Replacer workflow.

The adapter deliberately keeps its trust boundary narrow:

* ``probe`` is fully local.  It does not read Ark/TOS credentials, stage
  media, create a task, or make a network request.
* ``generate`` requires ``--confirm-paid`` *before* it reads a credential,
  imports the TOS SDK, stages an object, or calls Ark.
* signed URLs, raw TOS object keys, and credentials never appear in stdout,
  output manifests, or ordinary logs.  The only record containing raw object
  keys lives in the external state directory with owner-only permissions.

The parent workflow selects an reviewed ``backend_profile`` and supplies the
account-specific Ark model id as ``--model-version``.  This adapter never
guesses a model id or reads it from an environment variable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    from backend_profiles import BackendProfile, BackendProfileError, get_backend_profile
except ModuleNotFoundError as exc:
    # Path-loaded tests do not automatically add tools/ to sys.path.
    if exc.name != "backend_profiles":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backend_profiles import BackendProfile, BackendProfileError, get_backend_profile


sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "video-replacements"
PREFLIGHT_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
STATE_OVERRIDE_NAME = "VIDEO_REPLACER_STATE_DIR"
ARK_API_BASE_DEFAULT = "https://ark.cn-beijing.volces.com/api/v3"
ARK_API_BASE_ENV = "VIDEO_REPLACER_ARK_API_BASE_URL"
ARK_PROFILE_ID = "volcengine_ark_seedance_2_5"
ARK_TRANSPORT = "volcengine_ark_tos_presigned_url"
# A queued generation can start well after submission.  Keep the private GET
# capability long enough for the maximum supported task window; bucket
# lifecycle/terminal cleanup remains the fallback and primary removal path.
SIGNED_URL_EXPIRES_SECONDS = 24 * 60 * 60
# The fallback bucket rule must outlive a 24-hour signed URL plus ordinary
# queueing. Object-level deletion at terminal state remains the primary path.
MINIMUM_LIFECYCLE_EXPIRATION_DAYS = 2
MAX_IMAGES = 9

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
VIDEO_RE = re.compile(r"Video:.*?,\s*(\d+)x(\d+)(?:[\s,])")
FPS_RE = re.compile(r"([\d.]+)\s+fps")
VIDEO_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_]+)")
AUDIO_CODEC_RE = re.compile(r"Audio:\s*([A-Za-z0-9_]+)")
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class ArkPipelineError(RuntimeError):
    """A safe, user-actionable Ark workflow error."""


@dataclass(frozen=True)
class ArkEnvironment:
    """Runtime-only credentials and reviewed non-secret TOS configuration.

    ``repr=False`` prevents an accidental diagnostic from echoing secrets.
    Instances are never serialized.
    """

    ark_api_key: str = field(repr=False)
    tos_access_key: str = field(repr=False)
    tos_secret_key: str = field(repr=False)
    tos_security_token: Optional[str] = field(default=None, repr=False)
    tos_endpoint: str = ""
    tos_region: str = ""
    tos_bucket: str = ""
    tos_prefix: str = "video-replacer"
    tos_lifecycle_rule_id: str = ""
    ark_api_base: str = ARK_API_BASE_DEFAULT


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def absolute_file(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ArkPipelineError(f"找不到{label}：{path}")
    return path


def safe_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return normalized or fallback


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except OSError:
        pass


def atomic_write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist sensitive state with owner-only file permissions."""

    secure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_output_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a non-sensitive workflow artifact without weakening state privacy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_json_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArkPipelineError(f"{label}不是有效的 UTF-8 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ArkPipelineError(f"{label}根节点必须是 JSON object：{path}")
    return payload


def ffmpeg_executable() -> str:
    configured = os.getenv("FFMPEG", "").strip()
    if configured and Path(configured).is_file():
        return configured
    detected = shutil.which("ffmpeg")
    if detected:
        return detected
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise ArkPipelineError("找不到 ffmpeg；请安装 ffmpeg 或 imageio-ffmpeg。")


def probe_video(path: Path) -> Dict[str, Any]:
    """Read media facts locally using FFmpeg; this function has no network path."""

    completed = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    text = completed.stderr
    duration_match = DURATION_RE.search(text)
    video_match = VIDEO_RE.search(text)
    if not duration_match or not video_match:
        raise ArkPipelineError(f"无法读取视频元数据：{path}")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    width, height = map(int, video_match.groups())
    fps_match = FPS_RE.search(text)
    codec_match = VIDEO_CODEC_RE.search(text)
    audio_match = AUDIO_CODEC_RE.search(text)
    size_bytes = path.stat().st_size
    return {
        "path": str(path),
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_000_000, 3),
        "container": path.suffix.lower().lstrip("."),
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "fps": float(fps_match.group(1)) if fps_match else None,
        "video_codec": codec_match.group(1).lower() if codec_match else None,
        "audio_codec": audio_match.group(1).lower() if audio_match else None,
        "has_audio": audio_match is not None,
    }


def validate_video_metadata(metadata: Mapping[str, Any], profile: BackendProfile) -> None:
    """Enforce the profile's upload contract before any remote side effect."""

    failures: List[str] = []
    try:
        size_bytes = int(metadata["size_bytes"])
        duration = float(metadata["duration_seconds"])
        width = int(metadata["width"])
        height = int(metadata["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArkPipelineError("视频元数据不完整，无法按 backend profile 校验") from exc

    container = str(metadata.get("container") or "").lower()
    codec = str(metadata.get("video_codec") or "").lower()
    audio_codec = str(metadata.get("audio_codec") or "").lower()
    if container not in profile.allowed_containers:
        failures.append("容器格式不受支持")
    if codec not in profile.allowed_input_video_codecs:
        failures.append("视频编码不受支持")
    if bool(metadata.get("has_audio")) and (
        audio_codec not in profile.allowed_input_audio_codecs
    ):
        failures.append("音频编码不受支持")
    if size_bytes > profile.limit_bytes:
        failures.append(
            f"文件大小 {size_bytes} bytes 超过 profile 上限 {profile.limit_bytes} bytes"
        )
    if not profile.min_duration_seconds <= duration <= profile.max_duration_seconds:
        failures.append(
            "视频时长不在允许范围 "
            f"{profile.min_duration_seconds:g}–{profile.max_duration_seconds:g} 秒"
        )
    if not profile.min_width <= width <= profile.max_width or not profile.min_height <= height <= profile.max_height:
        failures.append("视频分辨率不在允许范围")
    pixels = width * height
    if not profile.min_pixels <= pixels <= profile.max_pixels:
        failures.append("视频像素数不在允许范围")
    if (
        profile.allowed_reference_heights
        and height not in profile.allowed_reference_heights
    ):
        values = "、".join(str(value) for value in sorted(profile.allowed_reference_heights))
        failures.append(f"视频高度必须为模型允许的 {values}p")
    if height <= 0:
        failures.append("视频高度无效")
    else:
        aspect_ratio = width / height
        if not profile.min_aspect_ratio <= aspect_ratio <= profile.max_aspect_ratio:
            failures.append("视频宽高比不在允许范围")
    fps = metadata.get("fps")
    if profile.min_fps is not None or profile.max_fps is not None:
        if fps is None:
            failures.append("无法读取帧率")
        else:
            fps_value = float(fps)
            if profile.min_fps is not None and fps_value < profile.min_fps:
                failures.append("视频帧率低于允许范围")
            if profile.max_fps is not None and fps_value > profile.max_fps:
                failures.append("视频帧率高于允许范围")
    if failures:
        raise ArkPipelineError("Ark profile 本地校验失败：" + "；".join(failures))


def validate_ark_profile(args: argparse.Namespace) -> Tuple[BackendProfile, str]:
    """Resolve only the reviewed Ark profile and an explicit parent model id."""

    try:
        profile = get_backend_profile(getattr(args, "backend_profile", None))
    except BackendProfileError as exc:
        raise ArkPipelineError(str(exc)) from exc
    if profile.profile_id != ARK_PROFILE_ID or profile.adapter_name != Path(__file__).name:
        raise ArkPipelineError("当前 backend_profile 不属于 Ark 受控执行器")
    if profile.transport != ARK_TRANSPORT:
        raise ArkPipelineError("Ark backend_profile 的传输方式不受支持")
    model_version = str(getattr(args, "model_version", "") or "").strip()
    if not model_version:
        raise ArkPipelineError("Ark 必须由 workflow 显式传入 --model-version")
    if not MODEL_ID_RE.fullmatch(model_version):
        raise ArkPipelineError("--model-version 格式无效")
    return profile, model_version


def build_input_bindings(prompt_file: Path, images: Sequence[Path]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "prompt": {
            "sha256": sha256_file(prompt_file),
            "size_bytes": prompt_file.stat().st_size,
        },
        "reference_images": [
            {
                "index": index,
                "sha256": sha256_file(image),
                "size_bytes": image.stat().st_size,
            }
            for index, image in enumerate(images, start=1)
        ],
    }


def validate_input_bindings(
    preflight: Mapping[str, Any], prompt_file: Path, images: Sequence[Path]
) -> Dict[str, Any]:
    recorded = preflight.get("input_bindings")
    if not isinstance(recorded, dict):
        raise ArkPipelineError("preflight 缺少提示词与有序参考图哈希绑定；请重新 probe")
    current = build_input_bindings(prompt_file, images)
    if recorded != current:
        raise ArkPipelineError("提示词或有序参考素材在 preflight 后发生变化")
    return current


def validate_upload_preparation_binding(payload: Mapping[str, Any]) -> None:
    """Refuse a paid run if the parent-bound local proof was edited later."""

    path_value = payload.get("upload_preparation_manifest")
    digest_value = payload.get("upload_preparation_manifest_sha256")
    if not isinstance(path_value, str) or not isinstance(digest_value, str):
        raise ArkPipelineError("受控 Ark preflight 缺少上传准备 manifest 绑定")
    manifest_path = Path(path_value).expanduser().resolve()
    if not manifest_path.is_file():
        raise ArkPipelineError("preflight 绑定的上传准备 manifest 不存在")
    current_digest = sha256_file(manifest_path)
    if not re.fullmatch(r"[0-9a-f]{64}", digest_value.lower()) or (
        current_digest != digest_value.lower()
    ):
        raise ArkPipelineError("upload-preparation manifest 在 preflight 后发生变化")


def upload_preparation_binding_from_args(args: argparse.Namespace) -> Dict[str, str]:
    """Read only local provenance evidence for a new Ark preflight."""

    value = str(getattr(args, "upload_preparation_manifest", "") or "").strip()
    if not value:
        raise ArkPipelineError("Ark profile probe 缺少上传准备 manifest")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ArkPipelineError("上传准备 manifest 不存在")
    return {
        "upload_preparation_manifest": str(path),
        "upload_preparation_manifest_sha256": sha256_file(path),
    }


def build_privacy_record(privacy_status: str, video: Path) -> Dict[str, Any]:
    if privacy_status != "workflow-selected-input":
        raise ArkPipelineError(
            "probe 必须由 workflow 传入 --privacy-status workflow-selected-input"
        )
    return {
        "status": "workflow-selected-input",
        "active_video_sha256": sha256_file(video),
        "remote_upload_authorized": True,
        "paid_task_authorized": True,
    }


def validate_preflight_manifest(
    path: Path, video: Path, profile: BackendProfile, model_version: str
) -> Dict[str, Any]:
    payload = load_json_object(path, "preflight manifest")
    if payload.get("schema_version") != PREFLIGHT_SCHEMA_VERSION:
        raise ArkPipelineError("preflight manifest schema_version 不受支持")
    if payload.get("preflight_passed") is not True:
        raise ArkPipelineError("preflight manifest 未通过 preflight_passed Gate")
    if payload.get("transport") != ARK_TRANSPORT:
        raise ArkPipelineError("preflight manifest 不是 Ark TOS 签名 URL 传输")
    if payload.get("backend_profile") != profile.profile_id:
        raise ArkPipelineError("preflight manifest 的 backend_profile 不匹配")
    # ``constraints_digest`` is the parent workflow's schema-v3 field;
    # ``backend_profile_constraints_sha256`` is retained in the local
    # preflight too so every parent/submission fingerprint names it uniformly.
    if payload.get("constraints_digest") != profile.constraints_digest:
        raise ArkPipelineError("preflight manifest 的 profile 约束哈希不匹配")
    if payload.get("backend_profile_constraints_sha256") != profile.constraints_digest:
        raise ArkPipelineError("preflight manifest 的 backend profile 约束哈希不匹配")
    if payload.get("model_version") != model_version:
        raise ArkPipelineError("preflight manifest 的模型版本不匹配")
    validate_upload_preparation_binding(payload)
    active = payload.get("active_video")
    if not isinstance(active, dict):
        raise ArkPipelineError("preflight manifest 缺少 active_video")
    if Path(str(active.get("path") or "")).expanduser().resolve() != video:
        raise ArkPipelineError("preflight manifest 的活动视频路径不匹配")
    current_sha = sha256_file(video)
    if str(active.get("sha256") or "").lower() != current_sha:
        raise ArkPipelineError("活动视频 SHA-256 与 preflight manifest 不一致")
    metadata = active.get("metadata")
    if not isinstance(metadata, dict):
        raise ArkPipelineError("preflight manifest 缺少活动视频元数据")
    validate_video_metadata(metadata, profile)
    privacy = payload.get("privacy")
    if not isinstance(privacy, dict):
        raise ArkPipelineError("preflight manifest 缺少 workflow 输入记录")
    if privacy.get("status") != "workflow-selected-input":
        raise ArkPipelineError("preflight manifest 的 workflow 输入记录无效")
    if str(privacy.get("active_video_sha256") or "").lower() != current_sha:
        raise ArkPipelineError("workflow 输入记录与活动视频不匹配")
    if privacy.get("remote_upload_authorized") is not True:
        raise ArkPipelineError("workflow 输入未授权上传")
    if privacy.get("paid_task_authorized") is not True:
        raise ArkPipelineError("workflow 输入未授权付费任务")
    return payload


def infer_ratio(width: int, height: int) -> str:
    actual = width / height
    supported = {
        "21:9": 21 / 9,
        "16:9": 16 / 9,
        "4:3": 4 / 3,
        "1:1": 1.0,
        "3:4": 3 / 4,
        "9:16": 9 / 16,
    }
    return min(supported, key=lambda name: abs(math.log(actual / supported[name])))


def api_duration(
    source_duration: float, requested: Optional[int], profile: BackendProfile
) -> int:
    if requested is not None:
        if not profile.min_duration_seconds <= requested <= profile.max_duration_seconds:
            raise ArkPipelineError(
                "--duration 必须在 "
                f"{profile.min_duration_seconds:g}–{profile.max_duration_seconds:g} 秒范围内"
            )
        return int(requested)
    return max(
        int(math.ceil(profile.min_duration_seconds)),
        min(int(math.floor(profile.max_duration_seconds)), int(math.floor(source_duration + 0.5))),
    )


def planned_request(
    video: Path,
    prompt_file: Path,
    images: Sequence[Path],
    metadata: Mapping[str, Any],
    profile: BackendProfile,
    model_version: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    resolution = str(getattr(args, "resolution", "") or "720p")
    if resolution not in {"480p", "720p"}:
        raise ArkPipelineError("Ark Seedance 2.5 当前只允许 480p 或 720p")
    requested_ratio = getattr(args, "ratio", None)
    ratio = requested_ratio or infer_ratio(int(metadata["width"]), int(metadata["height"]))
    if ratio not in {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise ArkPipelineError("--ratio 不受支持")
    duration = api_duration(
        float(metadata["duration_seconds"]), getattr(args, "duration", None), profile
    )
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "backend_profile": profile.profile_id,
        "profile_constraints_sha256": profile.constraints_digest,
        # The model id is account/region specific.  Bind a digest into state
        # identity without exposing a deployment identifier in ordinary output.
        "model_version_sha256": sha256_text(model_version),
        "video_sha256": sha256_file(video),
        "prompt_sha256": sha256_file(prompt_file),
        "image_sha256": [sha256_file(path) for path in images],
        "duration": duration,
        "ratio": ratio,
        "video_resolution": resolution,
        **(
            {
                "upload_preparation_manifest_sha256": str(
                    getattr(args, "_upload_preparation_manifest_sha256", "")
                ).lower(),
                "preflight_manifest_sha256": str(
                    getattr(args, "_preflight_manifest_sha256", "")
                ).lower(),
            }
            if hasattr(args, "_upload_preparation_manifest_sha256")
            or hasattr(args, "_preflight_manifest_sha256")
            else {}
        ),
    }


def request_fingerprint(request: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def request_state_root(explicit: Optional[str]) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ArkPipelineError("--state-dir 必须是绝对路径")
        return (path.resolve() / "ark-video").resolve()
    override = os.getenv(STATE_OVERRIDE_NAME, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ArkPipelineError(f"{STATE_OVERRIDE_NAME} 必须是绝对路径")
        return (path.resolve() / "ark-video").resolve()
    configured = os.getenv("XDG_STATE_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return (base / "video-replacer" / "ark-video").resolve()


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    secure_directory(path.parent)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ArkPipelineError("同一 Ark 请求正在由另一个进程处理") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _required_environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ArkPipelineError(f"Ark 提交缺少运行环境变量 {name}")
    if "\n" in value or "\r" in value:
        raise ArkPipelineError(f"运行环境变量 {name} 格式无效")
    return value


def _normalise_tos_prefix(value: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        return "video-replacer"
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArkPipelineError("VIDEO_REPLACER_TOS_PREFIX 格式无效")
    return cleaned


def _validate_https_url(value: str, label: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ArkPipelineError(f"{label} 必须是无查询参数的 HTTPS URL")
    return value.rstrip("/")


def _official_ark_api_base(value: str) -> str:
    normalized = _validate_https_url(value, ARK_API_BASE_ENV)
    parsed = urllib.parse.urlparse(normalized)
    if parsed.hostname != "ark.cn-beijing.volces.com" or parsed.port is not None:
        raise ArkPipelineError(f"{ARK_API_BASE_ENV} 只允许官方火山 Ark 域名")
    if parsed.path.rstrip("/") != "/api/v3":
        raise ArkPipelineError(f"{ARK_API_BASE_ENV} 路径必须是 /api/v3")
    return normalized


def _validate_tos_endpoint(value: str) -> str:
    """Accept the SDK's documented host form as well as an HTTPS endpoint."""

    raw = value.strip()
    if raw.startswith("https://"):
        normalized = _validate_https_url(raw, "VIDEO_REPLACER_TOS_ENDPOINT")
        parsed = urllib.parse.urlparse(normalized)
        host = parsed.hostname or ""
        if parsed.port is not None or parsed.path not in {"", "/"}:
            raise ArkPipelineError("VIDEO_REPLACER_TOS_ENDPOINT 不允许端口或路径")
    elif re.fullmatch(r"[A-Za-z0-9.-]+", raw):
        normalized = raw
        host = raw
    else:
        raise ArkPipelineError("VIDEO_REPLACER_TOS_ENDPOINT 格式无效")
    if not (host == "volces.com" or host.endswith(".volces.com")):
        raise ArkPipelineError("VIDEO_REPLACER_TOS_ENDPOINT 只允许官方火山 TOS 域名")
    return normalized


def load_ark_environment() -> ArkEnvironment:
    """Read credentials only from process environment after the paid guard."""

    endpoint = _validate_tos_endpoint(
        _required_environment_value("VIDEO_REPLACER_TOS_ENDPOINT")
    )
    api_base = _official_ark_api_base(
        os.getenv(ARK_API_BASE_ENV, ARK_API_BASE_DEFAULT).strip() or ARK_API_BASE_DEFAULT
    )
    token = os.getenv("VIDEO_REPLACER_TOS_SECURITY_TOKEN", "").strip() or None
    if token is not None and ("\n" in token or "\r" in token):
        raise ArkPipelineError("VIDEO_REPLACER_TOS_SECURITY_TOKEN 格式无效")
    return ArkEnvironment(
        ark_api_key=_required_environment_value("VIDEO_REPLACER_ARK_API_KEY"),
        tos_access_key=_required_environment_value("VIDEO_REPLACER_TOS_ACCESS_KEY"),
        tos_secret_key=_required_environment_value("VIDEO_REPLACER_TOS_SECRET_KEY"),
        tos_security_token=token,
        tos_endpoint=endpoint,
        tos_region=_required_environment_value("VIDEO_REPLACER_TOS_REGION"),
        tos_bucket=_required_environment_value("VIDEO_REPLACER_TOS_BUCKET"),
        tos_prefix=_normalise_tos_prefix(
            os.getenv("VIDEO_REPLACER_TOS_PREFIX", "video-replacer")
        ),
        tos_lifecycle_rule_id=_required_environment_value(
            "VIDEO_REPLACER_TOS_LIFECYCLE_RULE_ID"
        ),
        ark_api_base=api_base,
    )


def create_tos_client(environment: ArkEnvironment) -> Any:
    """Load the optional TOS SDK at the only point it is needed."""

    try:
        import tos  # type: ignore
    except ImportError as exc:
        raise ArkPipelineError(
            "缺少火山 TOS Python 依赖；请安装 tools/ark-tos-requirements.lock.txt"
        ) from exc
    try:
        return tos.TosClientV2(
            environment.tos_access_key,
            environment.tos_secret_key,
            environment.tos_endpoint,
            environment.tos_region,
            security_token=environment.tos_security_token,
        )
    except Exception as exc:
        raise ArkPipelineError("无法初始化 TOS 客户端") from exc


def verify_tos_lifecycle_fallback(tos_client: Any, environment: ArkEnvironment) -> None:
    """Require a preconfigured, sufficiently long private-object expiry rule.

    The workflow deliberately does not rewrite a user's bucket lifecycle
    policy during a paid generation. It instead verifies the named rule before
    staging any object. This keeps terminal cleanup primary and leaves expiry
    as a safe fallback for an interrupted process or an uncertain task.
    """

    try:
        response = tos_client.get_bucket_lifecycle(environment.tos_bucket)
        rules = getattr(response, "rules", None)
    except Exception as exc:
        raise ArkPipelineError("无法验证 TOS 自动过期规则；已在上传前停止") from exc
    if not isinstance(rules, list):
        raise ArkPipelineError("TOS 自动过期规则响应无效；已在上传前停止")
    required_prefix = f"{environment.tos_prefix}/video-replacer/"
    for rule in rules:
        rule_id = str(getattr(rule, "id", "") or "")
        if rule_id != environment.tos_lifecycle_rule_id:
            continue
        status = getattr(rule, "status", None)
        status_value = str(getattr(status, "value", status) or "")
        expiration = getattr(rule, "expiration", None)
        days = getattr(expiration, "days", None) if expiration is not None else None
        prefix = str(getattr(rule, "prefix", "") or "")
        if (
            status_value == "Enabled"
            and isinstance(days, int)
            and days >= MINIMUM_LIFECYCLE_EXPIRATION_DAYS
            and required_prefix.startswith(prefix)
        ):
            return
        raise ArkPipelineError(
            "TOS 自动过期规则必须启用、覆盖当前前缀，且至少保留 "
            f"{MINIMUM_LIFECYCLE_EXPIRATION_DAYS} 天；已在上传前停止"
        )
    raise ArkPipelineError("找不到指定的 TOS 自动过期规则；已在上传前停止")


def media_inputs(video: Path, images: Sequence[Path]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = [
        {
            "role": "reference_video",
            "index": 0,
            "path": str(video),
            "sha256": sha256_file(video),
            "size_bytes": video.stat().st_size,
            "suffix": video.suffix.lower(),
        }
    ]
    entries.extend(
        {
            "role": "reference_image",
            "index": index,
            "path": str(image),
            "sha256": sha256_file(image),
            "size_bytes": image.stat().st_size,
            "suffix": image.suffix.lower(),
        }
        for index, image in enumerate(images, start=1)
    )
    return entries


def object_key_for(
    environment: ArkEnvironment, fingerprint: str, entry: Mapping[str, Any]
) -> str:
    role = str(entry["role"])
    index = int(entry["index"])
    suffix = str(entry.get("suffix") or "")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ".bin"
    # No original filename reaches the object identity.  The deterministic key
    # supports resumed processing while its raw value remains external state.
    return (
        f"{environment.tos_prefix}/video-replacer/{fingerprint}/"
        f"{role}-{index}-{str(entry['sha256'])[:24]}{suffix}"
    )


def safe_remote_summary(staged_objects: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a manifest-safe reference that cannot reconstruct object keys."""

    digests = []
    for item in staged_objects:
        key = str(item.get("key") or "")
        if key:
            digests.append(sha256_text(key))
    return {
        "remote_object_count": len(digests),
        "remote_object_key_digests": digests,
    }


def _content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _expected_staged_object(
    environment: ArkEnvironment, fingerprint: str, entry: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        "key": object_key_for(environment, fingerprint, entry),
        "role": str(entry["role"]),
        "index": int(entry["index"]),
        "path": str(entry["path"]),
        "sha256": str(entry["sha256"]),
        "size_bytes": int(entry["size_bytes"]),
        "upload_state": "pending",
    }


def stage_media(
    tos_client: Any,
    environment: ArkEnvironment,
    fingerprint: str,
    entries: Sequence[Mapping[str, Any]],
    record: Dict[str, Any],
    persist: Any,
) -> List[Dict[str, Any]]:
    """Stage all bound media only after the paid guard and persist no URLs."""

    existing = record.get("staged_objects")
    if existing is None:
        staged: List[Dict[str, Any]] = []
        record["staged_objects"] = staged
    elif isinstance(existing, list):
        staged = existing
    else:
        raise ArkPipelineError("Ark 外置状态中的 TOS 暂存记录无效")

    expected = [_expected_staged_object(environment, fingerprint, entry) for entry in entries]
    if staged and len(staged) != len(expected):
        raise ArkPipelineError("Ark 外置状态的媒体数量不匹配；已拒绝复用")
    if staged:
        for actual, wanted in zip(staged, expected):
            if not isinstance(actual, dict):
                raise ArkPipelineError("Ark 外置状态的媒体记录无效")
            for key in ("key", "role", "index", "sha256", "size_bytes"):
                if actual.get(key) != wanted[key]:
                    raise ArkPipelineError("Ark 外置状态的媒体身份不匹配；已拒绝复用")
            if actual.get("upload_state") != "uploaded":
                raise ArkPipelineError(
                    "发现未决 TOS 上传记录；为避免错误复用，已阻止自动继续"
                )
        return staged

    for wanted in expected:
        staged.append(wanted)
        persist()
        wanted["upload_state"] = "uploading"
        persist()
        try:
            tos_client.put_object_from_file(
                environment.tos_bucket,
                str(wanted["key"]),
                str(wanted["path"]),
                content_length=int(wanted["size_bytes"]),
                content_type=_content_type(Path(str(wanted["path"]))),
                forbid_overwrite=True,
            )
        except Exception as exc:
            wanted["upload_state"] = "uncertain"
            persist()
            # Do not include the TOS SDK's text because it can echo endpoint
            # details, object keys, or request headers.
            raise ArkPipelineError("TOS 暂存失败；已保存未决状态并阻止自动重传") from exc
        wanted["upload_state"] = "uploaded"
        persist()
    return staged


def presigned_media_urls(
    tos_client: Any,
    environment: ArkEnvironment,
    staged_objects: Sequence[Mapping[str, Any]],
) -> List[str]:
    """Create short-lived URLs in memory only; callers must never persist them."""

    try:
        import tos  # type: ignore
    except ImportError as exc:
        raise ArkPipelineError("TOS 运行时依赖不可用") from exc
    urls: List[str] = []
    try:
        for staged in staged_objects:
            signed = tos_client.pre_signed_url(
                tos.HttpMethodType.Http_Method_Get,
                environment.tos_bucket,
                str(staged["key"]),
                expires=SIGNED_URL_EXPIRES_SECONDS,
            )
            value = str(getattr(signed, "signed_url", "") or "")
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ArkPipelineError("TOS 未返回有效的 HTTPS 签名 URL")
            urls.append(value)
    except ArkPipelineError:
        raise
    except Exception as exc:
        raise ArkPipelineError("无法创建 TOS 短期签名 URL") from exc
    return urls


def _ark_content(
    prompt: str, urls: Sequence[str], request: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    if not urls:
        raise ArkPipelineError("Ark 请求缺少参考视频")
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.append(
        {
            "type": "video_url",
            "video_url": {"url": urls[0]},
            "role": "reference_video",
        }
    )
    for url in urls[1:]:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": url},
                "role": "reference_image",
            }
        )
    return content


def ark_payload(
    model_version: str, prompt: str, urls: Sequence[str], request: Mapping[str, Any]
) -> Dict[str, Any]:
    """Build the documented Ark content-generation request in memory only."""

    return {
        "model": model_version,
        "content": _ark_content(prompt, urls, request),
        "duration": request["duration"],
        "resolution": request["video_resolution"],
        "ratio": request["ratio"],
    }


def _http_json(
    method: str,
    url: str,
    api_key: str,
    payload: Optional[Mapping[str, Any]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ArkPipelineError(f"Ark API 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ArkPipelineError("Ark API 网络请求失败") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArkPipelineError("Ark API 返回了无效 JSON") from exc
    if not isinstance(decoded, dict):
        raise ArkPipelineError("Ark API 返回结构无效")
    return decoded


def _first_value(value: Any, keys: Sequence[str]) -> Optional[Any]:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for child in value.values():
            found = _first_value(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_value(child, keys)
            if found not in (None, ""):
                return found
    return None


def extract_task_id(payload: Mapping[str, Any]) -> str:
    candidates: List[Any] = [payload.get("task_id"), payload.get("id")]
    for root_key in ("data", "result", "task"):
        nested = payload.get(root_key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("task_id"), nested.get("id")])
    for candidate in candidates:
        value = str(candidate or "").strip()
        if TASK_ID_RE.fullmatch(value):
            return value
    raise ArkPipelineError("Ark 提交未返回可验证 task_id；已阻止自动重提")


def normalize_task_status(payload: Mapping[str, Any]) -> str:
    value = _first_value(payload, ("status", "task_status", "state"))
    return str(value or "").strip().casefold() or "unknown"


def is_success_status(status: str) -> bool:
    return status in {"success", "succeeded", "completed", "done"}


def is_failed_status(status: str) -> bool:
    return status in {"failed", "fail", "error", "cancelled", "canceled"}


def extract_video_url(payload: Mapping[str, Any]) -> str:
    # Do not search a generic ``url`` key: completed task payloads may retain
    # the original signed input URLs alongside the generated output.
    def find_video_value(value: Any) -> Optional[Any]:
        if isinstance(value, dict):
            for key in ("output_video_url", "download_url", "video_url"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return candidate
            for child in value.values():
                found = find_video_value(child)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_video_value(child)
                if found not in (None, ""):
                    return found
        return None

    # Ark places completed output under data/result in the documented task
    # response.  Search those result containers before a whole-payload fallback
    # so a retained reference-video URL can never win on dictionary order.
    value: Optional[Any] = None
    for result_key in ("data", "result", "output"):
        nested = payload.get(result_key)
        if nested not in (None, ""):
            value = find_video_value(nested)
        if value not in (None, ""):
            break
    if value in (None, ""):
        value = find_video_value(payload)
    if isinstance(value, dict):
        value = value.get("url") or value.get("video_url") or value.get("download_url")
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ArkPipelineError("Ark 成功响应缺少有效的视频下载 URL")
    return url


def submit_task(
    environment: ArkEnvironment,
    model_version: str,
    prompt: str,
    urls: Sequence[str],
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    endpoint = environment.ark_api_base + "/contents/generations/tasks"
    return _http_json(
        "POST", endpoint, environment.ark_api_key, ark_payload(model_version, prompt, urls, request)
    )


def fetch_task(environment: ArkEnvironment, task_id: str) -> Dict[str, Any]:
    quoted_task_id = urllib.parse.quote(task_id, safe="._:-")
    endpoint = environment.ark_api_base + "/contents/generations/tasks/" + quoted_task_id
    return _http_json("GET", endpoint, environment.ark_api_key)


def download_video(url: str, target: Path) -> None:
    """Download a generated video while keeping its temporary URL out of errors."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.download")
    request = urllib.request.Request(url, headers={"Accept": "video/*"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if temporary.stat().st_size <= 0:
            raise ArkPipelineError("Ark 下载结果为空")
        temporary.replace(target)
    except ArkPipelineError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise ArkPipelineError("Ark 生成视频下载失败") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def cleanup_staged_objects(
    tos_client: Any,
    environment: ArkEnvironment,
    record: Dict[str, Any],
    persist: Any,
) -> str:
    """Delete private inputs after a terminal task; retain only safe status outside state."""

    staged = record.get("staged_objects")
    if not isinstance(staged, list):
        return "not_applicable"
    pending = [item for item in staged if isinstance(item, dict) and item.get("upload_state") == "uploaded"]
    if not pending:
        record["tos_cleanup_status"] = "complete"
        persist()
        return "complete"
    try:
        for item in pending:
            tos_client.delete_object(environment.tos_bucket, str(item["key"]))
            item["upload_state"] = "deleted"
            persist()
    except Exception:
        # An eventual cleanup run can use the owner-only state record.  Do not
        # serialize raw object keys or SDK exception text into a job artifact.
        record["tos_cleanup_status"] = "pending"
        persist()
        return "pending"
    record["tos_cleanup_status"] = "complete"
    persist()
    return "complete"


def command_config(args: argparse.Namespace) -> int:
    """Report static adapter availability without reading credentials or network."""

    profile_id = str(getattr(args, "backend_profile", "") or "").strip()
    if profile_id:
        if not getattr(args, "model_version", None):
            # Config is intentionally still offline; it only reports that a
            # parent-supplied model id will be needed for a real run.
            try:
                profile = get_backend_profile(profile_id)
            except BackendProfileError as exc:
                raise ArkPipelineError(str(exc)) from exc
            if profile.profile_id != ARK_PROFILE_ID:
                raise ArkPipelineError("当前 backend_profile 不属于 Ark 受控执行器")
        else:
            validate_ark_profile(args)
    report = {
        "backend": "volcengine-ark",
        "supported_profiles": [ARK_PROFILE_ID],
        "backend_profile": profile_id or None,
        "model_supplied_by_parent": bool(getattr(args, "model_version", None)),
        "ark_credentials_read": False,
        "tos_credentials_read": False,
        "network_called": False,
        "paid_task_created": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_probe(args: argparse.Namespace) -> int:
    """Perform local-only validation and write a profile-bound preflight manifest."""

    profile, model_version = validate_ark_profile(args)
    video = absolute_file(args.video, "源视频")
    metadata = probe_video(video)
    validate_video_metadata(metadata, profile)
    privacy = build_privacy_record(args.privacy_status, video)
    name = safe_name(args.name or video.stem, "video-replacement")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / name
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_value = str(getattr(args, "prompt_file", "") or "").strip()
    image_values = list(getattr(args, "image", None) or [])
    if image_values and not prompt_value:
        raise ArkPipelineError("probe 绑定参考图时必须同时提供 --prompt-file")
    manifest: Dict[str, Any] = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "created_at": utc_now(),
        "preflight_passed": True,
        "validation_scope": "input_identity_and_profile_metadata_only",
        "platform": "volcengine-ark",
        "transport": ARK_TRANSPORT,
        "backend_profile": profile.profile_id,
        "constraints_digest": profile.constraints_digest,
        "backend_profile_constraints_sha256": profile.constraints_digest,
        "profile_constraints_sha256": profile.constraints_digest,
        "model_version": model_version,
        "video_base64_used": False,
        "active_video": {
            "path": str(video),
            "sha256": sha256_file(video),
            "metadata": metadata,
        },
        "privacy": privacy,
    }
    manifest.update(upload_preparation_binding_from_args(args))
    if prompt_value:
        prompt_file = absolute_file(prompt_value, "提示词文件")
        images = [absolute_file(value, "参考图") for value in image_values]
        if len(images) > MAX_IMAGES:
            raise ArkPipelineError(f"Ark 替换任务最多接收 {MAX_IMAGES} 张参考图")
        manifest["input_bindings"] = build_input_bindings(prompt_file, images)
    manifest_path = output_dir / f"{name}-preflight.json"
    atomic_write_output_json(manifest_path, manifest)
    print(json.dumps({**manifest, "manifest_path": str(manifest_path)}, ensure_ascii=False, indent=2))
    return 0


def _record_persistor(record_path: Path, record: Dict[str, Any]) -> Any:
    def persist() -> None:
        record["updated_at"] = utc_now()
        atomic_write_private_json(record_path, record)

    return persist


def _safe_queued_report(
    profile: BackendProfile,
    request: Mapping[str, Any],
    fingerprint: str,
    task_id: str,
    staged_objects: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    return {
        "mode": "submit-only",
        "backend": "volcengine-ark",
        "backend_profile": profile.profile_id,
        "constraints_digest": profile.constraints_digest,
        "profile_constraints_sha256": profile.constraints_digest,
        "backend_profile_constraints_sha256": profile.constraints_digest,
        "request_fingerprint": fingerprint,
        "task_id": task_id,
        "gen_status": "queued",
        "duration": request["duration"],
        "resolution": request["video_resolution"],
        "ratio": request["ratio"],
        "transport": ARK_TRANSPORT,
        "video_base64_used": False,
        "ark_credentials_read": True,
        "tos_credentials_read": True,
        "paid_task_created": True,
        **safe_remote_summary(staged_objects),
    }


def _safe_task_record(
    profile: BackendProfile,
    request: Mapping[str, Any],
    fingerprint: str,
    record: Mapping[str, Any],
) -> Dict[str, Any]:
    """A public task marker for parent recovery, without TOS object identity."""

    staged = record.get("staged_objects")
    staged_objects = staged if isinstance(staged, list) else []
    return {
        "schema_version": 1,
        "updated_at": utc_now(),
        "backend": "volcengine-ark",
        "backend_profile": profile.profile_id,
        "constraints_digest": profile.constraints_digest,
        "backend_profile_constraints_sha256": profile.constraints_digest,
        "model_version_sha256": request["model_version_sha256"],
        "request_fingerprint": fingerprint,
        "task_id": record.get("task_id"),
        "task_status": record.get("task_status"),
        "submission_state": record.get("submission_state"),
        "tos_cleanup_status": record.get("tos_cleanup_status"),
        **safe_remote_summary(staged_objects),
    }


def command_generate(args: argparse.Namespace) -> int:
    """Stage private inputs, call Ark once, poll, download, and clean up.

    The first branch intentionally precedes *all* credential/TOS/network work.
    This makes a missing paid authorization a guaranteed no-upload outcome.
    """

    if not bool(getattr(args, "confirm_paid", False)):
        raise ArkPipelineError(
            "Ark generate 必须显式传入 --confirm-paid；提交、TOS 上传均已阻止"
        )

    profile, model_version = validate_ark_profile(args)
    video = absolute_file(args.video, "源视频")
    preflight_path = absolute_file(args.preflight_manifest, "preflight manifest")
    preflight = validate_preflight_manifest(preflight_path, video, profile, model_version)
    args._upload_preparation_manifest_sha256 = preflight.get(
        "upload_preparation_manifest_sha256"
    )
    args._preflight_manifest_sha256 = sha256_file(preflight_path)
    prompt_file = absolute_file(args.prompt_file, "提示词文件")
    images = [absolute_file(value, "参考图") for value in args.image]
    if len(images) > MAX_IMAGES:
        raise ArkPipelineError(f"Ark 替换任务最多接收 {MAX_IMAGES} 张参考图")
    validate_input_bindings(preflight, prompt_file, images)
    metadata = dict(preflight["active_video"]["metadata"])
    request = planned_request(video, prompt_file, images, metadata, profile, model_version, args)
    fingerprint = request_fingerprint(request)
    name = safe_name(args.name or video.stem, "video-replacement")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / name
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / f"{name}-manifest.json"
    final_path = output_dir / f"{name}-final.mp4"
    raw_path = output_dir / f"{name}-ark-download.mp4"
    task_path = output_dir / "tasks" / f"{fingerprint}.json"

    # This is the first credential read.  Earlier operations were strictly
    # local and did not import the TOS SDK or touch a remote endpoint.
    environment = load_ark_environment()
    tos_client = create_tos_client(environment)
    verify_tos_lifecycle_fallback(tos_client, environment)
    state_root = request_state_root(getattr(args, "state_dir", None))
    record_path = state_root / "requests" / f"{fingerprint}.json"
    lock_path = state_root / "locks" / f"{fingerprint}.lock"

    with exclusive_lock(lock_path):
        if manifest_path.is_file() and final_path.is_file():
            # The existing public manifest is intentionally safe to print.
            print(manifest_path.read_text(encoding="utf-8"), end="")
            return 0

        if record_path.is_file():
            record = load_json_object(record_path, "Ark 外置请求记录")
            if record.get("request_fingerprint") != fingerprint or record.get("request") != request:
                raise ArkPipelineError("Ark 外置请求记录与当前输入不匹配；已拒绝复用")
        else:
            record = {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "created_at": utc_now(),
                "request_fingerprint": fingerprint,
                "request": request,
                "backend_profile": profile.profile_id,
                "model_version": model_version,
                "task_id": None,
                "task_status": "prepared",
                "staged_objects": [],
                "tos_cleanup_status": "not_started",
            }
        persist = _record_persistor(record_path, record)
        persist()

        def persist_public_task() -> None:
            if str(record.get("task_id") or "").strip():
                atomic_write_output_json(
                    task_path,
                    _safe_task_record(profile, request, fingerprint, record),
                )

        entries = media_inputs(video, images)
        staged_objects = stage_media(
            tos_client, environment, fingerprint, entries, record, persist
        )
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            submission_state = str(record.get("submission_state") or "").strip()
            if submission_state in {"submitting", "uncertain"}:
                raise ArkPipelineError(
                    "检测到没有 task_id 的未决 Ark 提交记录；为避免重复付费，已拒绝重提"
                )
            # Hash all local inputs one final time before constructing URLs and
            # crossing the paid API boundary.
            validate_preflight_manifest(preflight_path, video, profile, model_version)
            validate_input_bindings(preflight, prompt_file, images)
            if planned_request(video, prompt_file, images, metadata, profile, model_version, args) != request:
                raise ArkPipelineError("请求内容在本地校验与提交之间发生变化")
            record["submission_state"] = "submitting"
            persist()
            urls = presigned_media_urls(tos_client, environment, staged_objects)
            try:
                response = submit_task(
                    environment,
                    model_version,
                    prompt_file.read_text(encoding="utf-8").strip(),
                    urls,
                    request,
                )
                task_id = extract_task_id(response)
            except Exception as exc:
                record["submission_state"] = "uncertain"
                record["task_status"] = "uncertain"
                persist()
                if isinstance(exc, ArkPipelineError):
                    raise
                raise ArkPipelineError("Ark 提交失败；已保存未决状态并阻止自动重提") from exc
            record["task_id"] = task_id
            record["submission_state"] = "submitted"
            record["task_status"] = normalize_task_status(response) or "queued"
            persist()
            persist_public_task()

        # This safe marker makes a successfully-created task discoverable by
        # the parent even if a later poll/download returns a non-zero exit.
        persist_public_task()

        if getattr(args, "submit_only", False):
            queued = _safe_queued_report(profile, request, fingerprint, task_id, staged_objects)
            queued_path = output_dir / f"{name}-queued.json"
            atomic_write_output_json(queued_path, queued)
            print(json.dumps({**queued, "queued_path": str(queued_path)}, ensure_ascii=False, indent=2))
            return 0

        deadline = time.monotonic() + max(1, int(getattr(args, "timeout", 3600)))
        interval = max(1, int(getattr(args, "interval", 20)))
        terminal_payload: Optional[Dict[str, Any]] = None
        terminal_status = "unknown"
        while True:
            payload = fetch_task(environment, task_id)
            status = normalize_task_status(payload)
            record["task_status"] = status
            persist()
            persist_public_task()
            if is_success_status(status) or is_failed_status(status):
                terminal_payload = payload
                terminal_status = status
                break
            if time.monotonic() >= deadline:
                raise ArkPipelineError(
                    "Ark 任务仍在运行；已保留外置状态，后续相同提交将继续查询"
                )
            time.sleep(interval)

        if is_failed_status(terminal_status):
            cleanup_staged_objects(tos_client, environment, record, persist)
            record["terminal_status"] = terminal_status
            persist()
            persist_public_task()
            raise ArkPipelineError("Ark 任务已进入失败终态；私有输入已尝试清理")

        if terminal_payload is None:
            raise ArkPipelineError("Ark 终态响应无效")
        # The download URL exists only in this local variable.  It is never
        # stored in record/manifest or included in an exception message.
        download_url = extract_video_url(terminal_payload)
        download_video(download_url, raw_path)
        shutil.copy2(raw_path, final_path)
        if not final_path.is_file() or final_path.stat().st_size <= 0:
            raise ArkPipelineError("Ark 生成文件下载后不存在或为空")
        cleanup_status = cleanup_staged_objects(tos_client, environment, record, persist)
        safe_objects = safe_remote_summary(staged_objects)
        manifest = {
            "generated_at": utc_now(),
            "task_id": task_id,
            "backend": "volcengine-ark",
            "backend_profile": profile.profile_id,
            "constraints_digest": profile.constraints_digest,
            "profile_constraints_sha256": profile.constraints_digest,
            "backend_profile_constraints_sha256": profile.constraints_digest,
            "model_version_sha256": sha256_text(model_version),
            "source": metadata,
            "preflight_manifest": str(preflight_path),
            "preflight_manifest_sha256": sha256_file(preflight_path),
            "reference_images": [str(path) for path in images],
            "prompt_file": str(prompt_file),
            "request_fingerprint": fingerprint,
            "api_duration": request["duration"],
            "resolution": request["video_resolution"],
            "ratio": request["ratio"],
            "raw_output": str(raw_path),
            "final_output": str(final_path),
            "final_size_bytes": final_path.stat().st_size,
            "final_sha256": sha256_file(final_path),
            "validation_scope": "downloaded_file_exists",
            "transport": ARK_TRANSPORT,
            "video_base64_used": False,
            "ark_credentials_read": True,
            "tos_credentials_read": True,
            "tos_cleanup_status": cleanup_status,
            "terminal_status": terminal_status,
            **safe_objects,
        }
        atomic_write_output_json(manifest_path, manifest)
        record["task_status"] = "success"
        record["terminal_status"] = terminal_status
        record["manifest_path"] = str(manifest_path)
        record["final_output"] = str(final_path)
        persist()
        persist_public_task()
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="火山 Ark 视频替换执行器；TOS 签名 URL 与凭证不写入输出"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config", help="检查静态 Ark adapter 配置（不读凭证）")
    config.add_argument("--backend-profile")
    config.add_argument("--model-version")
    config.set_defaults(handler=command_config)

    probe = subparsers.add_parser("probe", help="本地 Ark profile 前检与输入绑定")
    probe.add_argument("--video", required=True)
    probe.add_argument("--prompt-file")
    probe.add_argument("--image", action="append", default=[])
    probe.add_argument(
        "--privacy-status", choices=("workflow-selected-input",), required=True
    )
    probe.add_argument("--name")
    probe.add_argument("--output-dir")
    probe.add_argument("--state-dir")
    probe.add_argument("--backend-profile", required=True)
    probe.add_argument("--model-version", required=True)
    probe.add_argument("--upload-preparation-manifest", required=True)
    probe.set_defaults(handler=command_probe)

    generate = subparsers.add_parser("generate", help="经确认后暂存并提交 Ark 任务")
    generate.add_argument("--video", required=True)
    generate.add_argument("--preflight-manifest", required=True)
    generate.add_argument("--prompt-file", required=True)
    generate.add_argument("--image", action="append", default=[])
    generate.add_argument("--name")
    generate.add_argument("--output-dir")
    generate.add_argument("--duration", type=int)
    generate.add_argument("--resolution", choices=("480p", "720p"))
    generate.add_argument(
        "--ratio", choices=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
    )
    generate.add_argument("--backend-profile", required=True)
    generate.add_argument("--model-version", required=True)
    generate.add_argument(
        "--confirm-paid",
        action="store_true",
        help="确认本次进程获准上传私有 TOS 对象并创建 Ark 付费任务",
    )
    generate.add_argument(
        "--submit-only", action="store_true", help="创建或复用任务后立即返回"
    )
    generate.add_argument("--interval", type=int, default=20)
    generate.add_argument("--timeout", type=int, default=3600)
    generate.add_argument("--state-dir")
    generate.set_defaults(handler=command_generate)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ArkPipelineError, OSError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

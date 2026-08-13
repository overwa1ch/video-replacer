#!/usr/bin/env python3
"""Prepare one local reference video for a backend upload-size constraint.

This module intentionally has no backend client, credentials, or upload code.
It only creates a local, hash-bound preparation manifest.  Callers provide the
backend-specific limits through :class:`UploadConstraints` and must use the
returned ``output_path`` when they later build preflight or submission state.

The public functions are deliberately stdlib-only and accept an injectable
``runner`` so unit tests can exercise the decision logic without invoking a
real FFmpeg binary.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union


PathValue = Union[str, os.PathLike]
CommandRunner = Callable[..., Any]

MANIFEST_SCHEMA_VERSION = 1
OUTPUT_FILENAME = "source-upload-ready.mp4"
MANIFEST_FILENAME = "upload-preparation.json"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30 * 60
MAX_REENCODE_ATTEMPTS = 3

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_INPUT_FORMAT_RE = re.compile(r"^Input #\d+,\s*(.+?),\s*from\s+", re.MULTILINE)
_VIDEO_LINE_RE = re.compile(r"^\s*Stream #.*?:\s*Video:\s*([^,\s]+)(.*)$", re.MULTILINE)
_AUDIO_RE = re.compile(r"Audio:\s*([^,\s]+)")
_DIMENSIONS_RE = re.compile(r"(?<!\d)(\d{2,6})x(\d{2,6})(?!\d)")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*tbr")

_CODEC_ALIASES = {
    "avc": "h264",
    "avc1": "h264",
    "h.264": "h264",
    "h265": "hevc",
    "h.265": "hevc",
    "hev1": "hevc",
    "hvc1": "hevc",
}
_CONTAINER_ALIASES = {
    "m4v": "mp4",
    "m4a": "mp4",
    "3gp": "mp4",
    "3g2": "mp4",
    "mkv": "matroska",
}


class UploadPreparationError(RuntimeError):
    """A local preparation failure that must block remote submission."""

    def __init__(self, message: str, *, manifest_path: Optional[Path] = None):
        super().__init__(message)
        self.manifest_path = manifest_path


@dataclass(frozen=True)
class UploadConstraints:
    """Backend-independent technical constraints for one upload video.

    Empty ``allowed_containers`` or ``allowed_video_codecs`` means that the
    corresponding final-upload property is unrestricted.  Dimension bounds
    are checked independently so a profile can express non-square limits.
    """

    limit_bytes: int
    target_bytes: int
    allowed_containers: Sequence[str] = ("mp4", "mov")
    allowed_video_codecs: Sequence[str] = ("h264", "hevc")
    allowed_audio_codecs: Sequence[str] = ()
    min_duration_seconds: Optional[float] = None
    max_duration_seconds: Optional[float] = None
    min_fps: Optional[float] = None
    max_fps: Optional[float] = None
    min_width: Optional[int] = None
    max_width: Optional[int] = None
    min_height: Optional[int] = None
    max_height: Optional[int] = None
    min_pixels: Optional[int] = None
    max_pixels: Optional[int] = None
    min_aspect_ratio: Optional[float] = None
    max_aspect_ratio: Optional[float] = None
    allowed_heights: Sequence[int] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.limit_bytes, int) or self.limit_bytes <= 0:
            raise ValueError("limit_bytes 必须是正整数")
        if not isinstance(self.target_bytes, int) or self.target_bytes <= 0:
            raise ValueError("target_bytes 必须是正整数")
        if self.target_bytes > self.limit_bytes:
            raise ValueError("target_bytes 不能大于 limit_bytes")

        object.__setattr__(
            self,
            "allowed_containers",
            tuple(
                sorted(
                    {
                        _normalize_container(value)
                        for value in self.allowed_containers
                        if str(value).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "allowed_video_codecs",
            tuple(
                sorted(
                    {
                        _normalize_codec(value)
                        for value in self.allowed_video_codecs
                        if str(value).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "allowed_audio_codecs",
            tuple(
                sorted(
                    {
                        _normalize_codec(value)
                        for value in self.allowed_audio_codecs
                        if str(value).strip()
                    }
                )
            ),
        )
        try:
            allowed_heights = tuple(sorted({int(value) for value in self.allowed_heights}))
        except (TypeError, ValueError) as exc:
            raise ValueError("allowed_heights 必须是整数序列") from exc
        object.__setattr__(self, "allowed_heights", allowed_heights)

        for field_name in (
            "min_duration_seconds",
            "max_duration_seconds",
            "min_fps",
            "max_fps",
            "min_width",
            "max_width",
            "min_height",
            "max_height",
            "min_pixels",
            "max_pixels",
            "min_aspect_ratio",
            "max_aspect_ratio",
        ):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} 必须为正数")

        for lower, upper, label in (
            (self.min_duration_seconds, self.max_duration_seconds, "duration"),
            (self.min_fps, self.max_fps, "fps"),
            (self.min_width, self.max_width, "width"),
            (self.min_height, self.max_height, "height"),
            (self.min_pixels, self.max_pixels, "pixels"),
            (self.min_aspect_ratio, self.max_aspect_ratio, "aspect_ratio"),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{label} 的最小值不能大于最大值")
        if any(value <= 0 for value in self.allowed_heights):
            raise ValueError("allowed_heights 必须全部为正整数")


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _normalize_token(value: object) -> str:
    return str(value).strip().lower().replace(" ", "")


def _normalize_codec(value: object) -> str:
    token = _normalize_token(value)
    return _CODEC_ALIASES.get(token, token)


def _normalize_container(value: object) -> str:
    token = _normalize_token(value).lstrip(".")
    return _CONTAINER_ALIASES.get(token, token)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_resolve(value: PathValue) -> Path:
    return Path(value).expanduser().resolve()


def _file_record(path: Optional[Path], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if path is None:
        return {"path": None, "sha256": None, "size_bytes": None, "metadata": metadata}
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return {
            "path": str(resolved),
            "sha256": None,
            "size_bytes": None,
            "metadata": metadata,
        }
    return {
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
        "metadata": metadata,
    }


def _constraints_payload(constraints: UploadConstraints) -> Dict[str, Any]:
    payload = dataclasses.asdict(constraints)
    payload["allowed_containers"] = list(payload["allowed_containers"])
    payload["allowed_video_codecs"] = list(payload["allowed_video_codecs"])
    return payload


def constraints_digest(constraints: UploadConstraints) -> str:
    """Return a stable digest suitable for binding a manifest to constraints."""

    canonical = json.dumps(
        _constraints_payload(constraints),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _subprocess_runner(command: Sequence[str], *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_command(
    command: Sequence[str],
    runner: Optional[CommandRunner],
    *,
    timeout: Optional[int] = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> _CommandResult:
    """Run an FFmpeg command through the injectable runner.

    A test runner receives ``(command, timeout=...)`` and may return a
    ``subprocess.CompletedProcess``-like object or ``(returncode, stdout,
    stderr)`` tuple.
    """

    active_runner = runner or _subprocess_runner
    result = active_runner(list(command), timeout=timeout)
    if isinstance(result, _CommandResult):
        return result
    if isinstance(result, tuple) and len(result) == 3:
        return _CommandResult(int(result[0]), _as_text(result[1]), _as_text(result[2]))
    try:
        return _CommandResult(
            int(result.returncode),
            _as_text(getattr(result, "stdout", "")),
            _as_text(getattr(result, "stderr", "")),
        )
    except AttributeError as exc:
        raise TypeError(
            "runner 必须返回 CompletedProcess 风格对象或 (returncode, stdout, stderr)"
        ) from exc


def _recording_runner(
    runner: Optional[CommandRunner], commands: List[Dict[str, Any]]
) -> CommandRunner:
    def recorded(command: Sequence[str], *, timeout: Optional[int] = None) -> _CommandResult:
        record: Dict[str, Any] = {"argv": [str(value) for value in command], "returncode": None}
        try:
            result = _run_command(command, runner, timeout=timeout)
        except Exception:
            commands.append(record)
            raise
        record["returncode"] = result.returncode
        commands.append(record)
        return result

    return recorded


def _parse_duration(text: str) -> float:
    match = _DURATION_RE.search(text)
    if not match:
        raise UploadPreparationError("FFmpeg 未返回视频时长")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_video_metadata(
    path: PathValue,
    ffmpeg_path: PathValue,
    *,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Read local video metadata using ``ffmpeg -i`` only.

    FFmpeg commonly exits non-zero for this metadata-only invocation, so the
    parser validates its emitted stream information instead of its exit code.
    """

    video = _safe_resolve(path)
    if not video.is_file():
        raise UploadPreparationError(f"找不到待准备视频：{video}")
    executable = str(Path(ffmpeg_path).expanduser())
    result = _run_command(
        [executable, "-hide_banner", "-nostdin", "-i", str(video)], runner
    )
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    video_match = _VIDEO_LINE_RE.search(text)
    if not video_match:
        raise UploadPreparationError(f"无法读取视频流元数据：{video}")
    codec_token, video_tail = video_match.groups()
    dimensions_match = _DIMENSIONS_RE.search(video_tail)
    if not dimensions_match:
        raise UploadPreparationError(f"无法读取视频分辨率：{video}")
    width, height = (int(value) for value in dimensions_match.groups())
    fps_match = _FPS_RE.search(video_tail) or _TBR_RE.search(video_tail)
    input_match = _INPUT_FORMAT_RE.search(text)
    format_names: List[str] = []
    if input_match:
        format_names = [_normalize_container(value) for value in input_match.group(1).split(",")]

    audio_match = _AUDIO_RE.search(text)
    return {
        "path": str(video),
        "size_bytes": video.stat().st_size,
        "size_mb": round(video.stat().st_size / 1_000_000, 3),
        "container": _normalize_container(video.suffix),
        "format_names": format_names,
        "duration_seconds": round(_parse_duration(text), 3),
        "width": width,
        "height": height,
        "fps": float(fps_match.group(1)) if fps_match else None,
        "video_codec": _normalize_codec(codec_token),
        "audio_codec": _normalize_codec(audio_match.group(1)) if audio_match else None,
        "has_audio": audio_match is not None,
    }


def _validate_invariant_metadata(metadata: Dict[str, Any], constraints: UploadConstraints) -> None:
    try:
        duration = float(metadata["duration_seconds"])
        width = int(metadata["width"])
        height = int(metadata["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UploadPreparationError("视频元数据缺少时长或分辨率") from exc

    fps_value = metadata.get("fps")
    fps = float(fps_value) if fps_value is not None else None
    if constraints.min_duration_seconds is not None and duration < constraints.min_duration_seconds:
        raise UploadPreparationError(
            f"视频时长 {duration:.3f}s 小于 profile 最小值 {constraints.min_duration_seconds}s"
        )
    if constraints.max_duration_seconds is not None and duration > constraints.max_duration_seconds:
        raise UploadPreparationError(
            f"视频时长 {duration:.3f}s 超过 profile 最大值 {constraints.max_duration_seconds}s"
        )
    if constraints.min_fps is not None and (fps is None or fps < constraints.min_fps):
        raise UploadPreparationError(f"视频帧率不满足 profile 最小值 {constraints.min_fps}")
    if constraints.max_fps is not None and (fps is None or fps > constraints.max_fps):
        raise UploadPreparationError(f"视频帧率不满足 profile 最大值 {constraints.max_fps}")
    if constraints.min_width is not None and width < constraints.min_width:
        raise UploadPreparationError(f"视频宽度 {width} 小于 profile 最小值 {constraints.min_width}")
    if constraints.max_width is not None and width > constraints.max_width:
        raise UploadPreparationError(f"视频宽度 {width} 超过 profile 最大值 {constraints.max_width}")
    if constraints.min_height is not None and height < constraints.min_height:
        raise UploadPreparationError(f"视频高度 {height} 小于 profile 最小值 {constraints.min_height}")
    if constraints.max_height is not None and height > constraints.max_height:
        raise UploadPreparationError(f"视频高度 {height} 超过 profile 最大值 {constraints.max_height}")
    pixels = width * height
    if constraints.min_pixels is not None and pixels < constraints.min_pixels:
        raise UploadPreparationError(
            f"视频像素数 {pixels} 小于 profile 最小值 {constraints.min_pixels}"
        )
    if constraints.max_pixels is not None and pixels > constraints.max_pixels:
        raise UploadPreparationError(
            f"视频像素数 {pixels} 超过 profile 最大值 {constraints.max_pixels}"
        )
    aspect_ratio = width / height
    if (
        constraints.min_aspect_ratio is not None
        and aspect_ratio < constraints.min_aspect_ratio
    ):
        raise UploadPreparationError(
            "视频宽高比 "
            f"{aspect_ratio:.6f} 小于 profile 最小值 {constraints.min_aspect_ratio}"
        )
    if (
        constraints.max_aspect_ratio is not None
        and aspect_ratio > constraints.max_aspect_ratio
    ):
        raise UploadPreparationError(
            "视频宽高比 "
            f"{aspect_ratio:.6f} 超过 profile 最大值 {constraints.max_aspect_ratio}"
        )
    if constraints.allowed_heights and height not in constraints.allowed_heights:
        values = "、".join(str(value) for value in constraints.allowed_heights)
        raise UploadPreparationError(f"视频高度必须为 profile 允许的 {values}p")


def _transport_compatible(metadata: Dict[str, Any], constraints: UploadConstraints) -> bool:
    container = _normalize_container(metadata.get("container", ""))
    codec = _normalize_codec(metadata.get("video_codec", ""))
    has_audio = bool(metadata.get("has_audio"))
    audio_codec = _normalize_codec(metadata.get("audio_codec", ""))
    return (
        (not constraints.allowed_containers or container in constraints.allowed_containers)
        and (not constraints.allowed_video_codecs or codec in constraints.allowed_video_codecs)
        and (
            not has_audio
            or not constraints.allowed_audio_codecs
            or audio_codec in constraints.allowed_audio_codecs
        )
    )


def validate_video_metadata(metadata: Dict[str, Any], constraints: UploadConstraints) -> None:
    """Validate one final upload candidate against all supplied constraints."""

    _validate_invariant_metadata(metadata, constraints)
    container = _normalize_container(metadata.get("container", ""))
    codec = _normalize_codec(metadata.get("video_codec", ""))
    if constraints.allowed_containers and container not in constraints.allowed_containers:
        raise UploadPreparationError(
            f"视频容器 {container or 'unknown'} 不在 profile 允许范围内"
        )
    if constraints.allowed_video_codecs and codec not in constraints.allowed_video_codecs:
        raise UploadPreparationError(
            f"视频编码 {codec or 'unknown'} 不在 profile 允许范围内"
        )
    if bool(metadata.get("has_audio")) and constraints.allowed_audio_codecs:
        audio_codec = _normalize_codec(metadata.get("audio_codec", ""))
        if audio_codec not in constraints.allowed_audio_codecs:
            raise UploadPreparationError(
                f"音频编码 {audio_codec or 'unknown'} 不在 profile 允许范围内"
            )


def validate_full_decode(
    path: PathValue,
    ffmpeg_path: PathValue,
    *,
    runner: Optional[CommandRunner] = None,
) -> bool:
    """Raise unless FFmpeg can decode all streams in the candidate video."""

    video = _safe_resolve(path)
    executable = str(Path(ffmpeg_path).expanduser())
    result = _run_command(
        [
            executable,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-map",
            "0",
            "-f",
            "null",
            "-",
        ],
        runner,
    )
    if result.returncode != 0:
        raise UploadPreparationError("FFmpeg 完整解码校验失败")
    return True


def _ffmpeg_version(
    ffmpeg_path: PathValue, *, runner: Optional[CommandRunner] = None
) -> str:
    executable = str(Path(ffmpeg_path).expanduser())
    result = _run_command([executable, "-version"], runner, timeout=60)
    if result.returncode != 0:
        raise UploadPreparationError("无法读取 FFmpeg 版本")
    text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return text.splitlines()[0].strip() if text else "unknown"


def _temporary_video_path(output_dir: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".source-upload-ready-", suffix=".mp4", dir=str(output_dir)
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink(missing_ok=True)
    return temporary


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _commit_temporary_video(temporary: Path, destination: Path) -> Path:
    if not temporary.is_file():
        raise UploadPreparationError("FFmpeg 未生成预期的视频输出")
    os.replace(temporary, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination.resolve()


def _cleanup(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_preserved_timing_and_geometry(
    source_metadata: Dict[str, Any], candidate_metadata: Dict[str, Any]
) -> None:
    if (source_metadata["width"], source_metadata["height"]) != (
        candidate_metadata["width"],
        candidate_metadata["height"],
    ):
        raise UploadPreparationError("准备过程改变了视频分辨率")
    source_duration = float(source_metadata["duration_seconds"])
    candidate_duration = float(candidate_metadata["duration_seconds"])
    if abs(source_duration - candidate_duration) > 0.1:
        raise UploadPreparationError("准备过程改变视频时长超过 0.1 秒")
    source_fps = source_metadata.get("fps")
    candidate_fps = candidate_metadata.get("fps")
    if source_fps is not None and candidate_fps is not None:
        if abs(float(source_fps) - float(candidate_fps)) > 0.1:
            raise UploadPreparationError("准备过程改变了视频帧率")


def _candidate_is_acceptable(
    candidate: Path,
    source_metadata: Dict[str, Any],
    constraints: UploadConstraints,
    ffmpeg_path: PathValue,
    runner: Optional[CommandRunner],
) -> Tuple[Dict[str, Any], bool]:
    metadata = probe_video_metadata(candidate, ffmpeg_path, runner=runner)
    validate_video_metadata(metadata, constraints)
    _validate_preserved_timing_and_geometry(source_metadata, metadata)
    full_decode_passed = validate_full_decode(candidate, ffmpeg_path, runner=runner)
    return metadata, full_decode_passed


def _remux_command(ffmpeg_path: PathValue, source: Path, destination: Path) -> List[str]:
    return [
        str(Path(ffmpeg_path).expanduser()),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]


def _initial_bitrates(
    target_bytes: int, duration_seconds: float, has_audio: bool
) -> Tuple[int, int]:
    total_bitrate = max(1, int(target_bytes * 8 / max(duration_seconds, 0.001)))
    muxing_reserve = max(8_000, min(128_000, int(total_bitrate * 0.03)))
    audio_bitrate = 0
    if has_audio:
        audio_bitrate = min(128_000, max(16_000, int(total_bitrate * 0.12)))
        audio_bitrate = min(audio_bitrate, max(16_000, total_bitrate // 3))
    video_bitrate = max(1_000, total_bitrate - muxing_reserve - audio_bitrate)
    return video_bitrate, audio_bitrate


def _next_bitrates(
    video_bitrate: int,
    audio_bitrate: int,
    actual_size_bytes: int,
    target_bytes: int,
) -> Tuple[int, int]:
    if actual_size_bytes <= 0:
        return max(1_000, video_bitrate // 2), max(0, audio_bitrate // 2)
    scale = min(0.92, (target_bytes / actual_size_bytes) * 0.92)
    next_video = max(1_000, int(video_bitrate * scale))
    next_audio = max(0, int(audio_bitrate * scale))
    if next_video >= video_bitrate and video_bitrate > 1_000:
        next_video = max(1_000, video_bitrate - 1_000)
    if next_audio >= audio_bitrate and audio_bitrate > 0:
        next_audio = max(0, audio_bitrate - 1_000)
    return next_video, next_audio


def _passlog_paths(prefix: Path) -> Tuple[Path, Path]:
    return (
        Path(str(prefix) + "-0.log"),
        Path(str(prefix) + "-0.log.mbtree"),
    )


def _two_pass_commands(
    ffmpeg_path: PathValue,
    source: Path,
    destination: Path,
    passlog_prefix: Path,
    video_bitrate: int,
    audio_bitrate: int,
) -> Tuple[List[str], List[str]]:
    common = [
        str(Path(ffmpeg_path).expanduser()),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        str(video_bitrate),
        "-passlogfile",
        str(passlog_prefix),
    ]
    first_pass = common + [
        "-pass",
        "1",
        "-an",
        "-f",
        "mp4",
        os.devnull,
    ]
    second_pass = common + [
        "-map",
        "0:a:0?",
        "-pass",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate or 16_000),
        "-movflags",
        "+faststart",
        str(destination),
    ]
    return first_pass, second_pass


def _manifest(
    *,
    profile_id: str,
    constraints: UploadConstraints,
    action: str,
    source: Dict[str, Any],
    output: Dict[str, Any],
    full_decode_passed: bool,
    ffmpeg_path: PathValue,
    ffmpeg_version: Optional[str],
    commands: List[Dict[str, Any]],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "backend_profile": profile_id,
        "constraints_digest": constraints_digest(constraints),
        "action": action,
        "source": source,
        "output": output,
        "limit_bytes": constraints.limit_bytes,
        "target_bytes": constraints.target_bytes,
        "full_decode_passed": full_decode_passed,
        "ffmpeg": {
            "path": str(Path(ffmpeg_path).expanduser()),
            "version": ffmpeg_version,
            "commands": commands,
        },
    }
    if error:
        payload["error"] = error
    return payload


def prepare_upload_video(
    input_path: PathValue,
    output_dir: PathValue,
    profile_id: str,
    constraints: UploadConstraints,
    ffmpeg_path: PathValue,
    *,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Prepare one local video and always write ``upload-preparation.json``.

    The function never uploads anything.  On a local failure it writes a
    ``blocked`` manifest (when its output directory is writable) and raises
    :class:`UploadPreparationError`; callers must treat that as a submission
    blocker.  The returned success dict contains the JSON manifest fields plus
    absolute ``output_path`` and ``manifest_path`` convenience values.
    """

    if not isinstance(constraints, UploadConstraints):
        raise TypeError("constraints 必须是 UploadConstraints")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id 不能为空")

    source = _safe_resolve(input_path)
    destination_dir = _safe_resolve(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        destination_dir.chmod(0o700)
    except OSError:
        pass
    manifest_path = destination_dir / MANIFEST_FILENAME
    destination = destination_dir / OUTPUT_FILENAME
    # A previous preparation is immutable input to downstream preflight.  A
    # fresh invocation must use a new output directory rather than replace a
    # ready video or its provenance record underneath a frozen submission.
    if destination.exists() or manifest_path.exists():
        existing = destination if destination.exists() else manifest_path
        raise UploadPreparationError(
            f"上传准备目录已有冻结产物，拒绝覆盖：{existing}",
            manifest_path=manifest_path,
        )
    commands: List[Dict[str, Any]] = []
    recorded_runner = _recording_runner(runner, commands)
    source_metadata: Optional[Dict[str, Any]] = None
    output_metadata: Optional[Dict[str, Any]] = None
    output_path: Optional[Path] = None
    ffmpeg_version: Optional[str] = None
    action = "blocked"
    full_decode_passed = False

    try:
        if not source.is_file():
            raise UploadPreparationError(f"找不到待准备视频：{source}")
        if source == destination.resolve():
            raise UploadPreparationError("上传准备输出不能覆盖输入视频")
        ffmpeg_version = _ffmpeg_version(ffmpeg_path, runner=recorded_runner)
        source_metadata = probe_video_metadata(source, ffmpeg_path, runner=recorded_runner)
        _validate_invariant_metadata(source_metadata, constraints)

        unchanged_is_compatible = (
            source.stat().st_size <= constraints.limit_bytes
            and _transport_compatible(source_metadata, constraints)
        )
        if unchanged_is_compatible:
            validate_video_metadata(source_metadata, constraints)
            full_decode_passed = validate_full_decode(
                source, ffmpeg_path, runner=recorded_runner
            )
            action = "unchanged"
            output_path = source
            output_metadata = source_metadata
        else:
            remux_temporary = _temporary_video_path(destination_dir)
            try:
                remux_result = _run_command(
                    _remux_command(ffmpeg_path, source, remux_temporary), recorded_runner
                )
                if remux_result.returncode == 0 and remux_temporary.is_file():
                    try:
                        remux_metadata, remux_decode_passed = _candidate_is_acceptable(
                            remux_temporary,
                            source_metadata,
                            constraints,
                            ffmpeg_path,
                            recorded_runner,
                        )
                    except UploadPreparationError:
                        remux_metadata = None
                        remux_decode_passed = False
                    if (
                        remux_metadata is not None
                        and remux_temporary.stat().st_size <= constraints.limit_bytes
                    ):
                        output_path = _commit_temporary_video(remux_temporary, destination)
                        output_metadata = remux_metadata
                        full_decode_passed = remux_decode_passed
                        action = "remuxed"
                # A remux that fails, remains oversized, or cannot meet the
                # final format requirements simply falls through to reencode.
            finally:
                remux_temporary.unlink(missing_ok=True)

            if output_path is None:
                video_bitrate, audio_bitrate = _initial_bitrates(
                    constraints.target_bytes,
                    float(source_metadata["duration_seconds"]),
                    bool(source_metadata.get("has_audio")),
                )
                last_size: Optional[int] = None
                for attempt in range(1, MAX_REENCODE_ATTEMPTS + 1):
                    reencode_temporary = _temporary_video_path(destination_dir)
                    passlog_prefix = destination_dir / (
                        f".upload-preparation-{uuid.uuid4().hex}-pass{attempt}"
                    )
                    first_pass, second_pass = _two_pass_commands(
                        ffmpeg_path,
                        source,
                        reencode_temporary,
                        passlog_prefix,
                        video_bitrate,
                        audio_bitrate,
                    )
                    try:
                        first_result = _run_command(first_pass, recorded_runner)
                        if first_result.returncode != 0:
                            raise UploadPreparationError("FFmpeg 两遍编码第一遍失败")
                        second_result = _run_command(second_pass, recorded_runner)
                        if second_result.returncode != 0 or not reencode_temporary.is_file():
                            raise UploadPreparationError("FFmpeg 两遍编码第二遍失败")
                        candidate_metadata, candidate_decode_passed = _candidate_is_acceptable(
                            reencode_temporary,
                            source_metadata,
                            constraints,
                            ffmpeg_path,
                            recorded_runner,
                        )
                        last_size = reencode_temporary.stat().st_size
                        if last_size <= constraints.limit_bytes:
                            output_path = _commit_temporary_video(
                                reencode_temporary, destination
                            )
                            output_metadata = candidate_metadata
                            full_decode_passed = candidate_decode_passed
                            action = "reencoded"
                            break
                        video_bitrate, audio_bitrate = _next_bitrates(
                            video_bitrate,
                            audio_bitrate,
                            last_size,
                            constraints.target_bytes,
                        )
                    finally:
                        reencode_temporary.unlink(missing_ok=True)
                        _cleanup(_passlog_paths(passlog_prefix))
                if output_path is None:
                    suffix = f"（最后一次输出 {last_size} bytes）" if last_size is not None else ""
                    raise UploadPreparationError(
                        f"三次受限重编码后仍无法压入 {constraints.limit_bytes} bytes{suffix}"
                    )

        if output_path is None or output_metadata is None:
            raise UploadPreparationError("上传准备没有产生可用输出")
        if output_path.stat().st_size > constraints.limit_bytes:
            raise UploadPreparationError("最终上传视频仍超过 profile 大小限制")
        validate_video_metadata(output_metadata, constraints)
        if full_decode_passed is not True:
            raise UploadPreparationError("最终上传视频未通过完整解码校验")

        manifest = _manifest(
            profile_id=profile_id.strip(),
            constraints=constraints,
            action=action,
            source=_file_record(source, source_metadata),
            output=_file_record(output_path, output_metadata),
            full_decode_passed=True,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_version=ffmpeg_version,
            commands=commands,
        )
        _atomic_write_json(manifest_path, manifest)
        return {
            **manifest,
            "output_path": str(output_path),
            "manifest_path": str(manifest_path.resolve()),
        }
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        failed_manifest = _manifest(
            profile_id=profile_id.strip(),
            constraints=constraints,
            action="blocked",
            source=_file_record(source, source_metadata),
            output=_file_record(output_path, output_metadata),
            full_decode_passed=False,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_version=ffmpeg_version,
            commands=commands,
            error=message,
        )
        try:
            _atomic_write_json(manifest_path, failed_manifest)
        except OSError:
            # Preserve the original operational failure if a disk problem also
            # prevents the diagnostic manifest from being written.
            pass
        if isinstance(exc, UploadPreparationError):
            exc.manifest_path = manifest_path
            raise
        raise UploadPreparationError(message, manifest_path=manifest_path) from exc


def _parse_many(values: Optional[Sequence[str]], fallback: Sequence[str]) -> Tuple[str, ...]:
    if not values:
        return tuple(fallback)
    flattened: List[str] = []
    for value in values:
        flattened.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(flattened)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="仅在本地准备符合上传大小限制的视频；不会上传或创建远端任务。"
    )
    parser.add_argument("--video", required=True, help="输入视频路径")
    parser.add_argument("--output-dir", required=True, help="准备输出目录")
    parser.add_argument("--backend-profile", required=True, help="已选择的后端 profile 标识")
    parser.add_argument("--limit-bytes", required=True, type=int)
    parser.add_argument("--target-bytes", required=True, type=int)
    parser.add_argument("--ffmpeg", required=True, help="本地 FFmpeg 可执行文件")
    parser.add_argument("--allowed-container", action="append")
    parser.add_argument("--allowed-video-codec", action="append")
    parser.add_argument("--min-duration-seconds", type=float)
    parser.add_argument("--max-duration-seconds", type=float)
    parser.add_argument("--min-fps", type=float)
    parser.add_argument("--max-fps", type=float)
    parser.add_argument("--min-width", type=int)
    parser.add_argument("--max-width", type=int)
    parser.add_argument("--min-height", type=int)
    parser.add_argument("--max-height", type=int)
    parser.add_argument("--min-pixels", type=int)
    parser.add_argument("--max-pixels", type=int)
    parser.add_argument("--min-aspect-ratio", type=float)
    parser.add_argument("--max-aspect-ratio", type=float)
    args = parser.parse_args(argv)
    constraints = UploadConstraints(
        limit_bytes=args.limit_bytes,
        target_bytes=args.target_bytes,
        allowed_containers=_parse_many(args.allowed_container, ("mp4", "mov")),
        allowed_video_codecs=_parse_many(args.allowed_video_codec, ("h264", "hevc")),
        min_duration_seconds=args.min_duration_seconds,
        max_duration_seconds=args.max_duration_seconds,
        min_fps=args.min_fps,
        max_fps=args.max_fps,
        min_width=args.min_width,
        max_width=args.max_width,
        min_height=args.min_height,
        max_height=args.max_height,
        min_pixels=args.min_pixels,
        max_pixels=args.max_pixels,
        min_aspect_ratio=args.min_aspect_ratio,
        max_aspect_ratio=args.max_aspect_ratio,
    )
    manifest = prepare_upload_video(
        args.video,
        args.output_dir,
        args.backend_profile,
        constraints,
        args.ffmpeg,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UploadPreparationError as exc:
        suffix = f"；manifest：{exc.manifest_path}" if exc.manifest_path else ""
        print(f"错误：{exc}{suffix}", file=os.sys.stderr)
        raise SystemExit(1)

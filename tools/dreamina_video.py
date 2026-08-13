#!/usr/bin/env python3
"""Dreamina CLI backend for the source-video replacement workflow.

It exposes ``config``, ``probe``, and ``generate``, never reads Ark or TOS
credentials, and hands local files to the official ``dreamina`` CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from backend_profiles import BackendProfile, BackendProfileError, get_backend_profile
except ModuleNotFoundError as exc:
    # ``tools/test_dreamina_video.py`` loads this file by path, which does not
    # put the sibling tools directory on sys.path.  The production invocation
    # already has it there; this fallback preserves the same local module.
    if exc.name != "backend_profiles":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backend_profiles import BackendProfile, BackendProfileError, get_backend_profile


sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "video-replacements"
DEFAULT_MODEL = "seedance2.5"
DEFAULT_RESOLUTION = "720p"
PREFLIGHT_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
DREAMINA_TRANSPORT = "dreamina_cli_local_upload"
DREAMINA_ADAPTER_NAME = Path(__file__).name
MODEL_VERSION_CHOICES = (
    "seedance2.0",
    "seedance2.0fast",
    "seedance2.0_vip",
    "seedance2.0fast_vip",
    "seedance2.0mini",
    "seedance2.5",
)
RETRY_AUTHORIZATION_DECISION = "RETRY_CONCURRENCY_FAILED_TASKS_SEQUENTIALLY"
STATE_OVERRIDE_NAME = "VIDEO_REPLACER_STATE_DIR"
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
VIDEO_RE = re.compile(r"Video:.*?,\s*(\d+)x(\d+)(?:[\s,])")
FPS_RE = re.compile(r"([\d.]+)\s+fps")
VIDEO_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_]+)")
AUDIO_CODEC_RE = re.compile(r"Audio:\s*([A-Za-z0-9_]+)")
SUBMIT_ID_RE = re.compile(r'"submit_id"\s*:\s*"([^"\s]+)"')
STATUS_RE = re.compile(r'"gen_status"\s*:\s*"([^"\s]+)"')
FAIL_REASON_RE = re.compile(r'"fail_reason"\s*:\s*"([^"]*)"')
SAFE_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "WINDIR",
}


class DreaminaPipelineError(RuntimeError):
    pass


def cli_environment(
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Exclude unrelated provider credentials from Dreamina subprocesses."""

    source_env = os.environ if source is None else source
    allowed = {name.casefold() for name in SAFE_ENV_NAMES}
    environment = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def resolve_dreamina_profile(args: argparse.Namespace) -> Optional[BackendProfile]:
    """Resolve a reviewed profile when the workflow selected one.

    The legacy executor invocation deliberately has no profile argument.  New
    schema-v3 workflow calls provide one, and that profile owns both the
    adapter/transport pair and the allowed model version.  A caller cannot
    point this adapter at an Ark profile or silently substitute a model.
    """

    profile_id = str(getattr(args, "backend_profile", "") or "").strip()
    if not profile_id:
        return None
    try:
        profile = get_backend_profile(profile_id)
    except BackendProfileError as exc:
        raise DreaminaPipelineError(str(exc)) from exc
    if profile.adapter_name != DREAMINA_ADAPTER_NAME:
        raise DreaminaPipelineError(
            f"backend_profile {profile.profile_id} 必须由 {profile.adapter_name} 执行，"
            f"不能交给 {DREAMINA_ADAPTER_NAME}"
        )
    if profile.transport != DREAMINA_TRANSPORT:
        raise DreaminaPipelineError(
            f"backend_profile {profile.profile_id} 的传输方式不属于即梦 CLI 本地上传"
        )
    try:
        expected_model = profile.resolved_model_version()
    except BackendProfileError as exc:
        raise DreaminaPipelineError(str(exc)) from exc
    requested_model = str(getattr(args, "model_version", "") or "").strip()
    if requested_model and requested_model != expected_model:
        raise DreaminaPipelineError(
            f"backend_profile {profile.profile_id} 固定使用 {expected_model}，"
            f"不能传入 {requested_model}"
        )
    return profile


def effective_model_version(
    args: argparse.Namespace, profile: Optional[BackendProfile] = None
) -> str:
    """Return the model selected by a profile, or retain the legacy default."""

    if profile is None:
        profile = resolve_dreamina_profile(args)
    if profile is not None:
        try:
            return profile.resolved_model_version()
        except BackendProfileError as exc:
            raise DreaminaPipelineError(str(exc)) from exc
    return str(getattr(args, "model_version", "") or DEFAULT_MODEL)


def add_profile_binding(
    payload: Dict[str, Any], profile: Optional[BackendProfile]
) -> None:
    """Attach the reviewable profile identity to a new controlled artifact."""

    if profile is None:
        return
    try:
        model_version = profile.resolved_model_version()
    except BackendProfileError as exc:
        raise DreaminaPipelineError(str(exc)) from exc
    payload.update(
        {
            "backend_profile": profile.profile_id,
            "constraints_digest": profile.constraints_digest,
            "backend_profile_constraints_sha256": profile.constraints_digest,
            "model_version": model_version,
        }
    )


def validate_controlled_preflight_profile(
    payload: Dict[str, Any],
    profile: Optional[BackendProfile],
    model_version: Optional[str],
) -> None:
    """Ensure a profile-bound preflight cannot be replayed under another lane."""

    controlled_fields = ("backend_profile", "constraints_digest", "model_version")
    has_controlled_fields = any(field in payload for field in controlled_fields)
    if profile is None:
        if has_controlled_fields:
            raise DreaminaPipelineError(
                "受控 backend_profile 的 preflight 必须使用同一个 --backend-profile 提交"
            )
        return
    try:
        expected_model = profile.resolved_model_version()
    except BackendProfileError as exc:
        raise DreaminaPipelineError(str(exc)) from exc
    if model_version is not None and model_version != expected_model:
        raise DreaminaPipelineError("即梦执行模型与 backend_profile 不匹配")
    if not has_controlled_fields:
        raise DreaminaPipelineError(
            "受控 backend_profile 缺少绑定的 preflight 字段；请重新 probe"
        )
    if payload.get("backend_profile") != profile.profile_id:
        raise DreaminaPipelineError("preflight manifest 的 backend_profile 不匹配")
    if payload.get("constraints_digest") != profile.constraints_digest:
        raise DreaminaPipelineError("preflight manifest 的 profile 约束已变化；请重新 probe")
    if payload.get("model_version") != expected_model:
        raise DreaminaPipelineError("preflight manifest 的 model_version 不匹配")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def absolute_file(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise DreaminaPipelineError(f"找不到{label}：{path}")
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


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    secure_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temporary.replace(path)


def load_json_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DreaminaPipelineError(f"{label}不是有效的 UTF-8 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise DreaminaPipelineError(f"{label}根节点必须是 JSON object：{path}")
    return payload


def find_dreamina(explicit: Optional[str] = None) -> Path:
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    configured = os.getenv("DREAMINA_BINARY", "").strip()
    if configured:
        candidates.append(configured)
    candidates.append(
        str(
            PROJECT_ROOT
            / ".video-replacer"
            / "bin"
            / ("dreamina.exe" if os.name == "nt" else "dreamina")
        )
    )
    detected = shutil.which("dreamina")
    if detected:
        candidates.append(detected)
    candidates.append(str(Path.home() / ".local" / "bin" / "dreamina"))
    for candidate in candidates:
        resolved = Path(candidate).expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise DreaminaPipelineError(
        "找不到 dreamina CLI；请由仓库 Agent 运行受校验的 "
        "tools/install_dreamina.py 安装器"
    )


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
    user_ffmpeg_roots = (
        Path.home() / "Library" / "Python",
        Path.home() / ".local" / "lib",
        Path.home() / "AppData" / "Roaming" / "Python",
    )
    for root in user_ffmpeg_roots:
        for candidate in sorted(
            root.glob("**/site-packages/imageio_ffmpeg/binaries/ffmpeg-*")
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
    raise DreaminaPipelineError(
        "找不到 ffmpeg；请安装 ffmpeg 或 imageio-ffmpeg。"
    )


def probe_video(path: Path) -> Dict[str, Any]:
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
        raise DreaminaPipelineError(f"无法读取视频元数据：{path}")
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
    preflight: Dict[str, Any],
    prompt_file: Path,
    images: Sequence[Path],
    *,
    required: bool,
) -> Dict[str, Any]:
    recorded = preflight.get("input_bindings")
    if not isinstance(recorded, dict):
        if required:
            raise DreaminaPipelineError(
                "最终执行要求 preflight 绑定提示词与有序参考素材哈希；请重新 probe"
            )
        return {}
    current = build_input_bindings(prompt_file, images)
    if recorded != current:
        raise DreaminaPipelineError("提示词或有序参考素材在 preflight 后发生变化")
    return current


def validate_upload_preparation_binding(
    payload: Mapping[str, Any], *, required: bool
) -> None:
    """Verify the parent-bound local upload preparation evidence at submit.

    The parent validates the detailed lineage and media constraints. The
    adapter re-hashes the manifest here so a later local edit cannot be
    swapped in between the parent's plan check and the paid CLI invocation.
    """

    path_value = payload.get("upload_preparation_manifest")
    digest_value = payload.get("upload_preparation_manifest_sha256")
    if path_value in (None, "") and digest_value in (None, ""):
        if required:
            raise DreaminaPipelineError("受控 backend_profile 缺少上传准备 manifest 绑定")
        return
    if not isinstance(path_value, str) or not isinstance(digest_value, str):
        raise DreaminaPipelineError("preflight 的上传准备 manifest 绑定无效")
    manifest_path = Path(path_value).expanduser().resolve()
    if not manifest_path.is_file():
        raise DreaminaPipelineError("preflight 绑定的上传准备 manifest 不存在")
    current_digest = sha256_file(manifest_path)
    if not re.fullmatch(r"[0-9a-f]{64}", digest_value.lower()) or (
        current_digest != digest_value.lower()
    ):
        raise DreaminaPipelineError("upload-preparation manifest 在 preflight 后发生变化")


def upload_preparation_binding_from_args(
    args: argparse.Namespace, *, required: bool
) -> Optional[Dict[str, str]]:
    """Build a hash-bound local evidence reference for a new preflight."""

    value = str(getattr(args, "upload_preparation_manifest", "") or "").strip()
    if not value:
        if required:
            raise DreaminaPipelineError("受控 backend_profile probe 缺少上传准备 manifest")
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise DreaminaPipelineError("上传准备 manifest 不存在")
    return {
        "upload_preparation_manifest": str(path),
        "upload_preparation_manifest_sha256": sha256_file(path),
    }


def build_privacy_record(privacy_status: str, video: Path) -> Dict[str, Any]:
    """Record the workflow-selected input without any visual-review contract."""

    if privacy_status != "workflow-selected-input":
        raise DreaminaPipelineError(
            "probe 必须由 workflow 传入 --privacy-status workflow-selected-input"
        )
    return {
        "status": "workflow-selected-input",
        "active_video_sha256": sha256_file(video),
        "remote_upload_authorized": True,
        "paid_task_authorized": True,
    }

def validate_preflight_manifest(
    path: Path,
    video: Path,
    *,
    profile: Optional[BackendProfile] = None,
    model_version: Optional[str] = None,
) -> Dict[str, Any]:
    payload = load_json_object(path, "preflight manifest")
    if payload.get("schema_version") != PREFLIGHT_SCHEMA_VERSION:
        raise DreaminaPipelineError("preflight manifest schema_version 不受支持")
    if payload.get("preflight_passed") is not True:
        raise DreaminaPipelineError("preflight manifest 未通过 preflight_passed Gate")
    if payload.get("transport") != DREAMINA_TRANSPORT:
        raise DreaminaPipelineError("preflight manifest 不是即梦 CLI 本地上传传输")
    validate_controlled_preflight_profile(payload, profile, model_version)
    validate_upload_preparation_binding(payload, required=profile is not None)
    active = payload.get("active_video")
    if not isinstance(active, dict):
        raise DreaminaPipelineError("preflight manifest 缺少 active_video")
    if Path(str(active.get("path") or "")).expanduser().resolve() != video:
        raise DreaminaPipelineError("preflight manifest 的活动视频路径不匹配")
    current_sha = sha256_file(video)
    if str(active.get("sha256") or "").lower() != current_sha:
        raise DreaminaPipelineError("活动视频 SHA-256 与 preflight manifest 不一致")
    stored_metadata = active.get("metadata")
    if not isinstance(stored_metadata, dict):
        raise DreaminaPipelineError("preflight manifest 缺少活动视频元数据")
    privacy = payload.get("privacy")
    if not isinstance(privacy, dict):
        raise DreaminaPipelineError("preflight manifest 缺少 workflow 输入记录")
    if privacy.get("status") not in {
        "workflow-selected-input",
        "source-video-default-unmasked",
    }:
        raise DreaminaPipelineError("preflight manifest 的 workflow 输入记录无效")
    recorded_input_sha = str(privacy.get("active_video_sha256") or "").lower()
    if recorded_input_sha and recorded_input_sha != current_sha:
        raise DreaminaPipelineError("workflow 输入记录与活动视频不匹配")
    if privacy.get("remote_upload_authorized") is not True:
        raise DreaminaPipelineError("workflow 输入未授权上传")
    if privacy.get("paid_task_authorized") is not True:
        raise DreaminaPipelineError("workflow 输入未授权付费任务")
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
    source_duration: float,
    requested: Optional[int],
    model_version: Optional[str] = None,
) -> int:
    maximum = 30 if model_version == "seedance2.5" else 15
    if requested is not None:
        if not 4 <= requested <= maximum:
            raise DreaminaPipelineError(f"--duration 必须为 4–{maximum} 秒")
        return requested
    return max(4, min(maximum, int(math.floor(source_duration + 0.5))))


def extract_json_object(text: str) -> Dict[str, Any]:
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects[-1] if objects else {}


def recursive_value(value: Any, keys: Sequence[str]) -> Optional[Any]:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        for child in value.values():
            found = recursive_value(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = recursive_value(child, keys)
            if found not in (None, ""):
                return found
    return None


def parse_cli_result(text: str) -> Dict[str, Any]:
    payload = extract_json_object(text)
    submit_id = recursive_value(payload, ("submit_id", "task_id"))
    status = recursive_value(payload, ("gen_status", "status"))
    fail_reason = recursive_value(payload, ("fail_reason", "message", "error"))
    if submit_id in (None, ""):
        match = SUBMIT_ID_RE.search(text)
        submit_id = match.group(1) if match else None
    if status in (None, ""):
        match = STATUS_RE.search(text)
        status = match.group(1) if match else None
    if fail_reason in (None, ""):
        match = FAIL_REASON_RE.search(text)
        fail_reason = match.group(1) if match else None
    return {
        "payload": payload,
        "submit_id": str(submit_id or "").strip() or None,
        "gen_status": str(status or "").strip().casefold() or None,
        "fail_reason": str(fail_reason or "").strip() or None,
    }


def run_cli(command: Sequence[str], timeout: int) -> Tuple[int, str]:
    completed = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=cli_environment(),
    )
    return completed.returncode, completed.stdout


def account_snapshot(binary: Path) -> Dict[str, Any]:
    returncode, output = run_cli([str(binary), "user_credit"], timeout=30)
    payload = extract_json_object(output)
    if returncode != 0 or "total_credit" not in payload:
        tail = "\n".join(output.splitlines()[-8:])
        raise DreaminaPipelineError(f"即梦 CLI 未登录或账户查询失败：{tail}")
    return {
        "logged_in": True,
        "vip_level": payload.get("vip_level"),
        "total_credit": payload.get("total_credit"),
    }


def command_version(binary: Path) -> Dict[str, Any]:
    returncode, output = run_cli([str(binary), "version"], timeout=30)
    if returncode != 0:
        raise DreaminaPipelineError("无法读取 dreamina CLI 版本")
    return extract_json_object(output) or {"raw": output.strip()}


def request_state_root(explicit: Optional[str]) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise DreaminaPipelineError("--state-dir 必须是绝对路径")
        return path.resolve()
    override = os.getenv(STATE_OVERRIDE_NAME, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise DreaminaPipelineError(f"{STATE_OVERRIDE_NAME} 必须是绝对路径")
        return path.resolve()
    if os.name == "nt":
        configured = os.getenv("LOCALAPPDATA", "").strip()
        base = (
            Path(configured).expanduser()
            if configured
            else Path.home() / "AppData" / "Local"
        )
    else:
        configured = os.getenv("XDG_STATE_HOME", "").strip()
        base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return (base / "dreamina-video").resolve()


@contextmanager
def exclusive_lock(path: Path):
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
            raise DreaminaPipelineError("同一即梦请求正在由另一个进程处理") from exc
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


def request_fingerprint(request: Dict[str, Any]) -> str:
    canonical = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def persist_record(record_path: Path, task_path: Path, record: Dict[str, Any]) -> None:
    record["updated_at"] = utc_now()
    atomic_write_json(record_path, record)
    atomic_write_json(task_path, record)


def planned_request(
    video: Path,
    prompt_file: Path,
    images: Sequence[Path],
    metadata: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    profile = resolve_dreamina_profile(args)
    model = effective_model_version(args, profile)
    resolution = args.resolution or DEFAULT_RESOLUTION
    source_duration = float(metadata["duration_seconds"])
    if model != "seedance2.5" and source_duration > 15:
        raise DreaminaPipelineError(f"{model} 的参考视频时长必须在 2–15 秒范围内")
    if model == "seedance2.5" and resolution not in {"480p", "720p"}:
        raise DreaminaPipelineError(f"{model} 当前只支持 480p 或 720p")
    if model not in {"seedance2.0_vip", "seedance2.5"} and resolution != "720p":
        raise DreaminaPipelineError(f"{model} 当前只支持 720p")
    duration = api_duration(
        source_duration, args.duration, model
    )
    ratio = args.ratio or infer_ratio(int(metadata["width"]), int(metadata["height"]))
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "backend": "dreamina-cli",
        "model_version": model,
        "video_sha256": sha256_file(video),
        "prompt_sha256": sha256_file(prompt_file),
        "image_sha256": [sha256_file(path) for path in images],
        "duration": duration,
        "ratio": ratio,
        "video_resolution": resolution,
        "generate_audio": bool(args.generate_audio),
    }
    add_profile_binding(request, profile)
    if profile is not None and (
        hasattr(args, "_upload_preparation_manifest_sha256")
        or hasattr(args, "_preflight_manifest_sha256")
    ):
        upload_sha = str(
            getattr(args, "_upload_preparation_manifest_sha256", "") or ""
        ).lower()
        preflight_sha = str(
            getattr(args, "_preflight_manifest_sha256", "") or ""
        ).lower()
        if re.fullmatch(r"[0-9a-f]{64}", upload_sha) is None:
            raise DreaminaPipelineError("受控 backend_profile 缺少上传准备 manifest SHA-256")
        if re.fullmatch(r"[0-9a-f]{64}", preflight_sha) is None:
            raise DreaminaPipelineError("受控 backend_profile 缺少 preflight manifest SHA-256")
        request["upload_preparation_manifest_sha256"] = upload_sha
        request["preflight_manifest_sha256"] = preflight_sha
    retry_identity = getattr(args, "_retry_identity", None)
    if retry_identity is not None:
        request["retry"] = retry_identity
    return request


def validate_retry_authorization(
    manifest_value: Optional[str],
    state_root: Path,
    name: str,
    retry_attempt: Optional[int],
    model_version: str,
) -> Optional[Dict[str, Any]]:
    """Bind one paid retry to an external, user-issued authorization record."""

    if retry_attempt is None:
        if manifest_value:
            raise DreaminaPipelineError(
                "--retry-authorization-manifest 只能与 --retry-attempt 同时使用"
            )
        return None
    if retry_attempt < 2:
        raise DreaminaPipelineError("--retry-attempt 必须大于或等于 2")
    if not manifest_value:
        raise DreaminaPipelineError("付费重试缺少 --retry-authorization-manifest")
    manifest = absolute_file(manifest_value, "重试授权 manifest")
    authorization_root = (state_root / "retry-authorizations").resolve()
    try:
        manifest.relative_to(authorization_root)
    except ValueError as exc:
        raise DreaminaPipelineError(
            f"重试授权必须位于外置状态目录：{authorization_root}"
        ) from exc
    payload = load_json_object(manifest, "重试授权 manifest")
    batch_id = str(payload.get("batch_id", "")).strip()
    job_id = str(name).removeprefix(f"{batch_id}-")
    if not (
        payload.get("schema_version") == 1
        and payload.get("decision") == RETRY_AUTHORIZATION_DECISION
        and payload.get("issued_by") == "user-in-chat"
        and payload.get("paid_retry_authorized") is True
        and payload.get("mode") == "wait-terminal-before-next"
        and payload.get("model_version") == model_version
        and batch_id
        and job_id
        and name == f"{batch_id}-{job_id}"
    ):
        raise DreaminaPipelineError("重试授权 manifest 的批次、决定或授权字段无效")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != payload.get("max_new_tasks"):
        raise DreaminaPipelineError("重试授权 manifest 的 jobs 或 max_new_tasks 无效")
    matches = [
        item
        for item in jobs
        if isinstance(item, dict)
        and item.get("job_id") == job_id
        and item.get("retry_attempt") == retry_attempt
    ]
    if len(matches) != 1:
        raise DreaminaPipelineError(
            f"重试授权没有唯一绑定 {job_id} attempt {retry_attempt}"
        )
    original_submit_id = str(matches[0].get("original_submit_id", "")).strip()
    if not original_submit_id:
        raise DreaminaPipelineError(f"{job_id} 重试授权缺少 original_submit_id")
    return {
        "attempt": retry_attempt,
        "job_id": job_id,
        "original_submit_id": original_submit_id,
        "model_version": model_version,
        "authorization_manifest_sha256": sha256_file(manifest),
    }


def submit_command(
    binary: Path,
    video: Path,
    prompt: str,
    images: Sequence[Path],
    request: Dict[str, Any],
    poll: int,
) -> List[str]:
    command = [str(binary), "multimodal2video"]
    for image in images:
        command.extend(["--image", str(image)])
    command.extend(["--video", str(video), "--prompt", prompt])
    command.extend(
        [
            "--model_version",
            str(request["model_version"]),
            "--duration",
            str(request["duration"]),
            "--ratio",
            str(request["ratio"]),
            "--video_resolution",
            str(request["video_resolution"]),
            "--poll",
            str(max(0, poll)),
        ]
    )
    return command


def discover_downloaded_video(download_dir: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in download_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".mp4", ".mov"}
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise DreaminaPipelineError(f"即梦任务成功，但下载目录没有视频：{download_dir}")
    return candidates[0]


def query_until_terminal(
    binary: Path,
    submit_id: str,
    download_dir: Path,
    interval: int,
    timeout: int,
    on_update,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while True:
        command = [
            str(binary),
            "query_result",
            "--submit_id",
            submit_id,
            "--download_dir",
            str(download_dir),
        ]
        returncode, output = run_cli(command, timeout=min(120, max(30, timeout)))
        parsed = parse_cli_result(output)
        parsed["returncode"] = returncode
        parsed["output_tail"] = "\n".join(output.splitlines()[-20:])
        last = parsed
        on_update(parsed)
        status = parsed.get("gen_status")
        if returncode == 0 and status == "success":
            return parsed
        if status in {"fail", "failed", "error"}:
            reason = parsed.get("fail_reason") or parsed.get("output_tail")
            raise DreaminaPipelineError(f"即梦任务失败：{reason}")
        if time.monotonic() >= deadline:
            raise DreaminaPipelineError(
                f"即梦任务仍在运行；保留 submit_id={submit_id}，下次将继续查询"
            )
        time.sleep(max(1, interval))


def command_config(args: argparse.Namespace) -> int:
    binary = find_dreamina(args.dreamina_binary)
    returncode, help_output = run_cli([str(binary), "multimodal2video", "-h"], 30)
    required = (
        "--video",
        "--image",
        "--video_resolution",
        "--model_version",
        DEFAULT_MODEL,
    )
    if returncode != 0 or any(flag not in help_output for flag in required):
        raise DreaminaPipelineError("dreamina multimodal2video 命令不完整，请更新 CLI")
    report = {
        "backend": "dreamina-cli",
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "version": command_version(binary),
        "account": account_snapshot(binary),
        "ark_credentials_read": False,
        "tos_credentials_read": False,
        "paid_task_created": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_probe(args: argparse.Namespace) -> int:
    profile = resolve_dreamina_profile(args)
    video = absolute_file(args.video, "源视频")
    metadata = probe_video(video)
    video_sha256 = sha256_file(video)
    privacy = build_privacy_record(args.privacy_status, video)
    name = safe_name(args.name or video.stem, "video-replacement")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / name
    )
    secure_directory(output_dir)
    manifest = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "created_at": utc_now(),
        "preflight_passed": True,
        "validation_scope": "input_identity_and_request_metadata_only",
        "platform": "dreamina",
        "transport": DREAMINA_TRANSPORT,
        "video_base64_used": False,
        "active_video": {
            "path": str(video),
            "sha256": video_sha256,
            "metadata": metadata,
        },
        "privacy": privacy,
    }
    add_profile_binding(manifest, profile)
    binding = upload_preparation_binding_from_args(args, required=profile is not None)
    if binding is not None:
        manifest.update(binding)
    prompt_value = str(getattr(args, "prompt_file", "") or "").strip()
    image_values = list(getattr(args, "image", None) or [])
    if image_values and not prompt_value:
        raise DreaminaPipelineError("probe 绑定参考图时必须同时提供 --prompt-file")
    if prompt_value:
        prompt_file = absolute_file(prompt_value, "提示词文件")
        images = [absolute_file(value, "参考图") for value in image_values]
        if len(images) > 9:
            raise DreaminaPipelineError("参考图最多 9 张")
        manifest["input_bindings"] = build_input_bindings(prompt_file, images)
    manifest_path = output_dir / f"{name}-preflight.json"
    atomic_write_json(manifest_path, manifest)
    print(json.dumps({**manifest, "manifest_path": str(manifest_path)}, ensure_ascii=False, indent=2))
    return 0


def command_generate(args: argparse.Namespace) -> int:
    if not bool(getattr(args, "preview", False)) and not bool(
        getattr(args, "confirm_paid", False)
    ):
        raise DreaminaPipelineError(
            "非 preview 的 generate 必须显式传入 --confirm-paid；提交被阻止"
        )
    profile = resolve_dreamina_profile(args)
    model_version = effective_model_version(args, profile)
    binary = find_dreamina(args.dreamina_binary)
    state_root = request_state_root(args.state_dir)
    video = absolute_file(args.video, "源视频")
    preflight_path = absolute_file(args.preflight_manifest, "preflight manifest")
    preflight = validate_preflight_manifest(
        preflight_path,
        video,
        profile=profile,
        model_version=model_version if profile is not None else None,
    )
    if profile is not None:
        args._upload_preparation_manifest_sha256 = preflight.get(
            "upload_preparation_manifest_sha256"
        )
        args._preflight_manifest_sha256 = sha256_file(preflight_path)
    prompt_file = absolute_file(args.prompt_file, "提示词文件")
    images = [absolute_file(value, "参考图") for value in args.image]
    if len(images) > 9:
        raise DreaminaPipelineError("即梦替换任务最多接收 9 张参考图")
    validate_input_bindings(
        preflight, prompt_file, images, required=not bool(args.preview)
    )
    metadata = dict(preflight["active_video"]["metadata"])
    retry_identity = validate_retry_authorization(
        args.retry_authorization_manifest,
        state_root,
        safe_name(args.name or video.stem, "video-replacement"),
        args.retry_attempt,
        model_version,
    )
    args._retry_identity = retry_identity
    request = planned_request(video, prompt_file, images, metadata, args)
    fingerprint = request_fingerprint(request)
    name = safe_name(args.name or video.stem, "video-replacement")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / name
    )
    secure_directory(output_dir)
    preview = {
        "mode": "preview" if args.preview else "generate",
        "backend": "dreamina-cli",
        "binary": str(binary),
        "request_fingerprint": fingerprint,
        "video": str(video),
        "preflight_manifest": str(preflight_path),
        "prompt_file": str(prompt_file),
        "images": [str(path) for path in images],
        "model_version": request["model_version"],
        "duration": request["duration"],
        "ratio": request["ratio"],
        "video_resolution": request["video_resolution"],
        "local_upload_by_official_cli": True,
        "ark_credentials_read": False,
        "tos_credentials_read": False,
        "paid_task_created": False,
    }
    add_profile_binding(preview, profile)
    preview_suffix = (
        f"-retry-attempt-{args.retry_attempt}"
        if args.retry_attempt is not None
        else ""
    )
    preview_path = output_dir / f"{name}-dreamina-preview{preview_suffix}.json"
    atomic_write_json(preview_path, preview)
    if args.preview:
        print(json.dumps({**preview, "preview_path": str(preview_path)}, ensure_ascii=False, indent=2))
        return 0

    privacy_record = preflight.get("privacy")
    if not isinstance(privacy_record, dict) or privacy_record.get(
        "remote_upload_authorized", True
    ) is not True or privacy_record.get("paid_task_authorized", True) is not True:
        raise DreaminaPipelineError(
            "当前 preflight 未同时获得远端上传与付费任务授权；提交被阻止"
        )

    # Re-hash all credentialed inputs immediately before entering the paid guard.
    current_preflight = validate_preflight_manifest(
        preflight_path,
        video,
        profile=profile,
        model_version=model_version if profile is not None else None,
    )
    validate_input_bindings(current_preflight, prompt_file, images, required=True)
    if planned_request(video, prompt_file, images, metadata, args) != request:
        raise DreaminaPipelineError("请求内容在 preview 与提交之间发生变化")
    account = account_snapshot(binary)
    record_path = state_root / "requests" / f"{fingerprint}.json"
    task_path = output_dir / "tasks" / f"{fingerprint}.json"
    manifest_path = output_dir / f"{name}-manifest.json"
    final_path = output_dir / f"{name}-final.mp4"
    raw_path = output_dir / f"{name}-generated.mp4"
    download_dir = output_dir / "dreamina-download"
    secure_directory(download_dir)

    with exclusive_lock(state_root / "locks" / f"{fingerprint}.lock"):
        if manifest_path.is_file() and final_path.is_file():
            print(manifest_path.read_text(encoding="utf-8"), end="")
            return 0
        record: Dict[str, Any]
        if record_path.is_file():
            record = load_json_object(record_path, "即梦请求记录")
        else:
            record = {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "created_at": utc_now(),
                "request_fingerprint": fingerprint,
                "request": request,
                "name": name,
                "output_dir": str(output_dir),
                "task_id": None,
                "submit_id": None,
                "gen_status": "prepared",
                "account_before": account,
            }
            persist_record(record_path, task_path, record)

        submit_id = str(record.get("submit_id") or record.get("task_id") or "").strip()
        if not submit_id:
            if record.get("gen_status") in {"submitting", "uncertain"}:
                raise DreaminaPipelineError(
                    "检测到没有 submit_id 的未决提交记录；为避免重复扣费，已拒绝重提。"
                    f"请先用 dreamina list_task 对账：{record_path}"
                )
            locked_preflight = validate_preflight_manifest(
                preflight_path,
                video,
                profile=profile,
                model_version=model_version if profile is not None else None,
            )
            validate_input_bindings(locked_preflight, prompt_file, images, required=True)
            if planned_request(video, prompt_file, images, metadata, args) != request:
                raise DreaminaPipelineError("请求内容在取得防重锁后发生变化")
            record["gen_status"] = "submitting"
            persist_record(record_path, task_path, record)
            prompt = prompt_file.read_text(encoding="utf-8").strip()
            command = submit_command(
                binary,
                video,
                prompt,
                images,
                request,
                poll=min(30, max(0, args.timeout)),
            )
            try:
                returncode, output = run_cli(command, timeout=max(60, min(args.timeout + 60, 600)))
            except Exception:
                record["gen_status"] = "uncertain"
                persist_record(record_path, task_path, record)
                raise
            parsed = parse_cli_result(output)
            submit_id = str(parsed.get("submit_id") or "").strip()
            record.update(
                {
                    "submit_id": submit_id or None,
                    "task_id": submit_id or None,
                    "gen_status": parsed.get("gen_status") or "uncertain",
                    "submit_returncode": returncode,
                    "submit_output_tail": "\n".join(output.splitlines()[-20:]),
                }
            )
            persist_record(record_path, task_path, record)
            if not submit_id:
                raise DreaminaPipelineError(
                    "即梦提交没有返回 submit_id；状态按 uncertain 保存，已禁止自动重提"
                )
            if returncode != 0:
                raise DreaminaPipelineError(
                    f"即梦提交命令退出码 {returncode}；已保存 submit_id={submit_id}"
                )

        if args.submit_only:
            record["gen_status"] = "queued"
            persist_record(record_path, task_path, record)
            queued = {
                **preview,
                "mode": "submit-only",
                "task_id": submit_id,
                "submit_id": submit_id,
                "gen_status": "queued",
                "paid_task_created": True,
            }
            queued_path = output_dir / f"{name}-queued.json"
            atomic_write_json(queued_path, queued)
            print(
                json.dumps(
                    {**queued, "queued_path": str(queued_path)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        def update_record(parsed: Dict[str, Any]) -> None:
            record["gen_status"] = parsed.get("gen_status") or record.get("gen_status")
            record["fail_reason"] = parsed.get("fail_reason")
            record["query_output_tail"] = parsed.get("output_tail")
            persist_record(record_path, task_path, record)

        terminal = query_until_terminal(
            binary,
            submit_id,
            download_dir,
            interval=args.interval,
            timeout=args.timeout,
            on_update=update_record,
        )
        downloaded = discover_downloaded_video(download_dir)
        shutil.copy2(downloaded, raw_path)
        shutil.copy2(raw_path, final_path)
        if not final_path.is_file():
            raise DreaminaPipelineError("生成文件下载后不存在")
        manifest = {
            "generated_at": utc_now(),
            "task_id": submit_id,
            "submit_id": submit_id,
            "backend": "dreamina-cli",
            "model": request["model_version"],
            "source": metadata,
            "preflight_manifest": str(preflight_path),
            "preflight_manifest_sha256": sha256_file(preflight_path),
            "reference_images": [str(path) for path in images],
            "prompt_file": str(prompt_file),
            "request_fingerprint": fingerprint,
            "api_duration": request["duration"],
            "resolution": request["video_resolution"],
            "ratio": request["ratio"],
            "raw_download": str(downloaded),
            "raw_output": str(raw_path),
            "final_output": str(final_path),
            "final_size_bytes": final_path.stat().st_size,
            "final_sha256": sha256_file(final_path),
            "validation_scope": "downloaded_file_exists",
            "transport": DREAMINA_TRANSPORT,
            "video_base64_used": False,
            "ark_credentials_read": False,
            "tos_credentials_read": False,
            "tos_cleanup_status": "not_applicable",
            "terminal_status": terminal.get("gen_status"),
        }
        add_profile_binding(manifest, profile)
        atomic_write_json(manifest_path, manifest)
        record.update(
            {
                "gen_status": "success",
                "final_output": str(final_path),
                "manifest": str(manifest_path),
            }
        )
        persist_record(record_path, task_path, record)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="即梦 CLI 视频替换远端执行器；不读取 Ark/TOS 凭证"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config", help="检查 dreamina CLI、登录态和积分")
    config.add_argument("--dreamina-binary")
    config.set_defaults(handler=command_config)

    probe = subparsers.add_parser("probe", help="本地技术前检与 workflow 输入绑定")
    probe.add_argument("--video", required=True)
    probe.add_argument("--prompt-file")
    probe.add_argument("--image", action="append", default=[])
    probe.add_argument(
        "--privacy-status",
        choices=("workflow-selected-input",),
        required=True,
    )
    probe.add_argument(
        "--backend-profile",
        help="schema-v3 workflow 选择的受控后端 profile；省略时兼容既有批次",
    )
    probe.add_argument(
        "--model-version",
        dest="model_version",
        choices=MODEL_VERSION_CHOICES,
        help="必须与 --backend-profile 固定模型一致",
    )
    probe.add_argument(
        "--upload-preparation-manifest",
        help="schema-v3 本地上传准备证据；受控 profile 必填",
    )
    probe.add_argument("--name")
    probe.add_argument("--output-dir")
    probe.set_defaults(handler=command_probe)

    generate = subparsers.add_parser("generate", help="预览或执行即梦全能参考视频任务")
    generate.add_argument("--video", required=True)
    generate.add_argument("--preflight-manifest", required=True)
    generate.add_argument("--prompt-file", required=True)
    generate.add_argument("--image", action="append", default=[])
    generate.add_argument("--name")
    generate.add_argument("--output-dir")
    generate.add_argument("--duration", type=int)
    generate.add_argument("--resolution", choices=("480p", "720p", "1080p", "4k"))
    generate.add_argument(
        "--ratio", choices=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
    )
    generate.add_argument(
        "--model-version",
        dest="model_version",
        choices=MODEL_VERSION_CHOICES,
    )
    generate.add_argument(
        "--backend-profile",
        help="与 preflight 绑定的受控后端 profile；省略时兼容既有批次",
    )
    generate.add_argument("--generate-audio", action="store_true")
    generate.add_argument("--preview", action="store_true")
    generate.add_argument(
        "--confirm-paid",
        action="store_true",
        help="确认本次进程获准创建付费任务；非 preview generate 必须显式提供",
    )
    generate.add_argument(
        "--submit-only",
        action="store_true",
        help="创建或复用任务后立即返回，不等待生成完成",
    )
    generate.add_argument(
        "--retry-attempt",
        type=int,
        help="明确授权后的重试序号；首次重试使用 2",
    )
    generate.add_argument(
        "--retry-authorization-manifest",
        help="位于外置状态目录、绑定原失败任务 ID 的用户重试授权",
    )
    generate.add_argument("--interval", type=int, default=20)
    generate.add_argument("--timeout", type=int, default=3600)
    generate.add_argument("--state-dir")
    generate.add_argument("--dreamina-binary")
    generate.set_defaults(handler=command_generate)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (DreaminaPipelineError, OSError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

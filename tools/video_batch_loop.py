#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# The declaration is load-bearing, not decorative: past ~200 KB this file trips
# a CPython 3.9 tokenizer buffer-boundary bug and refuses to run with
# "Non-UTF-8 code ... no encoding declared". macOS ships 3.9 as python3 and the
# JavaScript control plane spawns [python, script] directly, so without it the
# module still imports — every test stays green — while the live pipeline dies
# at launch.
"""Folder-driven, node-orchestrated video replacement batch loop."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    # Some validation modules load this file through importlib.spec instead of
    # invoking it as a script, so Python has not added tools/ to sys.path.
    sys.path.insert(0, str(SCRIPT_ROOT))

from backend_profiles import (  # noqa: E402
    BackendProfile,
    BackendProfileError,
    get_backend_profile,
)
from codex_node_home import (  # noqa: E402
    CodexNodeHomeError,
    NODE_HOME_DIRECTORY,
    locked_node_home,
    validate_node_home,
)
from codex_artifact import CodexArtifactError, verify_windows_codex  # noqa: E402
from state_paths import StatePathError, resolve_state_root  # noqa: E402
from upload_preparation import (  # noqa: E402
    MANIFEST_FILENAME as UPLOAD_MODULE_MANIFEST_FILENAME,
    OUTPUT_FILENAME as UPLOAD_MODULE_OUTPUT_FILENAME,
    UploadConstraints,
    UploadPreparationError,
    constraints_digest as upload_constraints_digest,
    prepare_upload_video,
    probe_video_metadata,
    validate_full_decode,
    validate_video_metadata,
)

DEFAULT_LOOP_ROOT = PROJECT_ROOT / "workspace" / "video-loop"
STATE_NAMES = (
    "inbox",
    "needs-input",
    "ready",
    "running",
    "review",
    "blocked",
    "completed",
    "logs",
)
# Source inputs retain their original container extension until the local
# upload-preparation step decides that a remux or reencode is necessary.
# Renaming a MOV byte stream to .mp4 would make later container validation
# misleading, so the non-mosaic active-copy filename is suffix-preserving.
VIDEO_SUFFIXES = {".mp4", ".mov"}
TEMP_SUFFIXES = {".crdownload", ".download", ".part", ".tmp"}
VIDEO_ID_RE = re.compile(r"\bV(\d{3})\b", re.IGNORECASE)
VIDEO_RANGE_RE = re.compile(
    r"V(\d{3})\s*(?:-|–|—|~|至|到)\s*V?(\d{3})", re.IGNORECASE
)
DEFAULT_RULE_RE = re.compile(r"^\s*(?:默认|全部|所有视频)\s*[:：]\s*(.+?)\s*$")
PLACEHOLDERS = {"", "待填写", "待补充", "todo", "tbd", "-"}
COMPLETED_STATUS = "COMPLETED"
BLOCKED_STATUSES = {"BLOCKED", "FAILED"}
PREPARED_STATUS = "READY_FOR_SUBMISSION"
NODE_COMPLETE_STATUS = "COMPLETE"
RETRY_AUTHORIZATION_DECISION = "RETRY_CONCURRENCY_FAILED_TASKS_SEQUENTIALLY"
ACTIVE_REMOTE_STATUSES = {"prepared", "submitting", "uncertain", "queued", "querying"}
DEFAULT_MAX_BATCH_VIDEOS = 15
DEFAULT_MIN_FREE_GIB = 10.0
DEFAULT_DAILY_PAID_LIMIT = 1
DEFAULT_NODE_EXEC_TIMEOUT_SECONDS = 1200
VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS = 0.75
VIDEO_PROMPT_MAX_FRAMES = 48
VIDEO_PROMPT_SAMPLER_RECIPE = "ffmpeg-uniform-v1-0.75s-48-scale1280-q3"
SOURCE_EVIDENCE_CACHE_SCHEMA_VERSION = 1
SOURCE_EVIDENCE_INDEX_FILENAME = "source-evidence-index.json"
PAYMENT_AUTH_TOKEN_ENV = "VIDEO_LOOP_PAYMENT_AUTH_TOKEN"
PAYMENT_CHECKPOINT_TTL_SECONDS = 7200
PAYMENT_CHECKPOINT_CLOCK_SKEW_SECONDS = 30
OBSERVATION_STATE_FILE = ".watch-observations.json"
ASSEMBLING_PREFIX = ".assembling-"
PROMPT_FILENAME = "prompt.txt"
REFERENCE_BINDING_FILENAME = "reference-binding.json"
JOB_BINDINGS_FILENAME = "job-bindings.json"
PROMPT_PIPELINE_VERSION = "video-to-prompt-v3-parent-composed"
RETIRED_PROMPT_PIPELINE_VERSIONS = {
    "video-to-prompt-v1",
    "video-to-prompt-v2-sampled-readonly",
}
VIDEO_TO_PROMPT_MODEL = "gpt-5.6-terra"
ACTIVE_VIDEO_COPY_FILENAME = "source-active.mp4"
UPLOAD_PREPARATION_FILENAME = "upload-preparation.json"
UPLOAD_READY_FILENAME = "source-upload-ready.mp4"
PARENT_PROBE_LOG_FILENAME = "parent-probe.log"
MAX_REFERENCE_IMAGES = 9
BATCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
NODE_CONTRACT_ROOT = SCRIPT_ROOT / "video-replacement-node-contracts"
VIDEO_TO_PROMPT_MODEL_CATALOG = SCRIPT_ROOT / "video-to-prompt-model-catalog.json"
PROMPT_GATE_ERROR_PREFIX = "提示词格式 Gate 未通过："
IMMUTABLE_REQUIREMENTS_HEADER = "最高优先级用户要求（父层固定，逐项执行）："
NODE_EXECUTION_HEADER = "模型生成的逐镜执行说明："
PROMPT_RESERVED_SECTION_MARKERS = (
    "素材绑定：",
    IMMUTABLE_REQUIREMENTS_HEADER,
    NODE_EXECUTION_HEADER,
)
PRIVACY_MODES = {"none", "mosaic_required"}
FACE_MOSAIC_SCRIPT = SCRIPT_ROOT / "privacy" / "face_mosaic.py"


def safe_batch_name(name: str) -> bool:
    """Use one portable batch-name contract on every supported OS."""

    if not BATCH_NAME_RE.fullmatch(name) or name.endswith("."):
        return False
    return name.split(".", 1)[0].casefold() not in WINDOWS_RESERVED_BASENAMES


def active_video_copy_filename(source: Path) -> str:
    """Return the fixed active-copy basename without relabelling its container."""

    suffix = source.suffix.casefold()
    if suffix not in VIDEO_SUFFIXES:
        raise LoopError(f"源视频格式不受支持：{source}")
    return f"{Path(ACTIVE_VIDEO_COPY_FILENAME).stem}{suffix}"


@dataclass(frozen=True)
class ExecutorSpec:
    path: Path
    transport: str
    # The two defaults preserve the injected test executor contract used by
    # frozen schema-v1/v2 batches. Profile-managed schema-v3 batches always
    # fill these fields from backend_profiles.py.
    profile_id: str = "legacy_dreamina_cli_seedance_2_5"
    model_version: Optional[str] = "seedance2.5"
    supports_authorized_retry: bool = True


class ObservationTracker:
    """Persist watcher-owned stability and save-order observations.

    Source mtimes are used only as change signals. Their age and ordering are
    never trusted, because copy tools can preserve timestamps from another
    machine. A path must be unchanged across at least two watcher scans.
    """

    def __init__(
        self,
        loop_root: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.loop_root = loop_root.expanduser().resolve()
        self.path = self.loop_root / OBSERVATION_STATE_FILE
        self.clock = clock
        self.entries: Dict[str, Dict[str, object]] = {}
        self.batches: Dict[str, Dict[str, object]] = {}
        self._seen_this_scan: Set[str] = set()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return
        entries = value.get("entries")
        batches = value.get("batches")
        if isinstance(entries, dict):
            self.entries = {
                str(key): dict(item)
                for key, item in entries.items()
                if isinstance(item, dict)
            }
        if isinstance(batches, dict):
            self.batches = {
                str(key): dict(item)
                for key, item in batches.items()
                if isinstance(item, dict)
            }

    def begin_scan(self) -> float:
        self._seen_this_scan.clear()
        return self.clock()

    def _key(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        try:
            return str(resolved.relative_to(self.loop_root))
        except ValueError:
            return str(resolved)

    @staticmethod
    def _stat_signature(path: Path) -> Dict[str, int]:
        stat = path.stat()
        return {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns),
            "inode": int(getattr(stat, "st_ino", 0)),
            "device": int(getattr(stat, "st_dev", 0)),
        }

    def observe(self, path: Path, now: Optional[float] = None) -> Dict[str, object]:
        observed_at = self.clock() if now is None else now
        key = self._key(path)
        signature = self._stat_signature(path)
        existing = self.entries.get(key)
        if not isinstance(existing, dict) or existing.get("signature") != signature:
            entry: Dict[str, object] = {
                "signature": signature,
                "unchanged_since": observed_at,
                "observations": 1,
            }
            self.entries[key] = entry
            self._dirty = True
        else:
            entry = existing
            if key not in self._seen_this_scan:
                observations = min(2, int(entry.get("observations", 1)) + 1)
                if observations != entry.get("observations"):
                    entry["observations"] = observations
                    self._dirty = True
        self._seen_this_scan.add(key)
        return entry

    def stable(
        self, path: Path, stable_seconds: float, now: Optional[float] = None
    ) -> bool:
        observed_at = self.clock() if now is None else now
        entry = self.observe(path, observed_at)
        return (
            int(entry.get("observations", 0)) >= 2
            and observed_at - float(entry.get("unchanged_since", observed_at))
            >= max(stable_seconds, 0.0)
        )

    def requirements_follow_inputs(
        self, batch: Path, now: Optional[float] = None
    ) -> bool:
        observed_at = self.clock() if now is None else now
        inputs = material_files(batch)
        requirements = batch / "requirements.txt"
        if not inputs or not requirements.is_file():
            return False
        input_snapshot = [
            {
                "path": str(path.relative_to(batch)),
                "signature": self._stat_signature(path),
            }
            for path in inputs
        ]
        requirements_signature: Dict[str, object] = {
            **self._stat_signature(requirements),
            "sha256": sha256_file(requirements),
        }
        key = self._key(batch)
        existing = self.batches.get(key)
        if not isinstance(existing, dict):
            self.batches[key] = {
                "input_snapshot": input_snapshot,
                "input_revision": 1,
                "requirements_signature": requirements_signature,
                "requirements_confirmed_revision": None,
                "observed_at": observed_at,
            }
            self._dirty = True
            return False

        input_changed = existing.get("input_snapshot") != input_snapshot
        requirements_changed = (
            existing.get("requirements_signature") != requirements_signature
        )
        if input_changed:
            existing["input_snapshot"] = input_snapshot
            existing["input_revision"] = int(existing.get("input_revision", 0)) + 1
            existing["requirements_confirmed_revision"] = None
            self._dirty = True
        if requirements_changed:
            existing["requirements_signature"] = requirements_signature
            if not input_changed:
                existing["requirements_confirmed_revision"] = int(
                    existing.get("input_revision", 0)
                )
            self._dirty = True
        if input_changed or requirements_changed:
            existing["observed_at"] = observed_at
        return existing.get("requirements_confirmed_revision") == existing.get(
            "input_revision"
        )

    def baseline_batch(self, batch: Path, now: Optional[float] = None) -> None:
        observed_at = self.clock() if now is None else now
        inputs = material_files(batch)
        requirements = batch / "requirements.txt"
        if not inputs or not requirements.is_file():
            return
        self.batches[self._key(batch)] = {
            "input_snapshot": [
                {
                    "path": str(path.relative_to(batch)),
                    "signature": self._stat_signature(path),
                }
                for path in inputs
            ],
            "input_revision": 1,
            "requirements_signature": {
                **self._stat_signature(requirements),
                "sha256": sha256_file(requirements),
            },
            "requirements_confirmed_revision": None,
            "observed_at": observed_at,
        }
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "updated_at": utc_now(),
                "entries": self.entries,
                "batches": self.batches,
            },
        )
        self._dirty = False


class LoopError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def natural_key(value: str) -> List[object]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    chmod_private(path)


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link_like(path: Path) -> bool:
    """Reject both ordinary symlinks and native Windows junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def linked_descendants(root: Path) -> List[Path]:
    """Find link-like entries without descending into reparse-point trees."""

    if is_link_like(root):
        return [root]
    found: List[Path] = []
    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        retained: List[str] = []
        for name in directory_names:
            candidate = current_path / name
            if is_link_like(candidate):
                found.append(candidate)
            else:
                retained.append(name)
        directory_names[:] = retained
        for name in file_names:
            candidate = current_path / name
            if is_link_like(candidate):
                found.append(candidate)
    return found


def path_contains_link_like(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    # macOS exposes /var and /tmp through system symlinks. Canonicalize only
    # that platform prefix, then inspect the actual user-controlled path.
    if (
        sys.platform == "darwin"
        and len(absolute.parts) > 1
        and absolute.parts[1] in {"var", "tmp", "etc"}
    ):
        absolute = absolute.resolve(strict=False)
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if is_link_like(cursor):
            return True
    return False


def chmod_private(path: Path) -> None:
    if (
        os.name != "nt"
        and not is_link_like(path)
        and stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        path.chmod(0o600)


def make_tree_private(root: Path) -> None:
    """Restrict Loop directories and files without following symlinks."""
    if os.name == "nt" or is_link_like(root) or not root.exists():
        return
    if root.is_dir() and stat.S_IMODE(root.stat().st_mode) != 0o700:
        root.chmod(0o700)
    for item in root.rglob("*"):
        if is_link_like(item):
            continue
        if item.is_dir() and stat.S_IMODE(item.stat().st_mode) != 0o700:
            item.chmod(0o700)
        elif item.is_file() and stat.S_IMODE(item.stat().st_mode) != 0o600:
            item.chmod(0o600)


def material_files(batch: Path) -> List[Path]:
    files: List[Path] = []
    for folder_name in ("videos", "replacements"):
        folder = batch / folder_name
        if folder.is_dir():
            files.extend(
                item
                for item in folder.rglob("*")
                if item.is_file() and not item.name.startswith(".")
            )
    return sorted(files, key=lambda item: natural_key(str(item.relative_to(batch))))


def require_free_space(path: Path, minimum_free_bytes: int) -> None:
    if minimum_free_bytes <= 0:
        return
    free = shutil.disk_usage(path).free
    if free < minimum_free_bytes:
        raise LoopError(
            "磁盘可用空间不足："
            f"当前 {free / 1024**3:.2f} GiB，"
            f"要求至少 {minimum_free_bytes / 1024**3:.2f} GiB；已在提交前停止。"
        )


def ensure_layout(loop_root: Path) -> None:
    loop_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        loop_root.chmod(0o700)
    for name in STATE_NAMES:
        state_root = loop_root / name
        state_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            state_root.chmod(0o700)


@contextmanager
def exclusive_loop_lock(loop_root: Path):
    lock_path = loop_root / ".video-batch-loop.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        chmod_private(lock_path)
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
            raise LoopError("另一个 video_batch_loop 进程正在运行") from exc
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


@contextmanager
def waiting_file_lock(lock_path: Path, *, timeout_seconds: float = 240.0):
    """Take one private cross-process file lock, waiting for the current owner."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_like(lock_path.parent):
        raise LoopError(f"锁目录不得是链接：{lock_path.parent}")
    if os.name != "nt":
        lock_path.parent.chmod(0o700)
    if is_link_like(lock_path):
        raise LoopError(f"锁文件不得是链接：{lock_path}")
    with lock_path.open("a+b") as handle:
        chmod_private(lock_path)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LoopError(f"等待锁超时：{lock_path}") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def visible_children(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return sorted(
        (item for item in path.iterdir() if not item.name.startswith(".")),
        key=lambda item: natural_key(item.name),
    )


def next_automatic_batch_name(loop_root: Path) -> str:
    base = datetime.now().strftime("batch-%Y%m%d-%H%M%S")
    candidate = base
    suffix = 2
    while any(
        (loop_root / state / candidate).exists()
        for state in STATE_NAMES
    ) or (loop_root / "inbox" / f"{ASSEMBLING_PREFIX}{candidate}").exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def collect_loose_inbox_videos(
    loop_root: Path,
    stable_seconds: float,
    observer: ObservationTracker,
    observed_at: float,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Optional[Path]:
    inbox = loop_root / "inbox"
    videos = sorted(
        (
            item
            for item in inbox.iterdir()
            if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES
        ),
        key=lambda item: natural_key(item.name),
    )
    if not videos:
        return None
    if max_batch_videos <= 0:
        raise LoopError("批次视频上限必须大于 0")
    if len(videos) > max_batch_videos:
        raise LoopError(
            f"inbox 有 {len(videos)} 条散落视频，超过单批上限 {max_batch_videos}；"
            "为避免静默拆分需求，请主动拆成多个批次目录。"
        )
    selected = videos
    for video in selected:
        chmod_private(video)
    stable_observations = [
        observer.stable(video, stable_seconds, observed_at) for video in selected
    ]
    if not all(stable_observations):
        return None

    batch_name = next_automatic_batch_name(loop_root)
    staging = inbox / f"{ASSEMBLING_PREFIX}{batch_name}"
    video_root = staging / "videos"
    replacements_root = staging / "replacements"
    moved: List[Tuple[Path, Path]] = []
    try:
        video_root.mkdir(parents=True)
        replacements_root.mkdir(parents=True)
        for video in selected:
            destination = video_root / video.name
            if destination.exists():
                raise LoopError(f"组批目标已存在：{destination}")
            video.replace(destination)
            chmod_private(destination)
            moved.append((video, destination))
        batch = inbox / batch_name
        staging.replace(batch)
        return batch
    except Exception as exc:
        rollback_errors: List[str] = []
        for original, destination in reversed(moved):
            try:
                if destination.exists() and not original.exists():
                    destination.replace(original)
                    chmod_private(original)
            except OSError as rollback_exc:
                rollback_errors.append(f"{destination.name}: {rollback_exc}")
        if not rollback_errors:
            for directory in (replacements_root, video_root, staging):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        detail = f"散落视频组批失败，已回滚：{exc}"
        if rollback_errors:
            detail += "；回滚不完整：" + "；".join(rollback_errors)
        raise LoopError(detail) from exc


def batch_is_stable(
    batch: Path,
    stable_seconds: float,
    observer: ObservationTracker,
    observed_at: float,
) -> bool:
    if not batch.is_dir() or linked_descendants(batch):
        return False
    make_tree_private(batch)
    files = [item for item in batch.rglob("*") if item.is_file()]
    if not files or any(item.suffix.casefold() in TEMP_SUFFIXES for item in files):
        return False
    stable_observations = [
        observer.stable(item, stable_seconds, observed_at) for item in files
    ]
    return all(stable_observations)


def reject_symlinks(batch: Path) -> None:
    linked = linked_descendants(batch)
    if linked:
        sample = "、".join(str(item.relative_to(batch)) for item in linked[:5])
        raise LoopError(f"批次包含符号链接或 Windows junction，已拒绝：{sample}")


def discover_videos(batch: Path) -> List[Path]:
    video_root = batch / "videos"
    if not video_root.is_dir():
        raise LoopError("缺少 videos/ 目录")
    videos = [
        item
        for item in video_root.rglob("*")
        if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES
    ]
    return sorted(
        videos, key=lambda item: natural_key(str(item.relative_to(video_root)))
    )


def _reject_duplicate_basenames(paths: Sequence[Path], label: str) -> None:
    seen: Dict[str, Path] = {}
    for path in paths:
        key = path.name.casefold()
        if key in seen:
            raise LoopError(
                f"{label}存在重复 basename，无法安全绑定："
                f"{seen[key].name}、{path.name}"
            )
        seen[key] = path


def build_batch_index(
    batch: Path, max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS
) -> Dict[str, object]:
    reject_symlinks(batch)
    videos = discover_videos(batch)
    if not videos:
        raise LoopError("videos/ 中没有 MP4 视频")
    if len(videos) > max_batch_videos:
        raise LoopError(
            f"批次包含 {len(videos)} 条视频，超过上限 {max_batch_videos}；请拆分批次。"
        )
    _reject_duplicate_basenames(videos, "源视频")
    jobs = []
    for position, video in enumerate(videos, start=1):
        chmod_private(video)
        jobs.append(
            {
                "id": f"V{position:03d}",
                "filename": video.name,
                "relative_path": video.relative_to(batch).as_posix(),
                "size_bytes": video.stat().st_size,
                "sha256": sha256_file(video),
            }
        )
    return {
        "schema_version": 1,
        "batch_id": batch.name,
        "created_at": utc_now(),
        "jobs": jobs,
    }


def build_reference_index(batch: Path) -> Dict[str, object]:
    reference_root = batch / "replacements"
    reference_root.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (
            item
            for item in reference_root.rglob("*")
            if item.is_file() and not item.name.startswith(".")
        ),
        key=lambda item: natural_key(str(item.relative_to(reference_root))),
    )
    _reject_duplicate_basenames(files, "参考素材")
    references = []
    for position, reference in enumerate(files, start=1):
        chmod_private(reference)
        references.append(
            {
                "id": f"R{position:03d}",
                "filename": reference.name,
                "relative_path": reference.relative_to(batch).as_posix(),
                "size_bytes": reference.stat().st_size,
                "sha256": sha256_file(reference),
            }
        )
    existing_path = batch / "reference-index.json"
    if existing_path.is_file():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and set(existing)
            == {"schema_version", "batch_id", "created_at", "references"}
            and existing.get("schema_version") == 1
            and existing.get("batch_id") == batch.name
            and isinstance(existing.get("created_at"), str)
            and existing.get("references") == references
        ):
            return existing
    return {
        "schema_version": 1,
        "batch_id": batch.name,
        "created_at": utc_now(),
        "references": references,
    }


def requirements_template(index: Dict[str, object]) -> str:
    lines = [
        "# 只填写每条视频要替换什么；编号和原文件名由 Loop 自动维护。",
        "# 新批次在全部需求填写并保存后会自动串行执行；无需移动文件夹。",
        "# 若暂时不想启动，在批次根目录放一个名为 PAUSE 的空文件。",
        "# 示例：V001：替换人物和产品",
        "# 可写：默认：替换产品；V001、V003：同时替换人物；V002：跳过",
        "",
    ]
    for job in index["jobs"]:  # type: ignore[index]
        lines.append(f"{job['id']}：  # {job['filename']}")
    return "\n".join(lines) + "\n"


def write_batch_preview(batch: Path, index: Dict[str, object]) -> None:
    cards = []
    for job in index["jobs"]:  # type: ignore[index]
        relative_path = str(job["relative_path"])
        cards.append(
            f"""<article>
  <h2>{html.escape(str(job["id"]))}</h2>
  <video controls preload="metadata" src="{quote(relative_path, safe="/")}"></video>
  <p>{html.escape(str(job["filename"]))}</p>
</article>"""
        )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(batch.name)} 视频编号</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #f5f5f5; }}
main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
article {{ background: white; border-radius: 12px; padding: 12px; box-shadow: 0 2px 12px #0001; }}
h2 {{ margin: 0 0 8px; font-size: 18px; }}
video {{ width: 100%; max-height: 360px; background: black; border-radius: 8px; }}
p {{ margin: 8px 0 0; overflow-wrap: anywhere; color: #444; }}
</style>
</head>
<body>
<h1>{html.escape(batch.name)} 视频编号</h1>
<main>
{"".join(cards)}
</main>
</body>
</html>
"""
    atomic_write_text(batch / "batch-preview.html", document)


def move_batch(batch: Path, loop_root: Path, target_state: str) -> Path:
    if target_state not in STATE_NAMES:
        raise LoopError(f"未知目标状态：{target_state}")
    destination = loop_root / target_state / batch.name
    if destination.exists():
        raise LoopError(f"目标状态已存在同名批次：{destination}")
    try:
        batch.replace(destination)
    except OSError as exc:
        if destination.exists() and not batch.exists():
            try:
                destination.replace(batch)
            except OSError as rollback_exc:
                raise LoopError(
                    f"批次移动失败且回滚失败：{exc}；{rollback_exc}"
                ) from exc
        raise LoopError(f"批次移动失败，源批次保持不变：{exc}") from exc
    return destination


def write_blocker(batch: Path, title: str, messages: Sequence[str]) -> None:
    lines = [title, "", f"时间：{utc_now()}", ""]
    lines.extend(f"- {message}" for message in messages)
    atomic_write_text(batch / "requirements-check.txt", "\n".join(lines) + "\n")


def _block_stale_assembly(
    staging: Path, loop_root: Path, messages: Sequence[str]
) -> Path:
    write_blocker(staging, "陈旧组批目录需要人工恢复", messages)
    base = staging.name.removeprefix(ASSEMBLING_PREFIX) or "unknown"
    destination = loop_root / "blocked" / f"assembly-recovery-{base}"
    suffix = 2
    while destination.exists():
        destination = loop_root / "blocked" / f"assembly-recovery-{base}-{suffix}"
        suffix += 1
    staging.replace(destination)
    return destination


def recover_stale_assembling_batches(
    loop_root: Path,
    stable_seconds: float,
    observer: ObservationTracker,
    observed_at: float,
) -> Tuple[int, int]:
    inbox = loop_root / "inbox"
    recovered = 0
    blocked = 0
    for staging in sorted(inbox.glob(f"{ASSEMBLING_PREFIX}*"), key=lambda p: p.name):
        if not staging.is_dir():
            continue
        files = [item for item in staging.rglob("*") if item.is_file()]
        observed_paths = files or [staging]
        if any(item.suffix.casefold() in TEMP_SUFFIXES for item in files):
            continue
        stable_observations = [
            observer.stable(item, stable_seconds, observed_at)
            for item in observed_paths
        ]
        if not all(stable_observations):
            continue

        video_root = staging / "videos"
        replacements_root = staging / "replacements"
        staged_videos = sorted(
            (
                item
                for item in video_root.glob("*")
                if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES
            ),
            key=lambda item: natural_key(item.name),
        ) if video_root.is_dir() else []
        expected_files = set(staged_videos)
        unexpected = [item for item in files if item not in expected_files]
        conflicts = [inbox / item.name for item in staged_videos if (inbox / item.name).exists()]
        final_name = staging.name.removeprefix(ASSEMBLING_PREFIX)
        final_conflict = bool(final_name and (inbox / final_name).exists())
        if unexpected or conflicts or final_conflict:
            messages = ["为避免覆盖或误合并，未自动重新组批。"]
            if unexpected:
                messages.append("目录含非预期文件。")
            if conflicts:
                messages.append("inbox 已存在同名散落视频。")
            if final_conflict:
                messages.append("inbox 已存在目标批次目录。")
            _block_stale_assembly(staging, loop_root, messages)
            blocked += 1
            continue

        restored: List[Tuple[Path, Path]] = []
        try:
            for source in staged_videos:
                destination = inbox / source.name
                source.replace(destination)
                chmod_private(destination)
                restored.append((source, destination))
            for directory in (replacements_root, video_root, staging):
                if directory.exists():
                    directory.rmdir()
            recovered += 1
        except OSError as exc:
            rollback_errors: List[str] = []
            for source, destination in reversed(restored):
                try:
                    if destination.exists() and not source.exists():
                        destination.replace(source)
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            detail = [f"自动回滚散落视频失败：{exc}"]
            if rollback_errors:
                detail.append("回滚自身也未完成：" + "；".join(rollback_errors))
            if staging.exists():
                _block_stale_assembly(staging, loop_root, detail)
            blocked += 1
    return recovered, blocked


def prepare_inbox_batch(
    batch: Path,
    loop_root: Path,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    observer: Optional[ObservationTracker] = None,
    observed_at: Optional[float] = None,
) -> Path:
    reject_symlinks(batch)
    make_tree_private(batch)
    (batch / "replacements").mkdir(parents=True, exist_ok=True)
    make_tree_private(batch)
    index = build_batch_index(batch, max_batch_videos=max_batch_videos)
    atomic_write_json(batch / "batch-index.json", index)
    write_batch_preview(batch, index)
    requirements_path = batch / "requirements.txt"
    if not requirements_path.exists():
        atomic_write_text(requirements_path, requirements_template(index))
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "NEEDS_INPUT",
            "updated_at": utc_now(),
            "auto_ready": True,
            "message": "添加参考素材并填写 requirements.txt；最后一次保存后自动执行。",
        },
    )
    destination = move_batch(batch, loop_root, "needs-input")
    if observer is not None:
        observer.baseline_batch(destination, observed_at)
    return destination


def read_index(batch: Path) -> Dict[str, object]:
    path = batch / "batch-index.json"
    if not path.exists():
        raise LoopError("缺少 batch-index.json；请先让批次经过 inbox/")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise LoopError("batch-index.json 格式无效")
    return value


def _hash_without_change(path: Path) -> Tuple[int, str]:
    before = ObservationTracker._stat_signature(path)
    digest = sha256_file(path)
    after = ObservationTracker._stat_signature(path)
    if before != after:
        raise LoopError(f"文件在哈希校验期间发生变化：{path}")
    return int(after["size"]), digest


def _indexed_relative_path(
    batch: Path, raw_value: object, required_root: Path, label: str
) -> Path:
    raw = Path(str(raw_value))
    if raw.is_absolute() or ".." in raw.parts:
        raise LoopError(f"{label} relative_path 非法：{raw}")
    resolved = (batch / raw).resolve()
    try:
        resolved.relative_to(required_root.resolve())
    except ValueError as exc:
        raise LoopError(f"{label} 越过允许目录：{raw}") from exc
    return resolved


def verify_batch_index(
    batch: Path, max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS
) -> Dict[str, object]:
    reject_symlinks(batch)
    index = read_index(batch)
    if index.get("schema_version") != 1 or index.get("batch_id") != batch.name:
        raise LoopError("batch-index.json 的 schema_version 或 batch_id 不匹配")
    jobs = index.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise LoopError("batch-index.json 没有 jobs")
    if len(jobs) > max_batch_videos:
        raise LoopError(
            f"批次包含 {len(jobs)} 条视频，超过上限 {max_batch_videos}；已停止。"
        )
    video_root = batch / "videos"
    expected_paths: List[Path] = []
    for position, raw_job in enumerate(jobs, start=1):
        if not isinstance(raw_job, dict):
            raise LoopError("batch-index.json 的 job 不是对象")
        expected_id = f"V{position:03d}"
        if raw_job.get("id") != expected_id:
            raise LoopError(f"batch-index job 顺序或编号无效：预期 {expected_id}")
        path = _indexed_relative_path(
            batch, raw_job.get("relative_path"), video_root, expected_id
        )
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            raise LoopError(
                f"{expected_id} 索引源视频不存在或不是受支持的 MP4/MOV：{path}"
            )
        if raw_job.get("filename") != path.name:
            raise LoopError(f"{expected_id} filename 与 relative_path 不一致")
        size, digest = _hash_without_change(path)
        if raw_job.get("size_bytes") != size or raw_job.get("sha256") != digest:
            raise LoopError(f"{expected_id} 源视频在摄入后发生变化；拒绝继续")
        expected_paths.append(path)
    _reject_duplicate_basenames(expected_paths, "源视频")
    actual_paths = discover_videos(batch)
    if {path.resolve() for path in actual_paths} != set(expected_paths):
        raise LoopError("videos/ 的实际文件集合与 batch-index.json 不一致")
    return index


def read_reference_index(batch: Path) -> Dict[str, object]:
    path = batch / "reference-index.json"
    if not path.is_file():
        raise LoopError("缺少 reference-index.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError("reference-index.json 不是有效 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("references"), list):
        raise LoopError("reference-index.json 格式无效")
    return value


def verify_reference_index(batch: Path) -> Dict[str, object]:
    reject_symlinks(batch)
    index = read_reference_index(batch)
    if index.get("schema_version") != 1 or index.get("batch_id") != batch.name:
        raise LoopError("reference-index.json 的 schema_version 或 batch_id 不匹配")
    reference_root = batch / "replacements"
    references = index.get("references")
    assert isinstance(references, list)
    expected_paths: List[Path] = []
    for position, raw_reference in enumerate(references, start=1):
        if not isinstance(raw_reference, dict):
            raise LoopError("reference-index.json 的 reference 不是对象")
        expected_id = f"R{position:03d}"
        if raw_reference.get("id") != expected_id:
            raise LoopError(f"reference-index 编号无效：预期 {expected_id}")
        path = _indexed_relative_path(
            batch,
            raw_reference.get("relative_path"),
            reference_root,
            expected_id,
        )
        if not path.is_file():
            raise LoopError(f"{expected_id} 参考素材不存在：{path}")
        if raw_reference.get("filename") != path.name:
            raise LoopError(f"{expected_id} filename 与 relative_path 不一致")
        size, digest = _hash_without_change(path)
        if (
            raw_reference.get("size_bytes") != size
            or raw_reference.get("sha256") != digest
        ):
            raise LoopError(f"{expected_id} 参考素材在索引后发生变化；拒绝继续")
        expected_paths.append(path)
    _reject_duplicate_basenames(expected_paths, "参考素材")
    actual_paths = [
        item.resolve()
        for item in reference_root.rglob("*")
        if item.is_file() and not item.name.startswith(".")
    ] if reference_root.is_dir() else []
    if set(actual_paths) != set(expected_paths):
        raise LoopError("replacements/ 的实际文件集合与 reference-index.json 不一致")
    return index


def verify_batch_integrity(
    batch: Path, max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS
) -> Tuple[Dict[str, object], Dict[str, object]]:
    return (
        verify_batch_index(batch, max_batch_videos=max_batch_videos),
        verify_reference_index(batch),
    )


def _has_frozen_streaming_artifacts(batch: Path) -> bool:
    if (batch / "streaming-flow.json").is_file():
        return True
    # Source-evidence caches are derived, restartable preparation artifacts.
    # A partially built cache must not make a new batch look frozen and strand
    # a still-unwritten reference index. Only canonical Job results freeze it.
    result_root = batch / "streaming-results"
    return any(
        stage_root.is_dir() and any(stage_root.glob("*.json"))
        for stage_root in (
            result_root / "preparation",
            result_root / "submission",
        )
    )


def reference_index_for_validation(
    batch: Path,
    *,
    persist_if_unfrozen: bool,
) -> Dict[str, object]:
    """Validate the frozen index, or build a new-batch view without overwriting."""

    path = batch / "reference-index.json"
    if path.is_file():
        return verify_reference_index(batch)
    if _has_frozen_streaming_artifacts(batch):
        raise LoopError(
            "批次已冻结或已有 streaming 结果，但缺少 reference-index.json；"
            "拒绝静默重建证据"
        )
    index = build_reference_index(batch)
    if not persist_if_unfrozen:
        return index
    atomic_write_json(path, index)
    return verify_reference_index(batch)


def expand_video_ids(line: str) -> Set[str]:
    result: Set[str] = set()
    for match in VIDEO_RANGE_RE.finditer(line):
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end and (end - start) <= 999:
            result.update(f"V{number:03d}" for number in range(start, end + 1))
    result.update(f"V{int(match.group(1)):03d}" for match in VIDEO_ID_RE.finditer(line))
    return result


def meaningful_body(value: str) -> bool:
    cleaned = value.split("#", 1)[0].strip().casefold()
    return cleaned not in PLACEHOLDERS


def validate_requirements(batch: Path) -> Tuple[List[str], Dict[str, object]]:
    index = read_index(batch)
    known_ids = {str(job["id"]) for job in index["jobs"]}  # type: ignore[index]
    requirements_path = batch / "requirements.txt"
    if not requirements_path.exists():
        return ["缺少 requirements.txt"], {
            "covered": [],
            "missing": sorted(known_ids, key=natural_key),
        }

    default_present = False
    covered: Set[str] = set()
    referenced: Set[str] = set()
    malformed: List[str] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        default_match = DEFAULT_RULE_RE.match(line)
        if default_match:
            default_present = meaningful_body(default_match.group(1))
            continue
        ids = expand_video_ids(line)
        if not ids:
            continue
        referenced.update(ids)
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) != 2 or not meaningful_body(parts[1]):
            malformed.append(line)
            continue
        covered.update(ids)

    unknown = sorted(referenced - known_ids, key=natural_key)
    effective = set(known_ids) if default_present else covered & known_ids
    missing = sorted(known_ids - effective, key=natural_key)
    errors: List[str] = []
    if malformed:
        errors.append("这些规则没有填写有效需求：" + "；".join(malformed))
    if unknown:
        errors.append("需求引用了不存在的视频编号：" + "、".join(unknown))
    if missing:
        errors.append("这些视频没有需求、默认规则或跳过标记：" + "、".join(missing))
    return errors, {
        "default_present": default_present,
        "covered": sorted(effective, key=natural_key),
        "missing": missing,
        "unknown": unknown,
    }


def batch_auto_ready_enabled(batch: Path) -> bool:
    state_path = batch / "loop-state.json"
    if not state_path.is_file() or (batch / "PAUSE").exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(state, dict) and state.get("auto_ready") is True


def requirements_saved_after_inputs(
    batch: Path, observer: ObservationTracker, observed_at: float
) -> bool:
    return observer.requirements_follow_inputs(batch, observed_at)


def promote_auto_ready_batches(
    loop_root: Path,
    stable_seconds: float,
    observer: ObservationTracker,
    observed_at: float,
) -> List[Path]:
    promoted = []
    for batch in list(visible_children(loop_root / "needs-input")):
        if not batch.is_dir() or not batch_auto_ready_enabled(batch):
            continue
        if not batch_is_stable(batch, stable_seconds, observer, observed_at):
            continue
        saved_after_inputs = requirements_saved_after_inputs(
            batch, observer, observed_at
        )
        errors, coverage = validate_requirements(batch)
        if errors or not saved_after_inputs:
            continue
        atomic_write_json(batch / "requirements-coverage.json", coverage)
        atomic_write_json(batch / "reference-index.json", build_reference_index(batch))
        atomic_write_json(
            batch / "loop-state.json",
            {
                "batch_id": batch.name,
                "state": "READY_AUTO",
                "updated_at": utc_now(),
                "auto_ready": True,
                "message": "需求覆盖完整，且 watcher 观察到最新输入后 requirements.txt 又保存了一次；已自动进入执行队列。",
            },
        )
        promoted.append(move_batch(batch, loop_root, "ready"))
    return promoted


def node_contract_path(filename: str) -> Path:
    """Resolve one workflow-owned node contract and fail closed if missing."""

    path = (NODE_CONTRACT_ROOT / filename).resolve()
    try:
        path.relative_to(NODE_CONTRACT_ROOT.resolve())
    except ValueError as exc:
        raise LoopError(f"节点合同路径越界：{filename}") from exc
    if not path.is_file():
        raise LoopError(f"缺少节点合同：{path}")
    return path


def node_contract_text(filename: str) -> str:
    """Load a node contract for inline delivery to an isolated context."""

    return node_contract_path(filename).read_text(encoding="utf-8").strip()


def _assert_external_node_workspace(
    node_workspace: Path, batch: Path, project_root: Path
) -> Path:
    """Reject a node cwd that sits inside the batch or project tree."""

    workspace = node_workspace.expanduser().resolve()
    for protected_root, label in ((batch, "batch"), (project_root, "project")):
        try:
            workspace.relative_to(protected_root.expanduser().resolve())
        except ValueError:
            continue
        raise LoopError(f"节点工作区不得位于 {label} 目录内：{workspace}")
    return workspace


def _job_record(batch: Path, job_id: str) -> Dict[str, object]:
    index = verify_batch_index(batch)
    jobs = index.get("jobs")
    job = next(
        (
            dict(item)
            for item in jobs
            if isinstance(item, dict) and item.get("id") == job_id
        ),
        None,
    ) if isinstance(jobs, list) else None
    if not isinstance(job, dict):
        raise LoopError(f"{job_id} 不在 batch-index 中")
    return job


def _reference_semantic_name(
    item: Mapping[str, object], classification: Mapping[str, object], used: Set[str]
) -> str:
    """Produce a stable parent-owned noun phrase without exposing a path."""

    stem = Path(str(item.get("filename", ""))).stem.casefold()
    humanized_stem = re.sub(r"[-_]+", " ", stem).strip()
    if re.match(r"^(?:ref|interior)[ -]?\d", humanized_stem, re.IGNORECASE):
        humanized_stem = "参考素材"
    candidates = (
        classification.get("label_zh"),
        classification.get("role"),
        humanized_stem,
        "参考素材",
    )
    base = next(
        (
            str(value).strip()
            for value in candidates
            if value is not None and str(value).strip()
        ),
        "参考素材",
    )
    base = re.sub(r"[\r\n；;_/\\]+", " ", base).strip()
    name = base
    if any(name in existing or existing in name for existing in used):
        raise LoopError("旧素材分类产生重复或重叠名称；请改用显式素材绑定")
    used.add(name)
    return name


def _binding_semantic_name(value: object, job_id: str) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 100:
        raise LoopError(f"{job_id} 的素材语义名称长度无效")
    if name == "原视频":
        raise LoopError(f"{job_id} 的参考素材名称不能与原视频重名")
    if (
        len(name.splitlines()) != 1
        or re.search(r"[；;。:=：@/\\]", name)
        or any(marker in name for marker in PROMPT_RESERVED_SECTION_MARKERS)
    ):
        raise LoopError(f"{job_id} 的素材语义名称必须是自然名词短语")
    if re.search(r"\.(?:png|jpe?g|webp|gif|bmp|tiff?)$", name, re.IGNORECASE):
        raise LoopError(f"{job_id} 的素材语义名称不能是文件名")
    if re.fullmatch(r"R\d{3}", name, re.IGNORECASE):
        raise LoopError(f"{job_id} 的素材语义名称不能是内部编号")
    return name


def validate_job_bindings(
    batch: Path,
    index: Mapping[str, object],
    reference_index: Mapping[str, object],
) -> Dict[str, Dict[str, object]]:
    """Validate the Agent-owned reference and privacy map."""

    path = batch / JOB_BINDINGS_FILENAME
    if not path.is_file():
        raise LoopError(f"缺少 {JOB_BINDINGS_FILENAME}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 顶层字段无效")
    schema_version = payload.get("schema_version")
    expected_top_level = (
        {"schema_version", "batch_id", "backend_profile", "jobs"}
        if schema_version == 3
        else {"schema_version", "batch_id", "jobs"}
    )
    if set(payload) != expected_top_level:
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 顶层字段无效")
    if schema_version not in {1, 2, 3} or payload.get("batch_id") != batch.name:
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 与当前批次不匹配")
    if schema_version == 3:
        try:
            get_backend_profile(payload.get("backend_profile"))
        except BackendProfileError as exc:
            raise LoopError(str(exc)) from exc
        active_profile = os.getenv("VIDEO_REPLACER_ACTIVE_PROFILE", "").strip()
        repository_runtime = (PROJECT_ROOT / "workspace" / "video-loop").resolve()
        try:
            batch.resolve().relative_to(repository_runtime)
            inside_repository_runtime = True
        except ValueError:
            inside_repository_runtime = False
        if inside_repository_runtime and not active_profile:
            raise LoopError(
                "仓库内 schema-v3 批次只能由通过实时 READY Gate 的根 launcher 运行"
            )
        if active_profile and payload.get("backend_profile") != active_profile:
            raise LoopError(
                "job-bindings.json 的 backend_profile 与当前 Agent 验证的 setup profile 不一致"
            )
    if schema_version == 1:
        flow_path = batch / "streaming-flow.json"
        try:
            legacy_flow = json.loads(flow_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LoopError(
                f"新批次的 {JOB_BINDINGS_FILENAME} 必须使用 schema_version 3"
            ) from exc
        if not isinstance(legacy_flow, dict) or not (
            legacy_flow.get("schema_version") == 1
            and legacy_flow.get("batch_id") == batch.name
            and legacy_flow.get("job_bindings_sha256") == sha256_file(path)
            and re.fullmatch(
                r"[0-9a-f]{64}", str(legacy_flow.get("flow_fingerprint") or "")
            )
        ):
            raise LoopError(
                f"新批次的 {JOB_BINDINGS_FILENAME} 必须使用 schema_version 3"
            )
    indexed_jobs = index.get("jobs")
    indexed_references = reference_index.get("references")
    raw_jobs = payload.get("jobs")
    if not isinstance(indexed_jobs, list) or not isinstance(indexed_references, list):
        raise LoopError("素材绑定所需索引无效")
    if not isinstance(raw_jobs, list):
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 的 jobs 必须是数组")
    expected_ids = [
        str(item.get("id")) for item in indexed_jobs if isinstance(item, dict)
    ]
    actual_ids = [
        str(item.get("id")) for item in raw_jobs if isinstance(item, dict)
    ]
    if len(actual_ids) != len(raw_jobs) or actual_ids != expected_ids:
        raise LoopError(
            f"{JOB_BINDINGS_FILENAME} 必须按 batch-index 顺序覆盖每个 Job"
        )
    references_by_path = {
        str(item.get("relative_path")): dict(item)
        for item in indexed_references
        if isinstance(item, dict)
    }
    resolved: Dict[str, Dict[str, object]] = {}
    for raw_job in raw_jobs:
        assert isinstance(raw_job, dict)
        job_id = str(raw_job.get("id"))
        expected_fields = (
            {"id", "references", "privacy_mode"}
            if schema_version in {2, 3}
            else {"id", "references"}
        )
        if set(raw_job) != expected_fields:
            raise LoopError(f"{job_id} 的素材绑定字段无效")
        privacy_mode = (
            str(raw_job.get("privacy_mode") or "")
            if schema_version in {2, 3}
            else "none"
        )
        if privacy_mode not in PRIVACY_MODES:
            raise LoopError(
                f"{job_id} 的 privacy_mode 必须是 none 或 mosaic_required"
            )
        raw_references = raw_job.get("references")
        if not isinstance(raw_references, list) or len(raw_references) > MAX_REFERENCE_IMAGES:
            raise LoopError(f"{job_id} 的 references 必须包含 0–{MAX_REFERENCE_IMAGES} 项")
        job_records: List[Dict[str, object]] = []
        used_paths: Set[str] = set()
        used_names: Set[str] = set()
        for raw_reference in raw_references:
            if not isinstance(raw_reference, dict) or set(raw_reference) != {
                "relative_path",
                "semantic_name",
            }:
                raise LoopError(f"{job_id} 的单项素材绑定字段无效")
            raw_relative = str(raw_reference.get("relative_path") or "")
            relative = Path(raw_relative)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) < 2
                or relative.parts[0] != "replacements"
            ):
                raise LoopError(f"{job_id} 的参考素材必须位于 replacements/ 内")
            relative_text = relative.as_posix()
            if relative_text in used_paths:
                raise LoopError(f"{job_id} 重复绑定参考素材：{relative_text}")
            indexed = references_by_path.get(relative_text)
            if indexed is None:
                raise LoopError(f"{job_id} 绑定未索引的参考素材：{relative_text}")
            semantic_name = _binding_semantic_name(
                raw_reference.get("semantic_name"), job_id
            )
            if semantic_name in used_names:
                raise LoopError(f"{job_id} 的素材语义名称必须互不重复")
            if any(
                semantic_name in existing or existing in semantic_name
                for existing in used_names
            ):
                raise LoopError(
                    f"{job_id} 的素材语义名称不能互为完整子串"
                )
            used_paths.add(relative_text)
            used_names.add(semantic_name)
            indexed["semantic_name"] = semantic_name
            job_records.append(indexed)
        resolved[job_id] = {
            "references": job_records,
            "privacy_mode": privacy_mode,
        }
    return resolved


def _job_bindings_payload(batch: Path) -> Dict[str, object]:
    """Read the already-schema-validated Agent-owned batch declaration."""

    path = batch / JOB_BINDINGS_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise LoopError(f"{JOB_BINDINGS_FILENAME} 顶层字段无效")
    return payload


def backend_profile_for_batch(
    batch: Path,
    index: Optional[Mapping[str, object]] = None,
    reference_index: Optional[Mapping[str, object]] = None,
) -> Tuple[BackendProfile, bool]:
    """Return the reviewed profile and whether v3 upload preparation applies.

    Schema v1/v2 records remain readable as frozen legacy batches. They keep
    the former Dreamina behaviour and never acquire an upload-preparation
    manifest in place. New work uses schema v3 and is the only route that
    activates the automatic size gate.
    """

    resolved_index = index or verify_batch_index(batch)
    resolved_references = reference_index or verify_reference_index(batch)
    validate_job_bindings(batch, resolved_index, resolved_references)
    payload = _job_bindings_payload(batch)
    if payload.get("schema_version") != 3:
        return get_backend_profile("dreamina_cli_seedance_2_5"), False
    try:
        return get_backend_profile(payload.get("backend_profile")), True
    except BackendProfileError as exc:
        raise LoopError(str(exc)) from exc


def select_job_reference_records(batch: Path, job_id: str) -> List[Dict[str, object]]:
    """Resolve the current Job's material binding in deterministic parent code."""

    index = verify_batch_index(batch)
    reference_index = verify_reference_index(batch)
    declared = validate_job_bindings(batch, index, reference_index)
    if job_id not in declared:
        raise LoopError(f"{job_id} 不在 {JOB_BINDINGS_FILENAME} 中")
    references = declared[job_id].get("references")
    if not isinstance(references, list):
        raise LoopError(f"{job_id} 的 references 无效")
    return [dict(item) for item in references if isinstance(item, dict)]


def job_privacy_mode(batch: Path, job_id: str) -> str:
    """Return the explicit workflow privacy mode for one Job."""

    index = verify_batch_index(batch)
    reference_index = verify_reference_index(batch)
    declared = validate_job_bindings(batch, index, reference_index)
    if job_id not in declared:
        raise LoopError(f"{job_id} 不在 {JOB_BINDINGS_FILENAME} 中")
    mode = str(declared[job_id].get("privacy_mode") or "")
    if mode not in PRIVACY_MODES:
        raise LoopError(f"{job_id} 的 privacy_mode 无效")
    return mode


MEDIA_TOOL_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATHEXT",
    "PROGRAMDATA",
    "SHELL",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "WINDIR",
}


def media_tool_environment(
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return a credential-minimized environment for the trusted FFmpeg binary."""

    source_env = os.environ if source is None else source
    allowed = {name.casefold() for name in MEDIA_TOOL_ENV_ALLOWLIST}
    environment = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    environment["PATH"] = os.defpath
    return environment


def _run_video_prompt_ffmpeg(
    command: Sequence[str], *, timeout: Optional[int] = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=media_tool_environment(),
    )


def extract_video_prompt_frames(
    source_video: Path,
    frame_root: Path,
    analysis_tool: Path,
) -> Dict[str, object]:
    """Create bounded, timestamped local evidence for a tool-less model turn."""

    source = source_video.expanduser().resolve()
    executable = analysis_tool.expanduser().resolve()
    if not source.is_file() or is_link_like(source):
        raise LoopError(f"视频取样源必须是普通文件：{source}")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise LoopError(f"视频检查工具不可执行：{executable}")
    frame_root.mkdir(parents=True, exist_ok=False)
    try:
        metadata = probe_video_metadata(
            source,
            executable,
            runner=_run_video_prompt_ffmpeg,
        )
    except Exception as exc:
        raise LoopError("无法读取 Video-to-Prompt 源视频元数据") from exc
    duration = float(metadata.get("duration_seconds") or 0.0)
    if duration <= 0:
        raise LoopError("Video-to-Prompt 源视频时长无效")
    sampling_interval = max(
        VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS,
        duration / max(1, VIDEO_PROMPT_MAX_FRAMES - 1),
    )

    output_pattern = frame_root / "frame-%03d.jpg"
    select = (
        "select='eq(n,0)+"
        f"gte(t-prev_selected_t,{sampling_interval:.9f})',"
        "showinfo,scale='min(1280,iw)':-2"
    )
    completed = _run_video_prompt_ffmpeg(
        [
            str(executable),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-i",
            str(source),
            "-vf",
            select,
            "-an",
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(VIDEO_PROMPT_MAX_FRAMES),
            "-q:v",
            "3",
            str(output_pattern),
        ],
        timeout=180,
    )
    if completed.returncode != 0:
        raise LoopError("Video-to-Prompt 本地取样失败")
    frames = sorted(frame_root.glob("frame-*.jpg"))
    if not frames or len(frames) > VIDEO_PROMPT_MAX_FRAMES:
        raise LoopError("Video-to-Prompt 本地取样没有生成有效帧")
    timestamps = [
        float(value)
        for value in re.findall(
            r"\bpts_time:\s*([-+0-9.eE]+)", completed.stderr
        )
    ]
    if len(timestamps) < len(frames):
        raise LoopError("Video-to-Prompt 本地取样缺少时间戳")

    records: List[Dict[str, object]] = []
    for path, timestamp in zip(frames, timestamps):
        if is_link_like(path) or not path.is_file():
            raise LoopError("Video-to-Prompt 本地取样包含非普通文件")
        size_bytes, digest = _hash_without_change(path)
        records.append(
            {
                "image": path.relative_to(frame_root.parents[1]).as_posix(),
                "timestamp_seconds": round(max(0.0, min(timestamp, duration)), 3),
                "size_bytes": size_bytes,
                "sha256": digest,
            }
        )
    return {
        "video_metadata": {
            "duration_seconds": duration,
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "fps": metadata.get("fps"),
        },
        "sampled_frames": records,
        "sampling_policy": {
            "method": "uniform_timestamp_interval",
            "interval_seconds": round(sampling_interval, 6),
            "minimum_interval_seconds": VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS,
            "max_frames": VIDEO_PROMPT_MAX_FRAMES,
        },
    }


def _source_evidence_cache_spec(
    source_size: int,
    source_sha256: str,
    analysis_tool: Path,
) -> Dict[str, object]:
    tool_size, tool_sha256 = _hash_without_change(analysis_tool)
    return {
        "source_size_bytes": source_size,
        "source_sha256": source_sha256,
        "analysis_tool_size_bytes": tool_size,
        "analysis_tool_sha256": tool_sha256,
        "sampler_recipe": VIDEO_PROMPT_SAMPLER_RECIPE,
    }


def _source_evidence_cache_key(spec: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(spec), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_source_evidence_cache(
    cache_dir: Path,
    expected_spec: Mapping[str, object],
) -> Dict[str, object]:
    if is_link_like(cache_dir) or linked_descendants(cache_dir):
        raise LoopError("源证据缓存不得包含链接或 Windows junction")
    manifest_path = cache_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError("源证据缓存 manifest 无效") from exc
    if not isinstance(manifest, dict) or manifest.get(
        "schema_version"
    ) != SOURCE_EVIDENCE_CACHE_SCHEMA_VERSION:
        raise LoopError("源证据缓存 schema 无效")
    if manifest.get("cache_spec") != dict(expected_spec):
        raise LoopError("源证据缓存身份与当前源视频、取样规则或工具不匹配")
    video_metadata = manifest.get("video_metadata")
    sampled_frames = manifest.get("sampled_frames")
    sampling_policy = manifest.get("sampling_policy")
    if (
        not isinstance(video_metadata, dict)
        or not isinstance(sampled_frames, list)
        or not sampled_frames
        or len(sampled_frames) > VIDEO_PROMPT_MAX_FRAMES
        or not isinstance(sampling_policy, dict)
    ):
        raise LoopError("源证据缓存字段无效")
    frame_root = (cache_dir / "inputs" / "frames").resolve()
    expected_paths: Set[Path] = set()
    previous_timestamp = -1.0
    verified_frames: List[Dict[str, object]] = []
    for raw_frame in sampled_frames:
        if not isinstance(raw_frame, dict) or set(raw_frame) != {
            "image",
            "timestamp_seconds",
            "size_bytes",
            "sha256",
        }:
            raise LoopError("源证据缓存帧字段无效")
        relative = Path(str(raw_frame.get("image") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise LoopError("源证据缓存帧路径无效")
        candidate = (cache_dir / relative).resolve()
        try:
            candidate.relative_to(frame_root)
        except ValueError as exc:
            raise LoopError("源证据缓存帧越过缓存目录") from exc
        if is_link_like(candidate) or not candidate.is_file():
            raise LoopError("源证据缓存帧不是普通文件")
        actual_size, actual_sha256 = _hash_without_change(candidate)
        timestamp = raw_frame.get("timestamp_seconds")
        if (
            raw_frame.get("size_bytes") != actual_size
            or raw_frame.get("sha256") != actual_sha256
            or not isinstance(timestamp, (int, float))
            or float(timestamp) < previous_timestamp
        ):
            raise LoopError("源证据缓存帧身份或时间顺序无效")
        previous_timestamp = float(timestamp)
        expected_paths.add(candidate)
        verified_frames.append(dict(raw_frame))
    actual_paths = {
        item.resolve()
        for item in frame_root.rglob("*")
        if item.is_file()
    } if frame_root.is_dir() else set()
    if actual_paths != expected_paths:
        raise LoopError("源证据缓存帧集合与 manifest 不一致")
    return {
        "video_metadata": dict(video_metadata),
        "sampled_frames": verified_frames,
        "sampling_policy": dict(sampling_policy),
    }


def _cached_source_evidence(
    batch: Path,
    source_path: Path,
    source_size: int,
    source_sha256: str,
    analysis_tool: Path,
    frame_extractor: Callable[[Path, Path, Path], Dict[str, object]],
) -> Tuple[Path, Dict[str, object]]:
    spec = _source_evidence_cache_spec(
        source_size, source_sha256, analysis_tool
    )
    cache_key = _source_evidence_cache_key(spec)
    cache_root = batch / "streaming-results" / "source-evidence"
    cache_root.mkdir(parents=True, exist_ok=True)
    if is_link_like(cache_root):
        raise LoopError("源证据缓存根目录不得是链接或 Windows junction")
    if os.name != "nt":
        cache_root.chmod(0o700)
    cache_dir = cache_root / cache_key
    lock_path = cache_root / ".locks" / f"{cache_key}.lock"
    with waiting_file_lock(lock_path):
        if cache_dir.exists():
            if is_link_like(cache_dir) or not cache_dir.is_dir():
                raise LoopError("源证据缓存路径不是安全目录")
            return cache_dir, _validate_source_evidence_cache(cache_dir, spec)
        with tempfile.TemporaryDirectory(
            prefix=f".building-{cache_key[:12]}-", dir=cache_root
        ) as temporary_value:
            staging = Path(temporary_value)
            frame_root = staging / "inputs" / "frames"
            extracted = frame_extractor(source_path, frame_root, analysis_tool)
            if not isinstance(extracted, dict):
                raise LoopError("源证据缓存取样结果无效")
            manifest = {
                "schema_version": SOURCE_EVIDENCE_CACHE_SCHEMA_VERSION,
                "created_at": utc_now(),
                "cache_spec": spec,
                "video_metadata": extracted.get("video_metadata"),
                "sampled_frames": extracted.get("sampled_frames"),
                "sampling_policy": extracted.get("sampling_policy"),
            }
            atomic_write_json(staging / "manifest.json", manifest)
            _validate_source_evidence_cache(staging, spec)
            make_tree_private(staging)
            staging.replace(cache_dir)
        return cache_dir, _validate_source_evidence_cache(cache_dir, spec)


def _source_evidence_index_path(batch: Path) -> Path:
    return batch / SOURCE_EVIDENCE_INDEX_FILENAME


def _source_evidence_source(
    batch: Path, job: Mapping[str, object]
) -> Tuple[Path, int, str]:
    job_id = str(job.get("id") or "")
    source = _indexed_relative_path(
        batch, job.get("relative_path"), batch / "videos", job_id
    )
    if is_link_like(source) or not source.is_file():
        raise LoopError(f"{job_id} 源视频不是普通文件")
    size_bytes, sha256 = _hash_without_change(source)
    if size_bytes != job.get("size_bytes") or sha256 != job.get("sha256"):
        raise LoopError(f"{job_id} 源视频身份不匹配")
    return source, size_bytes, sha256


def build_source_evidence_index(
    batch: Path,
    index: Mapping[str, object],
    analysis_tool: Path,
    *,
    skipped_ids: Optional[Set[str]] = None,
    frame_extractor: Callable[
        [Path, Path, Path], Dict[str, object]
    ] = extract_video_prompt_frames,
) -> Dict[str, object]:
    """Materialize each unique active source once and return its frozen anchors."""

    jobs = index.get("jobs")
    if not isinstance(jobs, list):
        raise LoopError("源证据索引缺少 batch jobs")
    skipped = explicitly_skipped_ids(batch) if skipped_ids is None else skipped_ids
    trusted_tool = analysis_tool.expanduser().resolve()
    if is_link_like(trusted_tool) or not trusted_tool.is_file():
        raise LoopError("源证据分析工具不是普通文件")
    entries_by_key: Dict[str, Dict[str, object]] = {}
    job_cache_keys: Dict[str, str] = {}
    for raw_job in jobs:
        if not isinstance(raw_job, dict):
            raise LoopError("源证据索引包含无效 Job")
        job_id = str(raw_job.get("id") or "")
        if job_id in skipped:
            continue
        source, source_size, source_sha256 = _source_evidence_source(
            batch, raw_job
        )
        spec = _source_evidence_cache_spec(
            source_size, source_sha256, trusted_tool
        )
        cache_key = _source_evidence_cache_key(spec)
        cache_dir, extracted = _cached_source_evidence(
            batch,
            source,
            source_size,
            source_sha256,
            trusted_tool,
            frame_extractor,
        )
        manifest_path = cache_dir / "manifest.json"
        manifest_size, manifest_sha256 = _hash_without_change(manifest_path)
        sampled_frames = extracted.get("sampled_frames")
        if not isinstance(sampled_frames, list) or not sampled_frames:
            raise LoopError(f"{job_id} 源证据缓存没有有效帧")
        entry = {
            "cache_key": cache_key,
            "cache_relative_path": cache_dir.relative_to(batch).as_posix(),
            "cache_spec": spec,
            "manifest_size_bytes": manifest_size,
            "manifest_sha256": manifest_sha256,
            "sampled_frame_count": len(sampled_frames),
        }
        prior = entries_by_key.setdefault(cache_key, entry)
        if prior != entry:
            raise LoopError("同一源证据 cache key 对应了不同 manifest")
        job_cache_keys[job_id] = cache_key
    return {
        "schema_version": 1,
        "batch_id": batch.name,
        "created_at": utc_now(),
        "sampler_recipe": VIDEO_PROMPT_SAMPLER_RECIPE,
        "cache_schema_version": SOURCE_EVIDENCE_CACHE_SCHEMA_VERSION,
        "entries": [entries_by_key[key] for key in sorted(entries_by_key)],
        "job_cache_keys": job_cache_keys,
    }


def verify_source_evidence_index(
    batch: Path,
    index: Mapping[str, object],
    analysis_tool: Optional[Path] = None,
    *,
    skipped_ids: Optional[Set[str]] = None,
) -> Dict[str, object]:
    """Verify the immutable batch index and every manifest/frame it anchors."""

    path = _source_evidence_index_path(batch)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"{SOURCE_EVIDENCE_INDEX_FILENAME} 无效或缺失") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "batch_id",
        "created_at",
        "sampler_recipe",
        "cache_schema_version",
        "entries",
        "job_cache_keys",
    }:
        raise LoopError(f"{SOURCE_EVIDENCE_INDEX_FILENAME} 字段无效")
    if not (
        payload.get("schema_version") == 1
        and payload.get("batch_id") == batch.name
        and isinstance(payload.get("created_at"), str)
        and payload.get("sampler_recipe") == VIDEO_PROMPT_SAMPLER_RECIPE
        and payload.get("cache_schema_version")
        == SOURCE_EVIDENCE_CACHE_SCHEMA_VERSION
    ):
        raise LoopError(f"{SOURCE_EVIDENCE_INDEX_FILENAME} 身份无效")
    entries = payload.get("entries")
    job_cache_keys = payload.get("job_cache_keys")
    jobs = index.get("jobs")
    if (
        not isinstance(entries, list)
        or not isinstance(job_cache_keys, dict)
        or not isinstance(jobs, list)
    ):
        raise LoopError(f"{SOURCE_EVIDENCE_INDEX_FILENAME} 内容无效")
    trusted_tool = (
        _trusted_ffmpeg_executable()
        if analysis_tool is None
        else analysis_tool.expanduser().resolve()
    )
    skipped = explicitly_skipped_ids(batch) if skipped_ids is None else skipped_ids
    expected_job_keys: Dict[str, str] = {}
    expected_specs: Dict[str, Dict[str, object]] = {}
    for raw_job in jobs:
        if not isinstance(raw_job, dict):
            raise LoopError("源证据索引包含无效 Job")
        job_id = str(raw_job.get("id") or "")
        if job_id in skipped:
            continue
        _source, source_size, source_sha256 = _source_evidence_source(
            batch, raw_job
        )
        spec = _source_evidence_cache_spec(
            source_size, source_sha256, trusted_tool
        )
        cache_key = _source_evidence_cache_key(spec)
        expected_job_keys[job_id] = cache_key
        expected_specs[cache_key] = spec
    if job_cache_keys != expected_job_keys:
        raise LoopError("源证据 Job 映射与当前批次、跳过规则或工具不匹配")
    entries_by_key: Dict[str, Dict[str, object]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "cache_key",
            "cache_relative_path",
            "cache_spec",
            "manifest_size_bytes",
            "manifest_sha256",
            "sampled_frame_count",
        }:
            raise LoopError("源证据锚点字段无效")
        cache_key = str(raw_entry.get("cache_key") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise LoopError("源证据 cache key 无效")
        if cache_key in entries_by_key:
            raise LoopError("源证据 cache key 重复")
        expected_spec = expected_specs.get(cache_key)
        if expected_spec is None or raw_entry.get("cache_spec") != expected_spec:
            raise LoopError("源证据锚点与当前源视频、规则或工具不匹配")
        expected_cache_dir = (
            batch / "streaming-results" / "source-evidence" / cache_key
        ).resolve()
        raw_relative = Path(str(raw_entry.get("cache_relative_path") or ""))
        if raw_relative.is_absolute() or ".." in raw_relative.parts:
            raise LoopError("源证据缓存路径无效")
        cache_dir = (batch / raw_relative).resolve()
        if cache_dir != expected_cache_dir:
            raise LoopError("源证据缓存路径与 cache key 不匹配")
        manifest_size, manifest_sha256 = _hash_without_change(
            cache_dir / "manifest.json"
        )
        if (
            manifest_size != raw_entry.get("manifest_size_bytes")
            or manifest_sha256 != raw_entry.get("manifest_sha256")
        ):
            raise LoopError("源证据 manifest 已在 flow 冻结后发生变化")
        extracted = _validate_source_evidence_cache(cache_dir, expected_spec)
        frames = extracted.get("sampled_frames")
        if (
            not isinstance(frames, list)
            or len(frames) != raw_entry.get("sampled_frame_count")
        ):
            raise LoopError("源证据帧数量与冻结锚点不匹配")
        entries_by_key[cache_key] = dict(raw_entry)
    if set(entries_by_key) != set(expected_specs):
        raise LoopError("源证据缓存集合与当前活动 Job 不一致")
    return payload


def ensure_source_evidence_index(
    batch: Path,
    index: Mapping[str, object],
    analysis_tool: Optional[Path] = None,
    *,
    skipped_ids: Optional[Set[str]] = None,
    frame_extractor: Callable[
        [Path, Path, Path], Dict[str, object]
    ] = extract_video_prompt_frames,
) -> Dict[str, object]:
    """Create the source evidence freeze once; never rewrite an existing index."""

    trusted_tool = (
        _trusted_ffmpeg_executable()
        if analysis_tool is None
        else analysis_tool.expanduser().resolve()
    )
    path = _source_evidence_index_path(batch)
    if path.is_file():
        return verify_source_evidence_index(
            batch,
            index,
            trusted_tool,
            skipped_ids=skipped_ids,
        )
    built = build_source_evidence_index(
        batch,
        index,
        trusted_tool,
        skipped_ids=skipped_ids,
        frame_extractor=frame_extractor,
    )
    atomic_write_json(path, built)
    return verify_source_evidence_index(
        batch,
        index,
        trusted_tool,
        skipped_ids=skipped_ids,
    )


def _anchored_source_evidence_cache(
    batch: Path,
    source_size: int,
    source_sha256: str,
    analysis_tool: Path,
) -> Tuple[Path, Dict[str, object]]:
    """Resolve one cache only when its index is bound to the frozen flow."""

    index_path = _source_evidence_index_path(batch)
    flow_path = batch / "streaming-flow.json"
    try:
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError("源证据尚未被 streaming flow 冻结") from exc
    if not isinstance(flow, dict) or not isinstance(payload, dict):
        raise LoopError("源证据 flow 或索引无效")
    _index_size, index_sha256 = _hash_without_change(index_path)
    if flow.get("source_evidence_index_sha256") != index_sha256:
        raise LoopError("源证据索引与 frozen flow 不匹配")
    spec = _source_evidence_cache_spec(
        source_size, source_sha256, analysis_tool
    )
    cache_key = _source_evidence_cache_key(spec)
    entries = payload.get("entries")
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("cache_key") == cache_key
        ),
        None,
    ) if isinstance(entries, list) else None
    if not isinstance(entry, dict) or entry.get("cache_spec") != spec:
        raise LoopError("当前 Job 没有 frozen source evidence 锚点")
    cache_dir = batch / "streaming-results" / "source-evidence" / cache_key
    manifest_size, manifest_sha256 = _hash_without_change(
        cache_dir / "manifest.json"
    )
    if (
        manifest_size != entry.get("manifest_size_bytes")
        or manifest_sha256 != entry.get("manifest_sha256")
    ):
        raise LoopError("源证据 manifest 已在 flow 冻结后发生变化")
    return cache_dir, _validate_source_evidence_cache(cache_dir, spec)


def _stage_cached_source_evidence(
    batch: Path,
    workspace: Path,
    source_path: Path,
    source_size: int,
    source_sha256: str,
    analysis_tool: Path,
    frame_extractor: Callable[[Path, Path, Path], Dict[str, object]],
) -> Dict[str, object]:
    del source_path, frame_extractor
    cache_dir, extracted = _anchored_source_evidence_cache(
        batch, source_size, source_sha256, analysis_tool
    )
    frame_root = workspace / "inputs" / "frames"
    frame_root.mkdir(parents=True, exist_ok=False)
    staged_frames: List[Dict[str, object]] = []
    sampled_frames = extracted["sampled_frames"]
    assert isinstance(sampled_frames, list)
    for raw_frame in sampled_frames:
        assert isinstance(raw_frame, dict)
        relative = Path(str(raw_frame["image"]))
        source = (cache_dir / relative).resolve()
        destination = (workspace / relative).resolve()
        try:
            destination.relative_to(frame_root.resolve())
        except ValueError as exc:
            raise LoopError("源证据 staging 路径越过工作区") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        actual_size, actual_sha256 = _hash_without_change(destination)
        if (
            actual_size != raw_frame.get("size_bytes")
            or actual_sha256 != raw_frame.get("sha256")
        ):
            destination.unlink(missing_ok=True)
            raise LoopError("源证据 staging 身份不匹配")
        staged_frames.append(dict(raw_frame))
    return {
        "video_metadata": dict(extracted["video_metadata"]),
        "sampled_frames": staged_frames,
        "sampling_policy": dict(extracted["sampling_policy"]),
    }


def write_video_to_prompt_node_input(
    batch: Path,
    project_root: Path,
    job_id: str,
    node_workspace: Path,
    references: Sequence[Mapping[str, object]],
    *,
    analysis_tool: Path,
    frame_extractor: Callable[[Path, Path, Path], Dict[str, object]] = extract_video_prompt_frames,
) -> Path:
    """Stage one Job's bounded visual evidence and fixed prompt intent."""

    workspace = _assert_external_node_workspace(node_workspace, batch, project_root)
    job = _job_record(batch, job_id)
    source_path = (batch / str(job["relative_path"])).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    frame_root = workspace / "inputs" / "frames"
    reference_root = workspace / "inputs" / "references"
    reference_root.mkdir(parents=True, exist_ok=False)
    source_size, source_sha256 = _hash_without_change(source_path)
    if source_size != job.get("size_bytes") or source_sha256 != job.get("sha256"):
        raise LoopError(f"{job_id} 源视频身份不匹配")

    trusted_analysis_tool = analysis_tool.expanduser().resolve()
    extracted = _stage_cached_source_evidence(
        batch,
        workspace,
        source_path,
        source_size,
        source_sha256,
        trusted_analysis_tool,
        frame_extractor,
    )
    if not isinstance(extracted, dict):
        raise LoopError(f"{job_id} 视频取样结果无效")
    video_metadata = extracted.get("video_metadata")
    sampled_frames = extracted.get("sampled_frames")
    sampling_policy = extracted.get("sampling_policy")
    if (
        not isinstance(video_metadata, dict)
        or not isinstance(sampled_frames, list)
        or not sampled_frames
        or len(sampled_frames) > VIDEO_PROMPT_MAX_FRAMES
        or not isinstance(sampling_policy, dict)
    ):
        raise LoopError(f"{job_id} 视频取样结果字段无效")
    verified_frames: List[Dict[str, object]] = []
    previous_timestamp = -1.0
    for raw_frame in sampled_frames:
        if not isinstance(raw_frame, dict) or set(raw_frame) != {
            "image",
            "timestamp_seconds",
            "size_bytes",
            "sha256",
        }:
            raise LoopError(f"{job_id} 视频取样帧字段无效")
        relative = Path(str(raw_frame.get("image") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise LoopError(f"{job_id} 视频取样帧路径无效")
        candidate = (workspace / relative).resolve()
        try:
            candidate.relative_to(frame_root.resolve())
        except ValueError as exc:
            raise LoopError(f"{job_id} 视频取样帧越过工作区") from exc
        if is_link_like(candidate) or not candidate.is_file():
            raise LoopError(f"{job_id} 视频取样帧不是普通文件")
        actual_size, actual_sha256 = _hash_without_change(candidate)
        timestamp = raw_frame.get("timestamp_seconds")
        if (
            raw_frame.get("size_bytes") != actual_size
            or raw_frame.get("sha256") != actual_sha256
            or not isinstance(timestamp, (int, float))
            or float(timestamp) < previous_timestamp
        ):
            raise LoopError(f"{job_id} 视频取样帧身份或时间顺序无效")
        previous_timestamp = float(timestamp)
        verified_frames.append(dict(raw_frame))
    sampled_frames = verified_frames

    staged_references: List[Dict[str, object]] = []
    replacement_root = (batch / "replacements").resolve()
    for position, raw in enumerate(references, start=1):
        reference_id = str(raw.get("id") or "").strip()
        semantic_name = str(raw.get("semantic_name") or "").strip()
        relative_path = str(raw.get("relative_path") or "")
        sha256 = str(raw.get("sha256") or "").strip().lower()
        size_bytes = raw.get("size_bytes")
        if not re.fullmatch(r"R\d{3}", reference_id):
            raise LoopError(f"{job_id} 参考图编号无效：{reference_id!r}")
        if not semantic_name:
            raise LoopError(f"{job_id} 的 {reference_id} 缺少语义名称")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise LoopError(f"{job_id} 的 {reference_id} 缺少有效 SHA-256")
        source = _indexed_relative_path(
            batch, relative_path, replacement_root, reference_id
        )
        if not source.is_file():
            raise LoopError(f"{job_id} 的 {reference_id} 参考图不存在")
        actual_size, actual_sha256 = _hash_without_change(source)
        if size_bytes != actual_size or sha256 != actual_sha256:
            raise LoopError(f"{job_id} 的 {reference_id} 参考图身份不匹配")
        staged_path = reference_root / f"{reference_id}{source.suffix.casefold()}"
        shutil.copy2(source, staged_path)
        staged_size, staged_sha256 = _hash_without_change(staged_path)
        if staged_size != actual_size or staged_sha256 != actual_sha256:
            staged_path.unlink(missing_ok=True)
            raise LoopError(f"{job_id} 的 {reference_id} 参考图 staging 身份不匹配")
        staged_references.append(
            {
                "handle": f"@图片{position}",
                "reference_id": reference_id,
                "semantic_name": semantic_name,
                "size_bytes": actual_size,
                "sha256": actual_sha256,
                "image": staged_path.relative_to(workspace).as_posix(),
            }
        )

    bindings = [{"handle": "@视频1", "semantic_name": "原视频"}]
    bindings.extend(
        {
            "handle": str(item["handle"]),
            "semantic_name": str(item["semantic_name"]),
        }
        for item in staged_references
    )
    semantic_names = [str(item["semantic_name"]).strip() for item in bindings]
    if any(not name for name in semantic_names) or len(semantic_names) != len(
        set(semantic_names)
    ):
        raise LoopError(f"{job_id} 父层素材名称必须非空且互不重复")
    payload = {
        "schema_version": 1,
        "batch_id": batch.name,
        "job_id": job_id,
        "source_video": {
            "filename": str(job.get("filename", "")),
            "size_bytes": job.get("size_bytes"),
            "sha256": job.get("sha256"),
            **video_metadata,
        },
        "sampled_frames": sampled_frames,
        "sampling_policy": sampling_policy,
        "requirements": job_requirement_lines(batch, job_id),
        "material_bindings": bindings,
        "references": staged_references,
        "attachment_order": [
            *[str(item.get("image") or "") for item in sampled_frames if isinstance(item, dict)],
            *[str(item.get("image") or "") for item in staged_references],
        ],
        "output_contract": {"result_field": "jobs[0].prompt"},
    }
    path = workspace / "node-input.json"
    atomic_write_json(path, payload)
    return path.resolve()


def _write_parent_reference_binding(
    batch: Path, project_root: Path, job_id: str, references: Sequence[Mapping[str, object]]
) -> None:
    output_dir = job_output_dir(project_root, batch, job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    binding = [str(item["id"]) for item in references]
    if len(binding) != len(set(binding)) or len(binding) > MAX_REFERENCE_IMAGES:
        raise LoopError(f"{job_id} 父层参考素材绑定无效")
    atomic_write_json(output_dir / REFERENCE_BINDING_FILENAME, binding)


def video_to_prompt_node_prompt(
    job_id: str,
    node_input: Mapping[str, object],
) -> str:
    """Render a self-contained, image-attached media-to-prompt turn."""

    contract = node_contract_text("video-to-prompt.md")
    encoded_input = json.dumps(node_input, ensure_ascii=False, indent=2)
    return f"""完成视频分析并直接写替换提示词：{job_id}

## 节点合同（video-to-prompt.md）

{contract}

## 本次输入

下面 JSON 与本次消息附带图片一一对应；附图顺序严格等于 `attachment_order`。视频帧按 `timestamp_seconds` 排列：

```json
{encoded_input}
```

只根据当前 JSON 和附图处理 {job_id}。不要调用任何工具、读取路径、创建文件或请求更多上下文。直接在最终结构化结果的 `jobs[0].prompt` 返回完整提示词；不要创建中间分析产物。

最终响应只返回 {job_id} 一个 Job。完成时 status 使用 {NODE_COMPLETE_STATUS}、`blocker` 为 null、`prompt` 为非空字符串；无法完成时使用 BLOCKED、写明准确原因并令 `prompt` 为 null。
"""


def build_codex_command(
    codex_binary: str,
    batch: Path,
    project_root: Path,
    schema_path: Path,
    result_path: Path,
    *,
    node_workspace: Path,
    writable_workspace_paths: Sequence[Path] = (),
    image_paths: Sequence[Path] = (),
    platform_name: Optional[str] = None,
) -> List[str]:
    workspace = _assert_external_node_workspace(
        node_workspace, batch, project_root
    )
    if writable_workspace_paths:
        raise LoopError("Video-to-Prompt 观察节点不得获得工作区写权限")
    platform_name = os.name if platform_name is None else platform_name
    workspace_permissions: Dict[str, str] = {".": "read"}
    workspace_roots = ",".join(
        f"{json.dumps(relative)}={json.dumps(access)}"
        for relative, access in workspace_permissions.items()
    )
    filesystem_entries = [
        f'{json.dumps(":minimal")}={json.dumps("read")}',
        (
            f'{json.dumps(":workspace_roots")}='
            f"{{{workspace_roots}}}"
        ),
    ]
    filesystem_config = "{" + ",".join(filesystem_entries) + "}"
    command = codex_launcher(codex_binary, platform_name) + [
        "exec",
        "--model",
        VIDEO_TO_PROMPT_MODEL,
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-c",
        f"model_catalog_json={json.dumps(str(VIDEO_TO_PROMPT_MODEL_CATALOG.resolve()))}",
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        "mcp_servers={}",
        "-c",
        "notify=[]",
        "-c",
        "skills.include_instructions=false",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "include_environment_context=false",
        "-c",
        "include_permissions_instructions=false",
        "-c",
        "include_apps_instructions=false",
        "-c",
        "include_collaboration_mode_instructions=false",
        "-c",
        'web_search="disabled"',
        "-c",
        "tools.update_plan.enabled=false",
        "-c",
        "tools.experimental_request_user_input.enabled=false",
        "-c",
        'features.multi_agent_v2.root_agent_usage_hint_text=""',
        "-c",
        'features.multi_agent_v2.subagent_usage_hint_text=""',
        "-c",
        'features.multi_agent_v2.multi_agent_mode_hint_text=""',
        "-c",
        "skills.bundled.enabled=false",
        "-c",
        "orchestrator.skills.enabled=false",
        "-c",
        "orchestrator.mcp.enabled=false",
        "-c",
        'approval_policy="never"',
        "-c",
        'default_permissions="node_isolated"',
        "-c",
        f"permissions.node_isolated.filesystem={filesystem_config}",
        "-c",
        "permissions.node_isolated.network.enabled=false",
        "--json",
        "--color",
        "never",
        "--cd",
        str(workspace),
    ]
    if platform_name == "nt":
        command[command.index("--json"):command.index("--json")] = [
            "-c",
            'windows.sandbox="unelevated"',
        ]
    command.extend(
        [
            "--skip-git-repo-check",
            "--disable",
            "skill_search",
            "--disable",
            "plugins",
            "--disable",
            "hooks",
            "--disable",
            "apps",
            "--disable",
            "multi_agent",
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--disable",
            "shell_snapshot",
            "--disable",
            "image_generation",
            "--disable",
            "in_app_browser",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "workspace_dependencies",
            "--disable",
            "goals",
            "--disable",
            "tool_suggest",
            "--disable",
            "auth_elicitation",
            "--disable",
            "browser_use_external",
            "--disable",
            "code_mode",
            "--disable",
            "code_mode_host",
            "--disable",
            "code_mode_only",
            "--disable",
            "current_time_reminder",
            "--disable",
            "deferred_executor",
            "--disable",
            "memories",
            "--disable",
            "recommended_plugins",
            "--disable",
            "remote_plugin",
            "--disable",
            "standalone_web_search",
            "--disable",
            "token_budget",
            "--disable",
            "tool_call_mcp_elicitation",
            "--disable",
            "use_agent_identity",
        ]
    )
    for image_path in image_paths:
        candidate_image = image_path.expanduser()
        if is_link_like(candidate_image):
            raise LoopError(f"节点图片不得是链接或 Windows junction：{candidate_image}")
        resolved_image = candidate_image.resolve()
        try:
            resolved_image.relative_to(workspace)
        except ValueError as exc:
            raise LoopError(f"节点图片越过工作区：{resolved_image}") from exc
        if is_link_like(resolved_image) or not resolved_image.is_file():
            raise LoopError(f"节点图片必须是工作区内普通文件：{resolved_image}")
        command.extend(["--image", str(resolved_image)])
    command.extend(
        [
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "-",
        ]
    )
    return command


def codex_launcher(
    codex_binary: str, platform_name: Optional[str] = None
) -> List[str]:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt" and Path(codex_binary).suffix.casefold() != ".exe":
        raise LoopError(
            "Windows prompt nodes require the reviewed native codex.exe; "
            "script wrappers are not accepted"
        )
    return [codex_binary]


def find_codex() -> str:
    configured = os.getenv("CODEX_BINARY", "").strip()
    if configured:
        resolved = shutil.which(configured) or str(Path(configured).expanduser().resolve())
        if Path(resolved).is_file():
            return resolved
        raise LoopError(f"CODEX_BINARY 指向的文件不存在：{resolved}")
    detected = shutil.which("codex")
    if detected:
        return detected
    app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if app_binary.exists():
        return str(app_binary)
    raise LoopError("找不到 codex CLI")


CODEX_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATHEXT",
    "PROGRAMDATA",
    "SHELL",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "USER",
    "WINDIR",
}


def codex_subprocess_environment(
    source: Optional[Mapping[str, str]] = None,
    *,
    codex_home: Path,
    shell_home: Path,
    shell_tmp: Path,
) -> Dict[str, str]:
    source_env = os.environ if source is None else source
    allowed = {key.casefold() for key in CODEX_ENV_ALLOWLIST}
    result = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    result["PATH"] = os.defpath
    result["PYTHONUNBUFFERED"] = "1"
    result["PYTHONUTF8"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    # Codex v0.147+ treats this official sentinel as an explicit request for
    # no execution/filesystem environment.  With no environment, shell,
    # apply_patch, view_image and permission-request tools are not registered.
    # Images are still read by the Codex host and attached to the model turn.
    for key in list(result):
        if key.upper().startswith("CODEX_EXEC_SERVER_NOISE_"):
            result.pop(key, None)
    result["CODEX_EXEC_SERVER_URL"] = "none"
    result["CODEX_HOME"] = str(codex_home.resolve())
    isolated_shell_home = str(shell_home.resolve())
    isolated_shell_tmp = str(shell_tmp.resolve())
    result["HOME"] = isolated_shell_home
    result["USERPROFILE"] = isolated_shell_home
    result["TMPDIR"] = isolated_shell_tmp
    result["TMP"] = isolated_shell_tmp
    result["TEMP"] = isolated_shell_tmp
    return result


def _canonical_material_binding_line(
    references: Sequence[Mapping[str, object]],
) -> str:
    mappings = ["@视频1=原视频"]
    for index, item in enumerate(references, start=1):
        semantic_name = str(item.get("semantic_name") or "").strip()
        if not semantic_name:
            raise LoopError(f"@图片{index} 缺少父层语义名称")
        mappings.append(f"@图片{index}={semantic_name}")
    return "素材绑定：" + "；".join(mappings) + "。"


def _requirement_body(line: str, job_id: str) -> str:
    default_match = DEFAULT_RULE_RE.match(line)
    if default_match:
        return default_match.group(1).split("#", 1)[0].strip()
    parts = re.split(r"[:：]", line, maxsplit=1)
    if len(parts) != 2 or job_id not in expand_video_ids(parts[0]):
        raise LoopError(f"{job_id} 的父层需求行无法解析")
    return parts[1].split("#", 1)[0].strip()


def _canonical_requirement_lines(
    batch: Path,
    job_id: str,
    references: Sequence[Mapping[str, object]],
) -> List[str]:
    semantic_names = [
        str(item.get("semantic_name") or "").strip() for item in references
    ]

    def replace_handle(match: re.Match[str]) -> str:
        kind = match.group(1)
        index = int(match.group(2))
        if kind == "视频":
            if index != 1:
                raise LoopError(f"{job_id} 的需求引用了未知素材 @视频{index}")
            return "原视频"
        if not 1 <= index <= len(semantic_names):
            raise LoopError(f"{job_id} 的需求引用了未绑定素材 @图片{index}")
        return semantic_names[index - 1]

    rendered: List[str] = []
    used_reference_handles: Set[int] = set()
    for line in job_requirement_lines(batch, job_id):
        body = _requirement_body(line, job_id)
        used_reference_handles.update(
            int(value) for value in re.findall(r"@图片(\d+)", body)
        )
        body = re.sub(r"@(视频|图片)(\d+)", replace_handle, body)
        if body:
            rendered.append(body)
    missing_handles = sorted(
        set(range(1, len(semantic_names) + 1)) - used_reference_handles
    )
    if missing_handles:
        raise LoopError(
            f"{job_id} 的需求必须明确说明每张绑定素材的用途，缺少："
            + "、".join(f"@图片{index}" for index in missing_handles)
        )
    if not rendered:
        raise LoopError(f"{job_id} 缺少可写入执行提示词的父层需求")
    return rendered


def validate_requirement_binding_contract(
    batch: Path,
    bindings: Mapping[str, Mapping[str, object]],
) -> None:
    """Fail before prompt generation when a bound image has no stated use."""

    skipped = explicitly_skipped_ids(batch)
    for job_id, binding in bindings.items():
        if job_id in skipped:
            continue
        references = binding.get("references")
        if not isinstance(references, list):
            raise LoopError(f"{job_id} 的 references 无效")
        _canonical_requirement_lines(
            batch,
            job_id,
            [dict(item) for item in references if isinstance(item, dict)],
        )


def compose_execution_prompt(
    batch: Path,
    job_id: str,
    references: Sequence[Mapping[str, object]],
    node_prompt: object,
) -> str:
    """Compose binding and immutable requirements around node-authored prose."""

    prompt_text = node_prompt.strip() if isinstance(node_prompt, str) else ""
    if not prompt_text:
        raise LoopError(f"{job_id} 提示词交付为空")
    lines = prompt_text.splitlines()
    first_nonempty = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_nonempty is not None and lines[first_nonempty].strip().startswith(
        "素材绑定："
    ):
        del lines[first_nonempty]
    node_body = "\n".join(lines).strip()
    if not node_body:
        raise LoopError(f"{job_id} 提示词只有素材绑定，缺少逐镜执行说明")
    if any(marker in node_body for marker in PROMPT_RESERVED_SECTION_MARKERS):
        raise LoopError(f"{job_id} 模型逐镜说明不得重复父层保留区块标题")
    semantic_names = [
        str(item.get("semantic_name") or "").strip() for item in references
    ]
    missing_names = [
        name for name in semantic_names if name and name not in node_body
    ]
    if missing_names:
        raise LoopError(
            f"{job_id} 模型逐镜说明未实际使用全部父层素材，缺少："
            + "、".join(missing_names)
        )
    requirement_block = [
        IMMUTABLE_REQUIREMENTS_HEADER,
        *[
            f"- {line}"
            for line in _canonical_requirement_lines(batch, job_id, references)
        ],
    ]
    parts = [
        _canonical_material_binding_line(references),
        "\n".join(requirement_block),
        NODE_EXECUTION_HEADER,
    ]
    parts.append(node_body)
    return "\n".join(parts).strip()


def promote_prompt_result(
    batch: Path,
    project_root: Path,
    job_id: str,
    references: Sequence[Mapping[str, object]],
    prompt: object,
) -> None:
    """Persist the parent-composed final prompt using parent-owned I/O."""

    prompt_text = compose_execution_prompt(batch, job_id, references, prompt)
    output_dir = job_output_dir(project_root, batch, job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_dir / PROMPT_FILENAME, prompt_text + "\n")


def run_video_to_prompt_node(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    job_id: str,
    references: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    """Sample one Job locally and return its prompt from a read-only model turn."""

    trusted_analysis_tool = _trusted_ffmpeg_executable()
    attached_images: List[Path] = []
    staged_input: Dict[str, object] = {}

    def setup_workspace(workspace: Path) -> None:
        input_path = write_video_to_prompt_node_input(
            batch,
            project_root,
            job_id,
            workspace,
            references,
            analysis_tool=trusted_analysis_tool,
        )
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise LoopError(f"{job_id} 节点输入不是 JSON 对象")
        staged_input.update(payload)
        raw_order = payload.get("attachment_order")
        if not isinstance(raw_order, list) or not raw_order:
            raise LoopError(f"{job_id} 节点没有可附加的视觉输入")
        attached_images.extend(Path(str(value)) for value in raw_order)

    def build_prompt(_workspace: Path) -> str:
        if not staged_input:
            raise LoopError(f"{job_id} 节点输入尚未建立")
        return video_to_prompt_node_prompt(job_id, staged_input)

    result = _run_codex_exec(
        batch,
        loop_root,
        project_root,
        build_prompt,
        schema_path=SCRIPT_ROOT / "video_batch_node_result.schema.json",
        job_ids=[job_id],
        stage="video-to-prompt",
        node_workspace_setup=setup_workspace,
        attached_workspace_images=attached_images,
    )
    validated = validate_node_result(result, job_id)
    job = validated["jobs"][0]
    if isinstance(job, dict) and job.get("status") == NODE_COMPLETE_STATUS:
        promote_prompt_result(
            batch,
            project_root,
            job_id,
            references,
            job.get("prompt"),
        )
    return validated


def _node_exec_timeout_seconds() -> int:
    """Return the bounded wall-clock budget for one Codex node invocation."""

    return _environment_int(
        "VIDEO_LOOP_NODE_EXEC_TIMEOUT_SECONDS",
        DEFAULT_NODE_EXEC_TIMEOUT_SECONDS,
        1,
    )


def _run_codex_exec(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    prompt: str | Callable[[Path], str],
    *,
    schema_path: Path,
    job_ids: Sequence[str],
    stage: str = "prepare",
    node_workspace_setup: Optional[Callable[[Path], None]] = None,
    attached_workspace_images: Sequence[Path] = (),
) -> Dict[str, object]:
    """Run one `codex exec` against a fixed output schema and return its result.

    Each stage writes to its own agent directory so a later node cannot
    overwrite an earlier one's prompt, result, or process record.
    """

    scoped_ids = list(job_ids)
    if not scoped_ids:
        raise LoopError("节点执行必须绑定至少一个 Job")
    timeout_seconds = _node_exec_timeout_seconds()
    started_at = utc_now()
    total_started_monotonic = time.monotonic()
    invocation_id = (
        datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        + f"-{os.getpid()}-{uuid.uuid4().hex}"
    )
    scope_name = "-".join(scoped_ids) + f"-{stage}"
    result_root = (
        batch
        / "streaming-results"
        / "agents"
        / scope_name
        / invocation_id
    )
    result_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        result_root.chmod(0o700)
    result_path = result_root / "codex-result.json"
    prompt_path = result_root / "codex-prompt.txt"
    process_path = result_root / "codex-process.json"
    log_stem = f"{batch.name}-{scope_name}-{invocation_id}"
    stdout_path = loop_root / "logs" / f"{log_stem}.jsonl"
    stderr_path = loop_root / "logs" / f"{log_stem}.stderr.log"
    atomic_write_json(
        process_path,
        {
            "phase": "STAGING",
            "stage": stage,
            "job_ids": scoped_ids,
            "timeout_seconds": timeout_seconds,
            "started_at": started_at,
        },
    )

    def record_setup_failure(exc: BaseException) -> None:
        atomic_write_json(
            process_path,
            {
                "phase": "SETUP_FAILED",
                "stage": stage,
                "job_ids": scoped_ids,
                "timeout_seconds": timeout_seconds,
                "started_at": started_at,
                "finished_at": utc_now(),
                "total_seconds": round(
                    time.monotonic() - total_started_monotonic, 6
                ),
                "error": str(exc),
            },
        )

    state_dir = executor_state_dir(loop_root)
    try:
        require_external_state_dir(state_dir, loop_root, project_root)
        codex_node_home = validate_node_home(
            state_dir / NODE_HOME_DIRECTORY,
            repo_root=project_root,
            require_auth=True,
        )
    except (CodexNodeHomeError, LoopError) as exc:
        record_setup_failure(exc)
        raise LoopError(str(exc)) from exc
    node_runs_root = state_dir / "node-runs"
    try:
        node_runs_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            node_runs_root.chmod(0o700)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix=f"video-loop-{scope_name}-",
            dir=node_runs_root,
        )
    except Exception as exc:
        record_setup_failure(exc)
        raise
    with temporary_directory as temporary_value:
        temporary_root = Path(temporary_value).resolve()
        workspace_root = temporary_root / "workspace"
        transport_root = temporary_root / "transport"
        shell_home = workspace_root / "home"
        shell_tmp = workspace_root / "tmp"
        try:
            if os.name != "nt":
                temporary_root.chmod(0o700)
            workspace_root.mkdir(mode=0o700)
            transport_root.mkdir(mode=0o700)
            shell_home.mkdir(mode=0o700)
            shell_tmp.mkdir(mode=0o700)
            if node_workspace_setup is not None:
                node_workspace_setup(workspace_root)
            runtime_prompt = prompt(workspace_root) if callable(prompt) else prompt
            if not isinstance(runtime_prompt, str) or not runtime_prompt.strip():
                raise LoopError("节点 prompt 为空")
            atomic_write_text(prompt_path, runtime_prompt)
            attached_image_paths: List[Path] = []
            for raw_path in attached_workspace_images:
                if raw_path.is_absolute() or ".." in raw_path.parts:
                    raise LoopError(
                        f"节点附加图片路径必须位于工作区内：{raw_path}"
                    )
                candidate_image = workspace_root / raw_path
                if is_link_like(candidate_image):
                    raise LoopError(
                        f"节点附加图片不得是链接或 Windows junction：{raw_path}"
                    )
                image_path = candidate_image.resolve()
                try:
                    image_path.relative_to(workspace_root)
                except ValueError as exc:
                    raise LoopError(f"节点附加图片越过工作区：{raw_path}") from exc
                if is_link_like(image_path) or not image_path.is_file():
                    raise LoopError(
                        f"节点附加图片不存在或不是普通文件：{raw_path}"
                    )
                attached_image_paths.append(image_path)
            runtime_schema_path = transport_root / "output.schema.json"
            shutil.copy2(schema_path, runtime_schema_path)
            runtime_result_path = transport_root / "codex-result.json"
            codex_binary = find_codex()
            if os.name == "nt":
                verified_codex = verify_windows_codex(Path(codex_binary))
                codex_binary = str(verified_codex["binary"])
            command = build_codex_command(
                codex_binary,
                batch,
                project_root,
                runtime_schema_path,
                runtime_result_path,
                node_workspace=workspace_root,
                image_paths=attached_image_paths,
            )
        except Exception as exc:
            record_setup_failure(exc)
            if isinstance(exc, CodexArtifactError):
                raise LoopError(
                    f"official Windows Codex provenance failed before execution: {exc}"
                ) from exc
            raise
        queued_at = utc_now()
        lock_wait_started_monotonic = time.monotonic()
        lock_acquired_at: Optional[str] = None
        exec_started_at: Optional[str] = None
        exec_finished_at: Optional[str] = None
        lock_acquired_monotonic: Optional[float] = None
        exec_started_monotonic: Optional[float] = None
        exec_finished_monotonic: Optional[float] = None
        completed: Optional[subprocess.CompletedProcess[str]] = None
        timed_out = False
        lock_error: Optional[CodexNodeHomeError] = None
        exec_error: Optional[OSError] = None
        atomic_write_json(
            process_path,
            {
                "phase": "WAITING_FOR_AUTH_LOCK",
                "stage": stage,
                "job_ids": scoped_ids,
                "timeout_seconds": timeout_seconds,
                "started_at": started_at,
                "queued_at": queued_at,
            },
        )
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            chmod_private(stdout_path)
            chmod_private(stderr_path)
            try:
                with locked_node_home(
                    codex_node_home,
                    timeout_seconds=max(120.0, float(timeout_seconds) * 4),
                ):
                    lock_acquired_at = utc_now()
                    lock_acquired_monotonic = time.monotonic()
                    exec_started_at = utc_now()
                    exec_started_monotonic = time.monotonic()
                    atomic_write_json(
                        process_path,
                        {
                            "phase": "EXECUTING",
                            "stage": stage,
                            "job_ids": scoped_ids,
                            "timeout_seconds": timeout_seconds,
                            "started_at": started_at,
                            "queued_at": queued_at,
                            "lock_acquired_at": lock_acquired_at,
                            "exec_started_at": exec_started_at,
                            "auth_lock_wait_seconds": round(
                                lock_acquired_monotonic
                                - lock_wait_started_monotonic,
                                6,
                            ),
                        },
                    )
                    try:
                        completed = subprocess.run(
                            command,
                            cwd=str(workspace_root),
                            stdout=stdout_handle,
                            stderr=stderr_handle,
                            input=runtime_prompt,
                            text=True,
                            check=False,
                            timeout=timeout_seconds,
                            env=codex_subprocess_environment(
                                codex_home=codex_node_home,
                                shell_home=shell_home,
                                shell_tmp=shell_tmp,
                            ),
                        )
                    finally:
                        exec_finished_at = utc_now()
                        exec_finished_monotonic = time.monotonic()
            except subprocess.TimeoutExpired:
                timed_out = True
            except CodexNodeHomeError as exc:
                lock_error = exc
            except OSError as exc:
                exec_error = exc
        result_text = (
            runtime_result_path.read_text(encoding="utf-8")
            if runtime_result_path.is_file()
            else None
        )
    finished_at = utc_now()
    total_finished_monotonic = time.monotonic()
    auth_lock_wait_seconds = (
        (lock_acquired_monotonic or total_finished_monotonic)
        - lock_wait_started_monotonic
    )
    exec_seconds = (
        (exec_finished_monotonic - exec_started_monotonic)
        if exec_finished_monotonic is not None
        and exec_started_monotonic is not None
        else 0.0
    )
    if lock_error is not None:
        phase = "LOCK_FAILED"
    elif timed_out:
        phase = "TIMED_OUT"
    elif exec_error is not None:
        phase = "EXEC_FAILED"
    elif completed is not None and completed.returncode == 0:
        phase = "RESULT_PENDING_VALIDATION"
    else:
        phase = "FAILED"
    reconnect_text = ""
    for log_path in (stdout_path, stderr_path):
        try:
            reconnect_text += log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    reconnect_count = len(
        re.findall(
            r"\b(?:reconnecting|connection failed|tls handshake failed)\b",
            reconnect_text,
            flags=re.IGNORECASE,
        )
    )
    process_record = {
            "returncode": completed.returncode if completed is not None else None,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "phase": phase,
            "stage": stage,
            "job_ids": scoped_ids,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "isolated_node_workspace": True,
            "stable_instruction_free_codex_home": True,
            "codex_auth_processes_serialized": True,
            "isolated_transport": True,
            "execution_environment": "none",
            "permission_profile": "node_isolated",
            "writable_workspace_paths": [],
            "attached_image_count": len(attached_image_paths),
            "shell_tools_disabled": True,
            "tool_network_disabled": True,
            "skill_discovery_disabled": True,
            "plugins_disabled": True,
            "apps_disabled": True,
            "multi_agent_disabled": True,
            "queued_at": queued_at,
            "lock_acquired_at": lock_acquired_at,
            "exec_started_at": exec_started_at,
            "exec_finished_at": exec_finished_at,
            "auth_lock_wait_seconds": round(auth_lock_wait_seconds, 6),
            "exec_seconds": round(exec_seconds, 6),
            "total_seconds": round(
                total_finished_monotonic - total_started_monotonic, 6
            ),
            "transport_reconnect_count": reconnect_count,
            "started_at": started_at,
            "finished_at": finished_at,
        }
    if lock_error is not None:
        process_record["error"] = str(lock_error)
    elif exec_error is not None:
        process_record["error"] = str(exec_error)
    atomic_write_json(process_path, process_record)
    if lock_error is not None:
        raise LoopError(str(lock_error)) from lock_error
    if timed_out:
        raise LoopError(
            f"{','.join(scoped_ids)} {stage} 的 codex exec 超过 "
            f"{timeout_seconds} 秒；查看 {stdout_path} 和 {stderr_path}"
        )
    if exec_error is not None:
        raise LoopError(f"codex exec 启动失败：{exec_error}") from exec_error
    assert completed is not None
    if completed.returncode != 0:
        raise LoopError(f"codex exec 退出码 {completed.returncode}；查看 {stderr_path}")
    if result_text is None:
        process_record["phase"] = "RESULT_MISSING"
        process_record["error"] = "codex exec 成功退出，但没有生成节点结果"
        atomic_write_json(process_path, process_record)
        raise LoopError("codex exec 成功退出，但没有生成节点结果")
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        process_record["phase"] = "RESULT_INVALID"
        process_record["error"] = f"节点结果不是有效 JSON：{exc}"
        atomic_write_json(process_path, process_record)
        raise LoopError(f"节点结果不是有效 JSON：{exc}") from exc
    if not isinstance(result, dict):
        process_record["phase"] = "RESULT_INVALID"
        process_record["error"] = "codex-result.json 不是 JSON 对象"
        atomic_write_json(process_path, process_record)
        raise LoopError("codex-result.json 不是 JSON 对象")
    atomic_write_json(result_path, result)
    process_record["phase"] = "SUCCEEDED"
    process_record["result_validated_at"] = utc_now()
    process_record.pop("error", None)
    atomic_write_json(process_path, process_record)
    return result


def _path_within(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise LoopError(f"{label} 越过允许目录：{resolved}") from exc
    return resolved


def find_replacement_executor(
    project_root: Path = PROJECT_ROOT,
    environment: Optional[Mapping[str, str]] = None,
    *,
    profile: Optional[BackendProfile] = None,
) -> ExecutorSpec:
    selected_profile = profile or get_backend_profile("dreamina_cli_seedance_2_5")
    source = os.environ if environment is None else environment
    configured = str(source.get("VIDEO_REPLACEMENT_EXECUTOR", "")).strip()
    if not configured:
        configured = str(source.get("VIDEO_REPLACER_PIPELINE", "")).strip()
    project_candidate = (project_root / "tools" / selected_profile.adapter_name).resolve()
    packaged_candidate = (SCRIPT_ROOT / selected_profile.adapter_name).resolve()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        # Environment may select the reviewed *copy* used by a packaged
        # workflow, but cannot turn the adapter name into an arbitrary script
        # injection point.  Profile-managed batches have no free executor path.
        if candidate not in {project_candidate, packaged_candidate}:
            raise LoopError(
                f"{selected_profile.profile_id} 的执行器路径不在受控注册表中"
            )
    else:
        candidate = (
            project_candidate if project_candidate.is_file() else packaged_candidate
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise LoopError(f"找不到审核过的视频替换执行器：{resolved}")
    if resolved.name.casefold() != selected_profile.adapter_name.casefold():
        raise LoopError(
            f"{selected_profile.profile_id} 只允许审核过的执行器 "
            f"{selected_profile.adapter_name}，拒绝 {resolved.name}"
        )
    try:
        model_version = selected_profile.resolved_model_version(
            dict(source) if environment is not None else None
        )
    except BackendProfileError as exc:
        raise LoopError(str(exc)) from exc
    return ExecutorSpec(
        path=resolved,
        transport=selected_profile.transport,
        profile_id=selected_profile.profile_id,
        model_version=model_version,
        supports_authorized_retry=(selected_profile.adapter_name == "dreamina_video.py"),
    )


def executor_for_batch(
    batch: Path,
    project_root: Path,
    *,
    executor: Optional[ExecutorSpec] = None,
    index: Optional[Mapping[str, object]] = None,
    reference_index: Optional[Mapping[str, object]] = None,
) -> Tuple[ExecutorSpec, BackendProfile, bool]:
    """Resolve the immutable adapter for the batch's selected profile.

    ``executor`` exists for deterministic tests and direct parent integration;
    it cannot change a schema-v3 batch to another adapter or transport.
    """

    profile, profile_managed = backend_profile_for_batch(
        batch, index=index, reference_index=reference_index
    )
    if executor is None:
        return (
            find_replacement_executor(project_root, profile=profile),
            profile,
            profile_managed,
        )
    if profile_managed:
        if (
            executor.path.name.casefold() != profile.adapter_name.casefold()
            or executor.transport != profile.transport
        ):
            raise LoopError(
                f"{profile.profile_id} 的执行器或传输方式与审核 profile 不匹配"
            )
        try:
            model_version = profile.resolved_model_version()
        except BackendProfileError as exc:
            raise LoopError(str(exc)) from exc
        executor = ExecutorSpec(
            path=executor.path,
            transport=profile.transport,
            profile_id=profile.profile_id,
            model_version=model_version,
            supports_authorized_retry=(profile.adapter_name == "dreamina_video.py"),
        )
    return executor, profile, profile_managed


def executor_state_dir(
    loop_root: Path, environment: Optional[Mapping[str, str]] = None
) -> Path:
    source = os.environ if environment is None else environment
    try:
        resolved = resolve_state_root(source).resolve()
    except StatePathError as exc:
        raise LoopError(str(exc)) from exc
    _reject_temporary_state_dir(resolved)
    return resolved


LOCAL_TOOL_ENV_ALLOWLIST = {
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
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


def local_tool_environment(
    source: Optional[Mapping[str, str]] = None,
    *,
    extra_names: Iterable[str] = (),
) -> Dict[str, str]:
    """Return runtime basics without unrelated provider credentials."""

    source_env = os.environ if source is None else source
    allowed = {name.casefold() for name in LOCAL_TOOL_ENV_ALLOWLIST}
    allowed.update(name.casefold() for name in extra_names)
    environment = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    environment.pop(PAYMENT_AUTH_TOKEN_ENV, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def execution_environment_for_profile(
    profile: BackendProfile, state_dir: Path
) -> Dict[str, str]:
    """Create the minimal adapter environment without leaking credentials.

    Dreamina receives only runtime basics because its official CLI manages its
    own login. Ark receives only reviewed config and credential names; the
    parent never logs them and removes the payment capability before spawning
    either adapter.
    """

    if profile.adapter_name != "ark_video.py":
        environment = local_tool_environment(
            extra_names=(
                "DREAMINA_BINARY",
                "VIDEO_REPLACEMENT_EXECUTOR",
                "VIDEO_REPLACER_PIPELINE",
            )
        )
    else:
        allowed_names = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "TMPDIR",
            "TMP",
            "TEMP",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "VIDEO_REPLACER_ARK_API_KEY",
            "VIDEO_REPLACER_ARK_API_BASE_URL",
            "VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID",
            "VIDEO_REPLACER_TOS_ACCESS_KEY",
            "VIDEO_REPLACER_TOS_SECRET_KEY",
            "VIDEO_REPLACER_TOS_SECURITY_TOKEN",
            "VIDEO_REPLACER_TOS_ENDPOINT",
            "VIDEO_REPLACER_TOS_REGION",
            "VIDEO_REPLACER_TOS_BUCKET",
            "VIDEO_REPLACER_TOS_PREFIX",
            "VIDEO_REPLACER_TOS_LIFECYCLE_RULE_ID",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in allowed_names
        }
    environment.pop(PAYMENT_AUTH_TOKEN_ENV, None)
    environment["VIDEO_REPLACER_STATE_DIR"] = str(state_dir)
    environment["FFMPEG"] = str(_trusted_ffmpeg_executable())
    return environment


def probe_environment_for_profile(profile: BackendProfile) -> Dict[str, str]:
    """Give a no-cost probe only runtime basics, never Ark/TOS credentials."""

    # Dreamina's probe is local too. Keeping it on the ordinary environment
    # preserves existing CLI discovery while still removing the JS payment
    # capability. Ark gets a deliberately tiny environment: its probe cannot
    # accidentally read a credential that happens to be set for later paid
    # submission.
    if profile.adapter_name != "ark_video.py":
        environment = local_tool_environment(
            extra_names=(
                "DREAMINA_BINARY",
                "VIDEO_REPLACEMENT_EXECUTOR",
                "VIDEO_REPLACER_PIPELINE",
            )
        )
    else:
        safe_names = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "HOME",
            "USERPROFILE",
            "TMPDIR",
            "TMP",
            "TEMP",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in safe_names
        }
    environment.pop(PAYMENT_AUTH_TOKEN_ENV, None)
    environment["FFMPEG"] = str(_trusted_ffmpeg_executable())
    return environment


def _reject_temporary_state_dir(state_dir: Path) -> None:
    temporary_roots = {Path(tempfile.gettempdir()).expanduser().resolve()}
    for variable in ("TMPDIR", "TMP", "TEMP"):
        raw = os.getenv(variable, "").strip()
        if raw:
            temporary_roots.add(Path(raw).expanduser().resolve())
    for temporary_root in temporary_roots:
        if state_dir == temporary_root or temporary_root in state_dir.parents:
            raise LoopError(
                f"状态目录不得位于临时目录：{state_dir}"
            )


def require_external_state_dir(
    state_dir: Path, loop_root: Path, project_root: Path
) -> None:
    if path_contains_link_like(state_dir):
        raise LoopError(f"状态目录不得包含链接或 Windows junction：{state_dir}")
    resolved = state_dir.resolve()
    for root, label in ((loop_root, "Loop"), (project_root, "project")):
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        raise LoopError(f"状态目录必须位于 {label} workspace 之外：{resolved}")


def _plan_reference_relative_path(raw_image: object, batch: Path) -> Path:
    raw_path = Path(str(raw_image))
    replacement_root = (batch / "replacements").resolve()
    if raw_path.is_absolute():
        resolved = raw_path.expanduser().resolve()
        try:
            return resolved.relative_to(replacement_root)
        except ValueError:
            parts = raw_path.parts
            indexes = [index for index, part in enumerate(parts) if part == "replacements"]
            if indexes:
                relative = Path(*parts[indexes[-1] + 1 :])
                if relative.parts and ".." not in relative.parts:
                    return relative
    if not raw_path.is_absolute() and ".." not in raw_path.parts:
        if raw_path.parts and raw_path.parts[0] == "replacements":
            return Path(*raw_path.parts[1:])
        return raw_path
    raise LoopError(f"image 无法安全重绑定到当前 replacements/：{raw_image}")


def _expected_input_bindings(prompt_file: Path, images: Sequence[Path]) -> Dict[str, object]:
    prompt_size, prompt_sha = _hash_without_change(prompt_file)
    reference_images: List[Dict[str, object]] = []
    for index, image in enumerate(images, start=1):
        image_size, image_sha = _hash_without_change(image)
        reference_images.append(
            {"index": index, "sha256": image_sha, "size_bytes": image_size}
        )
    return {
        "schema_version": 1,
        "prompt": {"sha256": prompt_sha, "size_bytes": prompt_size},
        "reference_images": reference_images,
    }


_SHOT_HEADER_PATTERN = re.compile(
    r"^(?P<header>镜头\s*\d+\s*[（(]\s*\d+\.\d-\d+\.\ds\s*[）)])"
    r"(?:\s*[：:]?\s*(?P<inline_body>.*))?$"
)
_SHOT_HEADER_PREFIX_PATTERN = re.compile(r"^镜头")
_OVERBROAD_FALLBACK_PARTS = (
    "保持原视频本镜头的构图、机位、景别、运镜、时长和剪辑节奏不变",
    "保持原有车辆、人物、产品和环境的数量、位置、动作、视线、状态、接触、遮挡与空间关系不变",
    "保持原有光线、曝光和明暗变化逻辑不变",
)
_OFFSCREEN_NEGATION_PATTERN = re.compile(
    r"(?:不进入|不出现|不在(?:本)?镜头|未出现|没有出现|不可见|看不到|"
    r"无需出现|不应出现|不要出现|不得出现|不露面|画面(?:中|里)没有)"
)


def _validate_shot_writing_contract(prompt: str) -> List[str]:
    """Return advisory multi-shot writing findings without redefining source shots.

    Source analysis remains responsible for source shots. Natural prompt prose
    can express the same edit with a different heading or a user-required
    negation, so these findings are intentionally not submission blockers.
    """

    lines = prompt.splitlines()
    headers = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if _SHOT_HEADER_PREFIX_PATTERN.match(line.strip())
    ]
    if not headers:
        return []

    conflicts: List[str] = []
    for header_index, (line_index, header) in enumerate(headers, start=1):
        header_match = _SHOT_HEADER_PATTERN.match(header)
        if not header_match:
            conflicts.append(
                f"镜头{header_index}时间格式必须为“镜头N（0.0-2.5s）”，精确到0.1秒"
            )
            continue
        next_line = (
            headers[header_index][0]
            if header_index < len(headers)
            else len(lines)
        )
        body_lines = lines[line_index + 1 : next_line]
        inline_body = str(header_match.group("inline_body") or "").strip()
        body = "\n".join([inline_body, *body_lines]).strip()
        block = "\n".join([header, *body_lines])
        if not body:
            conflicts.append(f"镜头{header_index}缺少逐镜变更内容")
        if all(part in block for part in _OVERBROAD_FALLBACK_PARTS):
            conflicts.append(
                f"镜头{header_index}包含通用全量保持清单，"
                "不得描述本镜头未出现或未涉及的元素"
            )
        if _OFFSCREEN_NEGATION_PATTERN.search(body):
            conflicts.append(
                f"镜头{header_index}把未出镜内容写成否定句；"
                "逐镜正文只写本镜头可见或用户明确要求的内容"
            )
    return conflicts


def _validate_reference_alias_contract(
    prompt: str,
    reference_count: int,
    expected_reference_names: Optional[Sequence[str]] = None,
    expected_requirement_lines: Optional[Sequence[str]] = None,
) -> List[str]:
    raw_lines = prompt.splitlines()
    nonempty_lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    if not nonempty_lines or not nonempty_lines[0].startswith("素材绑定："):
        return ["首个非空行必须是完整的素材绑定"]

    binding_line = nonempty_lines[0]
    conflicts: List[str] = []
    video_mappings = re.findall(
        r"@视频([0-9]+)\s*=\s*([^；。\n]+)", binding_line
    )
    if [(int(index), name.strip()) for index, name in video_mappings] != [
        (1, "原视频")
    ]:
        conflicts.append("素材绑定行必须且只能声明 @视频1=原视频")
    expected_handles = list(range(1, reference_count + 1))
    mappings = re.findall(r"@图片([0-9]+)\s*=\s*([^；。\n]+)", binding_line)
    mapping_indexes = [int(index) for index, _ in mappings]
    if mapping_indexes != expected_handles:
        conflicts.append("素材绑定行必须为每张有序参考图声明一个名称")
    semantic_names = [name.strip() for _, name in mappings]
    if len(semantic_names) != len(set(semantic_names)):
        conflicts.append("素材绑定行的素材名称必须互不重复")
    if any(
        re.search(r"[\\/_]|\.(?:png|jpe?g|webp|gif)$|^(?:interior|ref)-\d", name, re.IGNORECASE)
        for name in semantic_names
    ):
        conflicts.append("素材绑定行必须使用自然、可读的名称，不能使用文件名或路径")
    body_lines = nonempty_lines[1:]
    if any(re.search(r"@(?:视频[0-9]+|图片[0-9]+)", line) for line in body_lines):
        conflicts.append("素材句柄只能出现在首行素材绑定；正文必须直接使用对应名称")
    if expected_reference_names is not None:
        expected_names = [str(name).strip() for name in expected_reference_names]
        if semantic_names != expected_names:
            conflicts.append("素材绑定名称必须逐项等于父层登记的有序语义名称")
        expected_binding = "素材绑定：" + "；".join(
            ["@视频1=原视频"]
            + [
                f"@图片{index}={name}"
                for index, name in enumerate(expected_names, start=1)
            ]
        ) + "。"
        if binding_line != expected_binding:
            conflicts.append("素材绑定首行必须逐字等于父层规范绑定")
    if any(
        first in second or second in first
        for index, first in enumerate(semantic_names)
        for second in semantic_names[index + 1 :]
    ):
        conflicts.append("素材绑定名称不能互为完整子串")
    semantic_body_lines = body_lines
    if expected_requirement_lines is not None:
        expected_body_prefix = [
            IMMUTABLE_REQUIREMENTS_HEADER,
            *[f"- {line}" for line in expected_requirement_lines],
            NODE_EXECUTION_HEADER,
        ]
        expected_full_prefix = [
            (
                "素材绑定："
                + "；".join(
                    ["@视频1=原视频"]
                    + [
                        f"@图片{index}={name}"
                        for index, name in enumerate(
                            (
                                [str(value).strip() for value in expected_reference_names]
                                if expected_reference_names is not None
                                else semantic_names
                            ),
                            start=1,
                        )
                    ]
                )
                + "。"
            ),
            *expected_body_prefix,
        ]
        if raw_lines[: len(expected_full_prefix)] != expected_full_prefix:
            conflicts.append("不可变用户需求块缺失、改写或顺序不一致")
            semantic_body_lines = []
        else:
            semantic_body_lines = raw_lines[len(expected_full_prefix) :]
        if not any(line.strip() for line in semantic_body_lines):
            conflicts.append("模型生成的逐镜执行说明不能为空")
        semantic_body = "\n".join(semantic_body_lines)
        if any(
            marker in semantic_body for marker in PROMPT_RESERVED_SECTION_MARKERS
        ):
            conflicts.append("模型逐镜说明不得重复父层保留区块标题")
    body = "\n".join(semantic_body_lines)
    missing_names = [name for name in semantic_names if name and name not in body]
    if missing_names:
        conflicts.append(
            "正文必须实际使用每个已绑定的素材名称，缺少："
            + "、".join(missing_names)
        )
    return conflicts


def validate_execution_prompt(
    prompt_file: Path,
    reference_count: Optional[int] = None,
    expected_reference_names: Optional[Sequence[str]] = None,
    expected_requirement_lines: Optional[Sequence[str]] = None,
) -> None:
    try:
        prompt = prompt_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LoopError(f"无法读取执行提示词：{prompt_file}") from exc
    if not prompt:
        raise LoopError("执行提示词不能为空")

    if expected_reference_names is not None:
        expected_count = len(expected_reference_names)
        if reference_count is not None and reference_count != expected_count:
            raise LoopError("父层参考素材数量与语义名称数量不一致")
        reference_count = expected_count
    if reference_count is None:
        return
    conflicts = _validate_reference_alias_contract(
        prompt,
        reference_count,
        expected_reference_names=expected_reference_names,
        expected_requirement_lines=expected_requirement_lines,
    )
    if conflicts:
        raise LoopError(PROMPT_GATE_ERROR_PREFIX + "；".join(conflicts))


def _trusted_ffmpeg_executable() -> Path:
    configured = os.getenv("FFMPEG", "").strip()
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    detected = shutil.which("ffmpeg")
    if detected:
        candidates.append(Path(detected))
    try:
        import imageio_ffmpeg  # type: ignore

        candidates.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:
        pass
    runtime_root = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "lib"
    )
    candidates.extend(
        sorted(runtime_root.glob("python*/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"))
    )
    user_ffmpeg_roots = (
        Path.home() / "Library" / "Python",
        Path.home() / ".local" / "lib",
        Path.home() / "AppData" / "Roaming" / "Python",
    )
    for root in user_ffmpeg_roots:
        candidates.extend(
            sorted(
                root.glob(
                    "**/site-packages/imageio_ffmpeg/binaries/ffmpeg-*"
                )
            )
        )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise LoopError("找不到可信 ffmpeg，无法核验源视频技术准备 lineage")


def h264_stream_sha256(path: Path) -> str:
    command = [
        str(_trusted_ffmpeg_executable()),
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-f",
        "h264",
        "-",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    _, stderr = process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise LoopError(f"无法核验 H.264 视频流：{detail or process.returncode}")
    return digest.hexdigest()


def upload_constraints_for_profile(profile: BackendProfile) -> UploadConstraints:
    """Translate the reviewed profile into the local media Gate contract."""

    return UploadConstraints(
        limit_bytes=profile.limit_bytes,
        target_bytes=profile.target_bytes,
        allowed_containers=tuple(sorted(profile.allowed_containers)),
        allowed_video_codecs=tuple(sorted(profile.allowed_input_video_codecs)),
        allowed_audio_codecs=tuple(sorted(profile.allowed_input_audio_codecs)),
        min_duration_seconds=profile.min_duration_seconds,
        max_duration_seconds=profile.max_duration_seconds,
        min_fps=profile.min_fps,
        max_fps=profile.max_fps,
        min_width=profile.min_width,
        max_width=profile.max_width,
        min_height=profile.min_height,
        max_height=profile.max_height,
        min_pixels=profile.min_pixels,
        max_pixels=profile.max_pixels,
        min_aspect_ratio=profile.min_aspect_ratio,
        max_aspect_ratio=profile.max_aspect_ratio,
        allowed_heights=tuple(profile.allowed_reference_heights),
    )


def prepare_upload_for_profile(
    active_video: Path,
    output_dir: Path,
    profile: BackendProfile,
) -> Tuple[Path, Path]:
    """Apply the schema-v3 local upload-size Gate after privacy processing.

    This is deliberately called before any executor probe.  The helper never
    invokes network code; the result is a final local path plus an immutable
    provenance manifest that the plan re-validates before paid execution.
    """

    try:
        result = prepare_upload_video(
            active_video,
            output_dir,
            profile.profile_id,
            upload_constraints_for_profile(profile),
            _trusted_ffmpeg_executable(),
        )
    except UploadPreparationError as exc:
        suffix = f"；manifest：{exc.manifest_path}" if exc.manifest_path else ""
        raise LoopError(f"上传准备失败，已在提交前阻塞：{exc}{suffix}") from exc
    output_path = Path(str(result.get("output_path", ""))).expanduser().resolve()
    manifest_path = Path(str(result.get("manifest_path", ""))).expanduser().resolve()
    if not output_path.is_file() or not manifest_path.is_file():
        raise LoopError("上传准备未生成可验证的文件与 manifest")
    return output_path, manifest_path


def _load_upload_preparation_manifest(path: Path) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError("upload-preparation manifest 不是有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise LoopError("upload-preparation manifest schema_version 无效")
    required = {
        "schema_version",
        "created_at",
        "backend_profile",
        "constraints_digest",
        "action",
        "source",
        "output",
        "limit_bytes",
        "target_bytes",
        "full_decode_passed",
        "ffmpeg",
    }
    if not required.issubset(payload):
        raise LoopError("upload-preparation manifest 缺少必需字段")
    return payload


def validate_upload_preparation_manifest(
    manifest_path: Path,
    *,
    profile: BackendProfile,
    preliminary_active_video: Path,
    final_video: Path,
) -> Dict[str, object]:
    """Re-check final bytes, media metadata and provenance before submission."""

    payload = _load_upload_preparation_manifest(manifest_path)
    constraints = upload_constraints_for_profile(profile)
    if (
        payload.get("backend_profile") != profile.profile_id
        or payload.get("constraints_digest") != upload_constraints_digest(constraints)
        or payload.get("limit_bytes") != profile.limit_bytes
        or payload.get("target_bytes") != profile.target_bytes
    ):
        raise LoopError("upload-preparation manifest 的 profile 或大小限制不匹配")
    action = payload.get("action")
    if action not in {"unchanged", "remuxed", "reencoded"}:
        raise LoopError("upload-preparation manifest 的 action 无效")
    source = payload.get("source")
    output = payload.get("output")
    ffmpeg = payload.get("ffmpeg")
    if not isinstance(source, dict) or not isinstance(output, dict) or not isinstance(ffmpeg, dict):
        raise LoopError("upload-preparation manifest 缺少 source/output/ffmpeg 记录")
    preliminary = preliminary_active_video.expanduser().resolve()
    final = final_video.expanduser().resolve()
    if not preliminary.is_file() or not final.is_file():
        raise LoopError("上传准备引用的视频不存在")
    source_sha = sha256_file(preliminary)
    if (
        Path(str(source.get("path", ""))).expanduser().resolve() != preliminary
        or str(source.get("sha256", "")).lower() != source_sha
        or source.get("size_bytes") != preliminary.stat().st_size
    ):
        raise LoopError("upload-preparation source 与活动视频不一致")
    final_sha = sha256_file(final)
    if (
        Path(str(output.get("path", ""))).expanduser().resolve() != final
        or str(output.get("sha256", "")).lower() != final_sha
        or output.get("size_bytes") != final.stat().st_size
    ):
        raise LoopError("upload-preparation output 与提交视频不一致")
    if action == "unchanged":
        if final != preliminary:
            raise LoopError("unchanged 上传准备必须直接使用打码后/原活动视频")
    else:
        expected_final = manifest_path.parent / UPLOAD_READY_FILENAME
        if final != expected_final or final == preliminary:
            raise LoopError("压缩或 remux 后必须使用独立的 source-upload-ready.mp4")
    if final.stat().st_size > profile.limit_bytes:
        raise LoopError("最终上传视频超过 backend_profile 的硬字节限制")
    if payload.get("full_decode_passed") is not True:
        raise LoopError("upload-preparation 未通过完整解码校验")
    recorded_metadata = output.get("metadata")
    if not isinstance(recorded_metadata, dict):
        raise LoopError("upload-preparation output 缺少技术元数据")
    # Metadata inside the manifest is evidence, never authority. Re-probe and
    # fully decode the actual bytes that are about to enter submission.
    try:
        current_metadata = probe_video_metadata(
            final, _trusted_ffmpeg_executable()
        )
        validate_video_metadata(current_metadata, constraints)
        validate_full_decode(final, _trusted_ffmpeg_executable())
    except UploadPreparationError as exc:
        raise LoopError(f"最终上传视频技术校验失败：{exc}") from exc
    return payload


def validate_source_preparation_manifest(
    manifest_path: Path,
    source_path: Path,
    source_sha256: str,
    expected_output: Path,
    expected_output_sha256: str,
) -> Dict[str, object]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError("source-preparation manifest 不是有效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise LoopError("source-preparation manifest schema_version 无效")
    if payload.get("operation") != "video-stream-copy-audio-transcode-aac":
        raise LoopError("source-preparation operation 不受支持")
    source = payload.get("source")
    output = payload.get("output")
    if not isinstance(source, dict) or not isinstance(output, dict):
        raise LoopError("source-preparation manifest 缺少 source/output")
    if (
        Path(str(source.get("path", ""))).expanduser().resolve() != source_path
        or str(source.get("sha256", "")).lower() != source_sha256
        or sha256_file(source_path) != source_sha256
    ):
        raise LoopError("source-preparation 没有绑定该 V 编号源视频")
    if (
        Path(str(output.get("path", ""))).expanduser().resolve() != expected_output
        or str(output.get("sha256", "")).lower() != expected_output_sha256
        or sha256_file(expected_output) != expected_output_sha256
    ):
        raise LoopError("source-preparation 输出与活动链路不一致")
    recorded_stream_sha = str(payload.get("h264_video_stream_sha256", "")).lower()
    if not recorded_stream_sha:
        raise LoopError("source-preparation 缺少 H.264 视频流哈希")
    if (
        h264_stream_sha256(source_path) != recorded_stream_sha
        or h264_stream_sha256(expected_output) != recorded_stream_sha
    ):
        raise LoopError("source-preparation 改变了 H.264 视频流")
    return payload


def validate_submission_preflight(
    plan: Mapping[str, object],
    executor: ExecutorSpec,
) -> Dict[str, object]:
    preflight_path = Path(str(plan["preflight_manifest"]))
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError("preflight manifest 不是有效 JSON") from exc
    if not isinstance(preflight, dict) or preflight.get("preflight_passed") is not True:
        raise LoopError("preflight manifest 未通过 preflight_passed Gate")
    if preflight.get("transport") != executor.transport:
        raise LoopError(
            f"preflight transport 不匹配：预期 {executor.transport}"
        )
    if plan.get("schema_version") == 2:
        if (
            preflight.get("backend_profile") != executor.profile_id
            or preflight.get("backend_profile") != plan.get("backend_profile")
            or preflight.get("constraints_digest")
            != plan.get("backend_profile_constraints_sha256")
            or preflight.get("model_version") != executor.model_version
        ):
            raise LoopError("preflight backend profile、约束或模型版本不匹配")
        upload_path = Path(
            str(plan.get("upload_preparation_manifest", ""))
        ).expanduser().resolve()
        upload_sha = sha256_file(upload_path) if upload_path.is_file() else ""
        if (
            preflight.get("upload_preparation_manifest") != str(upload_path)
            or preflight.get("upload_preparation_manifest_sha256") != upload_sha
        ):
            raise LoopError("preflight 未绑定当前 upload-preparation manifest")
    video = Path(str(plan["video"])).resolve()
    active_video = preflight.get("active_video")
    if not isinstance(active_video, dict):
        raise LoopError("preflight manifest 缺少 active_video")
    if Path(str(active_video.get("path", ""))).expanduser().resolve() != video:
        raise LoopError("preflight active_video.path 与 submission plan 不一致")
    _, video_sha = _hash_without_change(video)
    if active_video.get("sha256") != video_sha:
        raise LoopError("submission plan 活动视频在 preflight 后发生变化")
    prompt = Path(str(plan["prompt_file"]))
    images = [Path(str(value)) for value in plan["images"]]  # type: ignore[index]
    expected = _expected_input_bindings(prompt, images)
    if preflight.get("input_bindings") != expected:
        raise LoopError("提示词或有序参考素材与 preflight input_bindings 不一致")
    privacy = preflight.get("privacy")
    if not isinstance(privacy, dict):
        raise LoopError("preflight manifest 缺少 workflow 输入记录")
    if privacy.get("status") not in {
        "workflow-selected-input",
        "source-video-default-unmasked",
    }:
        raise LoopError("preflight workflow 输入记录无效")
    if (
        privacy.get("remote_upload_authorized") is not True
        or privacy.get("paid_task_authorized") is not True
    ):
        raise LoopError("preflight workflow 输入未授权上传与付费任务")
    return preflight


def validate_job_video_lineage(
    batch: Path,
    job_id: str,
    plan: Mapping[str, object],
    preflight: Mapping[str, object],
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> None:
    index = verify_batch_index(batch, max_batch_videos=max_batch_videos)
    indexed_job = next(
        (
            job
            for job in index["jobs"]  # type: ignore[index]
            if isinstance(job, dict) and job.get("id") == job_id
        ),
        None,
    )
    if not isinstance(indexed_job, dict):
        raise LoopError(f"{job_id} 不在 batch-index.json 中")
    source_path = _indexed_relative_path(
        batch, indexed_job.get("relative_path"), batch / "videos", job_id
    )
    source_sha = str(indexed_job.get("sha256", "")).lower()
    active_video = preflight.get("active_video")
    privacy = preflight.get("privacy")
    if not isinstance(active_video, dict) or not isinstance(privacy, dict):
        raise LoopError(f"{job_id} preflight 缺少活动视频或隐私 lineage")
    active_path = Path(str(active_video.get("path", ""))).expanduser().resolve()
    active_sha = str(active_video.get("sha256", "")).lower()
    if privacy.get("status") not in {
        "workflow-selected-input",
        "source-video-default-unmasked",
    }:
        raise LoopError(f"{job_id} preflight workflow 输入记录无效")
    output_dir = Path(str(plan["output_dir"])).resolve()
    mode = job_privacy_mode(batch, job_id)
    if plan.get("schema_version") == 2:
        manifest_path = _path_within(
            Path(str(plan.get("upload_preparation_manifest", ""))),
            output_dir,
            "upload_preparation_manifest",
        )
        profile, profile_managed = backend_profile_for_batch(batch)
        if not profile_managed:
            raise LoopError(f"{job_id} schema v2 提交计划只能用于 schema v3 批次")
        preliminary_active = (
            output_dir / "source-face-mosaic.mp4"
            if mode == "mosaic_required"
            else output_dir / active_video_copy_filename(source_path)
        )
        if not preliminary_active.is_file():
            raise LoopError(f"{job_id} 缺少上传准备前的 workflow 活动视频")
        if mode == "mosaic_required":
            # The same fixed artifact is produced by prepare_mosaic_video;
            # the manifest then proves whether a later size transformation
            # changed what gets uploaded.
            expected_mosaic = _path_within(
                output_dir / "source-face-mosaic.mp4", output_dir, "mosaic_video"
            )
            if preliminary_active != expected_mosaic:
                raise LoopError(f"{job_id} 打码活动视频路径无效")
        else:
            if sha256_file(preliminary_active) != source_sha:
                raise LoopError(f"{job_id} 的活动视频副本与源视频不一致")
        validate_upload_preparation_manifest(
            manifest_path,
            profile=profile,
            preliminary_active_video=preliminary_active,
            final_video=active_path,
        )
        return
    if mode == "mosaic_required":
        expected = _path_within(
            output_dir / "source-face-mosaic.mp4", output_dir, "mosaic_video"
        )
        if active_path != expected or not active_path.is_file():
            raise LoopError(f"{job_id} 必须上传 workflow 生成的打码视频")
        return
    if active_sha != source_sha:
        preparation_path = _path_within(
            output_dir / "source-preparation.json",
            output_dir,
            "source_preparation_manifest",
        )
        if not preparation_path.is_file():
            raise LoopError(
                f"{job_id} 活动视频与源视频不同，且缺少可信技术准备 manifest"
            )
        validate_source_preparation_manifest(
            preparation_path,
            source_path,
            source_sha,
            active_path,
            active_sha,
        )


def submission_fingerprint(
    preflight: Mapping[str, object],
    executor: ExecutorSpec,
    retry_identity: Optional[Mapping[str, object]] = None,
    preflight_manifest_sha256: Optional[str] = None,
) -> str:
    active_video = preflight.get("active_video")
    if not isinstance(active_video, dict):
        raise LoopError("preflight manifest 缺少 active_video")
    privacy = preflight.get("privacy")
    privacy_identity: Dict[str, object] = {}
    if isinstance(privacy, dict):
        privacy_identity = {
            key: privacy.get(key)
            for key in (
                "status",
                "active_video_sha256",
                "remote_upload_authorized",
                "paid_task_authorized",
            )
            if privacy.get(key) is not None
        }
    identity: Dict[str, object] = {
        "schema_version": 2,
        "executor": executor.path.name.casefold(),
        "transport": executor.transport,
        "backend_profile": preflight.get("backend_profile", executor.profile_id),
        "backend_profile_constraints_sha256": preflight.get("constraints_digest"),
        "model_version": preflight.get("model_version", executor.model_version),
        "active_video_sha256": active_video.get("sha256"),
        "input_bindings": preflight.get("input_bindings"),
        "privacy": privacy_identity,
    }
    if preflight_manifest_sha256 is not None:
        value = str(preflight_manifest_sha256).lower()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise LoopError("preflight manifest SHA-256 无效")
        identity["preflight_manifest_sha256"] = value
    upload_manifest_path = preflight.get("upload_preparation_manifest")
    if upload_manifest_path:
        path = Path(str(upload_manifest_path)).expanduser().resolve()
        if not path.is_file():
            raise LoopError("preflight 绑定的 upload-preparation manifest 不存在")
        recorded_manifest_sha = str(
            preflight.get("upload_preparation_manifest_sha256", "")
        ).lower()
        actual_manifest_sha = sha256_file(path)
        if recorded_manifest_sha != actual_manifest_sha:
            raise LoopError("preflight 绑定的 upload-preparation manifest 已变化")
        identity["upload_preparation_manifest_sha256"] = actual_manifest_sha
    if retry_identity is not None:
        identity["retry"] = dict(retry_identity)
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _secure_write_json(path: Path, value: object) -> None:
    atomic_write_json(path, value)
    chmod_private(path)


def local_date_now() -> str:
    return datetime.now().astimezone().date().isoformat()


def submission_record_local_date(record: Mapping[str, object]) -> str:
    explicit = str(record.get("local_date", "")).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit):
        return explicit
    created_at = str(record.get("created_at", "")).strip()
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date().isoformat()


def reserve_parent_submission(
    state_dir: Path,
    fingerprint: str,
    batch: Path,
    job_id: str,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
) -> Tuple[Path, Optional[Dict[str, object]]]:
    ledger_dir = state_dir / "video-batch-loop-submissions"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        ledger_dir.chmod(0o700)
    with exclusive_loop_lock(ledger_dir):
        return _reserve_parent_submission_unlocked(
            ledger_dir,
            fingerprint,
            batch,
            job_id,
            daily_paid_limit=daily_paid_limit,
        )


def _reserve_parent_submission_unlocked(
    ledger_dir: Path,
    fingerprint: str,
    batch: Path,
    job_id: str,
    daily_paid_limit: int,
) -> Tuple[Path, Optional[Dict[str, object]]]:
    path = ledger_dir / f"{fingerprint}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LoopError("父层付费账本已存在但不可读；为避免重复提交已停止") from exc
        if not isinstance(existing, dict):
            raise LoopError("父层付费账本格式无效；为避免重复提交已停止")
        if existing.get("state") == "completed":
            manifest_path = Path(str(existing.get("manifest_path", "")))
            final_output = Path(str(existing.get("final_output", "")))
            if manifest_path.is_file() and final_output.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    isinstance(manifest, dict)
                    and manifest.get("task_id") == existing.get("task_id")
                ):
                    return path, manifest
        if (
            existing.get("state") in {"task-created", "task-created-blocked"}
            and existing.get("batch_id") == batch.name
            and existing.get("job_id") == job_id
            and str(existing.get("task_id") or "").strip()
        ):
            return path, None
        raise LoopError(
            "父层付费账本已记录相同语义请求；为避免跨批次重复计费已停止。"
            f"状态：{existing.get('state', 'unknown')}"
        )

    if daily_paid_limit < 1:
        raise LoopError("父层每日付费提交上限必须大于 0")
    today = local_date_now()
    used = 0
    for record_path in ledger_dir.glob("*.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            used += 1
            continue
        if isinstance(record, dict) and submission_record_local_date(record) == today:
            used += 1
    if used >= daily_paid_limit:
        raise LoopError(
            f"父层每日付费提交上限为 {daily_paid_limit}，今天已保留 {used} 条；"
            "已在上传和提交前停止。"
        )
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "state": "reserved",
        "batch_id": batch.name,
        "job_id": job_id,
        "created_at": utc_now(),
        "local_date": local_date_now(),
        "updated_at": utc_now(),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise LoopError("父层付费账本发生并发占位；为避免重复提交已停止") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path, None


def update_parent_submission(path: Path, **fields: object) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {"schema_version": 1}
    if not isinstance(value, dict):
        value = {"schema_version": 1}
    value.update(fields)
    value["updated_at"] = utc_now()
    _secure_write_json(path, value)


def job_requirement_lines(batch: Path, job_id: str) -> List[str]:
    """Return the requirement text that applies to one job, in file order.

    Reuses the same default/group/per-line reading as the coverage check so
    parent-owned node inputs use the exact requirement text the gate accepted.
    """

    requirements_path = batch / "requirements.txt"
    if not requirements_path.is_file():
        return []
    lines: List[str] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        default_match = DEFAULT_RULE_RE.match(line)
        if default_match:
            if meaningful_body(default_match.group(1)):
                lines.append(line)
            continue
        ids = expand_video_ids(line)
        if job_id not in ids:
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) == 2 and meaningful_body(parts[1]):
            lines.append(line)
    return lines


def _strict_errors_enabled() -> bool:
    """Debug switch: let unexpected non-LoopError exceptions escape.

    Off by default. `prepare-job` returning exit code 0 is what keeps one bad
    job from aborting the whole batch in the JavaScript control plane, so this
    must never be enabled in unattended runs.
    """

    return os.getenv("VIDEO_LOOP_STRICT_ERRORS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def job_output_dir(project_root: Path, batch: Path, job_id: str) -> Path:
    """Single source of truth for a job's fixed output directory."""

    output_root = project_root / "outputs" / "video-replacements"
    candidate = output_root / f"{batch.name}-{job_id}"
    if path_contains_link_like(candidate):
        raise LoopError("Job output directory must not use a link or Windows junction")
    resolved_root = output_root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise LoopError("Job output directory escapes the repository output root") from exc
    return resolved_candidate

def resolve_reference_binding(batch: Path, project_root: Path, job_id: str) -> List[Path]:
    """Turn the agent's ordered R-number binding into absolute replacement paths."""

    output_dir = job_output_dir(project_root, batch, job_id)
    binding_path = output_dir / REFERENCE_BINDING_FILENAME
    if not binding_path.is_file():
        raise LoopError(f"{job_id} 缺少 {REFERENCE_BINDING_FILENAME}")
    try:
        value = json.loads(binding_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LoopError(f"{job_id} 的 {REFERENCE_BINDING_FILENAME} 无法解析：{exc}") from exc
    if isinstance(value, dict):
        value = value.get("references")
    if not isinstance(value, list):
        raise LoopError(f"{job_id} 的 {REFERENCE_BINDING_FILENAME} 必须是有序 R 编号列表")
    if len(value) > MAX_REFERENCE_IMAGES:
        raise LoopError(
            f"{job_id} 参考图绑定为 {len(value)} 张，超过 {MAX_REFERENCE_IMAGES} 张上限"
        )
    index = verify_reference_index(batch)
    by_id = {
        str(item.get("id")): item
        for item in index["references"]  # type: ignore[index]
        if isinstance(item, dict)
    }
    replacement_root = (batch / "replacements").resolve()
    resolved: List[Path] = []
    seen: Set[str] = set()
    for raw in value:
        reference_id = str(raw).strip()
        entry = by_id.get(reference_id)
        if entry is None:
            raise LoopError(
                f"{job_id} 参考图绑定含未知编号 {reference_id!r}；可用编号："
                f"{', '.join(sorted(by_id)) or '无'}"
            )
        relative = str(entry.get("relative_path", ""))
        image = _path_within(
            (batch / relative).resolve(), replacement_root, "image"
        )
        if not image.is_file():
            raise LoopError(f"{job_id} 绑定的参考图不存在：{image}")
        if str(image) in seen:
            raise LoopError(f"{job_id} 参考图绑定重复：{reference_id}")
        seen.add(str(image))
        resolved.append(image)
    return resolved


def cleanup_orphaned_node_runs(loop_root: Path, project_root: Path) -> Dict[str, object]:
    """Remove node workspaces left behind before the current lock owner started."""

    state_dir = executor_state_dir(loop_root)
    require_external_state_dir(state_dir, loop_root, project_root)
    node_runs_root = state_dir / "node-runs"
    node_runs_root.mkdir(parents=True, exist_ok=True)
    removed: List[str] = []
    for candidate in sorted(node_runs_root.iterdir(), key=lambda path: path.name):
        if (
            not candidate.name.startswith("video-loop-")
            or is_link_like(candidate)
            or not candidate.is_dir()
        ):
            continue
        shutil.rmtree(candidate)
        removed.append(candidate.name)
    return {"removed_count": len(removed), "removed": removed}


def prepare_active_video(batch: Path, project_root: Path, job_id: str) -> Path:
    """Copy the original bytes into the Job output directory for upload."""

    output_dir = job_output_dir(project_root, batch, job_id)
    index = verify_batch_index(batch)
    indexed_job = next(
        (
            job
            for job in index["jobs"]  # type: ignore[index]
            if isinstance(job, dict) and job.get("id") == job_id
        ),
        None,
    )
    if not isinstance(indexed_job, dict):
        raise LoopError(f"{job_id} 不在 batch-index.json 中")
    source = _indexed_relative_path(
        batch, indexed_job.get("relative_path"), batch / "videos", job_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    copied = output_dir / active_video_copy_filename(source)
    if copied.is_file():
        return copied
    shutil.copyfile(source, copied)
    return copied


def prepare_mosaic_video(batch: Path, project_root: Path, job_id: str) -> Path:
    """Run the workflow-owned mosaic and return its fixed output file."""

    indexed_job = _job_record(batch, job_id)
    source = _indexed_relative_path(
        batch, indexed_job.get("relative_path"), batch / "videos", job_id
    )
    output_dir = job_output_dir(project_root, batch, job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "source-face-mosaic.mp4"
    environment = local_tool_environment(
        extra_names=(
            "FFMPEG",
            "VIDEO_REPLACER_CACHE_DIR",
            "VIDEO_REPLACER_PYTHON",
        )
    )
    environment["VIDEO_REPLACER_PYTHON"] = sys.executable
    environment["FFMPEG"] = str(_trusted_ffmpeg_executable())
    completed = subprocess.run(
        [
            sys.executable,
            str(FACE_MOSAIC_SCRIPT),
            "--video",
            str(source),
            "--output",
            str(output),
        ],
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=environment,
    )
    atomic_write_text(output_dir / "parent-face-mosaic.log", completed.stdout or "")
    if completed.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise LoopError(f"{job_id} 自动打码失败")
    return output


def run_executor_probe(
    batch: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: ExecutorSpec,
    active_video: Path,
    prompt_file: Path,
    images: Sequence[Path],
    profile_managed: bool = False,
    upload_preparation_manifest: Optional[Path] = None,
) -> Path:
    """Run the audited executor's no-cost probe and return its manifest path.

    stdout is captured rather than inherited: this runs inside `prepare-job`,
    whose own stdout must stay parseable JSON for the JavaScript control plane.
    """

    output_dir = job_output_dir(project_root, batch, job_id)
    command = [
        sys.executable,
        str(executor.path),
        "probe",
        "--video",
        str(active_video),
        "--prompt-file",
        str(prompt_file),
    ]
    for image in images:
        command.extend(["--image", str(image)])
    if profile_managed:
        if upload_preparation_manifest is None or not upload_preparation_manifest.is_file():
            raise LoopError(f"{executor.profile_id} 缺少上传准备 manifest")
        command.extend(["--backend-profile", executor.profile_id])
        if not executor.model_version:
            raise LoopError(f"{executor.profile_id} 缺少已解析模型版本")
        command.extend(["--model-version", executor.model_version])
        command.extend(
            ["--upload-preparation-manifest", str(upload_preparation_manifest)]
        )
    command.extend(["--privacy-status", "workflow-selected-input"])
    command.extend(
        [
            "--name",
            f"{batch.name}-{job_id}",
            "--output-dir",
            str(output_dir),
        ]
    )
    try:
        profile = get_backend_profile(executor.profile_id)
    except BackendProfileError:
        # A legacy injected test executor has no registry entry. Its old probe
        # contract remains intentionally untouched.
        profile = get_backend_profile("dreamina_cli_seedance_2_5")
    environment = probe_environment_for_profile(profile)
    completed = subprocess.run(
        command,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=environment,
    )
    atomic_write_text(output_dir / PARENT_PROBE_LOG_FILENAME, completed.stdout or "")
    if completed.returncode != 0:
        raise LoopError(
            f"{job_id} preflight probe 失败，退出码 {completed.returncode}；"
            f"查看 {output_dir / PARENT_PROBE_LOG_FILENAME}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LoopError(f"{job_id} probe 没有返回可解析的 JSON：{exc}") from exc
    manifest_path = payload.get("manifest_path") if isinstance(payload, dict) else None
    if not manifest_path:
        raise LoopError(f"{job_id} probe 输出缺少 manifest_path")
    return _path_within(Path(str(manifest_path)), output_dir, "preflight_manifest")


def build_submission_plan(
    batch: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: ExecutorSpec,
    active_video: Path,
    images: Sequence[Path],
    manifest_path: Path,
    profile: Optional[BackendProfile] = None,
    profile_managed: bool = False,
    upload_preparation_manifest: Optional[Path] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, Any]:
    """Write the submission plan deterministically, then re-load it for validation.

    The plan is always re-read through load_submission_plan so the deterministic
    prompt-format gate stays on the only path into paid submission.
    """

    output_dir = job_output_dir(project_root, batch, job_id)
    plan: Dict[str, object] = {
        "schema_version": 2 if profile_managed else 1,
        "batch_id": batch.name,
        "job_id": job_id,
        "name": f"{batch.name}-{job_id}",
        "output_dir": str(output_dir),
        "prompt_file": str(output_dir / PROMPT_FILENAME),
        "video": str(active_video),
        "preflight_manifest": str(manifest_path),
        "images": [str(image) for image in images],
    }
    if profile_managed:
        if profile is None or upload_preparation_manifest is None:
            raise LoopError("schema v3 批次缺少上传准备 profile 或 manifest")
        plan.update(
            {
                "backend_profile": profile.profile_id,
                "backend_profile_constraints_sha256": profile.constraints_digest,
                "preflight_manifest_sha256": sha256_file(manifest_path),
                "upload_preparation_manifest": str(upload_preparation_manifest),
            }
        )
    atomic_write_json(output_dir / "submission-plan.json", plan)
    return load_submission_plan(
        batch,
        project_root,
        job_id,
        executor=executor,
        max_batch_videos=max_batch_videos,
    )

def load_submission_plan(
    batch: Path,
    project_root: Path,
    job_id: str,
    executor: Optional[ExecutorSpec] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, Any]:
    reference_index = verify_reference_index(batch)
    index = verify_batch_index(batch, max_batch_videos=max_batch_videos)
    executor, profile, profile_managed = executor_for_batch(
        batch,
        project_root,
        executor=executor,
        index=index,
        reference_index=reference_index,
    )
    expected_output = job_output_dir(project_root, batch, job_id)
    plan_path = expected_output / "submission-plan.json"
    if not plan_path.is_file():
        raise LoopError(f"{job_id} 缺少 submission-plan.json")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LoopError(f"{job_id} submission-plan.json 无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"{job_id} submission-plan.json 不是 JSON 对象")
    required_v1 = {
        "schema_version",
        "batch_id",
        "job_id",
        "video",
        "preflight_manifest",
        "prompt_file",
        "images",
        "name",
        "output_dir",
    }
    required_v2 = required_v1 | {
        "backend_profile",
        "backend_profile_constraints_sha256",
        "upload_preparation_manifest",
        "preflight_manifest_sha256",
    }
    schema_version = value.get("schema_version")
    required = required_v2 if schema_version == 2 else required_v1
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise LoopError(f"{job_id} 提交计划字段不匹配：missing={missing}, extra={extra}")
    if schema_version not in {1, 2}:
        raise LoopError(f"{job_id} 提交计划 schema_version 无效")
    if profile_managed != (schema_version == 2):
        raise LoopError(f"{job_id} 提交计划与批次 schema/profile 不匹配")
    if profile_managed:
        if (
            value.get("backend_profile") != profile.profile_id
            or value.get("backend_profile_constraints_sha256")
            != profile.constraints_digest
        ):
            raise LoopError(f"{job_id} 提交计划 backend_profile 或约束哈希不匹配")
    if value["batch_id"] != batch.name or value["job_id"] != job_id:
        raise LoopError(f"{job_id} 提交计划批次或 job 编号不匹配")
    expected_name = f"{batch.name}-{job_id}"
    if value["name"] != expected_name:
        raise LoopError(f"{job_id} 提交名称必须为 {expected_name}")
    output_dir = _path_within(Path(str(value["output_dir"])), expected_output, "output_dir")
    if output_dir != expected_output:
        raise LoopError(f"{job_id} output_dir 必须等于固定输出目录")
    for key in ("video", "preflight_manifest", "prompt_file"):
        path = _path_within(Path(str(value[key])), expected_output, key)
        if not path.is_file():
            raise LoopError(f"{job_id} 的 {key} 不存在：{path}")
        value[key] = str(path)
    if profile_managed:
        preflight_sha = str(value.get("preflight_manifest_sha256", "")).lower()
        if (
            re.fullmatch(r"[0-9a-f]{64}", preflight_sha) is None
            or sha256_file(Path(str(value["preflight_manifest"]))) != preflight_sha
        ):
            raise LoopError(f"{job_id} preflight manifest 在 submission plan 后发生变化")
    if profile_managed:
        upload_manifest = _path_within(
            Path(str(value["upload_preparation_manifest"])),
            expected_output,
            "upload_preparation_manifest",
        )
        if upload_manifest != expected_output / UPLOAD_PREPARATION_FILENAME:
            raise LoopError(f"{job_id} 上传准备 manifest 必须使用固定路径")
        if not upload_manifest.is_file():
            raise LoopError(f"{job_id} 缺少上传准备 manifest")
        value["upload_preparation_manifest"] = str(upload_manifest)
    images = value["images"]
    if not isinstance(images, list) or not 0 <= len(images) <= 9:
        raise LoopError(f"{job_id} images 必须包含 0–9 张参考图")
    declared_references = select_job_reference_records(batch, job_id)
    expected_reference_names = [
        str(item.get("semantic_name") or "").strip()
        for item in declared_references
    ]
    validate_execution_prompt(
        Path(str(value["prompt_file"])),
        reference_count=len(images),
        expected_reference_names=expected_reference_names,
        expected_requirement_lines=_canonical_requirement_lines(
            batch, job_id, declared_references
        ),
    )
    replacement_root = (batch / "replacements").resolve()
    indexed_paths = {
        str(item["relative_path"]): item
        for item in reference_index["references"]  # type: ignore[index]
        if isinstance(item, dict)
    }
    normalized_images: List[str] = []
    normalized_relative_images: List[str] = []
    seen_images: Set[str] = set()
    for raw_image in images:
        relative = _plan_reference_relative_path(raw_image, batch)
        indexed_key = (Path("replacements") / relative).as_posix()
        if indexed_key not in indexed_paths:
            raise LoopError(f"{job_id} image 未出现在可信 reference-index：{relative}")
        image = _path_within(replacement_root / relative, replacement_root, "image")
        if not image.is_file():
            raise LoopError(f"{job_id} 参考图不存在：{image}")
        if str(image) in seen_images:
            raise LoopError(f"{job_id} images 含重复参考图：{relative}")
        seen_images.add(str(image))
        normalized_images.append(str(image))
        normalized_relative_images.append(indexed_key)
    expected_relative_images = [
        str(item.get("relative_path") or "") for item in declared_references
    ]
    if normalized_relative_images != expected_relative_images:
        raise LoopError(f"{job_id} images 必须逐项等于父层登记的有序参考素材")
    value["images"] = normalized_images
    value["output_dir"] = str(expected_output)
    preflight = validate_submission_preflight(value, executor)
    validate_job_video_lineage(
        batch,
        job_id,
        value,
        preflight,
        max_batch_videos=max_batch_videos,
    )
    return value


def execute_submission_plan(
    batch: Path,
    project_root: Path,
    job_id: str,
    *,
    loop_root: Optional[Path] = None,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
    submit_only: bool = False,
    retry_attempt: Optional[int] = None,
    retry_authorization_manifest: Optional[Path] = None,
    task_timeout: Optional[int] = None,
    paid_execution_authorized: bool = False,
    payment_authorization_identity: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    if not paid_execution_authorized:
        raise LoopError(
            f"{job_id} 缺少当前父进程明确付费授权；拒绝执行提交计划"
        )
    loop_root = loop_root or batch.parent.parent
    executor, profile, profile_managed = executor_for_batch(
        batch, project_root, executor=executor
    )
    require_free_space(project_root, minimum_free_bytes)
    verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
    plan = load_submission_plan(
        batch,
        project_root,
        job_id,
        executor=executor,
        max_batch_videos=max_batch_videos,
    )
    preflight = validate_submission_preflight(plan, executor)
    base_output_dir = Path(str(plan["output_dir"]))
    name = str(plan["name"])
    state_dir = executor_state_dir(loop_root)
    require_external_state_dir(state_dir, loop_root, project_root)
    retry_identity: Optional[Dict[str, object]] = None
    if retry_attempt is not None:
        if not executor.supports_authorized_retry:
            raise LoopError("串行重试只支持审核过的 dreamina_video.py 执行器")
        if retry_authorization_manifest is None:
            raise LoopError("付费重试缺少外置授权 manifest")
        retry_identity = validate_retry_authorization(
            retry_authorization_manifest,
            state_dir,
            batch,
            job_id,
            retry_attempt,
        )
    elif retry_authorization_manifest is not None:
        raise LoopError("重试授权 manifest 只能与 retry_attempt 同时使用")
    output_dir = base_output_dir
    if retry_identity is not None:
        model_slug = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            str(retry_identity["model_version"]),
        ).strip("-")
        output_dir = base_output_dir.with_name(
            f"{base_output_dir.name}-{model_slug}-attempt-{retry_attempt}"
        )
    manifest_path = output_dir / f"{name}-manifest.json"
    final_path = output_dir / f"{name}-final.mp4"
    fingerprint = submission_fingerprint(
        preflight,
        executor,
        retry_identity,
        preflight_manifest_sha256=(
            str(plan.get("preflight_manifest_sha256"))
            if plan.get("schema_version") == 2
            else None
        ),
    )
    ledger_path, reused_manifest = reserve_parent_submission(
        state_dir,
        fingerprint,
        batch,
        job_id,
        daily_paid_limit=daily_paid_limit,
    )
    if reused_manifest is not None:
        return reused_manifest
    if manifest_path.exists() or final_path.exists():
        update_parent_submission(
            ledger_path,
            state="stale-output-blocked",
            blocker=(
                "job output already existed without a matching completed "
                "parent semantic ledger record"
            ),
        )
        raise LoopError(
            f"{job_id} 存在未经父层语义账本绑定的旧 manifest/final；"
            "为避免错误复用或重复付费已停止。"
        )

    command = [
        sys.executable,
        str(executor.path),
        "generate",
        "--video",
        str(plan["video"]),
        "--preflight-manifest",
        str(plan["preflight_manifest"]),
        "--prompt-file",
        str(plan["prompt_file"]),
    ]
    for image in plan["images"]:
        command.extend(["--image", str(image)])
    if profile_managed:
        if not executor.model_version:
            raise LoopError(f"{executor.profile_id} 缺少已解析模型版本")
        command.extend(
            [
                "--backend-profile",
                executor.profile_id,
                "--model-version",
                executor.model_version,
            ]
        )
    command.extend(
        [
            "--name",
            name,
            "--output-dir",
            str(output_dir),
            "--state-dir",
            str(state_dir),
        ]
    )
    if paid_execution_authorized:
        command.append("--confirm-paid")
    if submit_only:
        command.append("--submit-only")
    if retry_attempt is not None:
        command.extend(
            (
                ([] if profile_managed else ["--model-version", str(retry_identity["model_version"])])
                + [
                    "--retry-attempt",
                    str(retry_attempt),
                    "--retry-authorization-manifest",
                    str(retry_authorization_manifest),
                ]
            )
        )
    if task_timeout is not None:
        command.extend(["--timeout", str(task_timeout)])
    environment = execution_environment_for_profile(profile, state_dir)
    environment.pop(PAYMENT_AUTH_TOKEN_ENV, None)
    environment["VIDEO_REPLACER_STATE_DIR"] = str(state_dir)
    environment["FFMPEG"] = str(_trusted_ffmpeg_executable())
    require_free_space(project_root, minimum_free_bytes)
    verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
    # Re-load the complete plan immediately before launching the paid-capable
    # child. This checks the actual final bytes, upload manifest SHA, lineage,
    # profile and adapter identity again rather than trusting earlier prepare.
    plan = load_submission_plan(
        batch,
        project_root,
        job_id,
        executor=executor,
        max_batch_videos=max_batch_videos,
    )
    preflight = validate_submission_preflight(plan, executor)
    final_fingerprint = submission_fingerprint(
        preflight,
        executor,
        retry_identity,
        preflight_manifest_sha256=(
            str(plan.get("preflight_manifest_sha256"))
            if plan.get("schema_version") == 2
            else None
        ),
    )
    if final_fingerprint != fingerprint:
        update_parent_submission(
            ledger_path,
            state="input-changed-blocked",
            blocker="submission identity changed after parent ledger reservation",
        )
        raise LoopError("提交输入、上传准备 manifest 或 profile 在付费前发生变化；已阻止")
    if profile_managed:
        if payment_authorization_identity is None:
            update_parent_submission(
                ledger_path,
                state="payment-identity-missing-blocked",
                blocker="schema v3 paid execution lacked final-media authorization identity",
            )
            raise LoopError("schema v3 提交缺少最终视频绑定的明确付费授权；已阻止")
        current_authorization_identity = submission_authorization_identity(
            batch,
            project_root,
            job_id,
            executor=executor,
            max_batch_videos=max_batch_videos,
        )
        if current_authorization_identity != dict(payment_authorization_identity):
            update_parent_submission(
                ledger_path,
                state="payment-identity-changed-blocked",
                blocker="final media/profile no longer matched the verified payment checkpoint",
            )
            raise LoopError(
                "最终视频、上传准备 manifest 或 backend profile 已不匹配明确付费授权；已阻止"
            )
    completed = subprocess.run(
        command,
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=environment,
    )
    log_name = (
        f"parent-submission-retry-attempt-{retry_attempt}.log"
        if retry_attempt is not None
        else "parent-submission.log"
    )
    atomic_write_text(output_dir / log_name, completed.stdout)
    if completed.returncode != 0:
        task_id = recorded_task_id_in_output_dir(output_dir)
        update_parent_submission(
            ledger_path,
            state="task-created-blocked" if task_id else "ambiguous",
            task_id=task_id,
            returncode=completed.returncode,
        )
        tail = "\n".join(completed.stdout.splitlines()[-8:])
        raise LoopError(f"{job_id} 固定执行器退出码 {completed.returncode}：{tail}")
    if submit_only:
        task_id = recorded_task_id_in_output_dir(output_dir)
        if not task_id:
            update_parent_submission(
                ledger_path,
                state="ambiguous",
                blocker="submit-only exited successfully without task ID",
            )
            raise LoopError(f"{job_id} submit-only 成功退出但缺少 task ID")
        queued_path = output_dir / f"{name}-queued.json"
        if not queued_path.is_file():
            update_parent_submission(
                ledger_path,
                state="ambiguous",
                task_id=task_id,
                blocker="submit-only exited successfully without queued record",
            )
            raise LoopError(f"{job_id} submit-only 缺少 queued 记录")
        update_parent_submission(
            ledger_path,
            state="task-created",
            task_id=task_id,
            queued_path=str(queued_path),
        )
        return {
            "task_id": task_id,
            "submit_id": task_id,
            "queued": True,
            "queued_path": str(queued_path),
        }
    if not manifest_path.is_file() or not final_path.is_file():
        update_parent_submission(
            ledger_path,
            state="ambiguous",
            blocker="executor exited successfully without manifest or final output",
        )
        raise LoopError(f"{job_id} 执行器成功退出但缺少 manifest 或 final.mp4")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest.get("task_id"):
        update_parent_submission(ledger_path, state="ambiguous")
        raise LoopError(f"{job_id} 生成 manifest 缺少 task_id")
    update_parent_submission(
        ledger_path,
        state="completed",
        task_id=manifest.get("task_id"),
        manifest_path=str(manifest_path),
        final_output=str(final_path),
    )
    return manifest


def validate_retry_authorization(
    manifest_path: Path,
    state_dir: Path,
    batch: Path,
    job_id: str,
    retry_attempt: int,
) -> Dict[str, object]:
    resolved = manifest_path.expanduser().resolve()
    authorization_root = (state_dir / "retry-authorizations").resolve()
    try:
        resolved.relative_to(authorization_root)
    except ValueError as exc:
        raise LoopError(
            f"重试授权必须位于外置状态目录：{authorization_root}"
        ) from exc
    if not resolved.is_file():
        raise LoopError(f"找不到重试授权 manifest：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError("重试授权 manifest 不是有效 JSON") from exc
    if not isinstance(payload, dict) or not (
        payload.get("schema_version") == 1
        and payload.get("batch_id") == batch.name
        and payload.get("decision") == RETRY_AUTHORIZATION_DECISION
        and payload.get("issued_by") == "user-in-chat"
        and payload.get("paid_retry_authorized") is True
        and payload.get("mode") == "wait-terminal-before-next"
        and payload.get("model_version")
    ):
        raise LoopError("重试授权 manifest 的批次、决定或授权字段无效")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != payload.get("max_new_tasks"):
        raise LoopError("重试授权 manifest 的 jobs 或 max_new_tasks 无效")
    job_ids = [
        str(item.get("job_id", ""))
        for item in jobs
        if isinstance(item, dict)
    ]
    if len(job_ids) != len(jobs) or len(set(job_ids)) != len(job_ids):
        raise LoopError("重试授权 manifest 的 job_id 缺失或重复")
    matches = [
        item
        for item in jobs
        if isinstance(item, dict)
        and item.get("job_id") == job_id
        and item.get("retry_attempt") == retry_attempt
    ]
    if len(matches) != 1:
        raise LoopError(f"授权没有唯一绑定 {job_id} attempt {retry_attempt}")
    original_submit_id = str(matches[0].get("original_submit_id", "")).strip()
    if not original_submit_id:
        raise LoopError(f"{job_id} 授权缺少 original_submit_id")
    return {
        "attempt": retry_attempt,
        "job_id": job_id,
        "original_submit_id": original_submit_id,
        "model_version": str(payload["model_version"]),
        "authorization_manifest_sha256": sha256_file(resolved),
    }


def recorded_task_id_in_output_dir(output_dir: Path) -> Optional[str]:
    task_dir = output_dir / "tasks"
    records = (
        sorted(
            (path for path in task_dir.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if task_dir.is_dir()
        else []
    )
    for record in records:
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        candidates = [value.get("task_id")]
        response = value.get("response")
        if isinstance(response, dict):
            candidates.append(response.get("id"))
        for candidate in candidates:
            task_id = str(candidate or "").strip()
            if task_id:
                return task_id
    logs = sorted(
        output_dir.glob("parent-submission*.log"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for log_path in logs:
        match = re.search(
            r'(?:任务已创建：|"(?:task_id|submit_id)"\s*:\s*")([^"\s]+)',
            log_path.read_text(encoding="utf-8"),
        )
        if match:
            return match.group(1)
    return None


def recorded_task_id(project_root: Path, batch_name: str, job_id: str) -> Optional[str]:
    """Return the newest task ID across the base and authorized retry outputs."""
    output_root = (project_root / "outputs" / "video-replacements").resolve()
    prefix = f"{batch_name}-{job_id}"
    output_dirs = sorted(
        (
            path
            for path in output_root.glob(f"{prefix}*")
            if path.is_dir()
            and (path.name == prefix or path.name.startswith(prefix + "-"))
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    candidates: List[Tuple[int, str]] = []
    for output_dir in output_dirs:
        task_id = recorded_task_id_in_output_dir(output_dir)
        if task_id:
            task_dir = output_dir / "tasks"
            newest_mtime = max(
                (path.stat().st_mtime_ns for path in task_dir.glob("*.json")),
                default=output_dir.stat().st_mtime_ns,
            )
            candidates.append((newest_mtime, task_id))
    if candidates:
        return max(candidates)[1]
    return None


def explicitly_skipped_ids(batch: Path) -> Set[str]:
    index = read_index(batch)
    known_ids = {str(job["id"]) for job in index["jobs"]}  # type: ignore[index]
    requirements = batch / "requirements.txt"
    if not requirements.is_file():
        return set()
    skipped: Set[str] = set()
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        default_match = DEFAULT_RULE_RE.match(line)
        if default_match:
            if "跳过" in default_match.group(1):
                skipped = set(known_ids)
            else:
                skipped.clear()
            continue
        ids = expand_video_ids(line) & known_ids
        if not ids:
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) != 2:
            continue
        if "跳过" in parts[1]:
            skipped.update(ids)
        else:
            skipped.difference_update(ids)
    return skipped


def trusted_reuse_snapshot(
    batch: Path, project_root: Path
) -> Dict[str, Dict[str, object]]:
    """Workspace-authored reuse claims are never parent trust evidence.

    Completed semantic requests are reused only inside
    ``reserve_parent_submission`` from the external parent ledger. Keeping this
    hook fail-closed prevents a child from leaving ``reused-jobs.json`` in one
    run and having it promoted to trusted state in a later run.
    """
    del batch, project_root
    return {}


def validate_node_result(
    result: Mapping[str, object], expected_job_id: str
) -> Dict[str, object]:
    """Validate the minimal isolated-node response contract."""

    required = {"batch_status", "summary", "jobs"}
    if set(result) != required:
        raise LoopError(
            "节点结果字段不匹配："
            f"missing={sorted(required - set(result))}, extra={sorted(set(result) - required)}"
        )
    if result.get("batch_status") not in {
        NODE_COMPLETE_STATUS,
        "BLOCKED",
        "FAILED",
    }:
        raise LoopError(f"节点 batch_status 非法：{result.get('batch_status')!r}")
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1:
        raise LoopError("节点结果必须恰好包含一个 Job")
    job = jobs[0]
    required_job_fields = {"id", "status", "blocker", "prompt"}
    if not isinstance(job, dict) or set(job) != required_job_fields:
        raise LoopError("节点 Job 字段必须只有 id、status、blocker、prompt")
    if job.get("id") != expected_job_id:
        raise LoopError(
            f"节点返回错误 Job：{job.get('id')!r} != {expected_job_id!r}"
        )
    if job.get("status") not in {NODE_COMPLETE_STATUS, "BLOCKED", "FAILED"}:
        raise LoopError(f"{expected_job_id} 节点 status 非法：{job.get('status')!r}")
    if result.get("batch_status") != job.get("status"):
        raise LoopError(
            f"{expected_job_id} 节点 batch_status 必须等于唯一 Job status："
            f"{result.get('batch_status')!r} != {job.get('status')!r}"
        )
    blocker = job.get("blocker")
    if blocker is not None and not isinstance(blocker, str):
        raise LoopError(f"{expected_job_id} 节点 blocker 必须是字符串或 null")
    if job.get("status") == NODE_COMPLETE_STATUS and blocker is not None:
        raise LoopError(f"{expected_job_id} READY 节点不得携带 blocker")
    if job.get("status") != NODE_COMPLETE_STATUS and not blocker:
        raise LoopError(f"{expected_job_id} 阻塞节点必须写明 blocker")
    prompt = job.get("prompt")
    if job.get("status") == NODE_COMPLETE_STATUS:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LoopError(f"{expected_job_id} COMPLETE 节点必须返回非空 prompt")
    elif prompt is not None:
        raise LoopError(f"{expected_job_id} 阻塞节点的 prompt 必须为 null")
    return dict(result)


def validate_child_result(
    batch: Path,
    result: Mapping[str, object],
    trusted_reuse: Optional[Mapping[str, Mapping[str, object]]] = None,
    expected_job_ids: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    trusted_reuse = trusted_reuse or {}
    index = read_index(batch)
    all_ordered_ids = [str(job["id"]) for job in index["jobs"]]  # type: ignore[index]
    known_ids = set(all_ordered_ids)
    if expected_job_ids is None:
        ordered_ids = all_ordered_ids
    else:
        requested = set(str(value) for value in expected_job_ids)
        unknown_requested = requested - known_ids
        if unknown_requested:
            raise LoopError(
                "请求校验未知 job：" + "、".join(sorted(unknown_requested, key=natural_key))
            )
        ordered_ids = [job_id for job_id in all_ordered_ids if job_id in requested]
        if not ordered_ids:
            raise LoopError("expected_job_ids 不能为空")
    expected_ids = set(ordered_ids)
    raw_jobs = result.get("jobs")
    if not isinstance(raw_jobs, list):
        raise LoopError("Codex 结果缺少 jobs")
    by_id: Dict[str, Dict[str, object]] = {}
    skipped_by_requirement = explicitly_skipped_ids(batch)
    child_statuses: List[str] = []
    required_fields = {"id", "status", "task_id", "output_path", "blocker"}
    for raw in raw_jobs:
        if not isinstance(raw, dict) or set(raw) != required_fields:
            raise LoopError("Codex job 字段不符合受限 schema")
        job_id = str(raw.get("id", ""))
        if job_id not in known_ids:
            raise LoopError(f"Codex 返回未知 job：{job_id}")
        if job_id not in expected_ids:
            raise LoopError(f"Codex 返回本 worker 范围外的 job：{job_id}")
        if job_id in by_id:
            raise LoopError(f"Codex 重复返回 job：{job_id}")
        if raw.get("task_id") is not None or raw.get("output_path") is not None:
            raise LoopError(f"{job_id} 子 Codex 无权声明 task_id 或 output_path")
        status = str(raw.get("status", ""))
        child_statuses.append(status)
        blocker = raw.get("blocker")
        if status == PREPARED_STATUS:
            if blocker is not None:
                raise LoopError(f"{job_id} READY_FOR_SUBMISSION 不得带 blocker")
            normalized = dict(raw)
        elif status in BLOCKED_STATUSES:
            if not isinstance(blocker, str) or not blocker.strip():
                raise LoopError(f"{job_id} 阻塞状态必须提供 blocker")
            normalized = {
                "id": job_id,
                "status": "BLOCKED",
                "task_id": None,
                "output_path": None,
                "blocker": blocker.strip(),
            }
        elif status == "SKIPPED":
            if blocker is not None:
                raise LoopError(f"{job_id} SKIPPED 不得带 blocker")
            if job_id in trusted_reuse:
                normalized = dict(trusted_reuse[job_id])
            elif job_id in skipped_by_requirement:
                normalized = dict(raw)
            else:
                raise LoopError(
                    f"{job_id} SKIPPED 没有 requirements 明确跳过或父层可信复用证据"
                )
        elif status == COMPLETED_STATUS:
            raise LoopError(f"{job_id} 子 Codex 无权声明 COMPLETED")
        else:
            raise LoopError(f"{job_id} Codex status 无效：{status}")
        by_id[job_id] = normalized
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id), key=natural_key)
        raise LoopError("Codex 缺少 job：" + "、".join(missing))

    ready = child_statuses.count(PREPARED_STATUS)
    blocked = sum(status in BLOCKED_STATUSES for status in child_statuses)
    if ready and blocked:
        expected_statuses = {"PARTIAL"}
        normalized_batch_status = "PARTIAL"
    elif ready:
        expected_statuses = {PREPARED_STATUS}
        normalized_batch_status = PREPARED_STATUS
    elif blocked:
        expected_statuses = {"BLOCKED", "FAILED"}
        normalized_batch_status = "BLOCKED"
    else:
        expected_statuses = {"NO_ELIGIBLE_JOBS"}
        normalized_batch_status = "NO_ELIGIBLE_JOBS"
    if result.get("batch_status") not in expected_statuses:
        raise LoopError(
            "Codex batch_status 与逐 job 状态不一致："
            f"{result.get('batch_status')}，预期 {sorted(expected_statuses)}"
        )
    return {
        "batch_status": normalized_batch_status,
        "summary": str(result.get("summary", "")),
        "jobs": [by_id[job_id] for job_id in ordered_ids],
    }


def execute_prepared_jobs(
    batch: Path,
    project_root: Path,
    prepared: Dict[str, object],
    *,
    loop_root: Optional[Path] = None,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    trusted_reuse: Optional[Mapping[str, Mapping[str, object]]] = None,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
    already_validated: bool = False,
    submit_only: bool = False,
    paid_execution_authorized: bool = False,
    payment_authorization_identities: Optional[
        Mapping[str, Mapping[str, str]]
    ] = None,
) -> Dict[str, object]:
    loop_root = loop_root or batch.parent.parent
    # Resolve the adapter from this batch's immutable profile.  Falling back
    # directly to the historical Dreamina default here would make a valid Ark
    # profile reach the wrong adapter only at paid submission time.
    executor, _profile, _profile_managed = executor_for_batch(
        batch, project_root, executor=executor
    )
    normalized = prepared if already_validated else validate_child_result(
        batch, prepared, trusted_reuse=trusted_reuse
    )
    jobs = normalized.get("jobs")
    assert isinstance(jobs, list)
    verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
    final_jobs: List[Dict[str, object]] = []
    authorization_identities = payment_authorization_identities or {}
    for raw_job in jobs:
        assert isinstance(raw_job, dict)
        job_id = str(raw_job.get("id", ""))
        if raw_job.get("status") != PREPARED_STATUS:
            final_jobs.append(dict(raw_job))
            continue
        try:
            manifest = execute_submission_plan(
                batch,
                project_root,
                job_id,
                loop_root=loop_root,
                executor=executor,
                minimum_free_bytes=minimum_free_bytes,
                max_batch_videos=max_batch_videos,
                daily_paid_limit=daily_paid_limit,
                submit_only=submit_only,
                paid_execution_authorized=paid_execution_authorized,
                payment_authorization_identity=authorization_identities.get(job_id),
            )
            if submit_only:
                final_jobs.append(
                    {
                        "id": job_id,
                        "status": "QUEUED",
                        "task_id": manifest.get("task_id"),
                        "output_path": None,
                        "blocker": None,
                    }
                )
                continue
            final_jobs.append(
                {
                    "id": job_id,
                    "status": COMPLETED_STATUS,
                    "task_id": manifest.get("task_id"),
                    "output_path": manifest.get("final_output"),
                    "blocker": None,
                }
            )
        except Exception as exc:
            final_jobs.append(
                {
                    "id": job_id,
                    "status": "BLOCKED",
                    "task_id": recorded_task_id(project_root, batch.name, job_id),
                    "output_path": None,
                    "blocker": str(exc),
                }
            )
    succeeded = sum(job.get("status") == COMPLETED_STATUS for job in final_jobs)
    queued = sum(job.get("status") == "QUEUED" for job in final_jobs)
    blocked = sum(job.get("status") in BLOCKED_STATUSES for job in final_jobs)
    skipped = sum(job.get("status") == "SKIPPED" for job in final_jobs)
    if queued and not succeeded and not blocked:
        status = "QUEUED"
    elif (succeeded or queued) and blocked:
        status = "PARTIAL"
    elif succeeded:
        status = COMPLETED_STATUS
    elif skipped == len(final_jobs):
        status = "NO_ELIGIBLE_JOBS"
    else:
        status = "BLOCKED"
    return {
        "batch_status": status,
        "summary": (
            f"外层 Loop 串行执行完成：已排队 {queued}，技术成功 {succeeded}，"
            f"阻塞 {blocked}，跳过 {skipped}。"
        ),
        "jobs": final_jobs,
    }


def process_ready_batch(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    execute: bool,
    *,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
) -> Optional[Path]:
    """Read-only legacy scan; direct execution is permanently disabled."""

    del executor, minimum_free_bytes, daily_paid_limit
    if execute:
        raise LoopError(
            "旧整批 Agent 执行入口已停用；请使用 JavaScript 控制面的节点化流式路径"
        )

    try:
        errors, coverage = validate_requirements(batch)
        atomic_write_json(batch / "requirements-coverage.json", coverage)
        reference_path = batch / "reference-index.json"
        if not reference_path.is_file():
            atomic_write_json(reference_path, build_reference_index(batch))
        verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
        if errors:
            raise LoopError("；".join(errors))
    except Exception as exc:
        write_blocker(batch, "执行前完整性检查未通过", [str(exc)])
        return move_batch(batch, loop_root, "blocked")

    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "READY_DRY_RUN",
            "updated_at": utc_now(),
            "message": "需求覆盖通过；旧整批路径只读，未启动任何 Agent 或付费任务。",
        },
    )
    return None


def prepare_batch_without_submission(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    *,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    """Reject the retired whole-batch preparation entry."""

    del batch, loop_root, project_root, executor, minimum_free_bytes, max_batch_videos
    raise LoopError(
        "旧整批 prepare 入口已停用；请使用 JavaScript 控制面的 prepare，"
        "由 Video-to-Prompt 单节点和父层确定性 Gate 与尾巴完成准备"
    )


def _streaming_flow_path(batch: Path) -> Path:
    return batch / "streaming-flow.json"


def _has_frozen_legacy_flow(batch: Path) -> bool:
    """Recognise a pre-v3 flow shape; recovery evidence is checked separately."""

    path = _streaming_flow_path(batch)
    try:
        flow = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(flow, dict)
        and flow.get("schema_version") == 1
        and flow.get("batch_id") == batch.name
        and flow.get("job_bindings_sha256") == sha256_file(batch / JOB_BINDINGS_FILENAME)
        and re.fullmatch(
            r"[0-9a-f]{64}", str(flow.get("flow_fingerprint") or "")
        )
    )


def _streaming_job_result_path(batch: Path, stage: str, job_id: str) -> Path:
    if stage not in {"preparation", "submission"}:
        raise LoopError(f"未知 streaming stage：{stage}")
    root = batch / "streaming-results" / stage
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    return root / f"{job_id}.json"


def _expected_streaming_flow_jobs(
    batch: Path,
    index: Mapping[str, object],
) -> List[Dict[str, object]]:
    """Derive the one canonical ordered Job manifest for a frozen flow."""

    indexed_jobs = index.get("jobs")
    if not isinstance(indexed_jobs, list):
        raise LoopError("batch-index.json 缺少 jobs")
    skipped = explicitly_skipped_ids(batch)
    expected: List[Dict[str, object]] = []
    for raw_job in indexed_jobs:
        if not isinstance(raw_job, dict):
            raise LoopError("batch-index.json 的 Job 记录无效")
        expected.append(
            {
                "id": str(raw_job.get("id") or ""),
                "filename": str(raw_job.get("filename") or ""),
                "skipped": str(raw_job.get("id") or "") in skipped,
            }
        )
    return expected


def _streaming_flow_jobs_sha256(
    jobs: Sequence[Mapping[str, object]],
) -> str:
    canonical = json.dumps(
        list(jobs), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_frozen_flow_jobs(
    batch: Path,
    flow: Mapping[str, object],
    index: Mapping[str, object],
) -> List[Dict[str, object]]:
    """Reject reordered, duplicated, omitted, or rewritten frozen Jobs."""

    expected = _expected_streaming_flow_jobs(batch, index)
    actual = flow.get("jobs")
    if actual != expected:
        raise LoopError(
            "streaming-flow.json 的冻结 Job 清单与 batch-index/requirements 不一致"
        )
    expected_sha256 = _streaming_flow_jobs_sha256(expected)
    declared_sha256 = flow.get("flow_jobs_sha256")
    if declared_sha256 is not None and declared_sha256 != expected_sha256:
        raise LoopError("streaming-flow.json 的冻结 Job 清单哈希无效")
    return expected


def _streaming_flow_identity(
    batch: Path,
    project_root: Path,
    index: Mapping[str, object],
    reference_index: Mapping[str, object],
) -> Dict[str, object]:
    requirements = batch / "requirements.txt"
    if not requirements.is_file():
        raise LoopError("缺少 requirements.txt")
    jobs = index.get("jobs")
    references = reference_index.get("references")
    if not isinstance(jobs, list) or not isinstance(references, list):
        raise LoopError("streaming flow 索引格式无效")
    bindings = validate_job_bindings(batch, index, reference_index)
    binding_path = batch / JOB_BINDINGS_FILENAME
    binding_payload = json.loads(binding_path.read_text(encoding="utf-8"))
    profile, profile_managed = backend_profile_for_batch(
        batch, index=index, reference_index=reference_index
    )
    privacy_modes = {
        str(job.get("id")): str(bindings[str(job.get("id"))]["privacy_mode"])
        for job in jobs
        if isinstance(job, dict) and str(job.get("id")) in bindings
    }
    flow_jobs = _expected_streaming_flow_jobs(batch, index)
    identity = {
        "schema_version": 1,
        "batch_id": batch.name,
        "requirements_sha256": sha256_file(requirements),
        "batch_index_sha256": sha256_file(batch / "batch-index.json"),
        "reference_index_sha256": sha256_file(batch / "reference-index.json"),
        "job_bindings_sha256": sha256_file(binding_path),
        "job_ids": [str(job.get("id")) for job in jobs if isinstance(job, dict)],
        "flow_jobs_sha256": _streaming_flow_jobs_sha256(flow_jobs),
        "reference_ids": [
            str(item.get("id")) for item in references if isinstance(item, dict)
        ],
    }
    if binding_payload.get("schema_version") == 1:
        identity["prepared_sources"] = []
    else:
        identity["privacy_modes"] = privacy_modes
    if binding_payload.get("schema_version") == 3:
        identity["prompt_pipeline"] = PROMPT_PIPELINE_VERSION
        identity["video_to_prompt_model"] = VIDEO_TO_PROMPT_MODEL
        identity["video_to_prompt_contract_sha256"] = sha256_file(
            node_contract_path("video-to-prompt.md")
        )
        source_evidence_path = _source_evidence_index_path(batch)
        if source_evidence_path.is_file():
            trusted_tool = _trusted_ffmpeg_executable()
            source_evidence = verify_source_evidence_index(
                batch, index, trusted_tool
            )
            tool_size, tool_sha256 = _hash_without_change(trusted_tool)
            identity["source_evidence_index_sha256"] = sha256_file(
                source_evidence_path
            )
            identity["source_evidence_sampler_recipe"] = source_evidence.get(
                "sampler_recipe"
            )
            identity["source_evidence_cache_schema_version"] = (
                source_evidence.get("cache_schema_version")
            )
            identity["source_evidence_analysis_tool_size_bytes"] = tool_size
            identity["source_evidence_analysis_tool_sha256"] = tool_sha256
    if profile_managed:
        identity["backend_profile"] = profile.profile_id
        identity["backend_profile_constraints_sha256"] = profile.constraints_digest
    return identity


def _streaming_flow_fingerprint(identity: Mapping[str, object]) -> str:
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _matches_retired_prompt_pipeline_flow(
    flow: Mapping[str, object], current_identity: Mapping[str, object]
) -> bool:
    """Match a frozen, fully prepared flow from a retired prompt pipeline."""

    if current_identity.get("prompt_pipeline") != PROMPT_PIPELINE_VERSION:
        return False
    retired_identity = dict(current_identity)
    retired_version = flow.get("prompt_pipeline")
    if retired_version in RETIRED_PROMPT_PIPELINE_VERSIONS:
        retired_model = flow.get("video_to_prompt_model")
        retired_contract = flow.get("video_to_prompt_contract_sha256")
        if (
            retired_model != VIDEO_TO_PROMPT_MODEL
            or not isinstance(retired_contract, str)
            or not re.fullmatch(r"[0-9a-f]{64}", retired_contract)
        ):
            return False
        retired_identity["prompt_pipeline"] = retired_version
        retired_identity["video_to_prompt_model"] = retired_model
        retired_identity["video_to_prompt_contract_sha256"] = retired_contract
    elif retired_version == PROMPT_PIPELINE_VERSION and (
        "source_evidence_index_sha256" not in flow
        or "flow_jobs_sha256" not in flow
    ):
        # Fully prepared flows from before either immutable-manifest freeze
        # keep their original prompt contract, but may never create new prompts.
        pass
    elif "prompt_pipeline" not in flow:
        # Pre-versioned schema-v3 flows can only be resumed after every Job is
        # already prepared and its paid-capable submission plan is rechecked.
        retired_identity.pop("prompt_pipeline", None)
        retired_identity.pop("video_to_prompt_model", None)
        retired_identity.pop("video_to_prompt_contract_sha256", None)
    else:
        return False
    if "source_evidence_index_sha256" not in flow:
        for key in (
            "source_evidence_index_sha256",
            "source_evidence_sampler_recipe",
            "source_evidence_cache_schema_version",
            "source_evidence_analysis_tool_size_bytes",
            "source_evidence_analysis_tool_sha256",
        ):
            retired_identity.pop(key, None)
    if "flow_jobs_sha256" not in flow:
        retired_identity.pop("flow_jobs_sha256", None)
    return flow.get("flow_fingerprint") == _streaming_flow_fingerprint(
        retired_identity
    )


def _matches_pre_job_manifest_flow(
    flow: Mapping[str, object], current_identity: Mapping[str, object]
) -> bool:
    """Match a fully prepared legacy flow frozen before Job-manifest hashing."""

    if "flow_jobs_sha256" in flow:
        return False
    legacy_identity = dict(current_identity)
    legacy_identity.pop("flow_jobs_sha256", None)
    return flow.get("flow_fingerprint") == _streaming_flow_fingerprint(
        legacy_identity
    )


def _retired_prompt_pipeline_flow_is_fully_prepared(
    batch: Path,
    flow: Mapping[str, object],
    current_identity: Mapping[str, object],
) -> bool:
    """Allow frozen recovery only when every old-flow Job already has a result."""

    expected_ids = current_identity.get("job_ids")
    jobs = flow.get("jobs")
    if not isinstance(expected_ids, list) or not isinstance(jobs, list):
        return False
    actual_ids = [
        str(item.get("id")) for item in jobs if isinstance(item, dict)
    ]
    if actual_ids != [str(job_id) for job_id in expected_ids]:
        return False
    flow_fingerprint = str(flow.get("flow_fingerprint") or "")
    for item in jobs:
        if not isinstance(item, dict):
            return False
        job_id = str(item.get("id") or "")
        result_path = (
            batch / "streaming-results" / "preparation" / f"{job_id}.json"
        )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        expected_status = (
            "SKIPPED" if item.get("skipped") is True else PREPARED_STATUS
        )
        prepared_job = result.get("job") if isinstance(result, dict) else None
        if not (
            isinstance(result, dict)
            and result.get("schema_version") == 1
            and result.get("batch_id") == batch.name
            and result.get("job_id") == job_id
            and result.get("flow_fingerprint") == flow_fingerprint
            and isinstance(prepared_job, dict)
            and prepared_job.get("status") == expected_status
        ):
            return False
    return True


def _validate_frozen_flow_submission_plans(
    batch: Path,
    project_root: Path,
    flow: Mapping[str, object],
    *,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> None:
    """Validate every paid-capable artifact before an old flow can resume."""

    jobs = flow.get("jobs")
    if not isinstance(jobs, list):
        raise LoopError("冻结 flow 缺少 jobs")
    for item in jobs:
        if not isinstance(item, dict):
            raise LoopError("冻结 flow 的 Job 记录无效")
        if item.get("skipped") is True:
            continue
        job_id = str(item.get("id") or "")
        load_submission_plan(
            batch,
            project_root,
            job_id,
            max_batch_videos=max_batch_videos,
        )


def _is_current_prompt_pipeline_flow(
    flow: Mapping[str, object], current_identity: Mapping[str, object]
) -> bool:
    """Require both the explicit version and the exact inlined contract hash."""

    return bool(
        flow.get("prompt_pipeline") == PROMPT_PIPELINE_VERSION
        and flow.get("video_to_prompt_model") == VIDEO_TO_PROMPT_MODEL
        and flow.get("video_to_prompt_contract_sha256")
        == current_identity.get("video_to_prompt_contract_sha256")
        and isinstance(flow.get("source_evidence_index_sha256"), str)
        and flow.get("source_evidence_index_sha256")
        == current_identity.get("source_evidence_index_sha256")
        and isinstance(flow.get("flow_jobs_sha256"), str)
        and flow.get("flow_jobs_sha256")
        == current_identity.get("flow_jobs_sha256")
    )


def _write_streaming_inspection_state(
    batch: Path,
    flow_fingerprint: str,
    jobs: Sequence[Mapping[str, object]],
    *,
    preparation_only: bool,
) -> None:
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "STREAMING_PREPARATION_READY",
            "updated_at": utc_now(),
            "flow_fingerprint": flow_fingerprint,
            "jobs": len(jobs),
            "preparation_only": preparation_only,
        },
    )


def inspect_streaming_flow(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    *,
    preparation_only: bool = False,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    allowed_state = "needs-input" if preparation_only else "ready"
    if batch.parent.resolve() != (loop_root / allowed_state).resolve():
        raise LoopError(f"streaming flow 只接受 {allowed_state}/ 中的批次")
    if preparation_only and not (batch / "PAUSE").is_file():
        raise LoopError("并行 prepare 要求批次存在 PAUSE")
    # The executor sanitises the submission name before deriving manifest and
    # output filenames, while the parent looks for those files under the raw
    # name. A batch name that needs sanitising only shows up much later, as a
    # missing manifest, so reject it here.
    if not safe_batch_name(batch.name):
        raise LoopError(
            f"批次名 {batch.name!r} 只能包含英文字母、数字、点、下划线和短横线，"
            "且以字母或数字开头，并且不能使用 Windows 保留名或末尾点"
        )
    errors, coverage = validate_requirements(batch)
    atomic_write_json(batch / "requirements-coverage.json", coverage)
    if errors:
        raise LoopError("；".join(errors))
    reference_index = reference_index_for_validation(
        batch, persist_if_unfrozen=False
    )
    index = verify_batch_index(batch, max_batch_videos=max_batch_videos)
    binding_payload = _job_bindings_payload(batch)
    # Frozen schema-v1/v2 flows remain recoverable without changing their
    # identity.  A new batch, however, must begin as schema v3 so it cannot
    # bypass the selected profile and its local upload-preparation Gate.
    if (
        binding_payload.get("schema_version") != 3
        and not _has_frozen_legacy_flow(batch)
    ):
        raise LoopError(
            f"新批次的 {JOB_BINDINGS_FILENAME} 必须使用 schema_version 3"
        )
    require_free_space(project_root, minimum_free_bytes)
    bindings = validate_job_bindings(batch, index, reference_index)
    validate_requirement_binding_contract(batch, bindings)
    # Resolve the selected backend before freezing either derived index so a
    # correctable profile/binding error cannot strand a half-initialized batch.
    backend_profile_for_batch(
        batch, index=index, reference_index=reference_index
    )
    flow_path = _streaming_flow_path(batch)
    existing: Optional[Dict[str, object]] = None
    if flow_path.is_file():
        try:
            raw_existing = json.loads(flow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LoopError("既有 streaming-flow.json 无效") from exc
        if not isinstance(raw_existing, dict):
            raise LoopError("既有 streaming-flow.json 无效")
        existing = raw_existing
    skipped = explicitly_skipped_ids(batch)
    if binding_payload.get("schema_version") == 3:
        if existing is None:
            ensure_source_evidence_index(batch, index, skipped_ids=skipped)
        elif "source_evidence_index_sha256" in existing:
            # A frozen flow must retain its exact evidence index; deletion is
            # corruption, never a request to rebuild evidence in place.
            verify_source_evidence_index(batch, index, skipped_ids=skipped)
    reference_path = batch / "reference-index.json"
    if not reference_path.is_file():
        atomic_write_json(reference_path, reference_index)
        reference_index = verify_reference_index(batch)
    identity = _streaming_flow_identity(
        batch, project_root, index, reference_index
    )
    fingerprint = _streaming_flow_fingerprint(identity)
    if existing is not None:
        _validate_frozen_flow_jobs(batch, existing, index)
        existing_fingerprint = (
            str(existing.get("flow_fingerprint", ""))
            if isinstance(existing, dict) else ""
        )
        binding_schema = binding_payload.get("schema_version")
        if binding_schema in {1, 2}:
            if (
                (
                    existing_fingerprint == fingerprint
                    or _matches_pre_job_manifest_flow(existing, identity)
                )
                and _retired_prompt_pipeline_flow_is_fully_prepared(
                    batch, existing, identity
                )
            ):
                frozen_jobs = existing.get("jobs")
                assert isinstance(frozen_jobs, list)
                _validate_frozen_flow_submission_plans(
                    batch,
                    project_root,
                    existing,
                    max_batch_videos=max_batch_videos,
                )
                return existing
            raise LoopError(
                "旧 schema-v1/v2 flow 尚未完整准备并验证提交计划；"
                "请建立 schema-v3 新批次，拒绝混用提示词管线"
            )
        if not _is_current_prompt_pipeline_flow(existing, identity):
            if (
                _matches_retired_prompt_pipeline_flow(existing, identity)
                and _retired_prompt_pipeline_flow_is_fully_prepared(
                    batch, existing, identity
                )
            ):
                frozen_jobs = existing.get("jobs")
                assert isinstance(frozen_jobs, list)
                _validate_frozen_flow_submission_plans(
                    batch,
                    project_root,
                    existing,
                    max_batch_videos=max_batch_videos,
                )
                return existing
            raise LoopError(
                "批次使用未知或已退役的提示词管线且未完成可验证准备；"
                "请建立新批次，拒绝混用提示词管线"
            )
        result_root = batch / "streaming-results"
        has_results = result_root.is_dir() and any(result_root.rglob("*.json"))
        if has_results and existing_fingerprint != fingerprint:
            raise LoopError(
                "批次输入在 streaming job 产生后发生变化；请建立新批次，拒绝混用旧结果"
            )
        if existing_fingerprint == fingerprint:
            return existing
    jobs = _expected_streaming_flow_jobs(batch, index)
    flow = {
        **identity,
        "flow_fingerprint": fingerprint,
        "preparation_only": preparation_only,
        "created_at": utc_now(),
        "jobs": jobs,
    }
    atomic_write_json(flow_path, flow)
    _write_streaming_inspection_state(
        batch,
        fingerprint,
        jobs,
        preparation_only=preparation_only,
    )
    return flow


def verify_streaming_flow(
    batch: Path,
    project_root: Path = PROJECT_ROOT,
    *,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    flow_path = _streaming_flow_path(batch)
    if not flow_path.is_file():
        raise LoopError("缺少 streaming-flow.json；请先运行 inspect-flow")
    try:
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError("streaming-flow.json 不是有效 JSON") from exc
    if not isinstance(flow, dict) or not (
        flow.get("schema_version") == 1 and flow.get("batch_id") == batch.name
    ):
        raise LoopError("streaming-flow.json 与当前批次不匹配")
    index, reference_index = verify_batch_integrity(
        batch, max_batch_videos=max_batch_videos
    )
    errors, _ = validate_requirements(batch)
    if errors:
        raise LoopError("；".join(errors))
    current_identity = _streaming_flow_identity(
        batch, project_root, index, reference_index
    )
    current_fingerprint = _streaming_flow_fingerprint(current_identity)
    _validate_frozen_flow_jobs(batch, flow, index)
    binding_schema = _job_bindings_payload(batch).get("schema_version")
    if binding_schema in {1, 2}:
        if (
            (
                flow.get("flow_fingerprint") == current_fingerprint
                or _matches_pre_job_manifest_flow(flow, current_identity)
            )
            and _retired_prompt_pipeline_flow_is_fully_prepared(
                batch, flow, current_identity
            )
        ):
            _validate_frozen_flow_submission_plans(
                batch,
                project_root,
                flow,
                max_batch_videos=max_batch_videos,
            )
            return flow
        raise LoopError(
            "旧 schema-v1/v2 flow 尚未完整准备并验证提交计划；"
            "请建立 schema-v3 新批次，拒绝混用提示词管线"
        )
    if not _is_current_prompt_pipeline_flow(flow, current_identity):
        if (
            _matches_retired_prompt_pipeline_flow(flow, current_identity)
            and _retired_prompt_pipeline_flow_is_fully_prepared(
                batch, flow, current_identity
            )
        ):
            _validate_frozen_flow_submission_plans(
                batch,
                project_root,
                flow,
                max_batch_videos=max_batch_videos,
            )
            return flow
        raise LoopError(
            "批次使用未知或已退役的提示词管线且未完成可验证准备；"
            "请建立新批次，拒绝混用提示词管线"
        )
    if flow.get("flow_fingerprint") != current_fingerprint:
        raise LoopError("streaming flow 输入哈希已变化；拒绝继续")
    bindings = validate_job_bindings(batch, index, reference_index)
    validate_requirement_binding_contract(batch, bindings)
    return flow


def preflight_streaming_submission(
    batch: Path,
    project_root: Path = PROJECT_ROOT,
    *,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    """Validate every frozen Job before JavaScript may create a paid checkpoint."""

    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    jobs = flow.get("jobs")
    assert isinstance(jobs, list)
    eligible = [
        str(item.get("id") or "")
        for item in jobs
        if isinstance(item, dict) and item.get("skipped") is not True
    ]
    flow_fingerprint = str(flow["flow_fingerprint"])
    failures: List[str] = []
    for job_id in eligible:
        preparation = _read_streaming_job_result(
            batch, "preparation", job_id, flow_fingerprint
        )
        prepared_job = preparation["job"]
        assert isinstance(prepared_job, dict)
        status = str(prepared_job.get("status") or "")
        if status != PREPARED_STATUS:
            failures.append(
                f"{job_id} 当前 preparation status 为 {status or 'UNKNOWN'}；"
                "拒绝使用陈旧 submission plan"
            )
            continue
        try:
            load_submission_plan(
                batch,
                project_root,
                job_id,
                max_batch_videos=max_batch_videos,
            )
        except LoopError as exc:
            _invalidate_prepared_streaming_job_result(
                batch,
                job_id,
                flow_fingerprint,
                preparation,
                str(exc),
            )
            failures.append(f"{job_id} 提交计划复核失败：{exc}")
    if failures:
        # Keep the operator-facing state in sync before returning an error to
        # JavaScript.  This remains local-only and creates no checkpoint.
        finalize_streaming_preparation(
            batch, project_root, max_batch_videos=max_batch_videos
        )
        raise LoopError("；".join(failures))
    return {
        "schema_version": 1,
        "batch_id": batch.name,
        "flow_fingerprint": flow["flow_fingerprint"],
        "eligible_job_ids": eligible,
        "ready_for_paid_submission": True,
    }


def _read_streaming_job_result(
    batch: Path,
    stage: str,
    job_id: str,
    flow_fingerprint: str,
) -> Dict[str, object]:
    path = _streaming_job_result_path(batch, stage, job_id)
    if not path.is_file():
        raise LoopError(f"{job_id} 缺少 {stage} result")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError(f"{job_id} {stage} result 无效") from exc
    if not isinstance(value, dict) or not (
        value.get("schema_version") == 1
        and value.get("batch_id") == batch.name
        and value.get("job_id") == job_id
        and value.get("flow_fingerprint") == flow_fingerprint
        and isinstance(value.get("job"), dict)
    ):
        raise LoopError(f"{job_id} {stage} result 与当前 flow 不匹配")
    return value


def _invalidate_prepared_streaming_job_result(
    batch: Path,
    job_id: str,
    flow_fingerprint: str,
    existing: Mapping[str, object],
    reason: str,
) -> Dict[str, object]:
    """Atomically demote a stale READY artifact before any paid path runs."""

    existing_job = existing.get("job")
    if not isinstance(existing_job, dict):
        raise LoopError(f"{job_id} preparation result 缺少 job")
    invalidated = {
        "schema_version": 1,
        "batch_id": batch.name,
        "job_id": job_id,
        "flow_fingerprint": flow_fingerprint,
        "updated_at": utc_now(),
        # Preserve the prior READY record inside the new canonical result so
        # retry history remains explainable even before retry-prepare archives
        # this blocked attempt.
        "invalidated_prepared_job": dict(existing_job),
        "job": {
            "id": job_id,
            "status": "BLOCKED",
            "task_id": None,
            "output_path": None,
            "blocker": f"提交计划复核失败：{reason}",
        },
    }
    atomic_write_json(
        _streaming_job_result_path(batch, "preparation", job_id), invalidated
    )
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "LOCAL_PREPARATION_BLOCKED",
            "updated_at": utc_now(),
            "flow_fingerprint": flow_fingerprint,
            "invalidated_job": job_id,
            "auto_ready": False,
            "payment_approval_required": False,
        },
    )
    return invalidated


def prepare_streaming_job(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: Optional[ExecutorSpec] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    retry_existing: bool = False,
) -> Dict[str, object]:
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    flow_fingerprint = str(flow["flow_fingerprint"])
    jobs = flow.get("jobs")
    job_meta = next(
        (
            item
            for item in jobs
            if isinstance(item, dict) and item.get("id") == job_id
        ),
        None,
    ) if isinstance(jobs, list) else None
    if not isinstance(job_meta, dict):
        raise LoopError(f"{job_id} 不在 streaming flow 中")
    result_path = _streaming_job_result_path(batch, "preparation", job_id)
    if result_path.is_file() and not retry_existing:
        existing = _read_streaming_job_result(
            batch, "preparation", job_id, flow_fingerprint
        )
        existing_job = existing["job"]
        assert isinstance(existing_job, dict)
        if existing_job.get("status") == PREPARED_STATUS:
            try:
                load_submission_plan(
                    batch,
                    project_root,
                    job_id,
                    executor=executor,
                    max_batch_videos=max_batch_videos,
                )
            except LoopError as exc:
                return _invalidate_prepared_streaming_job_result(
                    batch,
                    job_id,
                    flow_fingerprint,
                    existing,
                    str(exc),
                )
        return existing
    if job_meta.get("skipped") is True:
        normalized_job = {
            "id": job_id,
            "status": "SKIPPED",
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
    else:
        try:
            executor, profile, profile_managed = executor_for_batch(
                batch, project_root, executor=executor
            )
            privacy_mode = job_privacy_mode(batch, job_id)
            references = select_job_reference_records(batch, job_id)
            _write_parent_reference_binding(
                batch, project_root, job_id, references
            )
            video_to_prompt_result = run_video_to_prompt_node(
                batch, loop_root, project_root, job_id, references
            )
            normalized_jobs = video_to_prompt_result.get("jobs")
            assert isinstance(normalized_jobs, list) and len(normalized_jobs) == 1
            video_to_prompt_job = normalized_jobs[0]
            assert isinstance(video_to_prompt_job, dict)
            normalized_job = {
                "id": video_to_prompt_job["id"],
                "status": (
                    PREPARED_STATUS
                    if video_to_prompt_job["status"] == NODE_COMPLETE_STATUS
                    else video_to_prompt_job["status"]
                ),
                "task_id": None,
                "output_path": None,
                "blocker": video_to_prompt_job["blocker"],
            }
            if normalized_job.get("status") == PREPARED_STATUS:
                output_dir = job_output_dir(project_root, batch, job_id)
                prompt_file = output_dir / PROMPT_FILENAME
                if not prompt_file.is_file():
                    raise LoopError(f"{job_id} 缺少 {PROMPT_FILENAME}")
                # Parent-owned ordered binding is resolved before the first
                # fixed-format check and before any local executor activity.
                images = resolve_reference_binding(batch, project_root, job_id)
                # This deterministic gate is the final prompt checkpoint
                # before preview or any paid-capable action. A failed prompt
                # blocks locally.
                validate_execution_prompt(
                    prompt_file,
                    reference_count=len(images),
                    expected_reference_names=[
                        str(item.get("semantic_name") or "").strip()
                        for item in references
                    ],
                    expected_requirement_lines=_canonical_requirement_lines(
                        batch, job_id, references
                    ),
                )
                active_video = (
                    prepare_mosaic_video(batch, project_root, job_id)
                    if privacy_mode == "mosaic_required"
                    else prepare_active_video(batch, project_root, job_id)
                )
                upload_preparation_manifest: Optional[Path] = None
                if profile_managed:
                    active_video, upload_preparation_manifest = prepare_upload_for_profile(
                        active_video,
                        output_dir,
                        profile,
                    )
                manifest_path = run_executor_probe(
                    batch,
                    project_root,
                    job_id,
                    executor=executor,
                    active_video=active_video,
                    prompt_file=prompt_file,
                    images=images,
                    profile_managed=profile_managed,
                    upload_preparation_manifest=upload_preparation_manifest,
                )
                build_submission_plan(
                    batch,
                    project_root,
                    job_id,
                    executor=executor,
                    active_video=active_video,
                    images=images,
                    manifest_path=manifest_path,
                    profile=profile,
                    profile_managed=profile_managed,
                    upload_preparation_manifest=upload_preparation_manifest,
                    max_batch_videos=max_batch_videos,
                )
        except Exception as exc:
            if _strict_errors_enabled() and not isinstance(exc, LoopError):
                raise
            normalized_job = {
                "id": job_id,
                "status": "BLOCKED",
                "task_id": None,
                "output_path": None,
                "blocker": str(exc),
            }
    result = {
        "schema_version": 1,
        "batch_id": batch.name,
        "job_id": job_id,
        "flow_fingerprint": flow_fingerprint,
        "updated_at": utc_now(),
        "job": normalized_job,
    }
    atomic_write_json(result_path, result)
    return result


def _retry_prepare_paid_evidence(
    batch: Path,
    project_root: Path,
    flow: Mapping[str, object],
) -> List[str]:
    """Return any paid or remote evidence that makes a local retry unsafe."""

    evidence: List[str] = []
    submission_root = batch / "streaming-results" / "submission"
    if submission_root.is_dir():
        evidence.extend(
            f"submission:{path.name}"
            for path in sorted(
                submission_root.glob("*.json"), key=lambda item: item.name
            )
            if path.is_file()
        )
    evidence.extend(
        f"payment-checkpoint:{path.name}"
        for path in sorted(
            batch.glob("payment-checkpoint*.json"), key=lambda item: item.name
        )
        if path.is_file()
    )
    jobs = flow.get("jobs")
    if not isinstance(jobs, list):
        raise LoopError("streaming flow 缺少 jobs")
    for item in jobs:
        if not isinstance(item, dict):
            raise LoopError("streaming flow 的 job 无效")
        candidate_job_id = str(item.get("id") or "")
        if not candidate_job_id:
            raise LoopError("streaming flow 的 job 缺少 id")
        if recorded_task_id(project_root, batch.name, candidate_job_id):
            evidence.append(f"task:{candidate_job_id}")
    return evidence


def retry_streaming_preparation_job(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: Optional[ExecutorSpec] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    """Archive and rerun exactly one failed local preparation attempt."""

    if batch.parent.resolve() != (loop_root / "needs-input").resolve():
        raise LoopError("retry-prepare 只接受 needs-input/ 中的批次")
    if not (batch / "PAUSE").is_file():
        raise LoopError("retry-prepare 要求批次存在 PAUSE")
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    flow_fingerprint = str(flow["flow_fingerprint"])
    jobs = flow.get("jobs")
    job_meta = next(
        (
            item
            for item in jobs
            if isinstance(item, dict) and item.get("id") == job_id
        ),
        None,
    ) if isinstance(jobs, list) else None
    if not isinstance(job_meta, dict) or job_meta.get("skipped") is True:
        raise LoopError(f"{job_id} 不是当前 flow 中可重试的 Job")
    result_path = _streaming_job_result_path(batch, "preparation", job_id)
    existing = _read_streaming_job_result(
        batch, "preparation", job_id, flow_fingerprint
    )
    existing_job = existing["job"]
    assert isinstance(existing_job, dict)
    status = str(existing_job.get("status") or "")
    if status == PREPARED_STATUS:
        try:
            load_submission_plan(
                batch,
                project_root,
                job_id,
                executor=executor,
                max_batch_videos=max_batch_videos,
            )
        except LoopError:
            pass
        else:
            raise LoopError(
                f"{job_id} 当前 preparation status 为 {PREPARED_STATUS}，"
                "且提交计划有效；拒绝重复准备"
            )
    elif status not in BLOCKED_STATUSES:
        raise LoopError(
            f"{job_id} 当前 preparation status 为 {status or 'UNKNOWN'}；"
            "只允许重试 BLOCKED/FAILED，或提交计划已失效的 READY"
        )
    paid_evidence = _retry_prepare_paid_evidence(batch, project_root, flow)
    if paid_evidence:
        raise LoopError(
            f"{job_id} 批次已存在提交、付费授权或远端任务证据（{', '.join(paid_evidence)}）；"
            "拒绝把本地重试与付费恢复混用"
        )
    history_root = (
        batch / "streaming-results" / "preparation-history" / job_id
    )
    history_root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        history_root.chmod(0o700)
    attempts = [
        int(match.group(1))
        for path in history_root.glob("attempt-*.json")
        if (match := re.fullmatch(r"attempt-(\d+)\.json", path.name))
    ]
    history_path = history_root / f"attempt-{max(attempts, default=0) + 1:03d}.json"
    atomic_write_json(history_path, existing)
    chmod_private(history_path)
    retried = prepare_streaming_job(
        batch,
        loop_root,
        project_root,
        job_id,
        executor=executor,
        max_batch_videos=max_batch_videos,
        retry_existing=True,
    )
    if not isinstance(retried, dict):
        raise LoopError(f"{job_id} retry preparation result 无效")
    atomic_write_json(result_path, retried)
    return retried


def _payment_checkpoint_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LoopError(f"payment-checkpoint {label} 无效")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LoopError(f"payment-checkpoint {label} 无效") from exc
    if parsed.tzinfo is None:
        raise LoopError(f"payment-checkpoint {label} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def payment_authorization_binding(
    token: str, checkpoint: Mapping[str, object]
) -> str:
    values: Tuple[str, ...] = (
        "video-loop-payment-v1",
        str(checkpoint.get("batch_id", "")),
        str(checkpoint.get("flow_fingerprint", "")),
        str(checkpoint.get("planned_paid_tasks", "")),
        str(checkpoint.get("authorization_scope", "")),
        str(checkpoint.get("orchestrator_pid", "")),
        str(checkpoint.get("authorization_nonce", "")),
        str(checkpoint.get("authorized_at", "")),
        str(checkpoint.get("expires_at", "")),
    )
    if checkpoint.get("schema_version") == 3:
        identities = checkpoint.get("submission_identities")
        if not isinstance(identities, list):
            raise LoopError("payment-checkpoint 缺少 submission_identities")
        canonical = json.dumps(
            identities, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        values += (hashlib.sha256(canonical).hexdigest(),)
    values += (token,)
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def submission_authorization_identity(
    batch: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: Optional[ExecutorSpec] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Optional[Dict[str, str]]:
    """Derive the safe, immutable media identity covered by a paid approval.

    Only schema-v3 plans have upload preparation artifacts. Older frozen plans
    retain their former payment-checkpoint contract; new profile-managed plans
    must bind final media, preflight and provenance hashes before submission.
    """

    index, reference_index = verify_batch_integrity(
        batch, max_batch_videos=max_batch_videos
    )
    resolved_executor, _profile, profile_managed = executor_for_batch(
        batch,
        project_root,
        executor=executor,
        index=index,
        reference_index=reference_index,
    )
    if not profile_managed:
        return None
    plan = load_submission_plan(
        batch,
        project_root,
        job_id,
        executor=resolved_executor,
        max_batch_videos=max_batch_videos,
    )
    preflight = validate_submission_preflight(plan, resolved_executor)
    active = preflight.get("active_video")
    if not isinstance(active, dict):
        raise LoopError("preflight manifest 缺少 active_video")
    model_version = str(preflight.get("model_version", ""))
    if not model_version:
        raise LoopError("受控 preflight 缺少模型版本")
    fields = {
        "job_id": job_id,
        "backend_profile": str(plan.get("backend_profile", "")),
        "backend_profile_constraints_sha256": str(
            plan.get("backend_profile_constraints_sha256", "")
        ).lower(),
        "model_version_sha256": hashlib.sha256(
            model_version.encode("utf-8")
        ).hexdigest(),
        "final_video_sha256": str(active.get("sha256", "")).lower(),
        "preflight_manifest_sha256": str(
            plan.get("preflight_manifest_sha256", "")
        ).lower(),
        "upload_preparation_manifest_sha256": str(
            preflight.get("upload_preparation_manifest_sha256", "")
        ).lower(),
    }
    for key, value in fields.items():
        if key in {"job_id", "backend_profile"}:
            if not value:
                raise LoopError(f"付费授权身份缺少 {key}")
        elif re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise LoopError(f"付费授权身份的 {key} 无效")
    return fields


def _payment_checkpoint_path_for_job(
    batch: Path, expected_job_id: Optional[str]
) -> Path:
    """Prefer a per-job approval when the JavaScript controller created one.

    Parallel streaming submission uses separate, short-lived checkpoint files so
    one ready job cannot overwrite another job's approval.  The batch-level
    filename remains supported for frozen schema-v1/v2 batches and the legacy
    batch submit command.
    """

    if expected_job_id:
        candidate = batch / f"payment-checkpoint-{expected_job_id}.json"
        if candidate.is_file():
            return candidate
    return batch / "payment-checkpoint.json"


def _expected_submission_authorization_identities(
    batch: Path,
    project_root: Path,
    flow: Mapping[str, object],
    *,
    expected_job_id: Optional[str],
    executor: Optional[ExecutorSpec],
    max_batch_videos: int,
) -> List[Dict[str, str]]:
    """Recompute the media identities that a schema-v3 approval covers."""

    jobs = flow.get("jobs")
    if not isinstance(jobs, list):
        raise LoopError("streaming flow 缺少 jobs")
    flow_job_ids = [
        str(item.get("id", ""))
        for item in jobs
        if isinstance(item, dict) and item.get("skipped") is not True
    ]
    job_ids = [expected_job_id] if expected_job_id is not None else flow_job_ids
    if not job_ids or any(job_id not in flow_job_ids for job_id in job_ids):
        raise LoopError("payment-checkpoint 的 Job 范围不在当前 flow 中")
    identities: List[Dict[str, str]] = []
    for job_id in job_ids:
        identity = submission_authorization_identity(
            batch,
            project_root,
            job_id,
            executor=executor,
            max_batch_videos=max_batch_videos,
        )
        if identity is None:
            raise LoopError("schema v3 payment-checkpoint 不能绑定旧版提交计划")
        identities.append(identity)
    return sorted(identities, key=lambda item: item["job_id"])


def payment_checkpoint_identity_map(
    checkpoint: Mapping[str, object],
) -> Dict[str, Dict[str, str]]:
    """Return the already-HMAC-verified v3 media identities by Job.

    Call this only after :func:`_validate_streaming_payment_checkpoint`.  The
    returned in-memory values are checked again immediately before the paid
    adapter launch, closing the ordinary prepare-to-submit mutation window.
    """

    if checkpoint.get("schema_version") != 3:
        return {}
    raw = checkpoint.get("submission_identities")
    if not isinstance(raw, list):
        raise LoopError("schema v3 payment-checkpoint 缺少 submission_identities")
    result: Dict[str, Dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise LoopError("schema v3 payment-checkpoint 媒体身份无效")
        job_id = str(item.get("job_id", ""))
        if not job_id or job_id in result:
            raise LoopError("schema v3 payment-checkpoint Job 身份无效或重复")
        result[job_id] = {str(key): str(value) for key, value in item.items()}
    return result


def _validate_streaming_payment_checkpoint(
    batch: Path,
    flow: Mapping[str, object],
    *,
    project_root: Path = PROJECT_ROOT,
    executor: Optional[ExecutorSpec] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    environment: Optional[Mapping[str, str]] = None,
    parent_pid: Optional[int] = None,
    observed_at: Optional[datetime] = None,
    expected_job_id: Optional[str] = None,
) -> Dict[str, object]:
    checkpoint_path = _payment_checkpoint_path_for_job(batch, expected_job_id)
    if not checkpoint_path.is_file():
        raise LoopError(f"缺少 {checkpoint_path.name}")
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopError(f"{checkpoint_path.name} 不是有效 JSON") from exc
    if not isinstance(checkpoint, dict):
        raise LoopError("payment-checkpoint 必须是 JSON object")
    jobs = flow.get("jobs")
    planned_paid_tasks = sum(
        item.get("skipped") is not True
        for item in jobs
        if isinstance(item, dict)
    ) if isinstance(jobs, list) else 0
    checkpoint_planned_tasks = checkpoint.get("planned_paid_tasks")
    authorization_scope = checkpoint.get("authorization_scope")
    flow_job_ids = [
        str(item.get("id", ""))
        for item in jobs
        if isinstance(item, dict) and item.get("skipped") is not True
    ] if isinstance(jobs, list) else []
    is_single_job_scope = authorization_scope == "current-batch-single-job"
    expected_authorization = (
        is_single_job_scope
        and expected_job_id is not None
        and checkpoint_planned_tasks == 1
        and checkpoint.get("authorized_job_ids") == [expected_job_id]
        and expected_job_id in flow_job_ids
    ) or (
        authorization_scope == "current-batch"
        and checkpoint_planned_tasks == planned_paid_tasks
        and (expected_job_id is None or expected_job_id in flow_job_ids)
    )
    if not (
        checkpoint.get("schema_version") in {2, 3}
        and checkpoint.get("batch_id") == batch.name
        and checkpoint.get("flow_fingerprint") == flow.get("flow_fingerprint")
        and isinstance(checkpoint_planned_tasks, int)
        and not isinstance(checkpoint_planned_tasks, bool)
        and checkpoint.get("explicit_payment_approval_received") is True
        and expected_authorization
    ):
        raise LoopError("payment-checkpoint 未绑定当前 JavaScript flow 的明确付费授权")
    orchestrator_pid = checkpoint.get("orchestrator_pid")
    actual_parent_pid = os.getppid() if parent_pid is None else parent_pid
    if (
        not isinstance(orchestrator_pid, int)
        or isinstance(orchestrator_pid, bool)
        or orchestrator_pid <= 0
        or orchestrator_pid != actual_parent_pid
    ):
        raise LoopError("payment-checkpoint 未绑定当前 Python 子进程的实际父 JavaScript PID")
    nonce = checkpoint.get("authorization_nonce")
    binding = checkpoint.get("authorization_binding_sha256")
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise LoopError("payment-checkpoint authorization nonce 无效")
    if not isinstance(binding, str) or re.fullmatch(r"[0-9a-f]{64}", binding) is None:
        raise LoopError("payment-checkpoint capability binding 无效")
    authorized_at = _payment_checkpoint_timestamp(
        checkpoint.get("authorized_at"), "authorized_at"
    )
    expires_at = _payment_checkpoint_timestamp(
        checkpoint.get("expires_at"), "expires_at"
    )
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    lifetime = (expires_at - authorized_at).total_seconds()
    if lifetime <= 0 or lifetime > PAYMENT_CHECKPOINT_TTL_SECONDS:
        raise LoopError("payment-checkpoint 有效期无效")
    if authorized_at > now + timedelta(seconds=PAYMENT_CHECKPOINT_CLOCK_SKEW_SECONDS):
        raise LoopError("payment-checkpoint 授权时间位于未来")
    if now > expires_at:
        raise LoopError("payment-checkpoint 已过期")
    if environment is None:
        token = str(os.environ.pop(PAYMENT_AUTH_TOKEN_ENV, "")).strip()
    else:
        token = str(environment.get(PAYMENT_AUTH_TOKEN_ENV, "")).strip()
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise LoopError("缺少当前 JavaScript 进程付费 capability")
    expected_binding = payment_authorization_binding(token, checkpoint)
    if not hmac.compare_digest(binding, expected_binding):
        raise LoopError("payment-checkpoint 无法由当前 JavaScript 进程 capability 验证")
    if checkpoint.get("schema_version") == 3:
        identities = checkpoint.get("submission_identities")
        if not isinstance(identities, list) or not all(
            isinstance(item, dict) for item in identities
        ):
            raise LoopError("schema v3 payment-checkpoint 缺少有效媒体身份")
        expected_identities = _expected_submission_authorization_identities(
            batch,
            project_root,
            flow,
            expected_job_id=expected_job_id,
            executor=executor,
            max_batch_videos=max_batch_videos,
        )
        if identities != expected_identities:
            raise LoopError(
                "payment-checkpoint 的最终视频、上传准备 manifest 或 backend profile 已变化"
            )
    return checkpoint


def submit_streaming_job(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    job_id: str,
    *,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
    task_timeout: Optional[int] = None,
) -> Dict[str, object]:
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    checkpoint = _validate_streaming_payment_checkpoint(
        batch,
        flow,
        project_root=project_root,
        executor=executor,
        max_batch_videos=max_batch_videos,
        expected_job_id=job_id,
    )
    authorization_identities = payment_checkpoint_identity_map(checkpoint)
    flow_fingerprint = str(flow["flow_fingerprint"])
    preparation = _read_streaming_job_result(
        batch, "preparation", job_id, flow_fingerprint
    )
    prepared_job = preparation["job"]
    assert isinstance(prepared_job, dict)
    if prepared_job.get("status") != PREPARED_STATUS:
        raise LoopError(f"{job_id} 未处于 READY_FOR_SUBMISSION")
    result_path = _streaming_job_result_path(batch, "submission", job_id)
    if result_path.is_file():
        existing = _read_streaming_job_result(
            batch, "submission", job_id, flow_fingerprint
        )
        existing_job = existing["job"]
        assert isinstance(existing_job, dict)
        existing_output = existing_job.get("output_path")
        if (
            existing_job.get("status") == COMPLETED_STATUS
            and isinstance(existing_output, str)
            and Path(existing_output).is_file()
        ):
            return existing
    try:
        executor, _profile, _profile_managed = executor_for_batch(
            batch, project_root, executor=executor
        )
        manifest = execute_submission_plan(
            batch,
            project_root,
            job_id,
            loop_root=loop_root,
            executor=executor,
            minimum_free_bytes=minimum_free_bytes,
            max_batch_videos=max_batch_videos,
            daily_paid_limit=daily_paid_limit,
            task_timeout=task_timeout,
            paid_execution_authorized=True,
            payment_authorization_identity=authorization_identities.get(job_id),
        )
        normalized_job = {
            "id": job_id,
            "status": COMPLETED_STATUS,
            "task_id": manifest.get("task_id"),
            "output_path": manifest.get("final_output"),
            "blocker": None,
        }
    except Exception as exc:
        normalized_job = {
            "id": job_id,
            "status": "BLOCKED",
            "task_id": recorded_task_id(project_root, batch.name, job_id),
            "output_path": None,
            "blocker": str(exc),
        }
    result = {
        "schema_version": 1,
        "batch_id": batch.name,
        "job_id": job_id,
        "flow_fingerprint": flow_fingerprint,
        "updated_at": utc_now(),
        "job": normalized_job,
    }
    atomic_write_json(result_path, result)
    return result


def _collect_streaming_preparation(
    batch: Path,
    flow: Mapping[str, object],
) -> List[Dict[str, object]]:
    flow_fingerprint = str(flow["flow_fingerprint"])
    jobs = flow.get("jobs")
    if not isinstance(jobs, list):
        raise LoopError("streaming flow 缺少 jobs")
    collected: List[Dict[str, object]] = []
    for item in jobs:
        if not isinstance(item, dict):
            raise LoopError("streaming flow job 格式无效")
        job_id = str(item.get("id", ""))
        result = _read_streaming_job_result(
            batch, "preparation", job_id, flow_fingerprint
        )
        job = result["job"]
        assert isinstance(job, dict)
        collected.append(dict(job))
    return collected


def finalize_streaming_preparation(
    batch: Path,
    project_root: Path = PROJECT_ROOT,
    *,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Dict[str, object]:
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    jobs = _collect_streaming_preparation(batch, flow)
    ready = sum(job.get("status") == PREPARED_STATUS for job in jobs)
    blocked = sum(job.get("status") in BLOCKED_STATUSES for job in jobs)
    skipped = sum(job.get("status") == "SKIPPED" for job in jobs)
    status = "PARTIAL" if ready and blocked else (
        PREPARED_STATUS if ready else ("NO_ELIGIBLE_JOBS" if skipped == len(jobs) else "BLOCKED")
    )
    workflow_state = (
        "LOCAL_PREPARATION_BLOCKED"
        if blocked
        else (
            "LOCAL_PREPARED_AWAITING_APPROVAL"
            if ready
            else (
                "NO_ELIGIBLE_JOBS"
                if skipped == len(jobs)
                else "LOCAL_PREPARATION_BLOCKED"
            )
        )
    )
    payment_approval_required = bool(ready and not blocked)
    result = {
        "batch_status": status,
        "workflow_state": workflow_state,
        "payment_approval_required": payment_approval_required,
        "summary": f"FIFO 本地准备完成：可提交 {ready}，阻塞 {blocked}，跳过 {skipped}。",
        "jobs": jobs,
    }
    atomic_write_json(batch / "local-preparation-result.json", result)
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": workflow_state,
            "updated_at": utc_now(),
            "flow_fingerprint": flow["flow_fingerprint"],
            "prepared_jobs": ready,
            "blocked_jobs": blocked,
            "skipped_jobs": skipped,
            "auto_ready": False,
            "payment_approval_required": payment_approval_required,
        },
    )
    return result


def finalize_streaming_flow(
    batch: Path,
    loop_root: Path,
    project_root: Path = PROJECT_ROOT,
    *,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
) -> Path:
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    flow_fingerprint = str(flow["flow_fingerprint"])
    prepared_jobs = _collect_streaming_preparation(batch, flow)
    final_jobs: List[Dict[str, object]] = []
    for prepared_job in prepared_jobs:
        job_id = str(prepared_job.get("id", ""))
        if prepared_job.get("status") != PREPARED_STATUS:
            final_jobs.append(dict(prepared_job))
            continue
        submission = _read_streaming_job_result(
            batch, "submission", job_id, flow_fingerprint
        )
        submitted_job = submission["job"]
        assert isinstance(submitted_job, dict)
        normalized_submission = dict(submitted_job)
        if normalized_submission.get("status") == COMPLETED_STATUS:
            output_path = normalized_submission.get("output_path")
            if not isinstance(output_path, str) or not Path(output_path).is_file():
                normalized_submission.update(
                    {
                        "status": "BLOCKED",
                        "blocker": "下载文件不存在",
                    }
                )
        final_jobs.append(normalized_submission)
    succeeded = sum(
        job.get("status") == COMPLETED_STATUS for job in final_jobs
    )
    blocked = sum(job.get("status") in BLOCKED_STATUSES for job in final_jobs)
    skipped = sum(job.get("status") == "SKIPPED" for job in final_jobs)
    if succeeded and blocked:
        status = "PARTIAL"
    elif succeeded:
        status = COMPLETED_STATUS
    elif skipped == len(final_jobs):
        status = "NO_ELIGIBLE_JOBS"
    else:
        status = "BLOCKED"
    result = {
        "batch_status": status,
        "summary": f"流式并发执行完成：下载完成 {succeeded}，阻塞 {blocked}，跳过 {skipped}。",
        "jobs": final_jobs,
    }
    atomic_write_json(batch / "streaming-result.json", result)
    atomic_write_json(batch / "codex-result.json", result)
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": status,
            "updated_at": utc_now(),
            "flow_fingerprint": flow_fingerprint,
            "succeeded": succeeded,
            "blocked": blocked,
            "skipped": skipped,
        },
    )
    if status in {COMPLETED_STATUS, "NO_ELIGIBLE_JOBS"}:
        return move_batch(batch, loop_root, "completed")
    return move_batch(batch, loop_root, "blocked")


def submit_prepared_batch(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    *,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
) -> Path:
    """Submit hash-bound previewed plans after explicit batch payment approval."""

    expected_parent = (loop_root / "needs-input").resolve()
    if batch.parent.resolve() != expected_parent:
        raise LoopError("submit-prepared 只接受 needs-input/ 中的批次")
    if not (batch / "PAUSE").is_file():
        raise LoopError("submit-prepared 要求 PAUSE 保持存在，避免后台并发提交")
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    checkpoint = _validate_streaming_payment_checkpoint(
        batch,
        flow,
        project_root=project_root,
        executor=executor,
        max_batch_videos=max_batch_videos,
    )
    authorization_identities = payment_checkpoint_identity_map(checkpoint)
    index = read_index(batch)
    indexed_jobs = index["jobs"]  # type: ignore[index]
    assert isinstance(indexed_jobs, list)

    executor, _profile, _profile_managed = executor_for_batch(
        batch, project_root, executor=executor
    )
    require_free_space(project_root, minimum_free_bytes)
    errors, coverage = validate_requirements(batch)
    atomic_write_json(batch / "requirements-coverage.json", coverage)
    if errors:
        raise LoopError("；".join(errors))
    verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
    ordered_ids = [str(job["id"]) for job in indexed_jobs]
    for job_id in ordered_ids:
        load_submission_plan(
            batch,
            project_root,
            job_id,
            executor=executor,
            max_batch_videos=max_batch_videos,
        )
    prepared = {
        "batch_status": PREPARED_STATUS,
        "summary": "父层已验证无费用 preview 与明确付费授权。",
        "jobs": [
            {
                "id": job_id,
                "status": PREPARED_STATUS,
                "task_id": None,
                "output_path": None,
                "blocker": None,
            }
            for job_id in ordered_ids
        ],
    }
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "SUBMITTING_EXPLICIT_PAYMENT_AUTHORIZATION",
            "updated_at": utc_now(),
            "planned_paid_tasks": len(ordered_ids),
        },
    )
    queued_result = execute_prepared_jobs(
        batch,
        project_root,
        prepared,
        loop_root=loop_root,
        executor=executor,
        minimum_free_bytes=minimum_free_bytes,
        max_batch_videos=max_batch_videos,
        daily_paid_limit=daily_paid_limit,
        already_validated=True,
        submit_only=True,
        paid_execution_authorized=True,
        payment_authorization_identities=authorization_identities,
    )
    atomic_write_json(batch / "queued-result.json", queued_result)
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "MONITORING_QUEUED_TASKS",
            "updated_at": utc_now(),
            "queued_jobs": sum(
                job.get("status") == "QUEUED"
                for job in queued_result.get("jobs", [])
                if isinstance(job, dict)
            ),
        },
    )
    result = execute_prepared_jobs(
        batch,
        project_root,
        prepared,
        loop_root=loop_root,
        executor=executor,
        minimum_free_bytes=minimum_free_bytes,
        max_batch_videos=max_batch_videos,
        daily_paid_limit=daily_paid_limit,
        already_validated=True,
        paid_execution_authorized=True,
        payment_authorization_identities=authorization_identities,
    )
    atomic_write_json(batch / "codex-result.json", result)
    status = str(result.get("batch_status", "FAILED"))
    atomic_write_json(
        batch / "loop-state.json",
        {"batch_id": batch.name, "state": status, "updated_at": utc_now()},
    )
    if status in {COMPLETED_STATUS, "NO_ELIGIBLE_JOBS"}:
        return move_batch(batch, loop_root, "completed")
    return move_batch(batch, loop_root, "blocked")


def latest_job_task_record(
    project_root: Path,
    batch_name: str,
    job_id: str,
    *,
    retry_attempt: Optional[int] = None,
    model_version: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    base_output_dir = (
        project_root
        / "outputs"
        / "video-replacements"
        / f"{batch_name}-{job_id}"
    ).resolve()
    output_dir = base_output_dir
    if retry_attempt is not None and model_version:
        model_slug = re.sub(
            r"[^A-Za-z0-9._-]+", "-", model_version
        ).strip("-")
        output_dir = base_output_dir.with_name(
            f"{base_output_dir.name}-{model_slug}-attempt-{retry_attempt}"
        )
    task_dir = output_dir / "tasks"
    if not task_dir.is_dir():
        return None
    records = sorted(
        (path for path in task_dir.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for record_path in records:
        try:
            value = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def retry_failed_jobs_sequentially(
    batch: Path,
    loop_root: Path,
    project_root: Path,
    retry_authorization_manifest: Path,
    *,
    executor: Optional[ExecutorSpec] = None,
    minimum_free_bytes: int = 0,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
    task_timeout: int = 86400,
) -> Path:
    """Resume the active first task, then retry authorized jobs one at a time."""

    allowed_states = {"needs-input", "review", "blocked"}
    if batch.parent.name not in allowed_states:
        raise LoopError(
            "retry-sequential 只接受 needs-input/review/blocked 中的批次"
        )
    if not (batch / "PAUSE").is_file():
        raise LoopError("retry-sequential 要求 PAUSE 保持存在，避免后台并发提交")
    flow = verify_streaming_flow(
        batch, project_root, max_batch_videos=max_batch_videos
    )
    checkpoint = _validate_streaming_payment_checkpoint(
        batch,
        flow,
        project_root=project_root,
        executor=executor,
        max_batch_videos=max_batch_videos,
    )
    authorization_identities = payment_checkpoint_identity_map(checkpoint)
    executor, _profile, _profile_managed = executor_for_batch(
        batch, project_root, executor=executor
    )
    state_dir = executor_state_dir(loop_root)
    require_external_state_dir(state_dir, loop_root, project_root)
    authorization_path = retry_authorization_manifest.expanduser().resolve()
    try:
        authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError("重试授权 manifest 不是有效 JSON") from exc
    if not isinstance(authorization, dict):
        raise LoopError("重试授权 manifest 根节点必须是 JSON object")
    jobs = authorization.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise LoopError("重试授权 manifest 没有 jobs")
    ordered_retry_ids = [
        str(item.get("job_id", ""))
        for item in jobs
        if isinstance(item, dict)
    ]
    indexed_ids = [
        str(job["id"]) for job in read_index(batch)["jobs"]  # type: ignore[index]
    ]
    expected_retry_ids = indexed_ids
    if ordered_retry_ids != expected_retry_ids:
        raise LoopError(
            "VIP 重试授权必须按 batch-index 顺序精确覆盖全部任务"
        )
    for item in jobs:
        assert isinstance(item, dict)
        validate_retry_authorization(
            authorization_path,
            state_dir,
            batch,
            str(item.get("job_id", "")),
            int(item.get("retry_attempt", 0)),
        )

    require_free_space(project_root, minimum_free_bytes)
    errors, coverage = validate_requirements(batch)
    atomic_write_json(batch / "requirements-coverage.json", coverage)
    if errors:
        raise LoopError("；".join(errors))
    verify_batch_integrity(batch, max_batch_videos=max_batch_videos)
    for job_id in indexed_ids:
        load_submission_plan(
            batch,
            project_root,
            job_id,
            executor=executor,
            max_batch_videos=max_batch_videos,
        )

    result_jobs: List[Dict[str, object]] = []
    original_v001: Dict[str, object]
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": "SEQUENTIAL_RETRY_WAITING_FOR_V001",
            "updated_at": utc_now(),
            "ordered_retry_jobs": ordered_retry_ids,
            "mode": "wait-terminal-before-next",
        },
    )

    allow_parallel_vip_lane = (
        authorization.get("original_v001_policy")
        == "allow-vip-submit-while-standard-active"
    )
    first_manifest = (
        project_root
        / "outputs"
        / "video-replacements"
        / f"{batch.name}-V001"
        / f"{batch.name}-V001-manifest.json"
    ).resolve()
    if allow_parallel_vip_lane:
        original_record = latest_job_task_record(
            project_root, batch.name, "V001"
        ) or {}
        original_v001 = {
            "status": "ACTIVE_OR_TERMINAL_NOT_USED_FOR_VIP_DELIVERY",
            "task_id": original_record.get("task_id"),
            "remote_status": original_record.get("gen_status"),
        }
    elif first_manifest.is_file():
        value = json.loads(first_manifest.read_text(encoding="utf-8"))
        original_v001 = {
            "status": "TERMINAL_SUCCESS_NOT_USED_FOR_VIP_DELIVERY",
            "task_id": value.get("task_id") if isinstance(value, dict) else None,
            "output_path": (
                value.get("final_output") if isinstance(value, dict) else None
            ),
        }
    else:
        try:
            manifest = execute_submission_plan(
                batch,
                project_root,
                "V001",
                loop_root=loop_root,
                executor=executor,
                minimum_free_bytes=minimum_free_bytes,
                max_batch_videos=max_batch_videos,
                daily_paid_limit=daily_paid_limit,
                task_timeout=task_timeout,
                paid_execution_authorized=True,
                payment_authorization_identity=authorization_identities.get("V001"),
            )
            original_v001 = {
                "status": "TERMINAL_SUCCESS_NOT_USED_FOR_VIP_DELIVERY",
                "task_id": manifest.get("task_id"),
                "output_path": manifest.get("final_output"),
            }
        except Exception as exc:
            record = latest_job_task_record(project_root, batch.name, "V001") or {}
            remote_status = str(record.get("gen_status", "")).casefold()
            if remote_status in ACTIVE_REMOTE_STATUSES:
                atomic_write_json(
                    batch / "loop-state.json",
                    {
                        "batch_id": batch.name,
                        "state": "SEQUENTIAL_RETRY_WAITING_FOR_V001",
                        "updated_at": utc_now(),
                        "task_id": record.get("task_id"),
                        "remote_status": remote_status,
                        "message": str(exc),
                    },
                )
                raise LoopError(
                    "原 V001 仍为活动任务；尚未创建任何 VIP 任务"
                ) from exc
            original_v001 = {
                "status": "TERMINAL_FAILURE",
                "task_id": record.get("task_id"),
                "blocker": str(exc),
            }

    for position, raw_job in enumerate(jobs, start=1):
        assert isinstance(raw_job, dict)
        job_id = str(raw_job["job_id"])
        retry_attempt = int(raw_job["retry_attempt"])
        atomic_write_json(
            batch / "loop-state.json",
            {
                "batch_id": batch.name,
                "state": "SEQUENTIAL_RETRY_RUNNING",
                "updated_at": utc_now(),
                "current_job": job_id,
                "retry_attempt": retry_attempt,
                "position": position,
                "total_retry_jobs": len(jobs),
                "mode": "wait-terminal-before-next",
            },
        )
        try:
            manifest = execute_submission_plan(
                batch,
                project_root,
                job_id,
                loop_root=loop_root,
                executor=executor,
                minimum_free_bytes=minimum_free_bytes,
                max_batch_videos=max_batch_videos,
                daily_paid_limit=daily_paid_limit,
                retry_attempt=retry_attempt,
                retry_authorization_manifest=authorization_path,
                task_timeout=task_timeout,
                paid_execution_authorized=True,
                payment_authorization_identity=authorization_identities.get(job_id),
            )
            result_jobs.append(
                {
                    "id": job_id,
                    "status": COMPLETED_STATUS,
                    "task_id": manifest.get("task_id"),
                    "output_path": manifest.get("final_output"),
                    "blocker": None,
                    "retry_attempt": retry_attempt,
                }
            )
        except Exception as exc:
            record = latest_job_task_record(
                project_root,
                batch.name,
                job_id,
                retry_attempt=retry_attempt,
                model_version=str(authorization["model_version"]),
            ) or {}
            remote_status = str(record.get("gen_status", "")).casefold()
            result_jobs.append(
                {
                    "id": job_id,
                    "status": "BLOCKED",
                    "task_id": record.get("task_id"),
                    "output_path": None,
                    "blocker": str(exc),
                    "retry_attempt": retry_attempt,
                }
            )
            failure_text = " ".join(
                (
                    str(exc),
                    str(record.get("fail_reason", "")),
                    str(record.get("submit_output_tail", "")),
                    str(record.get("query_output_tail", "")),
                )
            )
            if "ExceedConcurrencyLimit" in failure_text:
                atomic_write_json(
                    batch / "sequential-retry-result.json",
                    {
                        "batch_status": "VIP_CONCURRENCY_BLOCKED",
                        "updated_at": utc_now(),
                        "original_v001": original_v001,
                        "jobs": result_jobs,
                    },
                )
                atomic_write_json(
                    batch / "loop-state.json",
                    {
                        "batch_id": batch.name,
                        "state": "VIP_CONCURRENCY_BLOCKED",
                        "updated_at": utc_now(),
                        "current_job": job_id,
                        "task_id": record.get("task_id"),
                        "message": "VIP 通道仍受并发上限限制；未创建下一条任务。",
                    },
                )
                raise LoopError(
                    f"{job_id} VIP 通道返回 ExceedConcurrencyLimit；"
                    "未创建下一条任务"
                ) from exc
            if remote_status in ACTIVE_REMOTE_STATUSES:
                atomic_write_json(
                    batch / "sequential-retry-result.json",
                    {
                        "batch_status": "WAITING_ACTIVE_TASK",
                        "updated_at": utc_now(),
                        "jobs": result_jobs,
                    },
                )
                atomic_write_json(
                    batch / "loop-state.json",
                    {
                        "batch_id": batch.name,
                        "state": "SEQUENTIAL_RETRY_WAITING_ACTIVE_TASK",
                        "updated_at": utc_now(),
                        "current_job": job_id,
                        "task_id": record.get("task_id"),
                        "remote_status": remote_status,
                        "message": "未创建下一条任务。",
                    },
                )
                raise LoopError(
                    f"{job_id} 仍为活动任务；未创建下一条重试任务"
                ) from exc

    succeeded = sum(
        item["status"] == COMPLETED_STATUS for item in result_jobs
    )
    blocked = sum(item["status"] == "BLOCKED" for item in result_jobs)
    batch_status = (
        "PARTIAL"
        if succeeded and blocked
        else (COMPLETED_STATUS if succeeded else "BLOCKED")
    )
    result = {
        "batch_status": batch_status,
        "summary": (
            f"逐条串行重试完成：技术成功 {succeeded}，终态失败 {blocked}；"
            "每条均在上一条终态后提交。"
        ),
        "updated_at": utc_now(),
        "original_v001": original_v001,
        "jobs": result_jobs,
    }
    atomic_write_json(batch / "sequential-retry-result.json", result)
    atomic_write_json(
        batch / "loop-state.json",
        {
            "batch_id": batch.name,
            "state": result["batch_status"],
            "updated_at": utc_now(),
            "mode": "wait-terminal-before-next",
            "succeeded": succeeded,
            "blocked": blocked,
        },
    )
    target_state = "completed" if batch_status == COMPLETED_STATUS else "blocked"
    if batch.parent.name == target_state:
        return batch
    return move_batch(batch, loop_root, target_state)


def recover_running_batches(
    loop_root: Path, project_root: Path
) -> int:
    recovered = 0
    for batch in list(visible_children(loop_root / "running")):
        if not batch.is_dir():
            continue
        task_ids: Dict[str, str] = {}
        try:
            index = read_index(batch)
            for job in index["jobs"]:  # type: ignore[index]
                job_id = str(job["id"])
                task_id = recorded_task_id(project_root, batch.name, job_id)
                if task_id:
                    task_ids[job_id] = task_id
        except Exception:
            pass
        recovery = {
            "schema_version": 1,
            "batch_id": batch.name,
            "state": "RECOVERY_REQUIRED_NO_AUTOMATIC_SUBMISSION",
            "detected_at": utc_now(),
            "automatic_new_submission_allowed": False,
            "recorded_task_ids": task_ids,
            "evidence": {
                "codex_process_record": (batch / "codex-process.json").is_file(),
                "codex_result_record": (batch / "codex-result.json").is_file(),
                "submission_plan_count": len(
                    list(
                        (
                            project_root
                            / "outputs"
                            / "video-replacements"
                        ).glob(f"{batch.name}-V*/submission-plan.json")
                    )
                ),
            },
            "recovery_policy": (
                "先对账已有 task/submit ID；只允许 status、wait、download 或 cleanup。"
                "没有人工确认不得创建新付费任务。"
            ),
        }
        _secure_write_json(batch / "recovery-state.json", recovery)
        atomic_write_json(
            batch / "loop-state.json",
            {
                "batch_id": batch.name,
                "state": "BLOCKED_RECOVERY_REQUIRED",
                "updated_at": utc_now(),
                "automatic_new_submission_allowed": False,
            },
        )
        write_blocker(
            batch,
            "检测到上次 watcher 崩溃遗留的 running 批次",
            [
                "已 fail-closed；本次启动不会运行 Codex、上传或创建新任务。",
                "请依据 recovery-state.json 对账并人工恢复。",
            ],
        )
        try:
            move_batch(batch, loop_root, "blocked")
        except Exception as exc:
            print(f"RECOVERY_BLOCK_FAILED {batch}: {exc}", file=sys.stderr)
            continue
        recovered += 1
    return recovered


def scan_once(
    loop_root: Path,
    project_root: Path,
    stable_seconds: float,
    execute: bool,
    auto_ready: bool = False,
    observer: Optional[ObservationTracker] = None,
    max_batch_videos: int = DEFAULT_MAX_BATCH_VIDEOS,
    minimum_free_bytes: int = 0,
    daily_paid_limit: int = DEFAULT_DAILY_PAID_LIMIT,
) -> Dict[str, int]:
    ensure_layout(loop_root)
    observer = observer or ObservationTracker(loop_root)
    observed_at = observer.begin_scan()
    counts = {
        "prepared": 0,
        "auto_ready": 0,
        "ready": 0,
        "blocked": 0,
        "running_recovered": 0,
        "assembly_recovered": 0,
    }
    try:
        counts["running_recovered"] = recover_running_batches(
            loop_root, project_root
        )
        assembly_recovered, assembly_blocked = recover_stale_assembling_batches(
            loop_root, stable_seconds, observer, observed_at
        )
        counts["assembly_recovered"] = assembly_recovered
        counts["blocked"] += assembly_blocked
        try:
            collected = collect_loose_inbox_videos(
                loop_root,
                stable_seconds,
                observer,
                observed_at,
                max_batch_videos=max_batch_videos,
            )
        except Exception as exc:
            blocker_path = loop_root / "inbox" / "LOOSE-INPUT-BLOCKED.txt"
            message = f"散落视频未组批\n\n时间：{utc_now()}\n\n- {exc}\n"
            if not blocker_path.is_file() or blocker_path.read_text(encoding="utf-8") != message:
                atomic_write_text(blocker_path, message)
            print(f"LOOSE_INPUT_BLOCKED {exc}", file=sys.stderr)
            counts["blocked"] += 1
            collected = None
        if collected:
            print(f"COLLECTED {collected}", file=sys.stderr)
        for batch in list(visible_children(loop_root / "inbox")):
            if not batch.is_dir() or not batch_is_stable(
                batch, stable_seconds, observer, observed_at
            ):
                continue
            try:
                destination = prepare_inbox_batch(
                    batch,
                    loop_root,
                    max_batch_videos=max_batch_videos,
                    observer=observer,
                    observed_at=observed_at,
                )
                print(f"PREPARED {destination}", file=sys.stderr)
                counts["prepared"] += 1
            except Exception as exc:
                write_blocker(batch, "批次接入失败", [str(exc)])
                try:
                    destination = move_batch(batch, loop_root, "blocked")
                    print(f"BLOCKED {destination}: {exc}", file=sys.stderr)
                except Exception as move_exc:
                    print(f"BLOCK_FAILED {batch}: {move_exc}", file=sys.stderr)
                counts["blocked"] += 1

        if auto_ready:
            for batch in promote_auto_ready_batches(
                loop_root, stable_seconds, observer, observed_at
            ):
                print(f"AUTO_READY {batch}", file=sys.stderr)
                counts["auto_ready"] += 1

        executor: Optional[ExecutorSpec] = None
        for batch in list(visible_children(loop_root / "ready")):
            if not batch.is_dir() or not batch_is_stable(
                batch, stable_seconds, observer, observed_at
            ):
                continue
            if executor is None:
                try:
                    executor = find_replacement_executor(project_root)
                except Exception:
                    executor = None
            destination = process_ready_batch(
                batch,
                loop_root,
                project_root,
                execute,
                executor=executor,
                minimum_free_bytes=minimum_free_bytes,
                max_batch_videos=max_batch_videos,
                daily_paid_limit=daily_paid_limit,
            )
            counts["ready"] += 1
            print(
                f"MOVED {destination}" if destination else f"READY_DRY_RUN {batch}",
                file=sys.stderr,
            )
        return counts
    finally:
        observer.save()


def locate_batch(loop_root: Path, name_or_path: str) -> Path:
    direct = Path(name_or_path).expanduser().resolve()
    if direct.is_dir():
        return direct
    for state in STATE_NAMES:
        candidate = loop_root / state / name_or_path
        if candidate.is_dir():
            return candidate
    raise LoopError(f"找不到批次：{name_or_path}")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是大于或等于 0 的整数")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是大于或等于 0 的数")
    return parsed


def _environment_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LoopError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise LoopError(f"环境变量 {name} 不得小于 {minimum}")
    return value


def _environment_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise LoopError(f"环境变量 {name} 必须是数字") from exc
    if value < minimum:
        raise LoopError(f"环境变量 {name} 不得小于 {minimum}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="视频替换批次文件夹 Loop")
    parser.add_argument("--root", type=Path, default=DEFAULT_LOOP_ROOT)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--max-batch-videos",
        type=_positive_int,
        default=_environment_int(
            "VIDEO_LOOP_MAX_BATCH_VIDEOS", DEFAULT_MAX_BATCH_VIDEOS, minimum=1
        ),
        help="单批视频上限（默认 15）",
    )
    parser.add_argument(
        "--min-free-gib",
        type=_nonnegative_float,
        default=_environment_float(
            "VIDEO_LOOP_MIN_FREE_GIB", DEFAULT_MIN_FREE_GIB, minimum=0.0
        ),
        help="提交前要求的最小可用磁盘空间 GiB（默认 10）",
    )
    parser.add_argument(
        "--daily-paid-limit",
        type=_positive_int,
        default=_environment_int(
            "VIDEO_LOOP_DAILY_PAID_LIMIT", DEFAULT_DAILY_PAID_LIMIT, minimum=1
        ),
        help="父层每日付费提交上限（默认 1）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建状态目录")
    commands.add_parser("cleanup-node-runs", help=argparse.SUPPRESS)
    once = commands.add_parser("once", help="扫描一次 inbox/ 和 ready/")
    once.add_argument("--stable-seconds", type=float, default=5.0)
    watch = commands.add_parser("watch", help="持续轮询状态目录")
    watch.add_argument("--interval", type=float, default=5.0)
    watch.add_argument("--stable-seconds", type=float, default=5.0)
    check = commands.add_parser("check", help="检查一个批次的需求覆盖")
    check.add_argument("batch")
    prepare = commands.add_parser(
        "prepare", help="在 PAUSE 下完成本地 Gate，不上传或创建付费任务"
    )
    prepare.add_argument("batch")
    inspect_flow = commands.add_parser(
        "inspect-flow", help="校验并冻结 JavaScript 流式批次输入"
    )
    inspect_flow.add_argument("batch")
    inspect_flow.add_argument("--preparation-only", action="store_true")
    preflight_submission = commands.add_parser(
        "preflight-submission",
        help="在创建任何付费授权前整批验证冻结准备结果和提交计划",
    )
    preflight_submission.add_argument("batch")
    prepare_job = commands.add_parser(
        "prepare-job", help="为一个 V 编号运行隔离的 Video-to-Prompt 准备链"
    )
    prepare_job.add_argument("batch")
    prepare_job.add_argument("job_id")
    retry_prepare_job = commands.add_parser(
        "retry-prepare-job",
        help="事务式归档并重跑一个失败或提交计划失效的本地准备 Job",
    )
    retry_prepare_job.add_argument("batch")
    retry_prepare_job.add_argument("job_id")
    submit_job = commands.add_parser(
        "submit-job", help="提交或恢复一个已准备的 V 编号"
    )
    submit_job.add_argument("batch")
    submit_job.add_argument("job_id")
    submit_job.add_argument(
        "--task-timeout",
        type=_positive_int,
        default=86400,
    )
    finalize_preparation = commands.add_parser(
        "finalize-preparation-flow", help="汇总并行本地准备结果"
    )
    finalize_preparation.add_argument("batch")
    finalize_flow = commands.add_parser(
        "finalize-flow", help="汇总流式提交结果并移动批次"
    )
    finalize_flow.add_argument("batch")
    submit_prepared = commands.add_parser(
        "submit-prepared",
        help="在明确付费授权后串行提交已完成 preview 的批次",
    )
    submit_prepared.add_argument("batch")
    retry_sequential = commands.add_parser(
        "retry-sequential",
        help="等待当前任务终态后，按外置用户授权逐条创建一次付费重试",
    )
    retry_sequential.add_argument("batch")
    retry_sequential.add_argument(
        "--retry-authorization-manifest",
        type=Path,
        required=True,
    )
    retry_sequential.add_argument(
        "--task-timeout",
        type=_positive_int,
        default=86400,
        help="每条任务等待终态的最长秒数（默认 86400）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.name != "nt":
        os.umask(0o077)
    args = build_parser().parse_args(argv)
    loop_root = args.root.expanduser().resolve()
    project_root = args.project_root.expanduser().resolve()
    ensure_layout(loop_root)
    try:
        if args.command == "init":
            print(loop_root)
            for state in STATE_NAMES:
                print(loop_root / state)
            return 0
        if args.command == "check":
            batch = locate_batch(loop_root, args.batch)
            errors, coverage = validate_requirements(batch)
            if not errors:
                try:
                    index = verify_batch_index(
                        batch, max_batch_videos=args.max_batch_videos
                    )
                    reference_index = reference_index_for_validation(
                        batch, persist_if_unfrozen=False
                    )
                    binding_payload = _job_bindings_payload(batch)
                    if (
                        binding_payload.get("schema_version") != 3
                        and not _has_frozen_legacy_flow(batch)
                    ):
                        raise LoopError(
                            f"新批次的 {JOB_BINDINGS_FILENAME} 必须使用 schema_version 3"
                        )
                    bindings = validate_job_bindings(
                        batch, index, reference_index
                    )
                    validate_requirement_binding_contract(batch, bindings)
                except LoopError as exc:
                    errors.append(str(exc))
            print(json.dumps({"errors": errors, "coverage": coverage}, ensure_ascii=False, indent=2))
            return 1 if errors else 0
        if args.command == "cleanup-node-runs":
            result = cleanup_orphaned_node_runs(loop_root, project_root)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "prepare":
            result = prepare_batch_without_submission(
                locate_batch(loop_root, args.batch),
                loop_root,
                project_root,
                minimum_free_bytes=int(args.min_free_gib * 1024**3),
                max_batch_videos=args.max_batch_videos,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect-flow":
            with exclusive_loop_lock(loop_root):
                result = inspect_streaming_flow(
                    locate_batch(loop_root, args.batch),
                    loop_root,
                    project_root,
                    preparation_only=args.preparation_only,
                    minimum_free_bytes=int(args.min_free_gib * 1024**3),
                    max_batch_videos=args.max_batch_videos,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "preflight-submission":
            result = preflight_streaming_submission(
                locate_batch(loop_root, args.batch),
                project_root,
                max_batch_videos=args.max_batch_videos,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "prepare-job":
            result = prepare_streaming_job(
                locate_batch(loop_root, args.batch),
                loop_root,
                project_root,
                args.job_id,
                max_batch_videos=args.max_batch_videos,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "retry-prepare-job":
            with exclusive_loop_lock(loop_root):
                result = retry_streaming_preparation_job(
                    locate_batch(loop_root, args.batch),
                    loop_root,
                    project_root,
                    args.job_id,
                    max_batch_videos=args.max_batch_videos,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "submit-job":
            result = submit_streaming_job(
                locate_batch(loop_root, args.batch),
                loop_root,
                project_root,
                args.job_id,
                minimum_free_bytes=int(args.min_free_gib * 1024**3),
                max_batch_videos=args.max_batch_videos,
                daily_paid_limit=args.daily_paid_limit,
                task_timeout=args.task_timeout,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "finalize-preparation-flow":
            with exclusive_loop_lock(loop_root):
                result = finalize_streaming_preparation(
                    locate_batch(loop_root, args.batch),
                    project_root,
                    max_batch_videos=args.max_batch_videos,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "finalize-flow":
            with exclusive_loop_lock(loop_root):
                destination = finalize_streaming_flow(
                    locate_batch(loop_root, args.batch),
                    loop_root,
                    project_root,
                    max_batch_videos=args.max_batch_videos,
                )
            print(json.dumps({"destination": str(destination)}, ensure_ascii=False))
            return 0
        if args.command == "submit-prepared":
            with exclusive_loop_lock(loop_root):
                destination = submit_prepared_batch(
                    locate_batch(loop_root, args.batch),
                    loop_root,
                    project_root,
                    minimum_free_bytes=int(args.min_free_gib * 1024**3),
                    max_batch_videos=args.max_batch_videos,
                    daily_paid_limit=args.daily_paid_limit,
                )
            print(destination)
            return 0
        if args.command == "retry-sequential":
            with exclusive_loop_lock(loop_root):
                destination = retry_failed_jobs_sequentially(
                    locate_batch(loop_root, args.batch),
                    loop_root,
                    project_root,
                    args.retry_authorization_manifest,
                    minimum_free_bytes=int(args.min_free_gib * 1024**3),
                    max_batch_videos=args.max_batch_videos,
                    daily_paid_limit=args.daily_paid_limit,
                    task_timeout=args.task_timeout,
                )
            print(destination)
            return 0
        if args.command == "once":
            observer = ObservationTracker(loop_root)
            with exclusive_loop_lock(loop_root):
                counts = scan_once(
                    loop_root,
                    project_root,
                    args.stable_seconds,
                    False,
                    False,
                    observer=observer,
                    max_batch_videos=args.max_batch_videos,
                    minimum_free_bytes=int(args.min_free_gib * 1024**3),
                    daily_paid_limit=args.daily_paid_limit,
                )
            print(json.dumps(counts, ensure_ascii=False))
            return 0
        if args.command == "watch":
            print(f"SHADOW_MODE root={loop_root}")
            observer = ObservationTracker(loop_root)
            with exclusive_loop_lock(loop_root):
                while True:
                    scan_once(
                        loop_root,
                        project_root,
                        args.stable_seconds,
                        False,
                        False,
                        observer=observer,
                        max_batch_videos=args.max_batch_videos,
                        minimum_free_bytes=int(args.min_free_gib * 1024**3),
                        daily_paid_limit=args.daily_paid_limit,
                    )
                    time.sleep(max(args.interval, 1.0))
    except KeyboardInterrupt:
        print("STOPPED")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

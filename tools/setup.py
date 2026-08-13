#!/usr/bin/env python3
"""Prove and persist a live, secret-free setup for this clone."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor
from backend_profiles import BackendProfileError, get_backend_profile
from codex_node_home import (
    CodexNodeHomeError,
    NODE_HOME_DIRECTORY,
    ensure_node_home,
    locked_node_home,
    validate_file_auth,
    validate_node_home,
    validate_node_home_structure,
)
from codex_artifact import CodexArtifactError, verify_windows_codex
from state_paths import StatePathError, resolve_state_root


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_ROOT = REPO_ROOT / ".video-replacer"
SETUP_FILE = SETUP_ROOT / "setup.json"
SCHEMA_VERSION = 3
SETUP_CONTRACT_VERSION = 1
SUPPORTED_WINDOWS_MACHINE = "AMD64"

# Public first-run READY is intentionally durable: these profiles use the
# backend's own credential store.  The Ark adapter remains available for
# operator-managed development, but environment-only secrets cannot satisfy a
# cross-session installation contract.
PROFILE_BACKENDS = {
    "dreamina_cli_seedance_2_5": "dreamina",
    "dreamina_cli_seedance_2_0": "dreamina",
}
BASE_REQUIRED_CHECKS = {
    "python",
    "node",
    "ffmpeg",
    "codex-cli",
    "codex-interface",
    "codex-auth",
    "codex-model",
    "codex-wire",
    "codex-file-auth-wire",
    "runtime-layout",
    "external-state",
    "mosaic",
}
BACKEND_REQUIRED_CHECKS = {
    "dreamina": {
        "dreamina-cli",
        "dreamina-interface",
        "dreamina-model",
        "dreamina-login",
    },
}
CAPABILITIES = ["mosaic_required", "video_replacement"]
TOOL_KEYS = ("python", "node", "ffmpeg", "codex", "dreamina")
RUNTIME_PATH_KEYS = ("codex_node_home", "state_dir", "mosaic_cache_dir")
SETUP_CONTRACT_FILES = (
    Path("install"),
    Path("install.cmd"),
    Path("video-replacer"),
    Path("video-replacer.cmd"),
    Path("video-replacer-test.cmd"),
    Path("requirements-core.lock.txt"),
    Path("tools/backend_profiles.py"),
    Path("tools/bootstrap.py"),
    Path("tools/codex_node_home.py"),
    Path("tools/codex_artifact.py"),
    Path("tools/codex_wire_attestation.py"),
    Path("tools/doctor.py"),
    Path("tools/dreamina-install-manifest.json"),
    Path("tools/dreamina-version.json"),
    Path("tools/install_dreamina.py"),
    Path("tools/setup.py"),
    Path("tools/state_paths.py"),
    Path("tools/dreamina_video.py"),
    Path("tools/upload_preparation.py"),
    Path("tools/windows_launcher.py"),
    Path("tools/video_batch_loop.py"),
    Path("tools/video_batch_node_result.schema.json"),
    Path("tools/video-to-prompt-model-catalog.json"),
    Path("tools/video_batch_orchestrator.mjs"),
    Path("tools/privacy/face_mosaic.py"),
    Path("tools/privacy/openscrub-requirements.lock.txt"),
    Path("tools/video-replacement-node-contracts/video-to-prompt.md"),
)
EXPECTED_RECORD_FIELDS = {
    "schema_version",
    "setup_contract_version",
    "backend_profile",
    "backend",
    "profile_constraints_sha256",
    "setup_contract_sha256",
    "verified_at",
    "doctor_online",
    "capabilities",
    "runtime_paths",
    "tool_paths",
    "tool_identities",
    "checks",
}


class SetupError(RuntimeError):
    pass


def expected_state_root() -> Path:
    try:
        return resolve_state_root()
    except StatePathError as exc:
        raise SetupError(str(exc)) from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_contract_digest() -> str:
    digest = hashlib.sha256()
    digest.update(f"video-replacer-setup-v{SETUP_CONTRACT_VERSION}\0".encode())
    digest.update(f"codex-model={doctor.VIDEO_TO_PROMPT_MODEL}\0".encode())
    for relative in SETUP_CONTRACT_FILES:
        path = REPO_ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise SetupError(f"setup contract file is missing or unsafe: {relative}")
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def secure_setup_root() -> None:
    if doctor.is_link_like(SETUP_ROOT):
        raise SetupError(f"refusing linked setup directory: {SETUP_ROOT}")
    SETUP_ROOT.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        SETUP_ROOT.chmod(stat.S_IRWXU)


def atomic_write_record(payload: Mapping[str, object]) -> None:
    secure_setup_root()
    temporary = SETUP_FILE.with_name(f".{SETUP_FILE.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SETUP_FILE)
        if os.name != "nt":
            SETUP_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temporary.unlink(missing_ok=True)


def read_record() -> Dict[str, object]:
    if not SETUP_FILE.is_file() or SETUP_FILE.is_symlink():
        raise SetupError(
            "first-run setup is incomplete; ask the repository Agent to configure it"
        )
    if os.name != "nt" and SETUP_FILE.stat().st_mode & 0o077:
        raise SetupError("setup record permissions are unsafe; rerun Agent setup")
    try:
        payload = json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"invalid setup record: {SETUP_FILE}") from exc
    if not isinstance(payload, dict):
        raise SetupError("setup record must be a JSON object")
    return payload


def parse_verified_at(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise SetupError("setup record has no valid verification timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SetupError("setup record has no valid verification timestamp") from exc
    if parsed.tzinfo is None:
        raise SetupError("setup verification timestamp must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    if normalized > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise SetupError("setup verification timestamp is in the future")
    return normalized


def _required_names(backend: str) -> set[str]:
    return BASE_REQUIRED_CHECKS | BACKEND_REQUIRED_CHECKS[backend]


def _check_statuses(checks: object, backend: str) -> Dict[str, str]:
    if not isinstance(checks, list):
        raise SetupError("setup record contains invalid Doctor checks")
    statuses: Dict[str, str] = {}
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "status"}:
            raise SetupError("setup record contains invalid Doctor checks")
        name = item.get("name")
        status = item.get("status")
        if not isinstance(name, str) or not isinstance(status, str) or name in statuses:
            raise SetupError("setup record contains duplicate or invalid Doctor checks")
        statuses[name] = status
    required = _required_names(backend)
    if set(statuses) != required:
        missing = sorted(required - set(statuses))
        unexpected = sorted(set(statuses) - required)
        detail = ", ".join(missing + unexpected)
        raise SetupError(f"setup record check set changed: {detail}")
    not_passed = sorted(name for name in required if statuses.get(name) != "pass")
    if not_passed:
        raise SetupError(
            "setup record lacks passing required checks: " + ", ".join(not_passed)
        )
    return statuses


def _absolute_path_map(
    value: object, expected: Sequence[str], label: str
) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise SetupError(f"setup record {label} fields are invalid")
    resolved: Dict[str, str] = {}
    for name in expected:
        raw = value.get(name)
        if not isinstance(raw, str) or not raw or "\n" in raw or "\r" in raw:
            raise SetupError(f"setup record {label}.{name} is invalid")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise SetupError(f"setup record {label}.{name} must be absolute")
        resolved[name] = str(path)
    return resolved


def repository_python() -> Path:
    candidate = REPO_ROOT / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python3"
    )
    if not candidate.is_file():
        raise SetupError("repository Python runtime is missing; rerun Agent installation")
    try:
        same_runtime = os.path.samefile(candidate, sys.executable)
    except OSError:
        same_runtime = False
    if not same_runtime:
        raise SetupError("setup verification must run with the repository .venv")
    return candidate.absolute()


def require_supported_platform() -> None:
    """Fail closed before READY on unsupported native architectures."""

    if os.name != "nt":
        return
    import platform

    machine = platform.machine().upper()
    if machine not in {"AMD64", "X86_64"}:
        raise SetupError(
            f"native Windows READY currently requires x64; detected {machine or 'unknown'}"
        )


def capture_runtime_paths() -> Dict[str, str]:
    state_dir = expected_state_root()
    codex_node_home = state_dir / NODE_HOME_DIRECTORY
    mosaic = doctor.mosaic_module()
    mosaic_cache_dir = doctor.mosaic_cache_path()
    if (
        not state_dir.is_dir()
        or not doctor.writable_directory(state_dir)
        or doctor.path_contains_link_like(state_dir)
        or not doctor.local_windows_path(state_dir)
    ):
        raise SetupError("external workflow state is not writable")
    if doctor.inside(state_dir, REPO_ROOT):
        raise SetupError("external workflow state must remain outside the repository")
    if doctor.temporary_path(state_dir):
        raise SetupError("external workflow state must not use a temporary directory")
    try:
        codex_node_home = validate_node_home(
            codex_node_home,
            repo_root=REPO_ROOT,
            require_auth=True,
        )
    except CodexNodeHomeError as exc:
        raise SetupError(str(exc)) from exc
    if (
        not mosaic_cache_dir.is_dir()
        or mosaic.path_contains_symlink(mosaic_cache_dir)
        or doctor.inside(mosaic_cache_dir, REPO_ROOT)
        or doctor.temporary_path(mosaic_cache_dir)
    ):
        raise SetupError("mosaic cache must be installed in a stable external directory")
    return {
        "codex_node_home": str(codex_node_home),
        "state_dir": str(state_dir.resolve()),
        "mosaic_cache_dir": str(mosaic_cache_dir),
    }


def capture_tool_paths() -> Dict[str, str]:
    local_dreamina = REPO_ROOT / ".video-replacer" / "bin" / (
        "dreamina.exe" if os.name == "nt" else "dreamina"
    )
    candidates = {
        "python": repository_python(),
        "node": doctor.command_path("node", os.getenv("VIDEO_REPLACER_NODE", "")),
        "ffmpeg": doctor.ffmpeg_path(),
        "codex": doctor.codex_path(),
        "dreamina": local_dreamina if local_dreamina.is_file() else None,
    }
    missing = sorted(name for name, path in candidates.items() if path is None)
    if missing:
        raise SetupError("verified tools disappeared: " + ", ".join(missing))
    return {
        name: str(path.absolute() if name == "python" else path.resolve())
        for name, path in candidates.items()
        if path is not None
    }


def verified_dreamina_artifact(path_text: str) -> Dict[str, str]:
    try:
        from install_dreamina import artifact_for, platform_key, read_manifest
    except ImportError as exc:
        raise SetupError("Dreamina installer contract cannot be loaded") from exc
    expected_path = REPO_ROOT / ".video-replacer" / "bin" / (
        "dreamina.exe" if os.name == "nt" else "dreamina"
    )
    path = Path(path_text)
    try:
        same_path = os.path.samefile(path, expected_path)
    except OSError:
        same_path = False
    if not same_path or doctor.is_link_like(path):
        raise SetupError(
            "durable READY requires the project-local reviewed Dreamina binary"
        )
    artifact = artifact_for(read_manifest(), platform_key())
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != artifact["sha256"]:
        raise SetupError(
            "project-local Dreamina binary does not match the reviewed SHA-256"
        )
    return artifact


def _version_command(name: str, path: str) -> list[str]:
    arguments = {
        "python": ["--version"],
        "node": ["--version"],
        "ffmpeg": ["-version"],
        "codex": ["--version"],
        "dreamina": ["version"],
    }
    return [path, *arguments[name]]


def normalized_version(output: str) -> str:
    text = output.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        version = payload.get("version")
        commit = payload.get("commit")
        if isinstance(version, str) and version:
            return version + (f" ({commit})" if isinstance(commit, str) and commit else "")
    return text.splitlines()[0] if text else ""


def tool_identity(
    name: str, path_text: str, runtime_paths: Mapping[str, str]
) -> Dict[str, object]:
    path = Path(path_text)
    if not path.is_file():
        raise SetupError(f"configured {name} tool is missing")
    if name == "dreamina":
        verified_dreamina_artifact(path_text)
    codex_artifact = None
    if name == "codex" and os.name == "nt":
        try:
            codex_artifact = verify_windows_codex(path)
            path = Path(str(codex_artifact["binary"]))
        except CodexArtifactError as exc:
            raise SetupError(
                f"official Windows Codex provenance failed before execution: {exc}"
            ) from exc
    environment = doctor.safe_environment(extra=("CODEX_HOME", "DREAMINA_BINARY"))
    environment["CODEX_HOME"] = runtime_paths["codex_node_home"]
    environment["DREAMINA_BINARY"] = path_text if name == "dreamina" else environment.get(
        "DREAMINA_BINARY", ""
    )
    result = subprocess.run(
        _version_command(name, str(path)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        raise SetupError(f"configured {name} tool failed its version check")
    version = doctor.redact_text(normalized_version(result.stdout))
    if not version:
        raise SetupError(f"configured {name} tool returned no version")
    metadata = path.stat()
    identity: Dict[str, object] = {
        "resolved_path": str(path.resolve()),
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "version": version[:240],
    }
    if codex_artifact is not None:
        artifact_version = str(codex_artifact["version"])
        if artifact_version not in version:
            raise SetupError(
                "verified Windows Codex package version does not match --version"
            )
        identity["official_sha256"] = str(codex_artifact["sha256"])
        identity["official_release_tag"] = str(
            codex_artifact["release"]["tag_name"]
        )
    return identity


def capture_tool_identities(
    tool_paths: Mapping[str, str], runtime_paths: Mapping[str, str]
) -> Dict[str, object]:
    return {
        name: tool_identity(name, tool_paths[name], runtime_paths)
        for name in TOOL_KEYS
    }


def validate_tool_identities(
    raw: object,
    tool_paths: Mapping[str, str],
    runtime_paths: Mapping[str, str],
) -> None:
    if not isinstance(raw, dict) or set(raw) != set(TOOL_KEYS):
        raise SetupError("setup record tool identity fields are invalid")
    for name in TOOL_KEYS:
        expected = raw.get(name)
        expected_fields = {
            "resolved_path",
            "size",
            "mtime_ns",
            "version",
        }
        if name == "codex" and os.name == "nt":
            expected_fields.update({"official_sha256", "official_release_tag"})
        if not isinstance(expected, dict) or set(expected) != expected_fields:
            raise SetupError(f"setup record {name} identity is invalid")
        current = tool_identity(name, tool_paths[name], runtime_paths)
        if current != expected:
            raise SetupError(f"configured {name} tool changed; rerun Agent setup")


def runtime_environment(
    payload: Mapping[str, object], source: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    tool_paths = _absolute_path_map(payload.get("tool_paths"), TOOL_KEYS, "tool_paths")
    runtime_paths = _absolute_path_map(
        payload.get("runtime_paths"), RUNTIME_PATH_KEYS, "runtime_paths"
    )
    source_env = os.environ if source is None else source
    nonsecret_settings = {
        "VIDEO_LOOP_DAILY_PAID_LIMIT",
        "VIDEO_LOOP_GENERATION_CONCURRENCY",
        "VIDEO_LOOP_MAX_BATCH_VIDEOS",
        "VIDEO_LOOP_MIN_FREE_GIB",
        "VIDEO_LOOP_PREPARATION_CONCURRENCY",
        "VIDEO_REPLACER_CACHE_DIR",
    }
    environment = doctor.safe_environment(source_env, extra=nonsecret_settings)
    environment.update(
        {
            "VIDEO_REPLACER_PYTHON": tool_paths["python"],
            "VIDEO_REPLACER_NODE": tool_paths["node"],
            "FFMPEG": tool_paths["ffmpeg"],
            "CODEX_BINARY": tool_paths["codex"],
            "DREAMINA_BINARY": tool_paths["dreamina"],
            "CODEX_HOME": runtime_paths["codex_node_home"],
            "VIDEO_REPLACER_STATE_DIR": runtime_paths["state_dir"],
            "VIDEO_REPLACER_CACHE_DIR": runtime_paths["mosaic_cache_dir"],
            "VIDEO_REPLACER_ACTIVE_PROFILE": str(payload["backend_profile"]),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


@contextmanager
def temporary_environment(environment: Mapping[str, str]) -> Iterator[None]:
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _require_live_checks(payload: Mapping[str, object], backend: str) -> None:
    environment = runtime_environment(payload)
    profile = get_backend_profile(payload["backend_profile"])
    with temporary_environment(environment):
        checks = doctor.collect_checks(
            backend,
            online=True,
            check_mosaic_enabled=True,
            dreamina_model=profile.resolved_model_version(),
        )
    statuses = {item.name: item.status for item in checks}
    failed = sorted(
        name for name in _required_names(backend) if statuses.get(name) != "pass"
    )
    if failed:
        raise SetupError(
            "live setup checks failed; Agent repair required: " + ", ".join(failed)
        )


def validate_record(
    payload: Mapping[str, object], *, live: bool = False
) -> Dict[str, object]:
    if set(payload) != EXPECTED_RECORD_FIELDS:
        raise SetupError("setup record fields are unsupported; rerun Agent setup")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SetupError("setup record schema is unsupported; rerun Agent setup")
    if payload.get("setup_contract_version") != SETUP_CONTRACT_VERSION:
        raise SetupError("setup contract version changed; rerun Agent setup")
    profile_id = str(payload.get("backend_profile") or "")
    backend = PROFILE_BACKENDS.get(profile_id)
    if not backend or payload.get("backend") != backend:
        raise SetupError("setup backend is not supported for durable first-run READY")
    try:
        profile = get_backend_profile(profile_id)
    except BackendProfileError as exc:
        raise SetupError(str(exc)) from exc
    if payload.get("profile_constraints_sha256") != profile.constraints_digest:
        raise SetupError("backend profile changed after setup; rerun Agent setup")
    if payload.get("setup_contract_sha256") != setup_contract_digest():
        raise SetupError("repository setup contract changed; rerun Agent setup")
    parse_verified_at(payload.get("verified_at"))
    if payload.get("doctor_online") is not True:
        raise SetupError("backend has not passed online Doctor verification")
    if payload.get("capabilities") != CAPABILITIES:
        raise SetupError("setup capabilities changed; rerun Agent setup")
    _check_statuses(payload.get("checks"), backend)
    tool_paths = _absolute_path_map(payload.get("tool_paths"), TOOL_KEYS, "tool_paths")
    runtime_paths = _absolute_path_map(
        payload.get("runtime_paths"), RUNTIME_PATH_KEYS, "runtime_paths"
    )
    state_dir = Path(runtime_paths["state_dir"])
    if os.name == "nt":
        expected_state = expected_state_root()
        try:
            state_matches_contract = state_dir.resolve() == expected_state.resolve()
        except OSError:
            state_matches_contract = False
    else:
        # Preserve the existing POSIX record semantics for an absolute,
        # operator-managed override that may not be exported in every session.
        state_matches_contract = True
    if (
        not state_matches_contract
        or not state_dir.is_dir()
        or doctor.path_contains_link_like(state_dir)
        or not doctor.writable_directory(state_dir)
        or not doctor.local_windows_path(state_dir)
        or doctor.inside(state_dir, REPO_ROOT)
        or doctor.temporary_path(state_dir)
    ):
        raise SetupError("configured external state path is unavailable or unsafe")
    codex_node_home = Path(runtime_paths["codex_node_home"])
    try:
        validate_node_home(
            codex_node_home,
            repo_root=REPO_ROOT,
            require_auth=True,
        )
    except CodexNodeHomeError as exc:
        raise SetupError(str(exc)) from exc
    mosaic_cache_dir = Path(runtime_paths["mosaic_cache_dir"])
    mosaic = doctor.mosaic_module()
    if (
        not mosaic_cache_dir.is_dir()
        or mosaic.path_contains_symlink(mosaic_cache_dir)
        or doctor.inside(mosaic_cache_dir, REPO_ROOT)
        or doctor.temporary_path(mosaic_cache_dir)
    ):
        raise SetupError("configured mosaic cache path is unavailable or unsafe")
    validate_tool_identities(payload.get("tool_identities"), tool_paths, runtime_paths)
    if live:
        _require_live_checks(payload, backend)
    return dict(payload)


def readiness_report(*, live: bool = True) -> Dict[str, object]:
    try:
        record = validate_record(read_record(), live=live)
    except SetupError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ready": False,
            "reason": str(exc),
            "setup_file": str(SETUP_FILE),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": True,
        "backend_profile": record["backend_profile"],
        "backend": record["backend"],
        "capabilities": record["capabilities"],
        "verified_at": record["verified_at"],
        "live_checks": "pass" if live else "not-run",
        "setup_file": str(SETUP_FILE),
    }


def login_dreamina() -> Dict[str, object]:
    """Run the reviewed local provider's protected login flow."""

    repository_python()
    binary = REPO_ROOT / ".video-replacer" / "bin" / (
        "dreamina.exe" if os.name == "nt" else "dreamina"
    )
    if not binary.is_file():
        raise SetupError("reviewed project-local Dreamina CLI is not installed")
    artifact = verified_dreamina_artifact(str(binary))
    environment = doctor.safe_environment(extra=("DREAMINA_BINARY",))
    environment["DREAMINA_BINARY"] = str(binary)
    completed = subprocess.run(
        [str(binary), "login"],
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise SetupError("Dreamina protected login did not complete")
    account = subprocess.run(
        [str(binary), "user_credit"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
        env=environment,
    )
    from dreamina_video import extract_json_object

    account_payload = extract_json_object(account.stdout)
    if account.returncode != 0 or "total_credit" not in account_payload:
        raise SetupError("Dreamina login completed but the account check failed")
    return {
        "logged_in": True,
        "binary": str(binary),
        "artifact_sha256": artifact["sha256"],
        "paid_task_created": False,
    }


def login_codex_node() -> Dict[str, object]:
    """Create the stable prompt-node file-auth context through protected login."""

    repository_python()
    require_supported_platform()
    binary = doctor.codex_path()
    if binary is None or not doctor.supported_codex_binary(binary):
        raise SetupError(
            "supported Codex CLI is missing; Windows requires the official native codex.exe"
        )
    if os.name == "nt":
        try:
            verified_codex = verify_windows_codex(binary)
            binary = Path(str(verified_codex["binary"]))
        except CodexArtifactError as exc:
            raise SetupError(
                f"official Windows Codex provenance failed before execution: {exc}"
            ) from exc
    state_dir = expected_state_root()
    if (
        not state_dir.is_dir()
        or not doctor.writable_directory(state_dir)
        or doctor.path_contains_link_like(state_dir)
        or not doctor.local_windows_path(state_dir)
        or doctor.inside(state_dir, REPO_ROOT)
        or doctor.temporary_path(state_dir)
    ):
        raise SetupError("external workflow state is unavailable or unsafe")
    requested_home = Path(
        os.path.abspath(os.fspath(state_dir / NODE_HOME_DIRECTORY))
    )
    try:
        home = ensure_node_home(requested_home, repo_root=REPO_ROOT)
    except CodexNodeHomeError as initial_error:
        auth = requested_home / "auth.json"
        try:
            # Establish the path/type/ACL boundary before creating the lock.
            # This structural check deliberately does not parse auth content,
            # so malformed credentials can still be repaired safely.
            home = validate_node_home_structure(
                requested_home,
                repo_root=REPO_ROOT,
                require_auth=True,
            )
            with locked_node_home(home, timeout_seconds=120):
                expected = Path(
                    os.path.abspath(os.fspath(state_dir / NODE_HOME_DIRECTORY))
                )
                if requested_home != expected:
                    raise initial_error
                home = validate_node_home_structure(
                    home,
                    repo_root=REPO_ROOT,
                    require_auth=True,
                )
                auth = home / "auth.json"
                before = auth.lstat()
                try:
                    validate_file_auth(auth)
                except CodexNodeHomeError:
                    after = auth.lstat()
                    before_identity = (
                        before.st_dev,
                        before.st_ino,
                        before.st_mode,
                        before.st_size,
                        before.st_mtime_ns,
                    )
                    after_identity = (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_size,
                        after.st_mtime_ns,
                    )
                    if before_identity != after_identity or not stat.S_ISREG(
                        after.st_mode
                    ):
                        raise initial_error
                    final = auth.lstat()
                    final_identity = (
                        final.st_dev,
                        final.st_ino,
                        final.st_mode,
                        final.st_size,
                        final.st_mtime_ns,
                    )
                    if final_identity != after_identity or not stat.S_ISREG(
                        final.st_mode
                    ):
                        raise initial_error
                    auth.unlink()
                else:
                    raise initial_error
                home = ensure_node_home(requested_home, repo_root=REPO_ROOT)
        except (CodexNodeHomeError, OSError) as exc:
            raise SetupError(str(exc)) from exc
    environment = doctor.safe_environment(extra=("CODEX_HOME",))
    environment["CODEX_HOME"] = str(home)
    command = [
        str(binary),
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--device-auth",
    ]
    try:
        with locked_node_home(home, timeout_seconds=120):
            home = ensure_node_home(home, repo_root=REPO_ROOT)
            completed = subprocess.run(command, check=False, env=environment)
            if completed.returncode != 0:
                raise SetupError("Codex prompt-node protected login did not complete")
            auth = home / "auth.json"
            if auth.is_file() and os.name != "nt":
                auth.chmod(stat.S_IRUSR | stat.S_IWUSR)
            validate_node_home(
                home,
                repo_root=REPO_ROOT,
                require_auth=True,
            )
            status = subprocess.run(
                [
                    str(binary),
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "login",
                    "status",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
                env=environment,
            )
    except CodexNodeHomeError as exc:
        raise SetupError(str(exc)) from exc
    if status.returncode != 0:
        raise SetupError("Codex prompt-node login completed but status check failed")
    return {
        "logged_in": True,
        "credential_context": "stable-instruction-free-file-auth",
        "codex_node_home": str(home),
        "remote_task_created": False,
        "paid_task_created": False,
    }


def render_checks(checks: Sequence[doctor.Check]) -> None:
    for raw in checks:
        item = doctor.redacted_check(raw)
        print(f"{item.status.upper():4} {item.name}: {item.detail}")


def verify_profile(profile_id: str) -> Dict[str, object]:
    require_supported_platform()
    if profile_id not in PROFILE_BACKENDS:
        raise SetupError(
            "backend profile cannot satisfy durable public first-run READY"
        )
    backend = PROFILE_BACKENDS[profile_id]
    profile = get_backend_profile(profile_id)
    runtime_paths = capture_runtime_paths()
    tool_paths = capture_tool_paths()
    tool_identities = capture_tool_identities(tool_paths, runtime_paths)
    verification_environment = runtime_environment(
        {
            "backend_profile": profile_id,
            "runtime_paths": runtime_paths,
            "tool_paths": tool_paths,
        }
    )
    with temporary_environment(verification_environment):
        checks = doctor.collect_checks(
            backend,
            online=True,
            check_mosaic_enabled=True,
            dreamina_model=profile.resolved_model_version(),
        )
    render_checks(checks)
    statuses = {item.name: item.status for item in checks}
    failed = sorted(
        name for name in _required_names(backend) if statuses.get(name) != "pass"
    )
    if failed:
        raise SetupError("online Doctor failed: " + ", ".join(failed))
    record: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "setup_contract_version": SETUP_CONTRACT_VERSION,
        "backend_profile": profile_id,
        "backend": backend,
        "profile_constraints_sha256": profile.constraints_digest,
        "setup_contract_sha256": setup_contract_digest(),
        "verified_at": utc_now(),
        "doctor_online": True,
        "capabilities": CAPABILITIES,
        "runtime_paths": runtime_paths,
        "tool_paths": tool_paths,
        "tool_identities": tool_identities,
        "checks": [
            {"name": name, "status": statuses[name]}
            for name in sorted(_required_names(backend))
        ],
    }
    validate_record(record, live=False)
    atomic_write_record(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": True,
        "backend_profile": profile_id,
        "backend": backend,
        "capabilities": CAPABILITIES,
        "verified_at": record["verified_at"],
        "live_checks": "pass",
        "setup_file": str(SETUP_FILE),
    }


def launch_workflow(record: Mapping[str, object], arguments: Sequence[str]) -> None:
    command = list(arguments)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SetupError("workflow command is required")
    reserved_flags = {
        "--config",
        "--project-root",
        "--root",
        "--python",
        "--engine",
    }
    overridden = sorted(
        {
            token.split("=", 1)[0]
            for token in command
            if token.split("=", 1)[0] in reserved_flags
        }
    )
    if overridden:
        raise SetupError(
            "public launcher owns infrastructure options: " + ", ".join(overridden)
        )
    tool_paths = _absolute_path_map(record.get("tool_paths"), TOOL_KEYS, "tool_paths")
    node = tool_paths["node"]
    python = tool_paths["python"]
    environment = runtime_environment(record)
    argv = [
        node,
        str(REPO_ROOT / "tools" / "video_batch_orchestrator.mjs"),
        "--project-root",
        str(REPO_ROOT),
        "--root",
        str(REPO_ROOT / "workspace" / "video-loop"),
        "--python",
        python,
        *command,
    ]
    os.execve(node, argv, environment)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent-owned first-run setup; records no credentials."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--profile", choices=tuple(PROFILE_BACKENDS), required=True)
    subparsers.add_parser("profile")
    subparsers.add_parser("require-ready")
    subparsers.add_parser("login-dreamina")
    subparsers.add_parser("login-codex-node")
    launch_parser = subparsers.add_parser("launch", help=argparse.SUPPRESS)
    launch_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "verify":
        report = verify_profile(args.profile)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "login-dreamina":
        print(json.dumps(login_dreamina(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "login-codex-node":
        print(json.dumps(login_codex_node(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "launch":
        record = validate_record(read_record(), live=True)
        launch_workflow(record, args.arguments)
        return 0

    report = readiness_report(live=True)
    if args.command == "profile":
        if report["ready"]:
            print(report["backend_profile"])
            return 0
        print(f"ERROR: {report['reason']}", file=sys.stderr)
        return 1
    if args.command == "require-ready":
        if report["ready"]:
            return 0
        print(f"ERROR: {report['reason']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ready"]:
        print(
            f"READY backend_profile={report['backend_profile']} "
            f"verified_at={report['verified_at']} live_checks=pass"
        )
    else:
        print(f"NOT_READY {report['reason']}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {doctor.redact_text(exc)}", file=sys.stderr)
        raise SystemExit(1)

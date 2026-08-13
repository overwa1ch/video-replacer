#!/usr/bin/env python3
"""Check a clone for local preparation and selected-backend readiness."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_node_home import (  # noqa: E402
    CodexNodeHomeError,
    locked_node_home,
    node_home_path,
    validate_node_home,
)
from codex_artifact import (  # noqa: E402
    CodexArtifactError,
    verify_windows_codex,
)
from codex_wire_attestation import (  # noqa: E402
    CodexWireAttestationError,
    attest_prompt_node_file_auth,
    attest_prompt_node_wire,
)
from state_paths import StatePathError, resolve_state_root  # noqa: E402
from dreamina_environment import (  # noqa: E402
    DreaminaEnvironmentError,
    dreamina_environment,
)
MINIMUM_PYTHON = (3, 12)
MINIMUM_NODE = 20
MINIMUM_CODEX = (0, 147, 0)
VIDEO_TO_PROMPT_MODEL = "gpt-5.6-terra"
PROMPT_NODE_DISABLED_FEATURES = {
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "current_time_reminder",
    "deferred_executor",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "recommended_plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_search",
    "standalone_web_search",
    "token_budget",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
}
ARK_REQUIRED_ENV = (
    "VIDEO_REPLACER_ARK_API_KEY",
    "VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID",
    "VIDEO_REPLACER_TOS_ACCESS_KEY",
    "VIDEO_REPLACER_TOS_SECRET_KEY",
    "VIDEO_REPLACER_TOS_ENDPOINT",
    "VIDEO_REPLACER_TOS_REGION",
    "VIDEO_REPLACER_TOS_BUCKET",
    "VIDEO_REPLACER_TOS_LIFECYCLE_RULE_ID",
)
SAFE_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "DREAMINA_BINARY",
    "FFMPEG",
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
    "XDG_STATE_HOME",
}
SENSITIVE_NAME_PARTS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass
class Check:
    name: str
    status: str
    detail: str


def redact_text(
    value: object, source: Optional[Mapping[str, str]] = None
) -> str:
    """Remove current-process secret values before terminal or JSON output."""

    text = str(value)
    source_env = os.environ if source is None else source
    secrets = {
        raw_value
        for name, raw_value in source_env.items()
        if raw_value
        and len(raw_value) >= 4
        and (
            name in ARK_REQUIRED_ENV
            or any(part in name.upper() for part in SENSITIVE_NAME_PARTS)
        )
    }
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def redacted_check(
    check: Check, source: Optional[Mapping[str, str]] = None
) -> Check:
    return Check(check.name, check.status, redact_text(check.detail, source))


def safe_environment(
    source: Optional[Mapping[str, str]] = None, *, extra: Iterable[str] = ()
) -> Dict[str, str]:
    source_env = os.environ if source is None else source
    allowed = {name.casefold() for name in SAFE_ENV_NAMES}
    allowed.update(name.casefold() for name in extra)
    environment = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run(command: Sequence[str], *, timeout: int = 30, environment=None):
    return subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=environment,
    )


def command_path(name: str, override: str = "") -> Optional[Path]:
    candidate = override.strip() or shutil.which(name) or ""
    if candidate:
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return path
    return None


def ffmpeg_path() -> Optional[Path]:
    configured = os.getenv("FFMPEG", "").strip()
    path = command_path("ffmpeg", configured)
    if path:
        return path
    try:
        import imageio_ffmpeg  # type: ignore

        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        return candidate if candidate.is_file() else None
    except Exception:
        return None


def codex_path() -> Optional[Path]:
    configured = os.getenv("CODEX_BINARY", "").strip()
    path = command_path("codex", configured)
    if path:
        return path
    app = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    return app.resolve() if app.is_file() else None


def supported_codex_binary(
    binary: Path, platform_name: Optional[str] = None
) -> bool:
    platform_name = os.name if platform_name is None else platform_name
    return platform_name != "nt" or binary.suffix.casefold() == ".exe"


def dreamina_path() -> Optional[Path]:
    configured = os.getenv("DREAMINA_BINARY", "").strip()
    if configured:
        path = command_path("dreamina", configured)
        if path:
            return path
    local = REPO_ROOT / ".video-replacer" / "bin" / (
        "dreamina.exe" if os.name == "nt" else "dreamina"
    )
    if local.is_file():
        return local.resolve()
    path = command_path("dreamina")
    if path:
        return path
    user_local = Path.home() / ".local" / "bin" / (
        "dreamina.exe" if os.name == "nt" else "dreamina"
    )
    return user_local.resolve() if user_local.is_file() else None


def codex_auth_path() -> Path:
    return node_home_path() / "auth.json"


def codex_home_path() -> Path:
    return node_home_path()


def external_state_path() -> Path:
    return resolve_state_root()


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def temporary_path(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    roots = {Path(tempfile.gettempdir()).expanduser().resolve()}
    for variable in ("TMPDIR", "TMP", "TEMP"):
        raw = os.getenv(variable, "").strip()
        if raw:
            roots.add(Path(raw).expanduser().resolve())
    return any(resolved == root or root in resolved.parents for root in roots)


def is_link_like(path: Path) -> bool:
    """Return true for symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def path_contains_link_like(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if is_link_like(cursor):
            return True
    return False


def local_windows_path(path: Path, platform_name: Optional[str] = None) -> bool:
    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt":
        return True
    rendered = str(path)
    return not rendered.startswith(("\\\\", "//"))


def writable_directory(path: Path) -> bool:
    """Prove write access without trusting os.access() ACL approximations."""

    probe = path / f".video-replacer-write-probe-{os.getpid()}"
    try:
        descriptor = os.open(
            str(probe),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, b"ready\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def safe_repository_directory(path: Path) -> bool:
    try:
        relative = path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    cursor = REPO_ROOT
    for component in relative.parts:
        cursor = cursor / component
        if is_link_like(cursor):
            return False
    return path.is_dir() and inside(path, REPO_ROOT)


def check_python() -> Check:
    version = sys.version_info[:3]
    status = "pass" if version >= MINIMUM_PYTHON else "fail"
    return Check("python", status, f"{sys.executable} ({'.'.join(map(str, version))})")


def check_node() -> Check:
    node = command_path("node", os.getenv("VIDEO_REPLACER_NODE", ""))
    if not node:
        return Check("node", "fail", "Node.js was not found")
    result = run([str(node), "--version"], environment=safe_environment())
    match = re.search(r"v?(\d+)", result.stdout)
    if result.returncode != 0 or not match:
        return Check("node", "fail", result.stdout.strip() or "version check failed")
    major = int(match.group(1))
    return Check("node", "pass" if major >= MINIMUM_NODE else "fail", result.stdout.strip())


def check_ffmpeg() -> Check:
    binary = ffmpeg_path()
    if not binary:
        return Check("ffmpeg", "fail", "FFmpeg or imageio-ffmpeg was not found")
    result = run([str(binary), "-version"], environment=safe_environment())
    first = result.stdout.splitlines()[0] if result.stdout else "version check failed"
    return Check("ffmpeg", "pass" if result.returncode == 0 else "fail", f"{binary}: {first}")


def check_codex() -> List[Check]:
    binary = codex_path()
    if not binary:
        return [Check("codex-cli", "fail", "Codex CLI was not found")]
    if not supported_codex_binary(binary):
        return [
            Check(
                "codex-cli",
                "fail",
                "Windows READY requires the official native codex.exe; script wrappers are not accepted",
            )
        ]
    if os.name == "nt":
        try:
            verified_codex = verify_windows_codex(binary)
            binary = Path(str(verified_codex["binary"]))
        except CodexArtifactError as exc:
            detail = f"official Windows Codex provenance failed before execution: {exc}"
            return [
                Check("codex-cli", "fail", detail),
                Check("codex-interface", "fail", "unverified codex.exe was not executed"),
                Check("codex-auth", "fail", "unverified codex.exe was not executed"),
                Check("codex-model", "fail", "unverified codex.exe was not executed"),
                Check("codex-wire", "fail", "unverified codex.exe was not executed"),
                Check(
                    "codex-file-auth-wire",
                    "fail",
                    "unverified codex.exe was not executed",
                ),
            ]
    environment = safe_environment(extra=("CODEX_HOME",))
    try:
        node_home = validate_node_home(
            codex_home_path(),
            repo_root=REPO_ROOT,
            require_auth=True,
        )
        node_home_error = ""
    except CodexNodeHomeError as exc:
        node_home = codex_home_path()
        node_home_error = str(exc)
    environment["CODEX_HOME"] = str(node_home)
    version = run([str(binary), "--version"], environment=environment)
    version_match = re.search(r"\b(\d+)\.(\d+)\.(\d+)", version.stdout)
    version_tuple = (
        tuple(int(part) for part in version_match.groups())
        if version_match
        else None
    )
    version_supported = (
        version.returncode == 0
        and version_tuple is not None
        and version_tuple >= MINIMUM_CODEX
    )
    checks = [
        Check(
            "codex-cli",
            "pass" if version_supported else "fail",
            (
                f"{binary}: {version.stdout.strip()}"
                if version_supported
                else (
                    f"Codex {'.'.join(map(str, MINIMUM_CODEX))}+ is required for "
                    "the no-execution prompt-node contract"
                )
            ),
        )
    ]
    help_result = run([str(binary), "exec", "--help"], environment=environment)
    required_flags = (
        "--ephemeral",
        "--output-schema",
        "--output-last-message",
        "--skip-git-repo-check",
        "--image",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
    )
    missing = [flag for flag in required_flags if flag not in help_result.stdout]
    feature_result = run(
        [str(binary), "features", "list"], environment=environment
    )
    available_features = {
        line.split()[0]
        for line in feature_result.stdout.splitlines()
        if line.strip()
    }
    missing_features = sorted(
        PROMPT_NODE_DISABLED_FEATURES - available_features
    )
    interface_supported = (
        help_result.returncode == 0
        and feature_result.returncode == 0
        and not missing
        and not missing_features
    )
    checks.append(
        Check(
            "codex-interface",
            "pass" if interface_supported else "fail",
            (
                "required no-execution prompt-node interface is available"
                if interface_supported
                else (
                    "missing " + ", ".join([*missing, *missing_features])
                    if missing or missing_features
                    else "Codex feature interface check failed"
                )
            ),
        )
    )
    # The production prompt nodes and Doctor share one stable, instruction-free
    # file-auth home.  Serializing every auth-bearing invocation prevents two
    # processes from racing a single-use refresh-token rotation.
    login = None
    catalog = None
    wire_attested = False
    file_auth_attested = False
    wire_error = node_home_error
    file_auth_wire_error = node_home_error
    if not node_home_error and version_supported and interface_supported:
        try:
            with locked_node_home(node_home):
                login = run(
                    [
                        str(binary),
                        "-c",
                        'cli_auth_credentials_store="file"',
                        "login",
                        "status",
                    ],
                    timeout=60,
                    environment=environment,
                )
                catalog = run(
                    [
                        str(binary),
                        "-c",
                        'cli_auth_credentials_store="file"',
                        "debug",
                        "models",
                    ],
                    timeout=60,
                    environment=environment,
                )
                try:
                    attest_prompt_node_wire(
                        binary,
                        node_home,
                        source_environment=environment,
                    )
                    wire_attested = True
                except CodexWireAttestationError as exc:
                    wire_error = str(exc)
                try:
                    attest_prompt_node_file_auth(
                        binary,
                        node_home,
                        source_environment=environment,
                    )
                    file_auth_attested = True
                except CodexWireAttestationError as exc:
                    file_auth_wire_error = str(exc)
        except CodexNodeHomeError as exc:
            node_home_error = str(exc)
            wire_error = str(exc)
            file_auth_wire_error = str(exc)
    elif not wire_error:
        wire_error = "Codex CLI/interface checks failed before wire attestation"
        file_auth_wire_error = wire_error
    login_passed = login is not None and login.returncode == 0
    checks.append(
        Check(
            "codex-auth",
            "pass" if login_passed and not node_home_error else "fail",
            (
                "login is available from the stable prompt-node file-auth context"
                if login_passed and not node_home_error
                else node_home_error
                or "Codex prompt-node login status failed; ask the Agent to repair Codex login"
            ),
        )
    )
    available = False
    try:
        payload = json.loads(catalog.stdout if catalog is not None else "")
        models = payload.get("models", []) if isinstance(payload, dict) else []
        available = any(
            isinstance(item, dict) and item.get("slug") == VIDEO_TO_PROMPT_MODEL
            for item in models
        )
    except json.JSONDecodeError:
        models = []
    checks.append(
        Check(
            "codex-model",
            "pass" if catalog is not None and catalog.returncode == 0 and available else "fail",
            (
                f"{VIDEO_TO_PROMPT_MODEL} is present in the account model catalog"
                if available
                else f"{VIDEO_TO_PROMPT_MODEL} is unavailable; update Codex or use an account with access"
            ),
        )
    )
    checks.append(
        Check(
            "codex-wire",
            "pass" if wire_attested else "fail",
            (
                "loopback request proves images and zero tools or agent hints"
                if wire_attested
                else wire_error or "Codex prompt-node wire attestation failed"
            ),
        )
    )
    checks.append(
        Check(
            "codex-file-auth-wire",
            "pass" if file_auth_attested else "fail",
            (
                "loopback Bearer digest matches the stable file-auth access token"
                if file_auth_attested
                else file_auth_wire_error
                or "Codex prompt-node file-auth wire attestation failed"
            ),
        )
    )
    return checks


def check_layout() -> List[Check]:
    runtime = REPO_ROOT / "workspace" / "video-loop"
    required = (
        "inbox",
        "needs-input",
        "ready",
        "running",
        "review",
        "blocked",
        "completed",
        "logs",
    )
    unsafe = []
    if not safe_repository_directory(runtime):
        unsafe.append("workspace/video-loop")
    unsafe.extend(
        name for name in required if not safe_repository_directory(runtime / name)
    )
    outputs = REPO_ROOT / "outputs" / "video-replacements"
    if not safe_repository_directory(outputs):
        unsafe.append("outputs/video-replacements")
    state_error = ""
    try:
        state = external_state_path()
        state_ok = (
            state.is_dir()
            and writable_directory(state)
            and not path_contains_link_like(state)
            and local_windows_path(state)
            and not inside(state, REPO_ROOT)
            and not temporary_path(state)
        )
    except StatePathError as exc:
        state = Path("<unavailable>")
        state_ok = False
        state_error = redact_text(exc)
    return [
        Check(
            "runtime-layout",
            "pass" if not unsafe else "fail",
            str(runtime)
            if not unsafe
            else "missing, linked, or outside repository: " + ", ".join(unsafe) + "; rerun Agent installation",
        ),
        Check(
            "external-state",
            "pass" if state_ok else "fail",
            str(state)
            if state_ok
            else (
                state_error
                or f"must be writable, local, non-linked, non-temporary, and outside repository: {state}"
            ),
        ),
    ]


def check_dreamina(
    online: bool, required_model: str = "seedance2.5"
) -> List[Check]:
    binary = dreamina_path()
    if not binary:
        return [Check("dreamina-cli", "fail", "Dreamina CLI was not found")]
    try:
        environment = dreamina_environment()
    except DreaminaEnvironmentError as exc:
        detail = redact_text(str(exc))
        checks = [
            Check("dreamina-cli", "fail", detail),
            Check("dreamina-interface", "fail", detail),
            Check("dreamina-model", "fail", detail),
        ]
        if online:
            checks.append(Check("dreamina-login", "fail", detail))
        return checks
    environment["DREAMINA_BINARY"] = str(binary)
    version = run([str(binary), "version"], environment=environment)
    checks = [
        Check(
            "dreamina-cli",
            "pass" if version.returncode == 0 else "fail",
            f"{binary}: {version.stdout.strip()}",
        )
    ]
    interface = run([str(binary), "multimodal2video", "-h"], environment=environment)
    required = ("--video", "--image", "--video_resolution", "--model_version")
    missing = [flag for flag in required if flag not in interface.stdout]
    checks.append(
        Check(
            "dreamina-interface",
            "pass" if interface.returncode == 0 and not missing else "fail",
            "required multimodal2video flags available" if not missing else "missing " + ", ".join(missing),
        )
    )
    model_available = required_model in interface.stdout
    checks.append(
        Check(
            "dreamina-model",
            "pass" if interface.returncode == 0 and model_available else "fail",
            (
                f"{required_model} is exposed by the Dreamina CLI"
                if model_available
                else f"{required_model} is unavailable; install the reviewed CLI version"
            ),
        )
    )
    if online:
        result = run(
            [sys.executable, str(REPO_ROOT / "tools" / "dreamina_video.py"), "config"],
            timeout=60,
            environment=environment,
        )
        account_ready = False
        account_detail = "Dreamina account query failed"
        try:
            payload = json.loads(result.stdout)
            account = payload.get("account", {}) if isinstance(payload, dict) else {}
            logged_in = account.get("logged_in") is True if isinstance(account, dict) else False
            vip_level = str(account.get("vip_level") or "") if isinstance(account, dict) else ""
            entitlement_ready = required_model != "seedance2.5" or (
                vip_level.casefold() not in {"", "none", "free", "basic"}
            )
            account_ready = result.returncode == 0 and logged_in and entitlement_ready
            account_detail = (
                "account login and selected-model entitlement verified"
                if account_ready
                else f"account is not entitled for {required_model}"
            )
        except json.JSONDecodeError:
            account_detail = redact_text(result.stdout.strip(), environment)
        checks.append(
            Check(
                "dreamina-login",
                "pass" if account_ready else "fail",
                account_detail,
            )
        )
    else:
        checks.append(Check("dreamina-login", "warn", "not checked; rerun with --online"))
    return [redacted_check(item) for item in checks]


def check_ark(online: bool) -> List[Check]:
    checks: List[Check] = []
    tos_present = importlib.util.find_spec("tos") is not None
    checks.append(
        Check("ark-tos-sdk", "pass" if tos_present else "fail", "tos==2.9.2" if tos_present else "rerun Agent installation with Ark support")
    )
    missing = [name for name in ARK_REQUIRED_ENV if not os.getenv(name, "").strip()]
    checks.append(
        Check(
            "ark-environment",
            "pass" if not missing else "fail",
            "all required names are present" if not missing else "missing " + ", ".join(missing),
        )
    )
    if online and tos_present and not missing:
        tools = str(REPO_ROOT / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        try:
            import ark_video  # type: ignore

            environment = ark_video.load_ark_environment()
            client = ark_video.create_tos_client(environment)
            ark_video.verify_tos_lifecycle_fallback(client, environment)
        except Exception as exc:
            checks.append(
                Check("ark-tos-lifecycle", "fail", redact_text(exc))
            )
        else:
            checks.append(Check("ark-tos-lifecycle", "pass", "enabled private-prefix expiry rule verified"))
    elif online:
        checks.append(Check("ark-tos-lifecycle", "fail", "dependency or environment checks failed"))
    else:
        checks.append(Check("ark-tos-lifecycle", "warn", "not checked; rerun with --online"))
    return [redacted_check(item) for item in checks]


def check_mosaic() -> Check:
    try:
        executable = mosaic_module().verified_openscrub(mosaic_cache_path())
    except Exception as exc:
        return Check("mosaic", "fail", redact_text(exc))
    return Check(
        "mosaic",
        "pass" if executable else "fail",
        str(executable) if executable else "ask the Agent to reinstall the mosaic capability",
    )


def mosaic_module():
    tools = str(REPO_ROOT / "tools" / "privacy")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import face_mosaic  # type: ignore

    return face_mosaic


def mosaic_cache_path() -> Path:
    return mosaic_module().default_cache_root()


def collect_checks(
    backend: str,
    *,
    online: bool = False,
    check_mosaic_enabled: bool = False,
    dreamina_model: str = "seedance2.5",
) -> List[Check]:
    """Return one complete readiness report for Agent-driven setup."""

    if backend not in {"dreamina", "ark", "none"}:
        raise ValueError(f"unsupported backend: {backend}")
    checks = [check_python(), check_node(), check_ffmpeg(), *check_codex(), *check_layout()]
    if backend == "dreamina":
        checks.extend(check_dreamina(online, dreamina_model))
    elif backend == "ark":
        checks.extend(check_ark(online))
    if check_mosaic_enabled:
        checks.append(check_mosaic())
    return [redacted_check(item) for item in checks]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Video Replacer readiness without paid work.")
    parser.add_argument("--backend", choices=("dreamina", "ark", "none"), default="dreamina")
    parser.add_argument("--online", action="store_true", help="run no-cost account or storage checks")
    parser.add_argument("--check-mosaic", action="store_true")
    parser.add_argument(
        "--dreamina-model",
        choices=("seedance2.0", "seedance2.5"),
        default="seedance2.5",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = collect_checks(
        args.backend,
        online=args.online,
        check_mosaic_enabled=args.check_mosaic,
        dreamina_model=args.dreamina_model,
    )

    if args.json:
        print(json.dumps({"checks": [asdict(item) for item in checks]}, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"{item.status.upper():4} {item.name}: {item.detail}")
    return 1 if any(item.status == "fail" for item in checks) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Create the repository-local runtime without login or remote execution."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from codex_node_home import NODE_HOME_DIRECTORY, ensure_node_home
from state_paths import StatePathError, resolve_state_root
VENV = REPO_ROOT / ".venv"
CORE_REQUIREMENTS = REPO_ROOT / "requirements-core.lock.txt"
ARK_REQUIREMENTS = REPO_ROOT / "tools" / "ark-tos-requirements.lock.txt"
MINIMUM_PYTHON = (3, 12)
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
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


class BootstrapError(RuntimeError):
    pass


def safe_environment(
    source: Optional[Dict[str, str]] = None, *, extra: Iterable[str] = ()
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


def is_link_like(path: Path) -> bool:
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


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python3"


def require_host_tools() -> str:
    if os.name == "nt" and platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise BootstrapError(
            f"Native Windows installation currently requires x64; found {platform.machine() or 'unknown'}."
        )
    if sys.version_info < MINIMUM_PYTHON:
        raise BootstrapError(
            f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+ is required; "
            f"current={sys.version.split()[0]}"
        )
    node = os.getenv("VIDEO_REPLACER_NODE", "").strip() or shutil.which("node")
    if not node:
        raise BootstrapError("Node.js 20+ is required and was not found on PATH.")
    result = subprocess.run(
        [node, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=safe_environment(),
    )
    try:
        major = int(result.stdout.strip().lstrip("v").split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise BootstrapError("Unable to read the Node.js version.") from exc
    if result.returncode != 0 or major < 20:
        raise BootstrapError(f"Node.js 20+ is required; found {result.stdout.strip()!r}.")
    return node


def ensure_venv() -> Path:
    if is_link_like(VENV):
        raise BootstrapError(f"Refusing linked runtime: {VENV}")
    python = venv_python()
    if not python.is_file():
        if VENV.exists() and any(VENV.iterdir()):
            raise BootstrapError(
                f"Existing {VENV} is not a usable virtual environment; move it aside first."
            )
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)],
            check=True,
            env=safe_environment(),
        )
    version = subprocess.run(
        [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=safe_environment(),
    )
    if version.returncode != 0:
        raise BootstrapError(f"Repository Python is not executable: {version.stdout.strip()}")
    # Keep the venv entry path. On POSIX it is normally a symlink to the base
    # interpreter; resolving it would make later pip installs escape `.venv`.
    return python.absolute()


def installed_version(python: Path, distribution: str) -> Optional[str]:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata,sys; "
            "\ntry: print(importlib.metadata.version(sys.argv[1]))"
            "\nexcept importlib.metadata.PackageNotFoundError: pass",
            distribution,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=safe_environment(),
    )
    value = result.stdout.strip()
    return value or None


def install_requirements(python: Path, path: Path) -> None:
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(path),
        ],
        check=False,
        env=safe_environment(),
    )
    if result.returncode != 0:
        raise BootstrapError(f"Dependency installation failed: {path}")


def external_state_root() -> Path:
    try:
        return resolve_state_root()
    except StatePathError as exc:
        raise BootstrapError(str(exc)) from exc


def ensure_private_directory(path: Path) -> None:
    if path_contains_link_like(path):
        raise BootstrapError(f"State directory must not use links or junctions: {path}")
    if os.name == "nt" and str(path).startswith(("\\\\", "//")):
        raise BootstrapError(f"State directory must be on a local Windows drive: {path}")
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise BootstrapError(f"State directory must remain outside the repository: {path}")
    temporary_roots = {Path(tempfile.gettempdir()).expanduser().resolve()}
    for variable in ("TMPDIR", "TMP", "TEMP"):
        raw = os.getenv(variable, "").strip()
        if raw:
            temporary_roots.add(Path(raw).expanduser().resolve())
    if any(path == root or root in path.parents for root in temporary_roots):
        raise BootstrapError(f"State directory must not be temporary: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def initialize_runtime(node: str, python: Path) -> None:
    result = subprocess.run(
        [
            node,
            str(REPO_ROOT / "tools" / "video_batch_orchestrator.mjs"),
            "--project-root",
            str(REPO_ROOT),
            "--root",
            str(REPO_ROOT / "workspace" / "video-loop"),
            "--python",
            str(python),
            "init",
        ],
        check=False,
        env=safe_environment(extra=("VIDEO_REPLACER_PYTHON",)),
    )
    if result.returncode != 0:
        raise BootstrapError("Unable to initialize workspace/video-loop.")


def initialize_reference_catalog() -> None:
    root = REPO_ROOT / "assets" / "reference-images"
    root.mkdir(parents=True, exist_ok=True)
    catalog = root / "catalog.json"
    example = root / "catalog.example.json"
    if not catalog.exists():
        shutil.copyfile(example, catalog)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the local Video Replacer runtime without login or remote work."
    )
    parser.add_argument("--with-ark", action="store_true")
    parser.add_argument("--with-mosaic", action="store_true")
    args = parser.parse_args()

    node = require_host_tools()
    python = ensure_venv()
    if installed_version(python, "imageio-ffmpeg") != "0.6.0":
        install_requirements(python, CORE_REQUIREMENTS)
    if args.with_ark and installed_version(python, "tos") != "2.9.2":
        install_requirements(python, ARK_REQUIREMENTS)

    state_root = external_state_root()
    ensure_private_directory(state_root)
    codex_node_home = ensure_node_home(
        state_root / NODE_HOME_DIRECTORY,
        repo_root=REPO_ROOT,
    )
    initialize_runtime(node, python)
    initialize_reference_catalog()
    (REPO_ROOT / "outputs" / "video-replacements").mkdir(parents=True, exist_ok=True)

    if args.with_mosaic:
        environment = safe_environment(
            extra=("FFMPEG", "VIDEO_REPLACER_CACHE_DIR", "VIDEO_REPLACER_PYTHON")
        )
        environment["VIDEO_REPLACER_PYTHON"] = str(python)
        result = subprocess.run(
            [str(python), str(REPO_ROOT / "tools" / "privacy" / "face_mosaic.py"), "--install-only"],
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            raise BootstrapError("Optional mosaic dependency installation failed.")

    report = {
        "repo_root": str(REPO_ROOT),
        "python": str(python),
        "node": str(node),
        "runtime": str(REPO_ROOT / "workspace" / "video-loop"),
        "outputs": str(REPO_ROOT / "outputs" / "video-replacements"),
        "state": str(state_root),
        "codex_node_home": str(codex_node_home),
        "ark_installed": installed_version(python, "tos") == "2.9.2",
        "mosaic_requested": args.with_mosaic,
        "remote_task_created": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

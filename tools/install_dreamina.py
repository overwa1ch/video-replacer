#!/usr/bin/env python3
"""Install one pinned official Dreamina CLI binary inside this clone.

This intentionally does not execute Dreamina's remote shell installer.  It
does not modify PATH, shell startup files, global Agent skills, or unrelated
workspaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import BinaryIO, Dict, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("dreamina-install-manifest.json")
INSTALL_ROOT = REPO_ROOT / ".video-replacer" / "bin"
TARGET_PATH = INSTALL_ROOT / ("dreamina.exe" if os.name == "nt" else "dreamina")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def read_manifest(path: Path = MANIFEST_PATH) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"invalid installer manifest: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "version",
        "version_manifest_url",
        "official_installer_url",
        "artifacts",
    }:
        raise InstallError("installer manifest fields are invalid")
    if payload.get("schema_version") != 1:
        raise InstallError("installer manifest schema is unsupported")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise InstallError("installer manifest artifacts are invalid")
    return payload


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    system_name = (system or platform.system()).casefold()
    machine_name = (machine or platform.machine()).casefold()
    systems = {"darwin": "darwin", "linux": "linux", "windows": "windows"}
    machines = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    if (
        system_name not in systems
        or machine_name not in machines
        or (system_name == "windows" and machines.get(machine_name) != "amd64")
    ):
        raise InstallError(
            f"unsupported Dreamina platform: {system_name}/{machine_name}"
        )
    return f"{systems[system_name]}_{machines[machine_name]}"


def artifact_for(
    manifest: Mapping[str, object], key: Optional[str] = None
) -> Dict[str, str]:
    artifacts = manifest.get("artifacts")
    artifact = artifacts.get(key or platform_key()) if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict) or set(artifact) != {"url", "sha256"}:
        raise InstallError("installer artifact fields are invalid")
    url = artifact.get("url")
    digest = artifact.get("sha256")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise InstallError("installer artifact URL must use HTTPS")
    if not isinstance(digest, str) or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise InstallError("installer artifact SHA-256 is invalid")
    return {"url": url, "sha256": digest}


def stream_download(source: BinaryIO, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    descriptor = os.open(
        str(destination),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "wb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise InstallError("Dreamina download exceeds the size limit")
            digest.update(chunk)
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return digest.hexdigest(), total


def safe_environment() -> Dict[str, str]:
    allowed = {
        "APPDATA",
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
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed or key.upper().startswith("LC_")
    }
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def is_link_like(path: Path) -> bool:
    """Reject POSIX symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def ensure_install_root() -> None:
    setup_root = INSTALL_ROOT.parent
    for path in (setup_root, INSTALL_ROOT):
        if is_link_like(path):
            raise InstallError(f"refusing linked install directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)


def reported_version(output: str) -> str:
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
    return text.splitlines()[0][:240] if text else ""


def install(*, opener=urllib.request.urlopen) -> Dict[str, object]:
    manifest = read_manifest()
    artifact = artifact_for(manifest)
    ensure_install_root()
    request = urllib.request.Request(
        artifact["url"], headers={"User-Agent": "video-replacer-installer/1"}
    )
    # Keep the verified candidate on the same volume as its final path.  A
    # system TEMP directory may be on C: while the clone lives on another
    # Windows drive, where os.replace() cannot be atomic.
    with tempfile.TemporaryDirectory(
        prefix=".dreamina-download-", dir=INSTALL_ROOT
    ) as temporary:
        candidate = Path(temporary) / TARGET_PATH.name
        try:
            response = opener(request, timeout=120)
            with response:
                actual_digest, size = stream_download(response, candidate)
        except InstallError:
            raise
        except Exception as exc:
            raise InstallError("unable to download the pinned Dreamina CLI") from exc
        if actual_digest != artifact["sha256"]:
            raise InstallError(
                "Dreamina SHA-256 mismatch; the reviewed manifest must be updated"
            )
        if os.name != "nt":
            candidate.chmod(0o700)
        completed = subprocess.run(
            [str(candidate), "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
            env=safe_environment(),
        )
        if completed.returncode != 0:
            raise InstallError("downloaded Dreamina CLI failed its version check")
        binary_version = reported_version(completed.stdout)
        if not binary_version:
            raise InstallError("downloaded Dreamina CLI returned no version")
        os.replace(candidate, TARGET_PATH)
        if os.name != "nt":
            TARGET_PATH.chmod(0o700)
    return {
        "installed": True,
        "path": str(TARGET_PATH),
        "version": str(manifest["version"]),
        "binary_version": binary_version,
        "sha256": artifact["sha256"],
        "bytes": size,
        "modified_shell": False,
        "installed_global_skill": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the reviewed Dreamina CLI binary inside this clone."
    )
    parser.parse_args()
    print(json.dumps(install(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

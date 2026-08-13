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
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import BinaryIO, Dict, Mapping, Optional


sys.path.insert(0, str(Path(__file__).resolve().parent))
from dreamina_environment import (  # noqa: E402
    DreaminaEnvironmentError,
    dreamina_environment,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("dreamina-install-manifest.json")
VERSION_METADATA_SOURCE = Path(__file__).with_name("dreamina-version.json")
INSTALL_ROOT = REPO_ROOT / ".video-replacer" / "bin"
TARGET_PATH = INSTALL_ROOT / ("dreamina.exe" if os.name == "nt" else "dreamina")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_VERSION_METADATA_BYTES = 64 * 1024
VERSION_CHECK_TIMEOUT_SECONDS = 120 if os.name == "nt" else 30
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


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
        "version_manifest_sha256",
        "official_installer_url",
        "artifacts",
    }:
        raise InstallError("installer manifest fields are invalid")
    if payload.get("schema_version") != 1:
        raise InstallError("installer manifest schema is unsupported")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise InstallError("installer manifest artifacts are invalid")
    version_digest = payload.get("version_manifest_sha256")
    if not isinstance(version_digest, str) or len(version_digest) != 64 or any(
        character not in "0123456789abcdef" for character in version_digest
    ):
        raise InstallError("installer version metadata SHA-256 is invalid")
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
    try:
        return dreamina_environment()
    except DreaminaEnvironmentError as exc:
        raise InstallError(str(exc)) from exc


def is_link_like(path: Path) -> bool:
    """Reject POSIX symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def path_entry_exists(path: Path) -> bool:
    """Return true for ordinary entries and broken links."""

    return os.path.lexists(path)


def strict_json_object(data: bytes, *, label: str) -> Dict[str, object]:
    def pairs_object(pairs):
        result: Dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise InstallError(f"{label} contains duplicate JSON fields")
            result[key] = value
        return result

    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=pairs_object)
    except InstallError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise InstallError(f"{label} must be a JSON object")
    return payload


def validate_version_metadata_payload(
    data: bytes, *, expected_version: Optional[str] = None, label: str
) -> Dict[str, object]:
    if not data or len(data) > MAX_VERSION_METADATA_BYTES:
        raise InstallError(f"{label} has an invalid size")
    payload = strict_json_object(data, label=label)
    required = {"version", "release_date", "release_notes"}
    if not required.issubset(payload):
        raise InstallError(f"{label} fields are invalid")
    version = payload.get("version")
    release_date = payload.get("release_date")
    release_notes = payload.get("release_notes")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise InstallError(f"{label} version is invalid")
    if expected_version is not None and version != expected_version:
        raise InstallError(f"{label} version does not match the installer manifest")
    if not isinstance(release_date, str) or not DATE_RE.fullmatch(release_date):
        raise InstallError(f"{label} release date is invalid")
    if not isinstance(release_notes, str) or not release_notes.strip():
        raise InstallError(f"{label} release notes are invalid")
    return payload


def pinned_version_metadata(manifest: Mapping[str, object]) -> bytes:
    try:
        data = VERSION_METADATA_SOURCE.read_bytes()
    except OSError as exc:
        raise InstallError("unable to read pinned Dreamina version metadata") from exc
    expected_digest = manifest.get("version_manifest_sha256")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise InstallError("pinned Dreamina version metadata SHA-256 mismatch")
    expected_version = manifest.get("version")
    if not isinstance(expected_version, str):
        raise InstallError("installer manifest version is invalid")
    validate_version_metadata_payload(
        data,
        expected_version=expected_version,
        label="pinned Dreamina version metadata",
    )
    return data


def dreamina_home(
    environment: Mapping[str, str], *, windows: Optional[bool] = None
) -> Path:
    variable = "USERPROFILE" if (os.name == "nt" if windows is None else windows) else "HOME"
    raw = environment.get(variable)
    if not isinstance(raw, str) or not raw.strip():
        raise InstallError(f"{variable} is required for Dreamina metadata")
    home = Path(raw)
    if not home.is_absolute():
        raise InstallError(f"{variable} must be an absolute path")
    if not home.is_dir() or is_link_like(home):
        raise InstallError(f"refusing unsafe Dreamina home: {home}")
    return home


def _safe_metadata_root(home: Path) -> tuple[Path, bool, tuple[int, int]]:
    root = home / ".dreamina_cli"
    created = False
    if path_entry_exists(root):
        if is_link_like(root) or not root.is_dir():
            raise InstallError(f"refusing unsafe Dreamina metadata directory: {root}")
    else:
        try:
            root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if is_link_like(root) or not root.is_dir():
            raise InstallError(f"refusing unsafe Dreamina metadata directory: {root}")
        if os.name != "nt" and created:
            root.chmod(0o700)
    identity = root.stat()
    return root, created, (identity.st_dev, identity.st_ino)


def _read_existing_version_metadata(target: Path) -> bytes:
    if is_link_like(target) or not target.is_file():
        raise InstallError(f"refusing unsafe Dreamina version metadata: {target}")
    before = target.stat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise InstallError(
                    f"refusing unsafe Dreamina version metadata: {target}"
                )
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise InstallError("Dreamina version metadata changed while reading")
            data = handle.read(MAX_VERSION_METADATA_BYTES + 1)
    except OSError as exc:
        raise InstallError("unable to read existing Dreamina version metadata") from exc
    if is_link_like(target) or not target.is_file():
        raise InstallError("Dreamina version metadata changed while reading")
    after = target.stat()
    if (after.st_dev, after.st_ino, after.st_size) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
    ):
        raise InstallError("Dreamina version metadata changed while reading")
    validate_version_metadata_payload(data, label="existing Dreamina version metadata")
    return data


def provision_version_metadata(
    environment: Mapping[str, str], data: bytes
) -> Dict[str, object]:
    """Create missing provider metadata without overwriting another installation."""

    home = dreamina_home(environment)
    root, root_created, root_identity = _safe_metadata_root(home)
    target = root / "version.json"
    if path_entry_exists(target):
        existing = _read_existing_version_metadata(target)
        return {
            "path": str(target),
            "status": "preserved",
            "sha256": hashlib.sha256(existing).hexdigest(),
            "created_root": root_created,
        }

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".version.json.", suffix=".tmp", dir=root
    )
    temporary = Path(temporary_name)
    published: Optional[Dict[str, object]] = None
    failed = False
    try:
        if os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if is_link_like(root) or not root.is_dir():
            raise InstallError(f"refusing changed Dreamina metadata directory: {root}")
        current_root = root.stat()
        if (current_root.st_dev, current_root.st_ino) != root_identity:
            raise InstallError("Dreamina metadata directory changed during installation")
        try:
            # A same-directory hard link publishes fully written bytes without
            # replacing a file created concurrently by another installation.
            temporary_identity = temporary.stat()
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            existing = _read_existing_version_metadata(target)
            return {
                "path": str(target),
                "status": "preserved",
                "sha256": hashlib.sha256(existing).hexdigest(),
                "created_root": root_created,
            }
        except OSError as exc:
            raise InstallError("unable to publish Dreamina version metadata safely") from exc
        published = {
            "path": str(target),
            "status": "created",
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_root": root_created,
            "device": temporary_identity.st_dev,
            "inode": temporary_identity.st_ino,
        }
        if is_link_like(target) or not target.is_file():
            raise InstallError(f"Dreamina version metadata was not installed safely: {target}")
        identity = target.stat()
        if (identity.st_dev, identity.st_ino) != (
            temporary_identity.st_dev,
            temporary_identity.st_ino,
        ):
            raise InstallError("Dreamina version metadata identity changed")
        installed = _read_existing_version_metadata(target)
        if installed != data:
            raise InstallError("Dreamina version metadata changed during installation")
        return published
    except BaseException:
        failed = True
        if published is not None:
            rollback_created_version_metadata(published, data)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if failed and root_created:
            try:
                root.rmdir()
            except OSError:
                pass


def rollback_created_version_metadata(metadata: Mapping[str, object], data: bytes) -> None:
    if metadata.get("status") != "created":
        return
    target = Path(str(metadata.get("path", "")))
    try:
        if is_link_like(target) or not target.is_file():
            return
        identity = target.stat()
        if identity.st_dev != metadata.get("device") or identity.st_ino != metadata.get("inode"):
            return
        with target.open("rb") as handle:
            current = handle.read(MAX_VERSION_METADATA_BYTES + 1)
        if current != data:
            return
        final_identity = target.stat()
        if (
            final_identity.st_dev != metadata.get("device")
            or final_identity.st_ino != metadata.get("inode")
        ):
            return
        target.unlink()
        if metadata.get("created_root"):
            try:
                target.parent.rmdir()
            except OSError:
                pass
    except OSError:
        # Rollback is best effort and never replaces state created elsewhere.
        return


def install_candidate_for_check(candidate: Path, expected_digest: str) -> Dict[str, object]:
    """Publish the verified binary at its real path with a rollback handle."""

    if is_link_like(candidate) or not candidate.is_file():
        raise InstallError("Dreamina candidate binary is unsafe")
    candidate_identity = candidate.stat()
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_digest:
        raise InstallError("Dreamina candidate changed before installation")
    candidate_after = candidate.stat()
    if (candidate_after.st_dev, candidate_after.st_ino, candidate_after.st_size) != (
        candidate_identity.st_dev,
        candidate_identity.st_ino,
        candidate_identity.st_size,
    ):
        raise InstallError("Dreamina candidate changed before installation")
    if path_entry_exists(TARGET_PATH) and (
        is_link_like(TARGET_PATH) or not TARGET_PATH.is_file()
    ):
        raise InstallError(f"refusing unsafe existing Dreamina binary: {TARGET_PATH}")
    backup: Optional[Path] = None
    if TARGET_PATH.is_file():
        descriptor, backup_name = tempfile.mkstemp(
            prefix=f".{TARGET_PATH.name}.backup-", dir=INSTALL_ROOT
        )
        os.close(descriptor)
        backup = Path(backup_name)
        backup.unlink()
        try:
            os.link(TARGET_PATH, backup, follow_symlinks=False)
        except OSError as exc:
            raise InstallError("unable to preserve the existing Dreamina binary") from exc
    transaction: Dict[str, object] = {
        "backup": str(backup) if backup is not None else None,
        "device": candidate_identity.st_dev,
        "inode": candidate_identity.st_ino,
        "sha256": expected_digest,
    }
    published = False
    try:
        os.replace(candidate, TARGET_PATH)
        published = True
        validate_installed_candidate(transaction)
        return transaction
    except BaseException:
        if published:
            try:
                rollback_installed_candidate(transaction)
            except BaseException:
                # Preserve the backup for explicit recovery when Windows or a
                # concurrent process prevents restoration.
                raise
        elif backup is not None:
            backup.unlink(missing_ok=True)
        raise


def validate_installed_candidate(transaction: Mapping[str, object]) -> None:
    if is_link_like(TARGET_PATH) or not TARGET_PATH.is_file():
        raise InstallError("Dreamina binary was not installed safely")
    identity = TARGET_PATH.stat()
    if (
        identity.st_dev != transaction.get("device")
        or identity.st_ino != transaction.get("inode")
    ):
        raise InstallError("installed Dreamina binary identity changed")
    if hashlib.sha256(TARGET_PATH.read_bytes()).hexdigest() != transaction.get(
        "sha256"
    ):
        raise InstallError("installed Dreamina binary changed before its version check")


def finish_installed_candidate(transaction: Mapping[str, object]) -> None:
    validate_installed_candidate(transaction)
    backup_value = transaction.get("backup")
    if backup_value:
        Path(str(backup_value)).unlink(missing_ok=True)


def rollback_installed_candidate(transaction: Mapping[str, object]) -> None:
    backup_value = transaction.get("backup")
    backup = Path(str(backup_value)) if backup_value else None
    try:
        current_is_ours = False
        if not is_link_like(TARGET_PATH) and TARGET_PATH.is_file():
            identity = TARGET_PATH.stat()
            current_is_ours = (
                identity.st_dev == transaction.get("device")
                and identity.st_ino == transaction.get("inode")
            )
        if current_is_ours:
            if backup is not None and backup.is_file() and not is_link_like(backup):
                os.replace(backup, TARGET_PATH)
            else:
                TARGET_PATH.unlink()
        elif backup is not None:
            raise InstallError(
                f"Dreamina rollback could not restore the preserved binary: {backup}"
            )
    except BaseException:
        raise
    else:
        if backup is not None:
            backup.unlink(missing_ok=True)


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
        payload = strict_json_object(text.encode("utf-8"), label="Dreamina version output")
    except InstallError:
        return ""
    version = payload.get("version")
    commit = payload.get("commit")
    if not isinstance(version, str) or not version.strip():
        return ""
    return version + (f" ({commit})" if isinstance(commit, str) and commit else "")


def install(*, opener=urllib.request.urlopen) -> Dict[str, object]:
    manifest = read_manifest()
    artifact = artifact_for(manifest)
    version_metadata = pinned_version_metadata(manifest)
    environment = safe_environment()
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
        metadata = provision_version_metadata(environment, version_metadata)
        transaction: Optional[Dict[str, object]] = None
        try:
            # Match the provider's official installer order: the CLI runs from
            # its final location. Its Windows updater derives behavior from
            # the executable path. The transaction restores any old binary
            # if this real-path smoke fails.
            transaction = install_candidate_for_check(candidate, artifact["sha256"])
            completed = subprocess.run(
                [str(TARGET_PATH), "version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                # Windows uses the shared Dreamina-only environment, whose
                # restricted PATH prevents the provider's optional,
                # unbounded PowerShell/CIM ancestry probe.  The real pinned
                # executable must still return valid version JSON in-bounds.
                timeout=VERSION_CHECK_TIMEOUT_SECONDS,
                env=environment,
            )
            if completed.returncode != 0:
                raise InstallError("downloaded Dreamina CLI failed its version check")
            binary_version = reported_version(completed.stdout)
            if not binary_version:
                raise InstallError("downloaded Dreamina CLI returned invalid version JSON")
            finish_installed_candidate(transaction)
        except BaseException:
            rollback_error: Optional[BaseException] = None
            try:
                if transaction is not None:
                    rollback_installed_candidate(transaction)
            except BaseException as exc:
                rollback_error = exc
            finally:
                rollback_created_version_metadata(metadata, version_metadata)
            if rollback_error is not None:
                raise InstallError(
                    "Dreamina installation failed and its preserved binary "
                    "requires manual recovery"
                ) from rollback_error
            raise
    return {
        "installed": True,
        "path": str(TARGET_PATH),
        "version": str(manifest["version"]),
        "binary_version": binary_version,
        "sha256": artifact["sha256"],
        "bytes": size,
        "version_metadata": {
            "path": metadata["path"],
            "status": metadata["status"],
            "sha256": metadata["sha256"],
        },
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

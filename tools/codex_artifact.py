#!/usr/bin/env python3
"""Verify an official Windows x64 Codex standalone artifact without running it.

The public Windows installer resolves ``codex.exe`` through one or two
installer-owned junctions.  Callers may therefore pass either the visible
command or its canonical path; verification is performed against the resolved
``packages/standalone/releases`` package tree.  This module is intentionally
usable on any host so its Windows-artifact contract can be tested in CI.  POSIX
Codex installations use a different release layout and are outside its scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TARGET = "x86_64-pc-windows-msvc"
VARIANT = "codex"
ENTRYPOINT = "bin/codex.exe"
LAYOUT_VERSION = 1
RELEASES_ORIGIN = "https://releases.openai.com"
RELEASES_HOST = "releases.openai.com"
RELEASE_MANIFEST_MAX_BYTES = 512 * 1024
PACKAGE_METADATA_MAX_BYTES = 16 * 1024
RELEASE_ASSET_NAME = f"codex-{TARGET}.exe"
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-alpha(?:\.[0-9]+){0,2}|-beta(?:\.[0-9]+)?)?$"
)
SHA256_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")

REQUIRED_DIRECTORIES = (
    Path("bin"),
    Path("codex-resources"),
    Path("codex-path"),
)
REQUIRED_FILES = (
    Path("codex-package.json"),
    Path("bin/codex.exe"),
    Path("bin/codex-code-mode-host.exe"),
    Path("codex-path/rg.exe"),
    Path("codex-resources/codex-command-runner.exe"),
    Path("codex-resources/codex-windows-sandbox-setup.exe"),
)


class CodexArtifactError(RuntimeError):
    """The candidate cannot be proven to be the official Windows artifact."""


ReleaseFetcher = Callable[[str], object]


class _OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect hop that leaves the exact official endpoint."""

    def __init__(self, expected_path: str) -> None:
        super().__init__()
        self.expected_path = expected_path

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        _require_official_url(newurl, expected_path=self.expected_path)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify_windows_codex(
    binary: Path, *, release_fetcher: ReleaseFetcher | None = None
) -> dict[str, object]:
    """Verify a modern official Windows x64 standalone ``codex.exe``.

    The executable is never invoked.  Its version comes from the canonical
    package path and ``codex-package.json``; authenticity comes from the
    SHA-256 digest in that version's HTTPS release manifest.

    ``release_fetcher`` is an injection point for deterministic tests or a
    caller-owned authenticated cache.  It receives the exact manifest URL and
    may return a mapping, UTF-8 JSON bytes/text, or an HTTP-response-like object
    with ``read()`` and ``geturl()``.  The built-in fetcher enforces the official
    HTTPS endpoint, redirect target, content type, encoding, and size bound.
    """

    resolved_binary, release_root, version = _resolve_package_layout(Path(binary))
    metadata = _load_package_metadata(release_root, version)
    _verify_required_package_entries(release_root, resolved_binary)

    manifest_url = _release_manifest_url(version)
    manifest = _fetch_release_manifest(manifest_url, release_fetcher)
    asset = _release_asset(manifest, version)
    expected_sha256 = asset["sha256"]
    actual_sha256 = _sha256_stable_file(resolved_binary)
    if actual_sha256 != expected_sha256:
        raise CodexArtifactError(
            "Codex executable SHA-256 does not match the official release manifest"
        )

    return {
        "version": version,
        "sha256": actual_sha256,
        "target": TARGET,
        "variant": VARIANT,
        "entrypoint": ENTRYPOINT,
        "layout_version": LAYOUT_VERSION,
        "binary": str(resolved_binary),
        "release_root": str(release_root),
        "package_metadata": {
            key: metadata[key]
            for key in (
                "layoutVersion",
                "version",
                "target",
                "variant",
                "entrypoint",
                "resourcesDir",
                "pathDir",
            )
        },
        "release": {
            "manifest_url": manifest_url,
            "tag_name": manifest["tag_name"],
            "asset_name": RELEASE_ASSET_NAME,
            "asset_url": asset["url"],
            "asset_sha256": expected_sha256,
        },
    }


def _resolve_package_layout(binary: Path) -> tuple[Path, Path, str]:
    try:
        resolved = binary.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodexArtifactError(f"Codex executable cannot be resolved: {binary}") from exc

    if resolved.name.casefold() != "codex.exe":
        raise CodexArtifactError("Windows Codex entrypoint must be named codex.exe")
    if resolved.parent.name.casefold() != "bin":
        raise CodexArtifactError("Codex entrypoint is not in the standalone bin directory")

    release_root = resolved.parent.parent
    releases_dir = release_root.parent
    standalone_dir = releases_dir.parent
    packages_dir = standalone_dir.parent
    if (
        releases_dir.name.casefold() != "releases"
        or standalone_dir.name.casefold() != "standalone"
        or packages_dir.name.casefold() != "packages"
    ):
        raise CodexArtifactError(
            "Codex entrypoint is not in packages/standalone/releases"
        )

    suffix = f"-{TARGET}"
    release_name = release_root.name
    if not release_name.endswith(suffix):
        raise CodexArtifactError("Codex release directory has the wrong Windows target")
    version = release_name[: -len(suffix)]
    if not VERSION_PATTERN.fullmatch(version):
        raise CodexArtifactError("Codex release directory has an invalid version")

    # Resolve the installer aliases first, then reject links and all Windows
    # reparse points inside the canonical package boundary itself.
    for path in (packages_dir, standalone_dir, releases_dir, release_root):
        _require_plain_directory(path)
    _require_plain_directory(release_root / "bin")
    _require_plain_file(resolved)
    return resolved, release_root, version


def _load_package_metadata(release_root: Path, version: str) -> dict[str, object]:
    metadata_path = release_root / "codex-package.json"
    _require_plain_file(metadata_path)
    raw = _read_stable_file(metadata_path, PACKAGE_METADATA_MAX_BYTES)
    metadata = _decode_json_object(raw, "codex-package.json")
    expected = {
        "layoutVersion": LAYOUT_VERSION,
        "version": version,
        "target": TARGET,
        "variant": VARIANT,
        "entrypoint": ENTRYPOINT,
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    for key, expected_value in expected.items():
        actual_value = metadata.get(key)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise CodexArtifactError(
                f"Invalid codex-package.json field {key!r}: "
                f"expected {expected_value!r}"
            )
    return metadata


def _verify_required_package_entries(
    release_root: Path, resolved_binary: Path
) -> None:
    for relative in REQUIRED_DIRECTORIES:
        _require_plain_directory(release_root / relative)
    for relative in REQUIRED_FILES:
        _require_plain_file(release_root / relative)

    canonical_entrypoint = (release_root / ENTRYPOINT).resolve(strict=True)
    if not _same_path(canonical_entrypoint, resolved_binary):
        raise CodexArtifactError(
            "Resolved Codex executable does not match the package entrypoint"
        )


def _release_manifest_url(version: str) -> str:
    return f"{RELEASES_ORIGIN}/codex/releases/{version}/release.json"


def _fetch_release_manifest(
    url: str, release_fetcher: ReleaseFetcher | None
) -> dict[str, object]:
    expected_path = urlsplit(url).path
    _require_official_url(url, expected_path=expected_path)

    if release_fetcher is None:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "video-replacer-codex-provenance/1",
            },
            method="GET",
        )
        try:
            with _open_official_release(request, expected_path) as response:
                raw = _read_http_response(response, expected_path)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise CodexArtifactError("Unable to fetch official Codex release metadata") from exc
        return _decode_json_object(raw, "Codex release manifest")

    try:
        fetched = release_fetcher(url)
    except Exception as exc:
        raise CodexArtifactError("Release metadata fetcher failed") from exc
    if isinstance(fetched, Mapping):
        try:
            raw = json.dumps(
                dict(fetched), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CodexArtifactError(
                "Release metadata fetcher returned a non-JSON mapping"
            ) from exc
    elif isinstance(fetched, str):
        raw = fetched.encode("utf-8")
    elif isinstance(fetched, (bytes, bytearray)):
        raw = bytes(fetched)
    elif hasattr(fetched, "read"):
        raw = _read_http_response(fetched, expected_path)
    else:
        raise CodexArtifactError("Release metadata fetcher returned an unsupported value")
    if len(raw) > RELEASE_MANIFEST_MAX_BYTES:
        raise CodexArtifactError("Codex release manifest exceeds the size limit")
    return _decode_json_object(raw, "Codex release manifest")


def _open_official_release(
    request: urllib.request.Request, expected_path: str
) -> object:
    opener = urllib.request.build_opener(_OfficialRedirectHandler(expected_path))
    return opener.open(request, timeout=30)


def _read_http_response(response: object, expected_path: str) -> bytes:
    final_url_getter = getattr(response, "geturl", None)
    if not callable(final_url_getter):
        raise CodexArtifactError("Release response did not expose its final URL")
    _require_official_url(str(final_url_getter()), expected_path=expected_path)

    status = getattr(response, "status", 200)
    if status != 200:
        raise CodexArtifactError(f"Codex release endpoint returned HTTP {status}")
    headers = getattr(response, "headers", {})
    content_encoding = _header_value(headers, "Content-Encoding")
    if content_encoding and content_encoding.casefold() != "identity":
        raise CodexArtifactError("Encoded Codex release manifests are not accepted")
    content_type = _header_value(headers, "Content-Type")
    if content_type and content_type.split(";", 1)[0].strip().casefold() not in {
        "application/json",
        "application/problem+json",
    }:
        raise CodexArtifactError("Codex release endpoint did not return JSON")
    content_length = _header_value(headers, "Content-Length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise CodexArtifactError("Invalid release manifest Content-Length") from exc
        if declared_length < 0 or declared_length > RELEASE_MANIFEST_MAX_BYTES:
            raise CodexArtifactError("Codex release manifest exceeds the size limit")

    reader = getattr(response, "read", None)
    if not callable(reader):
        raise CodexArtifactError("Release response body is unreadable")
    chunks: list[bytes] = []
    remaining = RELEASE_MANIFEST_MAX_BYTES + 1
    while remaining > 0:
        chunk = reader(min(64 * 1024, remaining))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise CodexArtifactError("Release response body was not bytes")
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > RELEASE_MANIFEST_MAX_BYTES:
        raise CodexArtifactError("Codex release manifest exceeds the size limit")
    return raw


def _release_asset(
    manifest: Mapping[str, object], version: str
) -> dict[str, str]:
    if manifest.get("tag_name") != f"rust-v{version}":
        raise CodexArtifactError("Codex release manifest tag does not match the package")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) > 4096:
        raise CodexArtifactError("Codex release manifest has an invalid asset list")
    matches = [
        item
        for item in assets
        if isinstance(item, Mapping) and item.get("name") == RELEASE_ASSET_NAME
    ]
    if len(matches) != 1:
        raise CodexArtifactError(
            "Codex release manifest must contain exactly one Windows x64 executable"
        )
    asset = matches[0]
    digest = asset.get("digest")
    if not isinstance(digest, str):
        raise CodexArtifactError("Codex release asset has no SHA-256 digest")
    digest_match = SHA256_PATTERN.fullmatch(digest)
    if digest_match is None:
        raise CodexArtifactError("Codex release asset has an invalid SHA-256 digest")
    asset_url = asset.get("browser_download_url")
    if not isinstance(asset_url, str):
        raise CodexArtifactError("Codex release asset has no download URL")
    expected_path = f"/codex/releases/{version}/{RELEASE_ASSET_NAME}"
    _require_official_url(asset_url, expected_path=expected_path)
    return {"sha256": digest_match.group(1).lower(), "url": asset_url}


def _require_official_url(url: str, *, expected_path: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CodexArtifactError("Invalid Codex release URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != RELEASES_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise CodexArtifactError("Codex release URL is outside the official HTTPS endpoint")


def _header_value(headers: object, name: str) -> str:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name, "")
    return str(value).strip() if value is not None else ""


def _decode_json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexArtifactError(f"{label} is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise CodexArtifactError(f"{label} must be a JSON object")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CodexArtifactError(f"Required Codex package entry is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _require_plain_directory(path: Path) -> None:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise CodexArtifactError(
            f"Required Codex package directory is missing or reparse-linked: {path}"
        )


def _require_plain_file(path: Path) -> None:
    if _is_link_or_reparse(path) or not path.is_file():
        raise CodexArtifactError(
            f"Required Codex package file is missing or reparse-linked: {path}"
        )


def _read_stable_file(path: Path, maximum_bytes: int) -> bytes:
    _require_plain_file(path)
    try:
        before = path.lstat()
        if before.st_size > maximum_bytes:
            raise CodexArtifactError(f"File exceeds the verification size limit: {path}")
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            raw = handle.read(maximum_bytes + 1)
            opened_after = os.fstat(handle.fileno())
        after = path.lstat()
    except OSError as exc:
        raise CodexArtifactError(f"Unable to read Codex package file: {path}") from exc
    if len(raw) > maximum_bytes:
        raise CodexArtifactError(f"File exceeds the verification size limit: {path}")
    if not (
        _stable_identity(before) == _stable_identity(opened_before)
        == _stable_identity(opened_after)
        == _stable_identity(after)
        and before.st_mode == after.st_mode
        and opened_before.st_mode == opened_after.st_mode
    ):
        raise CodexArtifactError(f"Codex package file changed during verification: {path}")
    return raw


def _sha256_stable_file(path: Path) -> str:
    _require_plain_file(path)
    digest = hashlib.sha256()
    try:
        before = path.lstat()
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            opened_after = os.fstat(handle.fileno())
        after = path.lstat()
    except OSError as exc:
        raise CodexArtifactError("Unable to hash Codex executable") from exc
    if not (
        _stable_identity(before) == _stable_identity(opened_before)
        == _stable_identity(opened_after)
        == _stable_identity(after)
        and before.st_mode == after.st_mode
        and opened_before.st_mode == opened_after.st_mode
    ):
        raise CodexArtifactError("Codex executable changed during verification")
    return digest.hexdigest()


def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        # CPython's Windows path-stat layer infers 0111 from an .exe suffix,
        # while fstat() has no pathname and reports the same regular file
        # without those derived permission bits.  File type remains part of
        # the identity; extension-derived permissions do not.
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


__all__ = ["CodexArtifactError", "verify_windows_codex"]

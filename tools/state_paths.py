#!/usr/bin/env python3
"""Single authoritative external state-root resolver for every entrypoint."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional


STATE_DIRECTORY_NAME = "video-replacer"
STATE_OVERRIDE_NAME = "VIDEO_REPLACER_STATE_DIR"

_WINDOWS_DRIVE_FIXED = 3
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_WINDOWS_NOT_FOUND_ERRORS = frozenset({2, 3})
_WINDOWS_FOLDERID_LOCAL_APP_DATA = (
    0xF1B32785,
    0x6FBA,
    0x4FCF,
    (0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
)


class StatePathError(RuntimeError):
    pass


def _source_environment(
    source: Optional[Mapping[str, str]] = None,
) -> Mapping[str, str]:
    return os.environ if source is None else source


def _is_windows_host() -> bool:
    return os.name == "nt"


def _windows_api() -> tuple[Any, Any, Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return ctypes, wintypes, shell32, ole32, kernel32


def _windows_known_local_app_data() -> Path:
    """Resolve LocalAppData from the user token, never from environment text."""

    ctypes, wintypes, shell32, ole32, _kernel32 = _windows_api()

    class _Guid(ctypes.Structure):
        _fields_ = (
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        )

    data1, data2, data3, data4 = _WINDOWS_FOLDERID_LOCAL_APP_DATA
    folder_id = _Guid(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(_Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (wintypes.LPVOID,)
    ole32.CoTaskMemFree.restype = None

    allocated = wintypes.LPWSTR()
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id), 0, None, ctypes.byref(allocated)
    )
    try:
        if result != 0 or not allocated:
            raise StatePathError(
                f"Windows LocalAppData known-folder lookup failed (HRESULT 0x{result & 0xFFFFFFFF:08x})"
            )
        value = allocated.value
        if not value:
            raise StatePathError("Windows LocalAppData known-folder lookup was empty")
        return Path(value)
    finally:
        if allocated:
            ole32.CoTaskMemFree(ctypes.cast(allocated, wintypes.LPVOID))


def _windows_path_is_reparse(path: Path) -> bool:
    ctypes, _wintypes, _shell32, _ole32, kernel32 = _windows_api()
    kernel32.GetFileAttributesW.argtypes = (ctypes.c_wchar_p,)
    kernel32.GetFileAttributesW.restype = ctypes.c_uint32
    attributes = kernel32.GetFileAttributesW(str(path))
    if attributes == _WINDOWS_INVALID_FILE_ATTRIBUTES:
        error_code = ctypes.get_last_error()
        if error_code in _WINDOWS_NOT_FOUND_ERRORS:
            return False
        raise StatePathError(
            f"Windows state path attribute lookup failed (error {error_code})"
        )
    return bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_require_fixed_local_path(path: Path) -> None:
    ctypes, _wintypes, _shell32, _ole32, kernel32 = _windows_api()
    kernel32.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        raise StatePathError("Windows state root must use the local fixed system drive")
    anchor = str(path.anchor)
    if not anchor or kernel32.GetDriveTypeW(anchor) != _WINDOWS_DRIVE_FIXED:
        raise StatePathError("Windows state root must use the local fixed system drive")


def _windows_path_contains_reparse(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink() or _windows_path_is_reparse(cursor):
            return True
    return False


def _windows_canonical_safe_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    if not candidate.is_absolute():
        raise StatePathError("Windows state root must be absolute")
    _windows_require_fixed_local_path(candidate)
    if _windows_path_contains_reparse(candidate):
        raise StatePathError("Windows state root must not use a reparse point")
    resolved = candidate.resolve(strict=False)
    _windows_require_fixed_local_path(resolved)
    if _windows_path_contains_reparse(resolved):
        raise StatePathError("Windows state root must not use a reparse point")
    return resolved


def _windows_paths_equal(left: Path, right: Path) -> bool:
    return os.fspath(left).casefold() == os.fspath(right).casefold()


def resolve_state_root(
    source: Optional[Mapping[str, str]] = None,
) -> Path:
    """Resolve the stable external state root under the platform contract.

    Windows is intentionally immutable: the only accepted root is the real
    current user's ``FOLDERID_LocalAppData/video-replacer``. An override may
    restate that exact canonical path, but cannot redirect credentials or
    ledgers into a shared parent. POSIX retains its existing absolute override
    and XDG semantics.
    """

    environment = _source_environment(source)
    configured = str(environment.get(STATE_OVERRIDE_NAME, "")).strip()
    if _is_windows_host():
        local_app_data = _windows_canonical_safe_path(
            _windows_known_local_app_data()
        )
        if not local_app_data.is_dir():
            raise StatePathError("Windows LocalAppData known folder is unavailable")
        expected = _windows_canonical_safe_path(
            local_app_data / STATE_DIRECTORY_NAME
        )
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                raise StatePathError(
                    "VIDEO_REPLACER_STATE_DIR must be the canonical Windows state root"
                )
            candidate = _windows_canonical_safe_path(candidate)
            if not _windows_paths_equal(candidate, expected):
                raise StatePathError(
                    "VIDEO_REPLACER_STATE_DIR must equal LocalAppData/video-replacer on Windows"
                )
        return expected

    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise StatePathError("VIDEO_REPLACER_STATE_DIR must be absolute")
        return Path(os.path.abspath(os.fspath(candidate)))
    base = Path(
        str(environment.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    ).expanduser()
    if not base.is_absolute():
        raise StatePathError("XDG_STATE_HOME must be absolute")
    return Path(os.path.abspath(os.fspath(base / STATE_DIRECTORY_NAME)))

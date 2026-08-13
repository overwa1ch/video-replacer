#!/usr/bin/env python3
"""Construct the credential-minimized environment for Dreamina CLI calls."""

from __future__ import annotations

import ctypes
import ntpath
import os
import shutil
from typing import Dict, Mapping, Optional


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
WINDOWS_AGENT_DETECT_EXECUTABLES = ("powershell.exe", "pwsh.exe")


class DreaminaEnvironmentError(RuntimeError):
    pass


def _normalized_allowlist(
    source: Mapping[str, str], *, windows: bool
) -> Dict[str, str]:
    environment: Dict[str, str] = {}
    for key, value in source.items():
        normalized = key.upper()
        if windows:
            if normalized not in SAFE_ENV_NAMES and not normalized.startswith("LC_"):
                continue
            previous = environment.get(normalized)
            if previous is not None and previous != value:
                raise DreaminaEnvironmentError(
                    f"conflicting environment values for {normalized}"
                )
            environment[normalized] = value
        elif key in SAFE_ENV_NAMES or key.startswith("LC_"):
            environment[key] = value
    return environment


def windows_dreamina_path(
    environment: Mapping[str, str],
    *,
    directories: Optional[tuple[str, str]] = None,
) -> str:
    """Return the minimal native path needed by the reviewed Windows CLI.

    Dreamina 1.4.15 synchronously probes its parent process with PowerShell
    after every command and applies no timeout to that CIM query.  Keeping
    only the two non-recursive Windows system directories preserves native
    helpers such as ``cmd.exe`` and ``rundll32.exe`` while making that
    optional probe fail immediately.
    """

    system_root, system_directory = directories or _windows_directories(environment)
    filtered = ";".join((system_directory, system_root))
    for executable in WINDOWS_AGENT_DETECT_EXECUTABLES:
        if shutil.which(executable, path=filtered):
            raise DreaminaEnvironmentError(
                f"unable to isolate Dreamina from {executable}"
            )
    return filtered


def _validated_windows_directory(raw: str, *, label: str) -> str:
    value = raw.strip().strip('"')
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith(("\\", "/")):
        raise DreaminaEnvironmentError(
            f"{label} is required to isolate the Dreamina Windows subprocess"
        )
    normalized = ntpath.normpath(value)
    if normalized.startswith(("\\\\", "//")):
        raise DreaminaEnvironmentError(f"{label} must be on a local Windows drive")
    return normalized


def _windows_api_directory(function_name: str) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(kernel32, function_name)
    function.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
    function.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise DreaminaEnvironmentError(
            "unable to resolve trusted Windows directories for Dreamina"
        )
    return buffer.value


def _windows_directories(environment: Mapping[str, str]) -> tuple[str, str]:
    if os.name == "nt":
        system_root = _windows_api_directory("GetWindowsDirectoryW")
        system_directory = _windows_api_directory("GetSystemDirectoryW")
    else:
        # Explicit Windows semantic tests on another host use controlled
        # fixture values; production never trusts these environment fields.
        supplied_root = str(environment.get("SYSTEMROOT") or "").strip()
        supplied_windir = str(environment.get("WINDIR") or "").strip()
        if supplied_root and supplied_windir and ntpath.normcase(
            ntpath.normpath(supplied_root)
        ) != ntpath.normcase(ntpath.normpath(supplied_windir)):
            raise DreaminaEnvironmentError("SYSTEMROOT and WINDIR disagree")
        system_root = str(
            environment.get("SYSTEMROOT") or environment.get("WINDIR") or ""
        )
        system_directory = ntpath.join(system_root, "System32")
    root = _validated_windows_directory(system_root, label="SYSTEMROOT")
    directory = _validated_windows_directory(
        system_directory, label="Windows system directory"
    )
    if ntpath.normcase(ntpath.splitdrive(root)[0]) != ntpath.normcase(
        ntpath.splitdrive(directory)[0]
    ):
        raise DreaminaEnvironmentError("Windows system directories disagree")
    return root, directory


def dreamina_environment(
    source: Optional[Mapping[str, str]] = None,
    *,
    windows: Optional[bool] = None,
) -> Dict[str, str]:
    """Exclude provider credentials and the upstream unbounded Windows probe."""

    source_env = os.environ if source is None else source
    is_windows = os.name == "nt" if windows is None else windows
    environment = _normalized_allowlist(source_env, windows=is_windows)
    if is_windows:
        system_root, system_directory = _windows_directories(environment)
        environment["PATH"] = windows_dreamina_path(
            environment, directories=(system_root, system_directory)
        )
        environment["SYSTEMROOT"] = system_root
        environment["WINDIR"] = system_root
        environment["SYSTEMDRIVE"] = ntpath.splitdrive(system_root)[0]
        environment["COMSPEC"] = ntpath.join(system_directory, "cmd.exe")
        environment["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        environment.pop("SHELL", None)
        # Go's Windows LookPath otherwise checks the subprocess cwd even when
        # PATH no longer exposes PowerShell.
        environment["NODEFAULTCURRENTDIRECTORYINEXEPATH"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment

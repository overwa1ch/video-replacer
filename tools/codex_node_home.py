#!/usr/bin/env python3
"""Stable, instruction-free Codex credential home for prompt-only nodes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from state_paths import StatePathError, resolve_state_root


NODE_HOME_DIRECTORY = "codex-node-home"
LOCK_FILENAME = ".video-replacer-auth.lock"
FORBIDDEN_INSTRUCTION_ENTRIES = (
    "AGENTS.md",
    "AGENTS.override.md",
    "config.toml",
    "hooks",
    "memories",
    "plugins",
    "rules",
    "skills",
)
MAX_AUTH_FILE_BYTES = 1024 * 1024

_AUTH_TOP_LEVEL_FIELDS = frozenset(
    {
        "auth_mode",
        "OPENAI_API_KEY",
        "tokens",
        "last_refresh",
        "agent_identity",
        "personal_access_token",
        "bedrock_api_key",
    }
)
_AUTH_REQUIRED_FIELDS = frozenset(
    {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
)
_TOKEN_FIELDS = frozenset(
    {"id_token", "access_token", "refresh_token", "account_id"}
)
_AGENT_IDENTITY_FIELDS = frozenset(
    {
        "agent_runtime_id",
        "agent_private_key",
        "account_id",
        "chatgpt_user_id",
        "email",
        "plan_type",
        "chatgpt_account_is_fedramp",
        "task_id",
    }
)
_AGENT_IDENTITY_REQUIRED_FIELDS = frozenset(
    {
        "agent_runtime_id",
        "agent_private_key",
        "account_id",
        "chatgpt_user_id",
        "plan_type",
        "chatgpt_account_is_fedramp",
    }
)
_BASE64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_RFC3339_TIMESTAMP = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
_AUTH_SCHEMA_ERROR = (
    "Codex prompt-node auth.json does not match the supported ChatGPT file-auth schema"
)
class CodexNodeHomeError(RuntimeError):
    pass


_PROCESS_LOCK = threading.Lock()

_WINDOWS_OWNER_SECURITY_INFORMATION = 0x00000001
_WINDOWS_DACL_SECURITY_INFORMATION = 0x00000004
_WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_WINDOWS_SE_FILE_OBJECT = 1
_WINDOWS_TOKEN_QUERY = 0x0008
_WINDOWS_TOKEN_USER = 1
_WINDOWS_ACL_REVISION = 2
_WINDOWS_ACL_SIZE_INFORMATION = 2
_WINDOWS_ACCESS_ALLOWED_ACE_TYPE = 0x00
_WINDOWS_OBJECT_INHERIT_ACE = 0x01
_WINDOWS_CONTAINER_INHERIT_ACE = 0x02
_WINDOWS_NO_PROPAGATE_INHERIT_ACE = 0x04
_WINDOWS_INHERIT_ONLY_ACE = 0x08
_WINDOWS_INHERITED_ACE = 0x10
_WINDOWS_SE_DACL_PROTECTED = 0x1000
_WINDOWS_FILE_ALL_ACCESS = 0x001F01FF
_WINDOWS_LOCAL_SYSTEM_SID = 22
_WINDOWS_SECURITY_MAX_SID_SIZE = 68
_WINDOWS_DRIVE_FIXED = 3
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_WINDOWS_NOT_FOUND_ERRORS = frozenset({2, 3})


class _DuplicateJsonKey(ValueError):
    pass


def _is_windows_host() -> bool:
    return os.name == "nt"


def _windows_failure(operation: str, error_code: int) -> CodexNodeHomeError:
    return CodexNodeHomeError(
        f"Windows Codex prompt-node ACL {operation} failed (error {error_code})"
    )


def _windows_security_api() -> tuple[Any, Any, Any, Any]:
    """Load explicitly typed native security APIs only on a Windows host."""

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = (wintypes.LPVOID,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.CopySid.argtypes = (
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.CreateWellKnownSid.argtypes = (
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = (wintypes.LPVOID,)
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    advapi32.EqualSid.restype = wintypes.BOOL

    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return ctypes, wintypes, advapi32, kernel32


def _windows_current_user_sid() -> Any:
    ctypes, wintypes, advapi32, kernel32 = _windows_security_api()

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD))

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _WINDOWS_TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _windows_failure("current-user lookup", ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _WINDOWS_TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            raise _windows_failure("current-user lookup", ctypes.get_last_error())
        token_data = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _WINDOWS_TOKEN_USER,
            token_data,
            required.value,
            ctypes.byref(required),
        ):
            raise _windows_failure("current-user lookup", ctypes.get_last_error())
        token_user = ctypes.cast(
            token_data, ctypes.POINTER(_SidAndAttributes)
        ).contents
        sid_length = advapi32.GetLengthSid(token_user.sid)
        if sid_length == 0:
            raise _windows_failure("current-user lookup", ctypes.get_last_error())
        sid = ctypes.create_string_buffer(sid_length)
        if not advapi32.CopySid(sid_length, sid, token_user.sid):
            raise _windows_failure("current-user lookup", ctypes.get_last_error())
        return sid
    finally:
        kernel32.CloseHandle(token)


def _windows_system_sid() -> Any:
    ctypes, wintypes, advapi32, _kernel32 = _windows_security_api()
    size = wintypes.DWORD(_WINDOWS_SECURITY_MAX_SID_SIZE)
    sid = ctypes.create_string_buffer(size.value)
    if not advapi32.CreateWellKnownSid(
        _WINDOWS_LOCAL_SYSTEM_SID, None, sid, ctypes.byref(size)
    ):
        raise _windows_failure("SYSTEM SID lookup", ctypes.get_last_error())
    return sid


def _windows_require_current_user_owner(path: Path) -> None:
    """Read only the live owner and reject mutation of another owner's object."""

    ctypes, wintypes, advapi32, kernel32 = _windows_security_api()
    user_sid = _windows_current_user_sid()
    owner = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _WINDOWS_SE_FILE_OBJECT,
        _WINDOWS_OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise _windows_failure("owner lookup", int(result))
    try:
        if not owner or not advapi32.EqualSid(owner, user_sid):
            raise CodexNodeHomeError(
                "Windows Codex prompt-node data owner must be the current user"
            )
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _windows_apply_private_acl(path: Path, *, is_directory: bool) -> None:
    """Replace the DACL with current-user and SYSTEM full-control ACEs."""

    # Owners can replace a DACL through implicit WRITE_DAC, but changing the
    # owner requires WRITE_OWNER or a privilege ordinary users do not have.
    # The object must already belong to this user; never attempt ownership
    # takeover or even redundantly submit OWNER_SECURITY_INFORMATION.
    _windows_require_current_user_owner(path)
    ctypes, wintypes, advapi32, _kernel32 = _windows_security_api()
    user_sid = _windows_current_user_sid()
    system_sid = _windows_system_sid()
    user_length = advapi32.GetLengthSid(user_sid)
    system_length = advapi32.GetLengthSid(system_sid)
    if user_length == 0 or system_length == 0:
        raise _windows_failure("SID sizing", ctypes.get_last_error())

    # ACL header is 8 bytes. An ACCESS_ALLOWED_ACE contributes its 8-byte
    # header/mask prefix plus the complete variable-length SID.
    acl_size = 8 + 8 + user_length + 8 + system_length
    acl = ctypes.create_string_buffer(acl_size)
    if not advapi32.InitializeAcl(acl, acl_size, _WINDOWS_ACL_REVISION):
        raise _windows_failure("initialization", ctypes.get_last_error())
    ace_flags = (
        _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_CONTAINER_INHERIT_ACE
        if is_directory
        else 0
    )
    for label, sid in (("current-user ACE", user_sid), ("SYSTEM ACE", system_sid)):
        if not advapi32.AddAccessAllowedAceEx(
            acl,
            _WINDOWS_ACL_REVISION,
            ace_flags,
            _WINDOWS_FILE_ALL_ACCESS,
            sid,
        ):
            raise _windows_failure(label, ctypes.get_last_error())

    mutable_path = ctypes.create_unicode_buffer(str(path))
    result = advapi32.SetNamedSecurityInfoW(
        mutable_path,
        _WINDOWS_SE_FILE_OBJECT,
        _WINDOWS_DACL_SECURITY_INFORMATION
        | _WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    if result != 0:
        raise _windows_failure("application", int(result))


def _windows_verify_private_acl(path: Path, *, is_directory: bool) -> None:
    """Verify the live owner/DACL through native SID and ACE inspection."""

    ctypes, wintypes, advapi32, kernel32 = _windows_security_api()

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        )

    class _AceHeader(ctypes.Structure):
        _fields_ = (
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", ctypes.c_ushort),
        )

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = (
            ("header", _AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        )

    user_sid = _windows_current_user_sid()
    system_sid = _windows_system_sid()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _WINDOWS_SE_FILE_OBJECT,
        _WINDOWS_OWNER_SECURITY_INFORMATION | _WINDOWS_DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise _windows_failure("verification lookup", int(result))
    try:
        if not owner or not advapi32.EqualSid(owner, user_sid):
            raise CodexNodeHomeError(
                "Windows Codex prompt-node data owner must be the current user"
            )
        if not dacl:
            raise CodexNodeHomeError(
                "Windows Codex prompt-node data must have a private DACL"
            )
        control = ctypes.c_ushort()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise _windows_failure("DACL control lookup", ctypes.get_last_error())
        if is_directory and not (control.value & _WINDOWS_SE_DACL_PROTECTED):
            raise CodexNodeHomeError(
                "Windows Codex prompt-node directory DACL must disable inheritance"
            )

        acl_info = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _WINDOWS_ACL_SIZE_INFORMATION,
        ):
            raise _windows_failure("DACL inspection", ctypes.get_last_error())
        if acl_info.ace_count != 2:
            raise CodexNodeHomeError(
                "Windows Codex prompt-node DACL must contain only current-user and SYSTEM ACEs"
            )

        found_user = False
        found_system = False
        sid_offset = _AccessAllowedAce.sid_start.offset
        for index in range(acl_info.ace_count):
            ace_pointer = wintypes.LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise _windows_failure("ACE inspection", ctypes.get_last_error())
            if not ace_pointer:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL contains an invalid ACE"
                )
            ace = ctypes.cast(
                ace_pointer, ctypes.POINTER(_AccessAllowedAce)
            ).contents
            if ace.header.ace_type != _WINDOWS_ACCESS_ALLOWED_ACE_TYPE:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL contains a non-allow ACE"
                )
            if ace.mask != _WINDOWS_FILE_ALL_ACCESS:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL grants an unexpected access mask"
                )
            expected_flags = (
                _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_CONTAINER_INHERIT_ACE
                if is_directory
                else 0
            )
            actual_flags = int(ace.header.ace_flags)
            if is_directory:
                if actual_flags != expected_flags:
                    raise CodexNodeHomeError(
                        "Windows Codex prompt-node directory DACL has unsafe inheritance flags"
                    )
            elif actual_flags not in (expected_flags, _WINDOWS_INHERITED_ACE):
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node file DACL has unsafe inheritance flags"
                )
            if ace.header.ace_size <= sid_offset:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL contains a truncated ACE"
                )
            sid_pointer = wintypes.LPVOID(int(ace_pointer.value) + sid_offset)
            if not advapi32.IsValidSid(sid_pointer):
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL contains an invalid SID"
                )
            sid_length = advapi32.GetLengthSid(sid_pointer)
            if sid_length == 0 or sid_offset + sid_length > ace.header.ace_size:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL contains a truncated SID"
                )
            if advapi32.EqualSid(sid_pointer, user_sid):
                if found_user:
                    raise CodexNodeHomeError(
                        "Windows Codex prompt-node DACL duplicates the current-user ACE"
                    )
                found_user = True
            elif advapi32.EqualSid(sid_pointer, system_sid):
                if found_system:
                    raise CodexNodeHomeError(
                        "Windows Codex prompt-node DACL duplicates the SYSTEM ACE"
                    )
                found_system = True
            else:
                raise CodexNodeHomeError(
                    "Windows Codex prompt-node DACL grants access to an unexpected SID"
                )
        if not found_user or not found_system:
            raise CodexNodeHomeError(
                "Windows Codex prompt-node DACL must grant current-user and SYSTEM access"
            )
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)


def _windows_path_is_reparse(path: Path) -> bool:
    ctypes, _wintypes, _advapi32, kernel32 = _windows_security_api()
    attributes = kernel32.GetFileAttributesW(str(path))
    if attributes == _WINDOWS_INVALID_FILE_ATTRIBUTES:
        error_code = ctypes.get_last_error()
        if error_code in _WINDOWS_NOT_FOUND_ERRORS:
            return False
        raise _windows_failure("path attribute lookup", error_code)
    return bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_require_local_fixed_path(path: Path) -> None:
    _ctypes, _wintypes, _advapi32, kernel32 = _windows_security_api()
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        raise CodexNodeHomeError(
            "Codex prompt-node credential home must use a local Windows drive"
        )
    anchor = str(path.anchor)
    if not anchor or kernel32.GetDriveTypeW(anchor) != _WINDOWS_DRIVE_FIXED:
        raise CodexNodeHomeError(
            "Codex prompt-node credential home must use a local fixed Windows drive"
        )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_supported_id_token(value: object) -> bool:
    if not _is_nonempty_string(value):
        return False
    assert isinstance(value, str)
    segments = value.split(".")
    if len(segments) != 3 or any(
        not _BASE64URL_SEGMENT.fullmatch(segment) for segment in segments
    ):
        return False
    payload = segments[1]
    padded_payload = payload + ("=" * (-len(payload) % 4))
    try:
        decoded_payload = base64.b64decode(
            padded_payload,
            altchars=b"-_",
            validate=True,
        )
        claims = json.loads(
            decoded_payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        return False
    return isinstance(claims, dict)


def _is_supported_timestamp(value: object) -> bool:
    if not _is_nonempty_string(value):
        return False
    assert isinstance(value, str)
    match = _RFC3339_TIMESTAMP.fullmatch(value)
    if match is None:
        return False
    fraction = match.group("fraction")
    normalized_fraction = (
        "." + (fraction + "000000")[:6] if fraction is not None else ""
    )
    offset = "+00:00" if match.group("offset") == "Z" else match.group("offset")
    normalized = match.group("base") + normalized_fraction + offset
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_supported_agent_identity(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    fields = set(value)
    if fields - _AGENT_IDENTITY_FIELDS:
        return False
    if not _AGENT_IDENTITY_REQUIRED_FIELDS.issubset(fields):
        return False
    if not all(
        _is_nonempty_string(value[field])
        for field in (
            "agent_runtime_id",
            "agent_private_key",
            "account_id",
            "chatgpt_user_id",
            "plan_type",
        )
    ):
        return False
    if type(value["chatgpt_account_is_fedramp"]) is not bool:
        return False
    email = value.get("email")
    if email is not None and not isinstance(email, str):
        return False
    task_id = value.get("task_id")
    return task_id is None or _is_nonempty_string(task_id)


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _file_content_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_stable_file_auth(auth_path: Path) -> bytes:
    """Read one bounded regular file and detect replacement or mutation."""

    auth = Path(auth_path)
    if is_link_like(auth):
        raise CodexNodeHomeError(_AUTH_SCHEMA_ERROR)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(str(auth), flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            raw = handle.read(MAX_AUTH_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        path_after = os.stat(auth, follow_symlinks=False)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise CodexNodeHomeError(_AUTH_SCHEMA_ERROR) from None
    if (
        is_link_like(auth)
        or not stat.S_ISREG(path_after.st_mode)
        or _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(path_after)
        or _file_content_identity(before) != _file_content_identity(after)
        or _file_content_identity(after) != _file_content_identity(path_after)
        or after.st_size != len(raw)
    ):
        raise CodexNodeHomeError(_AUTH_SCHEMA_ERROR)
    if not raw or len(raw) > MAX_AUTH_FILE_BYTES:
        raise CodexNodeHomeError(_AUTH_SCHEMA_ERROR)
    return raw


def _validated_file_auth_access_token(raw: bytes) -> Optional[str]:
    """Return the access token only for a fully validated exact byte snapshot."""

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    fields = set(payload)
    if fields - _AUTH_TOP_LEVEL_FIELDS:
        return None
    if not _AUTH_REQUIRED_FIELDS.issubset(fields):
        return None
    if payload["auth_mode"] != "chatgpt" or payload["OPENAI_API_KEY"] is not None:
        return None

    tokens = payload["tokens"]
    if not isinstance(tokens, dict) or set(tokens) != _TOKEN_FIELDS:
        return None
    if not _is_supported_id_token(tokens["id_token"]):
        return None
    if not all(
        _is_nonempty_string(tokens[field])
        for field in ("access_token", "refresh_token", "account_id")
    ):
        return None
    if not _is_supported_timestamp(payload["last_refresh"]):
        return None
    if not _is_supported_agent_identity(payload.get("agent_identity")):
        return None
    if payload.get("personal_access_token") is not None:
        return None
    if payload.get("bedrock_api_key") is not None:
        return None
    access_token = tokens["access_token"]
    return access_token if isinstance(access_token, str) else None


def file_auth_access_token_sha256(auth_path: Path) -> bytes:
    """Validate one stable snapshot and return only its access-token SHA-256."""

    raw = _read_stable_file_auth(auth_path)
    access_token = _validated_file_auth_access_token(raw)
    if access_token is None:
        raise CodexNodeHomeError(_AUTH_SCHEMA_ERROR)
    digest = hashlib.sha256(access_token.encode("utf-8")).digest()
    del access_token, raw
    return digest


def validate_file_auth(auth_path: Path) -> None:
    """Validate file auth from one stable read without exposing token material."""

    file_auth_access_token_sha256(auth_path)


def _source_environment(
    source: Optional[Mapping[str, str]] = None,
) -> Mapping[str, str]:
    return os.environ if source is None else source


def is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    if _is_windows_host() and _windows_path_is_reparse(path):
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


def default_state_root(source: Optional[Mapping[str, str]] = None) -> Path:
    try:
        return resolve_state_root(source)
    except StatePathError as exc:
        raise CodexNodeHomeError(str(exc)) from exc


def node_home_path(source: Optional[Mapping[str, str]] = None) -> Path:
    return default_state_root(source) / NODE_HOME_DIRECTORY


def _temporary_roots(source: Optional[Mapping[str, str]] = None) -> set[Path]:
    environment = _source_environment(source)
    roots = {Path(tempfile.gettempdir()).expanduser().resolve()}
    for variable in ("TMPDIR", "TMP", "TEMP"):
        raw = str(environment.get(variable, "")).strip()
        if raw:
            roots.add(Path(raw).expanduser().resolve())
    return roots


def validate_node_home_structure(
    path: Path,
    *,
    repo_root: Path,
    require_auth: bool,
    source: Optional[Mapping[str, str]] = None,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if _is_windows_host():
        _windows_require_local_fixed_path(absolute)
    if not absolute.is_dir() or path_contains_link_like(absolute):
        raise CodexNodeHomeError(
            "Codex prompt-node credential home is missing or uses a link/junction"
        )
    resolved = absolute.resolve()
    if _is_windows_host():
        _windows_require_local_fixed_path(resolved)
        if path_contains_link_like(resolved):
            raise CodexNodeHomeError(
                "Codex prompt-node credential home uses a Windows reparse point"
            )
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        pass
    else:
        raise CodexNodeHomeError(
            "Codex prompt-node credential home must remain outside the repository"
        )
    if any(resolved == root or root in resolved.parents for root in _temporary_roots(source)):
        raise CodexNodeHomeError(
            "Codex prompt-node credential home must not use a temporary directory"
        )
    forbidden = [
        name
        for name in FORBIDDEN_INSTRUCTION_ENTRIES
        if (resolved / name).exists() or is_link_like(resolved / name)
    ]
    if forbidden:
        raise CodexNodeHomeError(
            "Codex prompt-node credential home contains forbidden instruction/config "
            "entries: " + ", ".join(forbidden)
        )
    auth = resolved / "auth.json"
    if is_link_like(auth):
        raise CodexNodeHomeError("Codex prompt-node auth.json must not be a link")
    if require_auth and not auth.is_file():
        raise CodexNodeHomeError(
            "Codex prompt-node login is missing; ask the repository Agent to run login-codex-node"
        )
    if auth.exists() and not auth.is_file():
        raise CodexNodeHomeError("Codex prompt-node auth.json is not a regular file")
    if _is_windows_host():
        lock = resolved / LOCK_FILENAME
        if is_link_like(lock):
            raise CodexNodeHomeError(
                "Codex prompt-node auth lock must not use a Windows reparse point"
            )
        if lock.exists() and not lock.is_file():
            raise CodexNodeHomeError("Codex prompt-node auth lock is not a regular file")
        _windows_verify_private_acl(resolved, is_directory=True)
        if auth.is_file():
            _windows_verify_private_acl(auth, is_directory=False)
        if lock.is_file():
            _windows_verify_private_acl(lock, is_directory=False)
    else:
        if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
            raise CodexNodeHomeError(
                "Codex prompt-node credential home permissions must be 0700"
            )
        if auth.is_file() and stat.S_IMODE(auth.stat().st_mode) != 0o600:
            raise CodexNodeHomeError("Codex prompt-node auth.json permissions must be 0600")
    return resolved


def validate_node_home(
    path: Path,
    *,
    repo_root: Path,
    require_auth: bool,
    source: Optional[Mapping[str, str]] = None,
) -> Path:
    resolved = validate_node_home_structure(
        path,
        repo_root=repo_root,
        require_auth=require_auth,
        source=source,
    )
    auth = resolved / "auth.json"
    if auth.is_file():
        validate_file_auth(auth)
        if _is_windows_host():
            # Re-read security after parsing to catch ACL replacement during validation.
            _windows_verify_private_acl(auth, is_directory=False)
    return resolved


def ensure_node_home(
    path: Path,
    *,
    repo_root: Path,
    source: Optional[Mapping[str, str]] = None,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if _is_windows_host():
        _windows_require_local_fixed_path(absolute)
    if path_contains_link_like(absolute):
        raise CodexNodeHomeError(
            "Codex prompt-node credential home must not use a link/junction"
        )
    absolute.mkdir(parents=True, exist_ok=True)
    if _is_windows_host():
        if path_contains_link_like(absolute):
            raise CodexNodeHomeError(
                "Codex prompt-node credential home must not use a Windows reparse point"
            )
        _windows_apply_private_acl(absolute, is_directory=True)
        for data_path in (
            absolute / "auth.json",
            absolute / LOCK_FILENAME,
        ):
            if is_link_like(data_path):
                raise CodexNodeHomeError(
                    "Codex prompt-node credential data must not use a Windows reparse point"
                )
            if data_path.exists():
                if not data_path.is_file():
                    raise CodexNodeHomeError(
                        "Codex prompt-node credential data is not a regular file"
                    )
                _windows_apply_private_acl(data_path, is_directory=False)
    else:
        absolute.chmod(0o700)
    return validate_node_home(
        absolute,
        repo_root=repo_root,
        require_auth=False,
        source=source,
    )


@contextmanager
def locked_node_home(home: Path, *, timeout_seconds: float = 120.0) -> Iterator[None]:
    """Serialize all auth-bearing Codex processes across parent processes."""

    if not _PROCESS_LOCK.acquire(timeout=timeout_seconds):
        raise CodexNodeHomeError(
            "timed out waiting for the in-process Codex prompt-node auth lock"
        )
    lock_path = home / LOCK_FILENAME
    descriptor: Optional[int] = None
    try:
        if not home.is_dir() or path_contains_link_like(home):
            raise CodexNodeHomeError(
                "Codex prompt-node credential home is unsafe before locking"
            )
        if is_link_like(lock_path):
            raise CodexNodeHomeError(
                "Codex prompt-node auth lock must not use a link/reparse point"
            )
        if _is_windows_host():
            _windows_require_local_fixed_path(home)
            _windows_verify_private_acl(home, is_directory=True)
        open_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        descriptor = os.open(
            str(lock_path),
            open_flags,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CodexNodeHomeError(
                "Codex prompt-node auth lock is not a safe regular file"
            )
        if _is_windows_host():
            if is_link_like(lock_path) or not lock_path.is_file():
                raise CodexNodeHomeError(
                    "Codex prompt-node auth lock is not a safe regular file"
                )
            _windows_apply_private_acl(lock_path, is_directory=False)
            _windows_verify_private_acl(lock_path, is_directory=False)
        handle_context = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = None
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        _PROCESS_LOCK.release()
        raise
    try:
        with handle_context as handle:
            if not _is_windows_host():
                os.chmod(lock_path, 0o600)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    handle.seek(0)
                    if _is_windows_host():
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise CodexNodeHomeError(
                            "timed out waiting for the Codex prompt-node auth lock"
                        ) from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                if _is_windows_host():
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        _PROCESS_LOCK.release()

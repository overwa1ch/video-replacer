#!/usr/bin/env python3
"""Loopback attestation for the exact prompt-node Codex request surface."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping

import video_batch_loop as loop
from codex_node_home import (
    CodexNodeHomeError,
    file_auth_access_token_sha256,
)


FORBIDDEN_AGENT_HINTS = (
    "primary agent in a team",
    "multi_agent",
    "multi-agent",
    "multi agent",
    "subagent",
    "sub-agent",
    "spawn_agent",
    "collaboration tools",
)

TOOL_SCHEMA_TYPES = frozenset(
    {
        "code_interpreter",
        "computer_use",
        "computer_use_preview",
        "custom",
        "file_search",
        "function",
        "image_generation",
        "local_shell",
        "mcp",
        "shell",
        "web_search",
        "web_search_preview",
    }
)
INPUT_TOOL_ITEM_TYPES = frozenset(
    {"extension", "extensions", "tool", "tool_schema", "tools"}
)
INPUT_TOOL_MARKER_KEYS = frozenset(
    {"additional_tools", "input_schema", "tool_schema"}
)
INPUT_TOOL_CONTAINER_KEYS = frozenset({"extensions", "tools"})
_SERIALIZED_TOOL_MARKER = re.compile(
    r"(?ix)"
    r"(?:"
    r"<\s*/?\s*additional[_-]?tools\b"
    r"|[\"'](?:additional_tools|input_schema|tool_schema)[\"']\s*:"
    r")"
)
_LINE_TOOL_MARKER = re.compile(
    r"(?im)^\s*(?:additional_tools|input_schema|tool_schema)\s*"
    r"(?::\s*(?:$|[\[{\-])|=)"
)
_SERIALIZED_TOOL_TYPE = re.compile(
    r"(?ix)[\"']type[\"']\s*:\s*[\"'](?:"
    + "|".join(sorted(TOOL_SCHEMA_TYPES))
    + r")[\"']"
)
_SERIALIZED_TOOL_FIELD = re.compile(
    r"(?ix)[\"'](?:function|input_schema|name|parameters|server_label|tool_schema)"
    r"[\"']\s*:"
)
_DECLARED_TOOL_TABLE = re.compile(r"(?i)\bdeclare\s+const\s+tools\s*:")
_TOOL_DECLARATION_LABEL = re.compile(
    r"(?im)^\s*(?://\s*)?[\w.-]+\s+tool\s+declaration\s*:"
)
_TOOL_DECLARATION_BODY = re.compile(
    r"(?im)^\s*(?://\s*)?(?:declare\s+const|namespace|type\s+\w+\s*=|function)\b"
)


class CodexWireAttestationError(RuntimeError):
    pass


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def test_png() -> bytes:
    width = 64
    height = 64
    scanline = bytes((32, 128, 224)) * width
    pixels = b"".join(b"\x00" + scanline for _ in range(height))
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(
                b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            ),
            _png_chunk(b"IDAT", zlib.compress(pixels)),
            _png_chunk(b"IEND", b""),
        )
    )


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, expected_authorization_digest: bytes | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _CaptureHandler)
        self.records: List[Dict[str, Any]] = []
        self.records_lock = threading.Lock()
        self.expected_authorization_digest = expected_authorization_digest


class _CaptureHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def _reply(self, status: int, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        body: object = None
        if 0 <= length <= 16 * 1024 * 1024:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"invalid_json": True}
        # Never retain a credential-bearing header value.  The authenticated
        # phase keeps only constant-time digest comparison results in memory;
        # the unauthenticated phase keeps only header names.
        header_names = {key.casefold() for key in self.headers}
        (
            authorization_count,
            authorization_is_bearer,
            authorization_matches,
        ) = _observe_and_discard_authorization(
            self.headers,
            self.server.expected_authorization_digest,
        )
        record = {
            "method": "POST",
            "path": self.path,
            "headers": {key: True for key in header_names},
            "authorization_count": authorization_count,
            "authorization_is_bearer": authorization_is_bearer,
            "authorization_matches": authorization_matches,
            "body": body,
        }
        with self.server.records_lock:
            self.server.records.append(record)
        if self.path != "/v1/responses":
            self._reply(404, {"error": {"message": "unexpected attestation path"}})
            return
        self._reply(
            418,
            {
                "error": {
                    "message": "wire attestation captured the request",
                    "type": "wire_attestation_stop",
                }
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        with self.server.records_lock:
            self.server.records.append(
                {"method": "GET", "path": self.path, "headers": {}, "body": None}
            )
        self._reply(405, {"error": {"message": "POST required"}})

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _observe_and_discard_authorization(
    headers: Any,
    expected_digest: bytes | None,
) -> tuple[int, bool, bool]:
    """Observe only count/type/digest match, then remove raw header references."""

    values = headers.get_all("Authorization") or []
    count = len(values)
    is_bearer = False
    matches = False
    if count == 1:
        value = values[0]
        if isinstance(value, str) and value.startswith("Bearer ") and len(value) > 7:
            is_bearer = True
            candidate_digest = hashlib.sha256(value[7:].encode("utf-8")).digest()
            if expected_digest is not None:
                matches = hmac.compare_digest(candidate_digest, expected_digest)
            del candidate_digest
        del value
    for index in range(len(values)):
        values[index] = ""
    try:
        del headers["Authorization"]
    except KeyError:
        pass
    del values
    return count, is_bearer, matches


def _contains_image(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            image_url = value.get("image_url")
            if isinstance(image_url, str) and image_url.startswith("data:image/"):
                return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def _surface_is_nonempty(value: object) -> bool:
    return value is not None and value != [] and value != {}


def _normalized_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.casefold().replace("-", "_")


def _mapping_looks_like_tool_schema(value: Mapping[object, object]) -> bool:
    normalized = {_normalized_key(key): item for key, item in value.items()}
    if any(key in normalized for key in INPUT_TOOL_MARKER_KEYS):
        return True
    if any(
        key in normalized and _surface_is_nonempty(normalized[key])
        for key in INPUT_TOOL_CONTAINER_KEYS
    ):
        return True
    schema_type = normalized.get("type")
    if isinstance(schema_type, str):
        normalized_type = schema_type.casefold().replace("-", "_")
        if normalized_type == "additional_tools":
            return "content" in normalized and _surface_is_nonempty(
                normalized["content"]
            )
        if normalized_type in TOOL_SCHEMA_TYPES or normalized_type in INPUT_TOOL_ITEM_TYPES:
            return True
    function = normalized.get("function")
    if isinstance(function, Mapping):
        function_keys = {_normalized_key(key) for key in function}
        if function_keys.intersection({"input_schema", "name", "parameters"}):
            return True
    return "name" in normalized and bool(
        {"input_schema", "parameters", "tool_schema"}.intersection(normalized)
    )


def _text_contains_tool_schema_marker(value: str) -> bool:
    if (
        _SERIALIZED_TOOL_MARKER.search(value)
        or _LINE_TOOL_MARKER.search(value)
        or _DECLARED_TOOL_TABLE.search(value)
    ):
        return True
    if _SERIALIZED_TOOL_TYPE.search(value) and _SERIALIZED_TOOL_FIELD.search(value):
        return True
    if _TOOL_DECLARATION_LABEL.search(value) and _TOOL_DECLARATION_BODY.search(value):
        return True
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        if isinstance(decoded, (dict, list)):
            return _contains_input_tool_markers(decoded)
    return False


def _contains_input_tool_markers(value: object) -> bool:
    if isinstance(value, dict):
        if _mapping_looks_like_tool_schema(value):
            return True
        return any(_contains_input_tool_markers(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_input_tool_markers(item) for item in value)
    if isinstance(value, str):
        return _text_contains_tool_schema_marker(value)
    return False


def _visible_text(value: object) -> str:
    if isinstance(value, dict):
        return "\n".join(
            _visible_text(item)
            for item in value.values()
            if isinstance(item, (dict, list, str))
        )
    if isinstance(value, list):
        return "\n".join(_visible_text(item) for item in value)
    return value if isinstance(value, str) else ""


def _validate_captured_request(request: Mapping[str, object]) -> dict[str, object]:
    if request.get("method") != "POST" or request.get("path") != "/v1/responses":
        raise CodexWireAttestationError("Codex used an unexpected loopback endpoint")
    body = request.get("body")
    if not isinstance(body, dict):
        raise CodexWireAttestationError("Codex Responses request was not valid JSON")
    if _surface_is_nonempty(body.get("tools")):
        raise CodexWireAttestationError("Codex advertised top-level model tools")
    if _surface_is_nonempty(body.get("additional_tools")):
        raise CodexWireAttestationError("Codex advertised top-level additional_tools")
    if _surface_is_nonempty(body.get("extensions")):
        raise CodexWireAttestationError("Codex advertised top-level extensions")
    if _contains_input_tool_markers(body.get("input")):
        raise CodexWireAttestationError(
            "Codex injected additional_tools or tool schema markers into model input"
        )
    visible = _visible_text(body.get("input")).casefold()
    leaked_hints = [hint for hint in FORBIDDEN_AGENT_HINTS if hint in visible]
    if leaked_hints:
        raise CodexWireAttestationError(
            "Codex injected multi-agent host instructions: " + ", ".join(leaked_hints)
        )
    if not _contains_image(body.get("input")):
        raise CodexWireAttestationError("Codex did not attach the sampled image")
    headers = request.get("headers")
    if not isinstance(headers, dict):
        raise CodexWireAttestationError("Codex request headers were unavailable")
    for name in ("authorization", "x-api-key", "openai-api-key"):
        if name in headers:
            raise CodexWireAttestationError(
                "Codex sent credentials to the loopback attestation provider"
            )
    return {
        "attested": True,
        "request_count": 1,
        "tools": 0,
        "additional_tools": 0,
        "multi_agent_hints": 0,
        "attached_image": True,
        "model_request_target": "loopback",
        "model_inference_response_accepted": False,
    }


def _validate_authenticated_captured_request(
    request: Mapping[str, object],
) -> dict[str, object]:
    """Validate the same request surface plus exact file-auth Bearer use."""

    unauthenticated_view = dict(request)
    headers = request.get("headers")
    if not isinstance(headers, dict):
        raise CodexWireAttestationError("Codex request headers were unavailable")
    if "authorization" not in headers:
        raise CodexWireAttestationError(
            "Codex did not send an Authorization header"
        )
    header_names_without_authorization = dict(headers)
    header_names_without_authorization.pop("authorization", None)
    unauthenticated_view["headers"] = header_names_without_authorization
    _validate_captured_request(unauthenticated_view)

    if type(request.get("authorization_count")) is not int or request.get(
        "authorization_count"
    ) != 1:
        raise CodexWireAttestationError(
            "Codex did not send exactly one Authorization header"
        )
    if request.get("authorization_is_bearer") is not True:
        raise CodexWireAttestationError(
            "Codex did not use Bearer authentication for the loopback provider"
        )
    if request.get("authorization_matches") is not True:
        raise CodexWireAttestationError(
            "Codex did not load the access token from the stable file-auth home"
        )
    return {
        "attested": True,
        "request_count": 1,
        "file_auth_loaded": True,
        "model_request_target": "loopback",
        "model_inference_response_accepted": False,
    }


def _file_auth_access_token_digest(codex_home: Path) -> bytes:
    """Return only a digest of the validated file-auth access token."""

    try:
        return file_auth_access_token_sha256(Path(codex_home) / "auth.json")
    except CodexNodeHomeError as exc:
        raise CodexWireAttestationError(
            "stable prompt-node file authentication is unavailable"
        ) from None


def _capture_prompt_node_request(
    codex_binary: Path,
    codex_home: Path,
    *,
    requires_openai_auth: bool,
    expected_authorization_digest: bytes | None = None,
    source_environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> Mapping[str, object]:
    """Run the exact production command against a configured loopback provider."""

    server = _CaptureServer(expected_authorization_digest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    if host != "127.0.0.1":
        server.shutdown()
        server.server_close()
        raise CodexWireAttestationError("attestation server did not bind loopback")
    try:
        with tempfile.TemporaryDirectory(prefix="codex-wire-attestation-") as raw:
            root = Path(raw)
            project_root = root / "project"
            batch = root / "batch"
            workspace = root / "node-workspace"
            transport = root / "transport"
            shell_home = root / "shell-home"
            shell_tmp = root / "shell-tmp"
            for directory in (
                project_root,
                batch,
                workspace,
                transport,
                shell_home,
                shell_tmp,
            ):
                directory.mkdir()
            schema = transport / "output.schema.json"
            result = transport / "result.json"
            image = workspace / "sampled-frame.png"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"attested": {"type": "boolean"}},
                        "required": ["attested"],
                    }
                ),
                encoding="utf-8",
            )
            image.write_bytes(test_png())
            command = loop.build_codex_command(
                str(codex_binary),
                batch,
                project_root,
                schema,
                result,
                node_workspace=workspace,
                image_paths=[image],
            )
            auth_literal = "true" if requires_openai_auth else "false"
            provider = [
                "-c",
                'model_provider="capture"',
                "-c",
                (
                    'model_providers.capture={name="Loopback capture",'
                    f'base_url="http://127.0.0.1:{port}/v1",'
                    f'wire_api="responses",requires_openai_auth={auth_literal},'
                    "request_max_retries=0,stream_max_retries=0,"
                    "supports_websockets=false,"
                    "supports_standalone_web_search=false}"
                ),
            ]
            exec_index = command.index("exec")
            command[exec_index + 1 : exec_index + 1] = provider
            child = loop.codex_subprocess_environment(
                source_environment,
                codex_home=codex_home,
                shell_home=shell_home,
                shell_tmp=shell_tmp,
            )
            for key in list(child):
                if key.casefold() in {"http_proxy", "https_proxy", "all_proxy"}:
                    child.pop(key)
            child["NO_PROXY"] = "127.0.0.1,localhost,::1"
            child["no_proxy"] = "127.0.0.1,localhost,::1"
            completed = subprocess.run(
                command,
                cwd=str(workspace),
                input="Wire attestation only. Observe the attached image.",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
                env=child,
            )
            if completed.returncode == 0:
                raise CodexWireAttestationError(
                    "attestation endpoint returned HTTP 418 but Codex reported success"
                )
        with server.records_lock:
            records = list(server.records)
        if len(records) != 1:
            raise CodexWireAttestationError(
                f"expected one loopback request; observed {len(records)}"
            )
        return records[0]
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexWireAttestationError(f"Codex wire attestation failed: {exc}") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def attest_prompt_node_wire(
    codex_binary: Path,
    codex_home: Path,
    *,
    source_environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    """Capture one loopback Responses request and prove the real tool surface."""

    request = _capture_prompt_node_request(
        codex_binary,
        codex_home,
        requires_openai_auth=False,
        source_environment=source_environment,
        timeout_seconds=timeout_seconds,
    )
    return _validate_captured_request(request)


def attest_prompt_node_file_auth(
    codex_binary: Path,
    codex_home: Path,
    *,
    source_environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    """Prove file-auth loading against loopback without accepting model output."""

    expected_digest = _file_auth_access_token_digest(codex_home)
    request = _capture_prompt_node_request(
        codex_binary,
        codex_home,
        requires_openai_auth=True,
        expected_authorization_digest=expected_digest,
        source_environment=source_environment,
        timeout_seconds=timeout_seconds,
    )
    return _validate_authenticated_captured_request(request)


__all__ = [
    "CodexWireAttestationError",
    "attest_prompt_node_file_auth",
    "attest_prompt_node_wire",
    "test_png",
]

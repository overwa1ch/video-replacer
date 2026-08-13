import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import video_batch_loop as loop  # noqa: E402
from codex_artifact import CodexArtifactError, verify_windows_codex  # noqa: E402
import codex_wire_attestation as wire  # noqa: E402
from codex_wire_attestation import (  # noqa: E402
    CodexWireAttestationError,
    attest_prompt_node_file_auth,
    attest_prompt_node_wire,
)


MINIMUM_CODEX_VERSION = (0, 147, 0)
REQUIRE_ATTESTATION_ENV = "VIDEO_REPLACER_REQUIRE_CODEX_WIRE_ATTESTATION"


def _captured_request(text: str = "Wire attestation only. Observe the image.") -> dict:
    return {
        "method": "POST",
        "path": "/v1/responses",
        "headers": {"content-type": "application/json"},
        "body": {
            "tools": [],
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                }
            ],
        },
    }


def _write_fixture_auth(home: Path) -> None:
    auth = home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "e30.e30.c2ln",
                    "access_token": "fixture-access-token-never-valid",
                    "refresh_token": "fixture-refresh-token-never-valid",
                    "account_id": "fixture-account-id-never-valid",
                },
                "last_refresh": "2026-08-13T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        auth.chmod(0o600)


def _native_codex_0147_or_newer(test_case: unittest.TestCase) -> Path:
    def unavailable(message: str) -> None:
        if os.environ.get(REQUIRE_ATTESTATION_ENV, "").strip() == "1":
            test_case.fail(
                f"{message}; {REQUIRE_ATTESTATION_ENV}=1 requires this release gate"
            )
        test_case.skipTest(message)

    try:
        binary = Path(loop.find_codex()).resolve()
    except (OSError, loop.LoopError) as exc:
        unavailable(f"supported native Codex is unavailable: {exc}")
        raise AssertionError("unreachable")
    try:
        header = binary.read_bytes()[:4]
    except OSError as exc:
        unavailable(f"cannot inspect Codex binary: {exc}")
        raise AssertionError("unreachable")
    native_magic = (
        header.startswith(b"MZ")
        or header == b"\x7fELF"
        or header
        in {
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        }
    )
    if not binary.is_file() or not native_magic:
        unavailable(f"Codex launcher is not a supported native binary: {binary}")
    if os.name == "nt" and binary.suffix.casefold() != ".exe":
        unavailable(f"Windows Codex launcher is not native codex.exe: {binary}")
    if os.name == "nt":
        try:
            verified = verify_windows_codex(binary)
            binary = Path(str(verified["binary"]))
        except CodexArtifactError as exc:
            unavailable(
                "Windows Codex provenance could not be verified before execution: "
                f"{exc}"
            )
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        unavailable(f"cannot execute native Codex: {exc}")
        raise AssertionError("unreachable")
    match = re.search(r"\bcodex-cli\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
    if result.returncode != 0 or match is None:
        unavailable(f"unsupported Codex version output: {result.stdout!r}")
    version = tuple(int(value) for value in match.groups())
    if version < MINIMUM_CODEX_VERSION:
        unavailable(f"Codex {version!r} predates required {MINIMUM_CODEX_VERSION!r}")
    return binary


class CodexPromptNodeWireAttestationTest(unittest.TestCase):
    def test_authorization_observation_discards_raw_and_counts_duplicates(self) -> None:
        first = "first-secret-never-log"
        second = "second-secret-never-log"
        headers = Message()
        headers.add_header("Authorization", f"Bearer {first}")
        headers.add_header("Authorization", f"Bearer {second}")
        observation = wire._observe_and_discard_authorization(
            headers,
            hashlib.sha256(first.encode("utf-8")).digest(),
        )
        self.assertEqual(observation, (2, False, False))
        self.assertIsNone(headers.get_all("Authorization"))
        rendered = repr(observation) + repr(headers)
        self.assertNotIn(first, rendered)
        self.assertNotIn(second, rendered)

    def test_authorization_observation_matches_without_retaining_token(self) -> None:
        secret = "matched-secret-never-log"
        headers = Message()
        headers.add_header("Authorization", f"Bearer {secret}")
        observation = wire._observe_and_discard_authorization(
            headers,
            hashlib.sha256(secret.encode("utf-8")).digest(),
        )
        self.assertEqual(observation, (1, True, True))
        self.assertIsNone(headers.get_all("Authorization"))
        self.assertNotIn(secret, repr(observation) + repr(headers))

    def test_authenticated_validator_rejects_fallback_and_mismatched_credentials(self) -> None:
        valid = _captured_request()
        valid["headers"]["authorization"] = True
        valid["authorization_count"] = 1
        valid["authorization_is_bearer"] = True
        valid["authorization_matches"] = True
        self.assertEqual(
            wire._validate_authenticated_captured_request(valid),
            {
                "attested": True,
                "request_count": 1,
                "file_auth_loaded": True,
                "model_request_target": "loopback",
                "model_inference_response_accepted": False,
            },
        )

        malicious = []
        missing = copy.deepcopy(valid)
        missing["headers"].pop("authorization")
        missing["authorization_count"] = 0
        missing["authorization_is_bearer"] = False
        missing["authorization_matches"] = False
        malicious.append(("missing bearer", missing))
        mismatched = copy.deepcopy(valid)
        mismatched["authorization_matches"] = False
        malicious.append(("wrong bearer", mismatched))
        duplicate = copy.deepcopy(valid)
        duplicate["authorization_count"] = 2
        malicious.append(("duplicate bearer", duplicate))
        api_key_fallback = copy.deepcopy(valid)
        api_key_fallback["headers"]["x-api-key"] = True
        malicious.append(("API key fallback", api_key_fallback))
        for label, request in malicious:
            with self.subTest(label=label), self.assertRaises(
                CodexWireAttestationError
            ):
                wire._validate_authenticated_captured_request(request)

    def test_file_auth_digest_errors_never_echo_token(self) -> None:
        secret = "fixture-secret-must-never-appear"
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": secret}}), encoding="utf-8"
            )
            with self.assertRaises(CodexWireAttestationError) as raised:
                wire._file_auth_access_token_digest(home)
        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_additional_tool_and_extension_surfaces(self) -> None:
        fixtures = []

        additional_tools = _captured_request()
        additional_tools["body"]["additional_tools"] = [{"name": "exec"}]
        fixtures.append(("top-level additional_tools", additional_tools))

        extensions = _captured_request()
        extensions["body"]["extensions"] = {"mcp": [{"server": "host"}]}
        fixtures.append(("top-level extensions", extensions))

        input_dictionary = _captured_request()
        input_dictionary["body"]["input"].append(
            {
                "type": "additional_tools",
                "content": "declare const tools: { exec(...): unknown }",
            }
        )
        fixtures.append(("input additional_tools item", input_dictionary))

        tagged_text = _captured_request(
            "<additional_tools>declare const tools: { exec(...): unknown }"
            "</additional_tools>"
        )
        fixtures.append(("tagged additional_tools text", tagged_text))

        json_schema_text = _captured_request(
            '{"type":"function","name":"exec"}'
        )
        fixtures.append(("serialized function schema", json_schema_text))

        for label, request in fixtures:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    CodexWireAttestationError,
                    "additional_tools|extensions|tool schema",
                ):
                    wire._validate_captured_request(request)

    def test_allows_empty_surfaces_and_normal_tool_words_in_prose(self) -> None:
        prompts = (
            "Please explain the word additional_tools in ordinary prose.",
            "What does a tool schema mean for an API user?",
            "A function can have parameters, and an extension can be optional.",
        )
        expected = {
            "attested": True,
            "request_count": 1,
            "tools": 0,
            "additional_tools": 0,
            "multi_agent_hints": 0,
            "attached_image": True,
            "model_request_target": "loopback",
            "model_inference_response_accepted": False,
        }
        for prompt in prompts:
            request = _captured_request(prompt)
            request["body"]["additional_tools"] = []
            request["body"]["extensions"] = {}
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    wire._validate_captured_request(copy.deepcopy(request)), expected
                )

    def test_native_codex_request_has_image_and_zero_tools(self) -> None:
        binary = _native_codex_0147_or_newer(self)
        with tempfile.TemporaryDirectory(prefix="codex-wire-home-") as raw:
            codex_home = Path(raw) / "codex-home"
            codex_home.mkdir()
            try:
                report = attest_prompt_node_wire(binary, codex_home)
            except CodexWireAttestationError as exc:
                self.fail(str(exc))
        self.assertEqual(
            report,
            {
                "attested": True,
                "request_count": 1,
                "tools": 0,
                "additional_tools": 0,
                "multi_agent_hints": 0,
                "attached_image": True,
                "model_request_target": "loopback",
                "model_inference_response_accepted": False,
            },
        )

    def test_native_codex_loads_file_auth_for_loopback_only(self) -> None:
        binary = _native_codex_0147_or_newer(self)
        with tempfile.TemporaryDirectory(prefix="codex-auth-wire-home-") as raw:
            codex_home = Path(raw) / "codex-home"
            codex_home.mkdir()
            _write_fixture_auth(codex_home)
            try:
                report = attest_prompt_node_file_auth(
                    binary,
                    codex_home,
                    source_environment={
                        "OPENAI_API_KEY": "fixture-fallback-must-not-be-used",
                        "PATH": os.environ.get("PATH", ""),
                    },
                )
            except CodexWireAttestationError as exc:
                self.fail(str(exc))
        self.assertEqual(
            report,
            {
                "attested": True,
                "request_count": 1,
                "file_auth_loaded": True,
                "model_request_target": "loopback",
                "model_inference_response_accepted": False,
            },
        )


if __name__ == "__main__":
    unittest.main()

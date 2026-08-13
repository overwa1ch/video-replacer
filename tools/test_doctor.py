#!/usr/bin/env python3
"""Unit tests for cross-platform readiness checks."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("doctor.py")
SPEC = importlib.util.spec_from_file_location("video_replacer_doctor", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)


class DoctorTests(unittest.TestCase):
    def test_dreamina_checks_use_shared_provider_environment(self) -> None:
        binary = Path("/fixture/dreamina")
        environment = {"PATH": "/provider-only", "PYTHONUTF8": "1"}
        results = [
            mock.Mock(returncode=0, stdout='{"version":"fixture"}'),
            mock.Mock(
                returncode=0,
                stdout=(
                    "--video --image --video_resolution --model_version seedance2.5"
                ),
            ),
        ]
        with mock.patch.object(
            doctor, "dreamina_path", return_value=binary
        ), mock.patch.object(
            doctor, "dreamina_environment", return_value=environment.copy()
        ) as provider_environment, mock.patch.object(
            doctor, "run", side_effect=results
        ) as run:
            checks = {item.name: item for item in doctor.check_dreamina(False)}
        provider_environment.assert_called_once_with()
        self.assertEqual(checks["dreamina-cli"].status, "pass")
        self.assertEqual(checks["dreamina-interface"].status, "pass")
        for call in run.call_args_list:
            child_environment = call.kwargs["environment"]
            self.assertEqual(child_environment["PATH"], "/provider-only")
            self.assertEqual(child_environment["DREAMINA_BINARY"], str(binary))

    def test_windows_ready_requires_native_codex_executable(self) -> None:
        self.assertTrue(doctor.supported_codex_binary(Path("codex.exe"), "nt"))
        self.assertFalse(doctor.supported_codex_binary(Path("codex.cmd"), "nt"))
        self.assertFalse(doctor.supported_codex_binary(Path("codex.ps1"), "nt"))
        self.assertTrue(doctor.supported_codex_binary(Path("codex"), "posix"))

    def test_windows_provenance_rejects_before_codex_execution(self) -> None:
        binary = Path("C:/fixture/codex.exe")
        with mock.patch.object(doctor, "codex_path", return_value=binary), mock.patch.object(
            doctor.os, "name", "nt"
        ), mock.patch.object(
            doctor,
            "verify_windows_codex",
            side_effect=doctor.CodexArtifactError("digest mismatch"),
        ), mock.patch.object(doctor, "run") as run:
            checks = {item.name: item for item in doctor.check_codex()}
        run.assert_not_called()
        self.assertEqual(checks["codex-cli"].status, "fail")
        self.assertIn("provenance", checks["codex-cli"].detail)
        self.assertEqual(checks["codex-auth"].status, "fail")

    def test_stable_file_auth_home_is_used_for_login_and_model_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / "codex"
            binary.write_bytes(b"fixture")
            node_home = root / "codex-node-home"
            node_home.mkdir()
            (node_home / "auth.json").write_text(
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
            results = [
                mock.Mock(returncode=0, stdout="codex-cli 0.147.0"),
                mock.Mock(
                    returncode=0,
                    stdout="--ephemeral --output-schema --output-last-message "
                    "--skip-git-repo-check --image --ignore-user-config "
                    "--ignore-rules --disable",
                ),
                mock.Mock(
                    returncode=0,
                    stdout="\n".join(
                        f"{name} stable false"
                        for name in sorted(doctor.PROMPT_NODE_DISABLED_FEATURES)
                    ),
                ),
                mock.Mock(returncode=0, stdout="Logged in using file credentials"),
                mock.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {"models": [{"slug": doctor.VIDEO_TO_PROMPT_MODEL}]}
                    ),
                ),
            ]
            with mock.patch.object(doctor, "codex_path", return_value=binary), mock.patch.object(
                doctor, "codex_home_path", return_value=node_home
            ), mock.patch.object(
                doctor, "validate_node_home", return_value=node_home
            ), mock.patch.object(
                doctor, "locked_node_home", return_value=mock.MagicMock()
            ), mock.patch.object(
                doctor, "attest_prompt_node_wire", return_value={"attested": True}
            ), mock.patch.object(
                doctor,
                "attest_prompt_node_file_auth",
                return_value={"attested": True, "file_auth_loaded": True},
            ), mock.patch.object(doctor, "run", side_effect=results) as run:
                checks = {item.name: item for item in doctor.check_codex()}
        self.assertEqual(checks["codex-auth"].status, "pass")
        self.assertEqual(checks["codex-model"].status, "pass")
        self.assertEqual(checks["codex-wire"].status, "pass")
        self.assertEqual(checks["codex-file-auth-wire"].status, "pass")
        auth_command = run.call_args_list[3].args[0]
        self.assertIn('cli_auth_credentials_store="file"', auth_command)

    def test_codex_below_prompt_node_minimum_fails_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / "codex"
            binary.write_bytes(b"fixture")
            results = [
                mock.Mock(returncode=0, stdout="codex-cli 0.146.9"),
                mock.Mock(returncode=0, stdout=""),
                mock.Mock(returncode=0, stdout=""),
                mock.Mock(returncode=1, stdout="not logged in"),
                mock.Mock(returncode=1, stdout="{}"),
            ]
            node_home = root / "codex-node-home"
            node_home.mkdir()
            with mock.patch.object(doctor, "codex_path", return_value=binary), mock.patch.object(
                doctor, "codex_home_path", return_value=node_home
            ), mock.patch.object(
                doctor, "validate_node_home", return_value=node_home
            ), mock.patch.object(
                doctor, "locked_node_home", return_value=mock.MagicMock()
            ), mock.patch.object(
                doctor,
                "attest_prompt_node_wire",
                return_value={"attested": True},
            ), mock.patch.object(
                doctor,
                "attest_prompt_node_file_auth",
                return_value={"attested": True, "file_auth_loaded": True},
            ), mock.patch.object(doctor, "run", side_effect=results):
                checks = {item.name: item for item in doctor.check_codex()}
        self.assertEqual(checks["codex-cli"].status, "fail")
        self.assertIn("0.147.0+", checks["codex-cli"].detail)

    def test_file_auth_wire_failure_is_a_distinct_required_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "codex"
            binary.write_bytes(b"fixture")
            node_home = root / "codex-node-home"
            node_home.mkdir()
            results = [
                mock.Mock(returncode=0, stdout="codex-cli 0.147.0"),
                mock.Mock(
                    returncode=0,
                    stdout="--ephemeral --output-schema --output-last-message "
                    "--skip-git-repo-check --image --ignore-user-config "
                    "--ignore-rules --disable",
                ),
                mock.Mock(
                    returncode=0,
                    stdout="\n".join(
                        f"{name} stable false"
                        for name in sorted(doctor.PROMPT_NODE_DISABLED_FEATURES)
                    ),
                ),
                mock.Mock(returncode=0, stdout="Logged in using file credentials"),
                mock.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {"models": [{"slug": doctor.VIDEO_TO_PROMPT_MODEL}]}
                    ),
                ),
            ]
            with mock.patch.object(
                doctor, "codex_path", return_value=binary
            ), mock.patch.object(
                doctor, "codex_home_path", return_value=node_home
            ), mock.patch.object(
                doctor, "validate_node_home", return_value=node_home
            ), mock.patch.object(
                doctor, "locked_node_home", return_value=mock.MagicMock()
            ), mock.patch.object(
                doctor, "attest_prompt_node_wire", return_value={"attested": True}
            ), mock.patch.object(
                doctor,
                "attest_prompt_node_file_auth",
                side_effect=doctor.CodexWireAttestationError("digest mismatch"),
            ), mock.patch.object(doctor, "run", side_effect=results):
                checks = {item.name: item for item in doctor.check_codex()}
        self.assertEqual(checks["codex-auth"].status, "pass")
        self.assertEqual(checks["codex-wire"].status, "pass")
        self.assertEqual(checks["codex-file-auth-wire"].status, "fail")
        self.assertIn("digest mismatch", checks["codex-file-auth-wire"].detail)

    def test_windows_network_state_path_is_rejected(self) -> None:
        self.assertFalse(
            doctor.local_windows_path(Path("//server/share/video-replacer"), "nt")
        )
        self.assertTrue(
            doctor.local_windows_path(Path("C:/Users/example/video-replacer"), "nt")
        )


if __name__ == "__main__":
    unittest.main()

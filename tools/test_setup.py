#!/usr/bin/env python3
"""Offline tests for the Agent-owned live READY record."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("setup.py")
SPEC = importlib.util.spec_from_file_location("video_replacer_setup", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.setup_root = root / ".video-replacer"
        self.setup_file = self.setup_root / "setup.json"
        state = root / "external-state"
        codex_home = root / "codex-node-home"
        mosaic_cache = root / "mosaic-cache"
        state.mkdir()
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
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
            codex_home.chmod(0o700)
            (codex_home / "auth.json").chmod(0o600)
        mosaic_cache.mkdir()
        self.runtime_paths = {
            "codex_node_home": str(codex_home),
            "state_dir": str(state),
            "mosaic_cache_dir": str(mosaic_cache),
        }
        tools = root / "tools"
        tools.mkdir()
        self.tool_paths = {}
        for name in setup.TOOL_KEYS:
            path = tools / name
            path.write_text(name, encoding="utf-8")
            self.tool_paths[name] = str(path)
        self.tool_versions = {name: f"{name}-1" for name in setup.TOOL_KEYS}

        def fake_identity(name, path_text, runtime_paths):
            path = Path(path_text)
            metadata = path.stat()
            return {
                "resolved_path": str(path.resolve()),
                "size": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "version": self.tool_versions[name],
            }

        self.tool_identity_patcher = mock.patch.object(
            setup, "tool_identity", side_effect=fake_identity
        )
        self.patches = (
            mock.patch.object(setup, "SETUP_ROOT", self.setup_root),
            mock.patch.object(setup, "SETUP_FILE", self.setup_file),
            mock.patch.object(setup, "resolve_state_root", return_value=state),
            mock.patch.object(setup, "setup_contract_digest", return_value="a" * 64),
            mock.patch.object(
                setup, "capture_runtime_paths", return_value=self.runtime_paths
            ),
            mock.patch.object(setup, "capture_tool_paths", return_value=self.tool_paths),
            self.tool_identity_patcher,
            mock.patch.object(
                setup,
                "verified_dreamina_artifact",
                return_value={"url": "https://official.example/dreamina", "sha256": "f" * 64},
            ),
            mock.patch.object(setup.doctor, "inside", return_value=False),
            mock.patch.object(setup.doctor, "temporary_path", return_value=False),
            mock.patch.object(setup.doctor, "path_contains_link_like", return_value=False),
            mock.patch.object(setup.doctor, "local_windows_path", return_value=True),
            mock.patch.object(setup.doctor, "writable_directory", return_value=True),
            mock.patch.object(
                setup, "validate_node_home", side_effect=lambda path, **_kwargs: Path(path)
            ),
            mock.patch.object(
                setup.doctor.mosaic_module(), "path_contains_symlink", return_value=False
            ),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def passing_checks(backend: str = "dreamina"):
        names = setup.BASE_REQUIRED_CHECKS | setup.BACKEND_REQUIRED_CHECKS[backend]
        return [setup.doctor.Check(name, "pass", "fixture") for name in sorted(names)]

    def verify(self) -> dict:
        with mock.patch.object(
            setup.doctor, "collect_checks", return_value=self.passing_checks()
        ):
            return setup.verify_profile("dreamina_cli_seedance_2_5")

    def test_missing_record_is_not_ready(self) -> None:
        report = setup.readiness_report()
        self.assertFalse(report["ready"])
        self.assertIn("Agent", report["reason"])

    def test_windows_arm64_cannot_claim_ready(self) -> None:
        with mock.patch.object(setup.os, "name", "nt"), mock.patch(
            "platform.machine", return_value="ARM64"
        ):
            with self.assertRaisesRegex(setup.SetupError, "requires x64"):
                setup.require_supported_platform()

    def test_online_verification_writes_strict_secret_free_record(self) -> None:
        secret = "must-never-enter-setup-record"
        with mock.patch.object(
            setup.doctor, "collect_checks", return_value=self.passing_checks()
        ), mock.patch.dict(
            os.environ, {"VIDEO_REPLACER_ARK_API_KEY": secret}, clear=False
        ):
            report = setup.verify_profile("dreamina_cli_seedance_2_5")
        self.assertTrue(report["ready"])
        payload = json.loads(self.setup_file.read_text(encoding="utf-8"))
        rendered = json.dumps(payload)
        self.assertEqual(set(payload), setup.EXPECTED_RECORD_FIELDS)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("detail", rendered)
        self.assertEqual(payload["backend"], "dreamina")
        self.assertEqual(payload["capabilities"], setup.CAPABILITIES)
        if os.name != "nt":
            self.assertEqual(self.setup_file.stat().st_mode & 0o777, 0o600)

    def test_required_mosaic_check_cannot_be_omitted(self) -> None:
        checks = [
            item for item in self.passing_checks() if item.name != "mosaic"
        ]
        with mock.patch.object(setup.doctor, "collect_checks", return_value=checks):
            with self.assertRaisesRegex(setup.SetupError, "mosaic"):
                setup.verify_profile("dreamina_cli_seedance_2_5")
        self.assertFalse(self.setup_file.exists())

    def test_profile_change_invalidates_record(self) -> None:
        self.verify()
        payload = json.loads(self.setup_file.read_text(encoding="utf-8"))
        payload["profile_constraints_sha256"] = "0" * 64
        setup.atomic_write_record(payload)
        report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("profile changed", report["reason"])

    def test_contract_change_invalidates_record(self) -> None:
        self.verify()
        with mock.patch.object(
            setup, "setup_contract_digest", return_value="b" * 64
        ):
            report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("contract changed", report["reason"])

    def test_runtime_environment_persists_mosaic_cache(self) -> None:
        self.verify()
        payload = setup.validate_record(setup.read_record(), live=False)
        environment = setup.runtime_environment(payload, source={})
        self.assertEqual(
            environment["VIDEO_REPLACER_CACHE_DIR"],
            self.runtime_paths["mosaic_cache_dir"],
        )

    def test_temporary_external_state_invalidates_record(self) -> None:
        self.verify()
        state = Path(self.runtime_paths["state_dir"])
        with mock.patch.object(
            setup.doctor, "temporary_path", side_effect=lambda path: Path(path) == state
        ):
            report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("external state", report["reason"])

    def test_setup_contract_covers_runtime_node_result_schema(self) -> None:
        self.assertIn(
            Path("tools/video_batch_node_result.schema.json"),
            setup.SETUP_CONTRACT_FILES,
        )
        self.assertIn(Path("tools/codex_node_home.py"), setup.SETUP_CONTRACT_FILES)
        self.assertIn(Path("tools/state_paths.py"), setup.SETUP_CONTRACT_FILES)
        self.assertIn(Path("tools/codex_artifact.py"), setup.SETUP_CONTRACT_FILES)
        self.assertIn(
            Path("tools/codex_wire_attestation.py"), setup.SETUP_CONTRACT_FILES
        )

    def test_tool_change_invalidates_record(self) -> None:
        self.verify()
        self.tool_versions["dreamina"] = "dreamina-2"
        report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("dreamina tool changed", report["reason"])

    def test_live_logout_invalidates_ready(self) -> None:
        self.verify()
        checks = self.passing_checks()
        checks = [
            setup.doctor.Check(item.name, "fail", "logged out")
            if item.name == "dreamina-login"
            else item
            for item in checks
        ]
        with mock.patch.object(setup.doctor, "collect_checks", return_value=checks):
            report = setup.readiness_report(live=True)
        self.assertFalse(report["ready"])
        self.assertIn("dreamina-login", report["reason"])

    def test_public_launcher_rejects_infrastructure_overrides(self) -> None:
        self.verify()
        record = setup.validate_record(setup.read_record(), live=False)
        for flag in ("--config", "--project-root", "--root", "--python", "--engine"):
            with self.subTest(flag=flag), self.assertRaisesRegex(
                setup.SetupError, "owns infrastructure options"
            ):
                setup.launch_workflow(record, ["once", flag, "/tmp/attacker"])
        with self.assertRaisesRegex(setup.SetupError, "--engine"):
            setup.launch_workflow(record, ["once", "--engine=/tmp/attacker.py"])

    def test_unknown_record_field_is_rejected(self) -> None:
        self.verify()
        payload = json.loads(self.setup_file.read_text(encoding="utf-8"))
        payload["credential"] = "unexpected"
        setup.atomic_write_record(payload)
        report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("fields are unsupported", report["reason"])

    def test_future_timestamp_is_rejected(self) -> None:
        self.verify()
        payload = json.loads(self.setup_file.read_text(encoding="utf-8"))
        payload["verified_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        setup.atomic_write_record(payload)
        report = setup.readiness_report(live=False)
        self.assertFalse(report["ready"])
        self.assertIn("future", report["reason"])

    def test_ark_environment_cannot_claim_durable_ready(self) -> None:
        self.assertNotIn("volcengine_ark_seedance_2_5", setup.PROFILE_BACKENDS)
        with self.assertRaisesRegex(setup.SetupError, "durable"):
            setup.verify_profile("volcengine_ark_seedance_2_5")

    def test_unreviewed_dreamina_binary_cannot_claim_ready(self) -> None:
        self.tool_identity_patcher.stop()
        try:
            with mock.patch.object(
                setup,
                "verified_dreamina_artifact",
                side_effect=setup.SetupError("reviewed SHA-256 mismatch"),
            ):
                with self.assertRaisesRegex(setup.SetupError, "reviewed SHA-256"):
                    setup.tool_identity(
                        "dreamina", self.tool_paths["dreamina"], self.runtime_paths
                    )
        finally:
            self.tool_identity_patcher.start()

    def test_dreamina_login_verifies_artifact_before_execution(self) -> None:
        binary = setup.REPO_ROOT / ".video-replacer" / "bin" / (
            "dreamina.exe" if os.name == "nt" else "dreamina"
        )
        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(
            setup,
            "verified_dreamina_artifact",
            side_effect=setup.SetupError("reviewed SHA-256 mismatch"),
        ), mock.patch.object(setup.subprocess, "run") as run:
            with self.assertRaisesRegex(setup.SetupError, "reviewed SHA-256"):
                setup.login_dreamina()
        run.assert_not_called()

    def test_dreamina_login_uses_minimized_environment(self) -> None:
        calls = [
            mock.Mock(returncode=0),
            mock.Mock(returncode=0, stdout='{"total_credit": 1}'),
        ]
        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(
            setup,
            "verified_dreamina_artifact",
            return_value={"sha256": "f" * 64},
        ), mock.patch.object(
            setup.subprocess, "run", side_effect=calls
        ) as run, mock.patch.dict(
            os.environ,
            {
                "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross",
                "OPENAI_API_KEY": "must-not-cross",
            },
            clear=False,
        ):
            report = setup.login_dreamina()
        self.assertTrue(report["logged_in"])
        for call in run.call_args_list:
            environment = call.kwargs["env"]
            self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)

    def test_codex_node_login_uses_stable_file_auth_home(self) -> None:
        home = Path(self.runtime_paths["codex_node_home"])
        binary = Path(self.tool_paths["codex"])
        calls = [
            mock.Mock(returncode=0),
            mock.Mock(returncode=0, stdout="Logged in"),
        ]
        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.doctor, "external_state_path", return_value=Path(self.runtime_paths["state_dir"])
        ), mock.patch.object(
            setup, "ensure_node_home", return_value=home
        ), mock.patch.object(
            setup, "validate_node_home", return_value=home
        ), mock.patch.object(
            setup, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(
            setup.subprocess, "run", side_effect=calls
        ) as run, mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "must-not-cross", "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross"},
            clear=False,
        ):
            report = setup.login_codex_node()
        self.assertTrue(report["logged_in"])
        self.assertEqual(report["codex_node_home"], str(home))
        login_command = run.call_args_list[0].args[0]
        self.assertEqual(login_command[-2:], ["login", "--device-auth"])
        self.assertIn('cli_auth_credentials_store="file"', login_command)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["CODEX_HOME"], str(home))
            self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
            self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", call.kwargs["env"])

    def test_codex_node_login_clears_only_malformed_regular_auth(self) -> None:
        home = Path(self.runtime_paths["state_dir"]) / setup.NODE_HOME_DIRECTORY
        home.mkdir()
        if os.name != "nt":
            home.chmod(0o700)
        auth = home / "auth.json"
        auth.write_text("malformed", encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        binary = Path(self.tool_paths["codex"])
        ensure_calls = 0

        def ensure(path, **_kwargs):
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 1:
                raise setup.CodexNodeHomeError("unsupported auth schema")
            return Path(path)

        def validate_auth(path):
            if Path(path).read_text(encoding="utf-8") != "replacement-valid":
                raise setup.CodexNodeHomeError("unsupported auth schema")

        def validate_home(path, *, require_auth, **_kwargs):
            if require_auth:
                validate_auth(Path(path) / "auth.json")
            return Path(path)

        def run(command, **_kwargs):
            if command[-2:] == ["login", "--device-auth"]:
                self.assertFalse(auth.exists())
                auth.write_text("replacement-valid", encoding="utf-8")
                if os.name != "nt":
                    auth.chmod(0o600)
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=0, stdout="Logged in")

        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.doctor,
            "external_state_path",
            return_value=Path(self.runtime_paths["state_dir"]),
        ), mock.patch.object(
            setup, "ensure_node_home", side_effect=ensure
        ), mock.patch.object(
            setup, "validate_node_home_structure", return_value=home
        ), mock.patch.object(
            setup, "validate_file_auth", side_effect=validate_auth
        ), mock.patch.object(
            setup, "validate_node_home", side_effect=validate_home
        ), mock.patch.object(
            setup, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(setup.subprocess, "run", side_effect=run):
            report = setup.login_codex_node()
        self.assertTrue(report["logged_in"])
        self.assertEqual(auth.read_text(encoding="utf-8"), "replacement-valid")

    def test_codex_node_login_preserves_valid_auth_for_codex(self) -> None:
        home = Path(self.runtime_paths["state_dir"]) / setup.NODE_HOME_DIRECTORY
        home.mkdir()
        if os.name != "nt":
            home.chmod(0o700)
        auth = home / "auth.json"
        original = (
            Path(self.runtime_paths["codex_node_home"]) / "auth.json"
        ).read_bytes()
        auth.write_bytes(original)
        if os.name != "nt":
            auth.chmod(0o600)
        binary = Path(self.tool_paths["codex"])
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if command[-2:] == ["login", "--device-auth"]:
                self.assertEqual(auth.read_bytes(), original)
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=0, stdout="Logged in")

        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.doctor,
            "external_state_path",
            return_value=Path(self.runtime_paths["state_dir"]),
        ), mock.patch.object(
            setup, "ensure_node_home", return_value=home
        ), mock.patch.object(
            setup, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(setup.subprocess, "run", side_effect=run):
            setup.login_codex_node()
        self.assertEqual(auth.read_bytes(), original)
        self.assertEqual(len(calls), 2)

    def test_codex_node_login_unsafe_repair_runs_no_subprocess(self) -> None:
        home = Path(self.runtime_paths["state_dir"]) / setup.NODE_HOME_DIRECTORY
        home.mkdir()
        if os.name != "nt":
            home.chmod(0o700)
        auth = home / "auth.json"
        auth.write_text("malformed", encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        binary = Path(self.tool_paths["codex"])

        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.doctor,
            "external_state_path",
            return_value=Path(self.runtime_paths["state_dir"]),
        ), mock.patch.object(
            setup, "ensure_node_home", side_effect=setup.CodexNodeHomeError("unsafe")
        ), mock.patch.object(
            setup,
            "validate_node_home_structure",
            side_effect=setup.CodexNodeHomeError("unsafe"),
        ), mock.patch.object(
            setup, "locked_node_home", return_value=mock.MagicMock()
        ) as locked, mock.patch.object(setup.subprocess, "run") as run:
            with self.assertRaisesRegex(setup.SetupError, "unsafe"):
                setup.login_codex_node()
        run.assert_not_called()
        locked.assert_not_called()
        self.assertTrue(auth.exists())

    def test_codex_node_login_requires_new_strict_auth_file(self) -> None:
        home = Path(self.runtime_paths["state_dir"]) / setup.NODE_HOME_DIRECTORY
        home.mkdir()
        if os.name != "nt":
            home.chmod(0o700)
        auth = home / "auth.json"
        auth.write_text("malformed", encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        binary = Path(self.tool_paths["codex"])
        ensure_calls = 0

        def ensure(path, **_kwargs):
            nonlocal ensure_calls
            ensure_calls += 1
            if ensure_calls == 1:
                raise setup.CodexNodeHomeError("unsupported auth schema")
            return Path(path)

        def validate_auth(_path):
            raise setup.CodexNodeHomeError("unsupported auth schema")

        def strict_home(path, *, require_auth, **_kwargs):
            if require_auth and not (Path(path) / "auth.json").is_file():
                raise setup.CodexNodeHomeError("login is missing")
            return Path(path)

        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.doctor,
            "external_state_path",
            return_value=Path(self.runtime_paths["state_dir"]),
        ), mock.patch.object(
            setup, "ensure_node_home", side_effect=ensure
        ), mock.patch.object(
            setup, "validate_node_home_structure", return_value=home
        ), mock.patch.object(
            setup, "validate_file_auth", side_effect=validate_auth
        ), mock.patch.object(
            setup, "validate_node_home", side_effect=strict_home
        ), mock.patch.object(
            setup, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(
            setup.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run:
            with self.assertRaisesRegex(setup.SetupError, "login is missing"):
                setup.login_codex_node()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(auth.exists())

    def test_windows_codex_login_verifies_provenance_before_execution(self) -> None:
        binary = Path(self.tool_paths["codex"])
        with mock.patch.object(setup, "repository_python"), mock.patch.object(
            setup, "require_supported_platform"
        ), mock.patch.object(
            setup.doctor, "codex_path", return_value=binary
        ), mock.patch.object(
            setup.doctor, "supported_codex_binary", return_value=True
        ), mock.patch.object(
            setup.os, "name", "nt"
        ), mock.patch.object(
            setup,
            "verify_windows_codex",
            side_effect=setup.CodexArtifactError("digest mismatch"),
        ), mock.patch.object(setup.subprocess, "run") as run:
            with self.assertRaisesRegex(setup.SetupError, "provenance"):
                setup.login_codex_node()
        run.assert_not_called()

    def test_windows_codex_identity_executes_only_verified_canonical_binary(self) -> None:
        alias = Path(self.tool_paths["codex"])
        canonical = alias.with_name("official-canonical-codex.exe")
        canonical.write_text("canonical-fixture", encoding="utf-8")
        artifact = {
            "binary": str(canonical),
            "version": "0.147.0",
            "sha256": "a" * 64,
            "release": {"tag_name": "rust-v0.147.0"},
        }
        self.tool_identity_patcher.stop()
        try:
            with mock.patch.object(
                setup.Path, "is_file", return_value=True
            ), mock.patch.object(setup.os, "name", "nt"), mock.patch.object(
                setup, "Path", side_effect=lambda value: type(alias)(value)
            ), mock.patch.object(
                setup, "verify_windows_codex", return_value=artifact
            ), mock.patch.object(
                setup.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=0, stdout="codex-cli 0.147.0\n"
                ),
            ) as run:
                identity = setup.tool_identity(
                    "codex", str(alias), self.runtime_paths
                )
        finally:
            self.tool_identity_patcher.start()
        self.assertEqual(run.call_args.args[0][0], str(canonical))
        self.assertEqual(identity["resolved_path"], str(canonical.resolve()))
        self.assertEqual(identity["official_sha256"], "a" * 64)
        self.assertEqual(identity["official_release_tag"], "rust-v0.147.0")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the pinned, project-local Dreamina installer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("install_dreamina.py")
SPEC = importlib.util.spec_from_file_location("install_dreamina", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class DreaminaInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pinned = installer.VERSION_METADATA_SOURCE.read_bytes()

    @staticmethod
    def home_environment(home: Path) -> dict[str, str]:
        key = "USERPROFILE" if os.name == "nt" else "HOME"
        return {key: str(home), "PATH": os.environ.get("PATH", "")}

    def test_supported_platform_keys_are_deterministic(self) -> None:
        self.assertEqual(installer.platform_key("Darwin", "arm64"), "darwin_arm64")
        self.assertEqual(installer.platform_key("Linux", "x86_64"), "linux_amd64")
        self.assertEqual(installer.platform_key("Windows", "AMD64"), "windows_amd64")
        with self.assertRaises(installer.InstallError):
            installer.platform_key("Windows", "arm64")

    def test_manifest_uses_https_and_sha256_for_every_artifact(self) -> None:
        manifest = installer.read_manifest()
        artifacts = manifest["artifacts"]
        self.assertEqual(
            set(artifacts),
            {
                "darwin_amd64",
                "darwin_arm64",
                "linux_amd64",
                "linux_arm64",
                "windows_amd64",
            },
        )
        for key in artifacts:
            artifact = installer.artifact_for(manifest, key)
            self.assertTrue(artifact["url"].startswith("https://"))
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")

        windows = installer.artifact_for(manifest, "windows_amd64")
        self.assertTrue(windows["url"].endswith("dreamina_cli_windows_amd64.exe"))
        self.assertEqual(
            windows["sha256"],
            "74c0de7a451f09d58f4429071015cde2d311d728e43b92ea9813741b4d2a15ac",
        )
        self.assertEqual(
            hashlib.sha256(self.pinned).hexdigest(),
            manifest["version_manifest_sha256"],
        )

    def test_home_is_taken_from_exact_child_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            expected = Path(temporary)
            self.assertEqual(
                installer.dreamina_home({"HOME": temporary}, windows=False), expected
            )
            self.assertEqual(
                installer.dreamina_home({"USERPROFILE": temporary}, windows=True), expected
            )
        for environment, windows in (({}, False), ({"HOME": "relative"}, False)):
            with self.assertRaises(installer.InstallError):
                installer.dreamina_home(environment, windows=windows)
        with self.assertRaises(installer.InstallError):
            installer.dreamina_home({}, windows=True)

    def test_metadata_create_preserve_and_reject_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            environment = self.home_environment(home)
            created = installer.provision_version_metadata(environment, self.pinned)
            target = home / ".dreamina_cli" / "version.json"
            self.assertEqual(created["status"], "created")
            self.assertEqual(target.read_bytes(), self.pinned)

            newer = {
                "version": "9.9.9",
                "release_date": "2099-01-01",
                "release_notes": "provider-managed future metadata",
            }
            target.write_text(json.dumps(newer), encoding="utf-8")
            preserved = installer.provision_version_metadata(environment, self.pinned)
            self.assertEqual(preserved["status"], "preserved")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), newer)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            linked = home / ".dreamina_cli"
            if os.name == "nt":
                linked.mkdir()
                with mock.patch.object(
                    installer, "is_link_like", side_effect=lambda path: path == linked
                ):
                    with self.assertRaisesRegex(installer.InstallError, "unsafe"):
                        installer.provision_version_metadata(
                            self.home_environment(home), self.pinned
                        )
            else:
                (home / "real").mkdir()
                linked.symlink_to(home / "real", target_is_directory=True)
                with self.assertRaisesRegex(installer.InstallError, "unsafe"):
                    installer.provision_version_metadata(
                        self.home_environment(home), self.pinned
                    )

    def test_created_metadata_rolls_back_on_version_timeout(self) -> None:
        payload = b"fixture-dreamina-binary"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "clone" / ".video-replacer" / "bin"
            target = root / "dreamina"
            home = base / "home"
            home.mkdir()
            environment = self.home_environment(home)
            with mock.patch.object(installer, "INSTALL_ROOT", root), mock.patch.object(
                installer, "TARGET_PATH", target
            ), mock.patch.object(
                installer,
                "artifact_for",
                return_value={"url": "https://official.example/dreamina", "sha256": digest},
            ), mock.patch.object(
                installer, "safe_environment", return_value=environment
            ), mock.patch.object(
                installer.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["dreamina", "version"], 30),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    installer.install(
                        opener=lambda request, timeout: FakeResponse(payload)
                    )
            self.assertFalse((home / ".dreamina_cli" / "version.json").exists())
            self.assertFalse(target.exists())

    def test_verified_install_is_project_local_and_side_effect_limited(self) -> None:
        payload = b"fixture-dreamina-binary"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / ".video-replacer" / "bin"
            target = root / "dreamina"
            home = base / "home"
            home.mkdir()

            def opener(request, timeout):
                self.assertEqual(timeout, 120)
                return FakeResponse(payload)

            completed = mock.Mock(
                returncode=0,
                stdout='{"version":"a857341-dirty","commit":"a857341"}\n',
            )
            with mock.patch.object(installer, "INSTALL_ROOT", root), mock.patch.object(
                installer, "TARGET_PATH", target
            ), mock.patch.object(
                installer,
                "artifact_for",
                return_value={"url": "https://official.example/dreamina", "sha256": digest},
            ), mock.patch.object(
                installer,
                "safe_environment",
                return_value=self.home_environment(home),
            ), mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as run:
                report = installer.install(opener=opener)

            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(report["path"], str(target))
            self.assertEqual(report["binary_version"], "a857341-dirty (a857341)")
            self.assertFalse(report["modified_shell"])
            self.assertFalse(report["installed_global_skill"])
            self.assertEqual(report["version_metadata"]["status"], "created")
            self.assertEqual(
                (home / ".dreamina_cli" / "version.json").read_bytes(), self.pinned
            )
            command = run.call_args
            self.assertEqual(command.args[0], [str(target), "version"])
            self.assertIs(command.kwargs["stdin"], installer.subprocess.DEVNULL)
            self.assertEqual(
                command.kwargs["timeout"], installer.VERSION_CHECK_TIMEOUT_SECONDS
            )
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_hash_mismatch_never_replaces_existing_binary(self) -> None:
        payload = b"unreviewed"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / ".video-replacer" / "bin"
            root.mkdir(parents=True)
            target = root / "dreamina"
            home = base / "home"
            home.mkdir()
            target.write_bytes(b"existing-reviewed-binary")
            with mock.patch.object(installer, "INSTALL_ROOT", root), mock.patch.object(
                installer, "TARGET_PATH", target
            ), mock.patch.object(
                installer,
                "artifact_for",
                return_value={"url": "https://official.example/dreamina", "sha256": "0" * 64},
            ), mock.patch.object(
                installer,
                "safe_environment",
                return_value=self.home_environment(home),
            ):
                with self.assertRaisesRegex(installer.InstallError, "SHA-256 mismatch"):
                    installer.install(opener=lambda request, timeout: FakeResponse(payload))
            self.assertEqual(target.read_bytes(), b"existing-reviewed-binary")
            self.assertFalse((home / ".dreamina_cli").exists())

    def test_binary_replace_failure_rolls_back_created_metadata(self) -> None:
        payload = b"fixture-dreamina-binary"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / ".video-replacer" / "bin"
            target = root / ("dreamina.exe" if os.name == "nt" else "dreamina")
            home = base / "home"
            home.mkdir()
            completed = mock.Mock(returncode=0, stdout='{"version":"build-1"}')
            real_replace = installer.os.replace

            def fail_binary_replace(source, destination):
                if Path(destination) == target:
                    raise OSError("fixture sharing violation")
                return real_replace(source, destination)

            with mock.patch.object(installer, "INSTALL_ROOT", root), mock.patch.object(
                installer, "TARGET_PATH", target
            ), mock.patch.object(
                installer,
                "artifact_for",
                return_value={"url": "https://official.example/dreamina", "sha256": digest},
            ), mock.patch.object(
                installer, "safe_environment", return_value=self.home_environment(home)
            ), mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ), mock.patch.object(
                installer.os, "replace", side_effect=fail_binary_replace
            ):
                with self.assertRaisesRegex(OSError, "sharing violation"):
                    installer.install(opener=lambda request, timeout: FakeResponse(payload))
            self.assertFalse((home / ".dreamina_cli" / "version.json").exists())
            self.assertFalse(target.exists())

    def test_version_output_must_be_json(self) -> None:
        self.assertEqual(installer.reported_version("updater timed out"), "")
        self.assertEqual(
            installer.reported_version('{"version":"a857341","commit":"abc"}'),
            "a857341 (abc)",
        )


if __name__ == "__main__":
    unittest.main()

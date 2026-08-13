#!/usr/bin/env python3
"""Tests for the pinned, project-local Dreamina installer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
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

    def test_verified_install_is_project_local_and_side_effect_limited(self) -> None:
        payload = b"fixture-dreamina-binary"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".video-replacer" / "bin"
            target = root / "dreamina"

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
            ), mock.patch.object(installer.subprocess, "run", return_value=completed):
                report = installer.install(opener=opener)

            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(report["path"], str(target))
            self.assertEqual(report["binary_version"], "a857341-dirty (a857341)")
            self.assertFalse(report["modified_shell"])
            self.assertFalse(report["installed_global_skill"])
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_hash_mismatch_never_replaces_existing_binary(self) -> None:
        payload = b"unreviewed"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".video-replacer" / "bin"
            root.mkdir(parents=True)
            target = root / "dreamina"
            target.write_bytes(b"existing-reviewed-binary")
            with mock.patch.object(installer, "INSTALL_ROOT", root), mock.patch.object(
                installer, "TARGET_PATH", target
            ), mock.patch.object(
                installer,
                "artifact_for",
                return_value={"url": "https://official.example/dreamina", "sha256": "0" * 64},
            ):
                with self.assertRaisesRegex(installer.InstallError, "SHA-256 mismatch"):
                    installer.install(opener=lambda request, timeout: FakeResponse(payload))
            self.assertEqual(target.read_bytes(), b"existing-reviewed-binary")


if __name__ == "__main__":
    unittest.main()

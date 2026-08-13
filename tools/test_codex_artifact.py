#!/usr/bin/env python3
"""Fixture tests for pre-execution Windows Codex provenance verification."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_artifact


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self.headers = headers or {"Content-Type": "application/json"}
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class WindowsCodexArtifactTests(unittest.TestCase):
    VERSION = "0.147.0"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.release_root = (
            root
            / "home"
            / "packages"
            / "standalone"
            / "releases"
            / f"{self.VERSION}-{codex_artifact.TARGET}"
        )
        for relative in codex_artifact.REQUIRED_DIRECTORIES:
            (self.release_root / relative).mkdir(parents=True, exist_ok=True)
        self.binary = self.release_root / codex_artifact.ENTRYPOINT
        for relative in codex_artifact.REQUIRED_FILES:
            path = self.release_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative != Path("codex-package.json"):
                path.write_bytes(b"MZ" + relative.as_posix().encode("ascii"))
        self.metadata = {
            "layoutVersion": 1,
            "version": self.VERSION,
            "target": codex_artifact.TARGET,
            "variant": "codex",
            "entrypoint": "bin/codex.exe",
            "resourcesDir": "codex-resources",
            "pathDir": "codex-path",
        }
        self._write_metadata()

    def _write_metadata(self) -> None:
        (self.release_root / "codex-package.json").write_text(
            json.dumps(self.metadata), encoding="utf-8"
        )

    def _manifest(self, *, digest: str | None = None) -> dict[str, object]:
        digest = digest or hashlib.sha256(self.binary.read_bytes()).hexdigest()
        asset_name = codex_artifact.RELEASE_ASSET_NAME
        return {
            "tag_name": f"rust-v{self.VERSION}",
            "assets": [
                {
                    "name": asset_name,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": (
                        "https://releases.openai.com/codex/releases/"
                        f"{self.VERSION}/{asset_name}"
                    ),
                }
            ],
        }

    def test_verifies_fixture_without_executing_binary(self) -> None:
        requested: list[str] = []

        def fetch(url: str) -> dict[str, object]:
            requested.append(url)
            return self._manifest()

        result = codex_artifact.verify_windows_codex(
            self.binary, release_fetcher=fetch
        )

        self.assertEqual(result["version"], self.VERSION)
        self.assertEqual(
            result["sha256"], hashlib.sha256(self.binary.read_bytes()).hexdigest()
        )
        self.assertEqual(result["target"], codex_artifact.TARGET)
        self.assertEqual(result["entrypoint"], "bin/codex.exe")
        self.assertEqual(
            requested,
            [
                "https://releases.openai.com/codex/releases/"
                f"{self.VERSION}/release.json"
            ],
        )

    def test_rejects_non_modern_standalone_layout(self) -> None:
        outside = Path(self.temporary.name) / "codex.exe"
        outside.write_bytes(self.binary.read_bytes())
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError,
            "standalone bin directory|packages/standalone/releases",
        ):
            codex_artifact.verify_windows_codex(
                outside, release_fetcher=lambda _url: self._manifest()
            )

    def test_rejects_invalid_package_metadata_fields(self) -> None:
        invalid_values = {
            "layoutVersion": 2,
            "version": "0.146.0",
            "target": "aarch64-pc-windows-msvc",
            "variant": "codex-app-server",
            "entrypoint": "codex.exe",
            "resourcesDir": "resources",
            "pathDir": "path",
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key):
                original = self.metadata[key]
                self.metadata[key] = value
                self._write_metadata()
                with self.assertRaisesRegex(
                    codex_artifact.CodexArtifactError,
                    "Invalid codex-package.json field",
                ):
                    codex_artifact.verify_windows_codex(
                        self.binary, release_fetcher=lambda _url: self._manifest()
                    )
                self.metadata[key] = original
                self._write_metadata()

    def test_rejects_boolean_layout_version(self) -> None:
        self.metadata["layoutVersion"] = True
        self._write_metadata()
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError,
            "Invalid codex-package.json field",
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: self._manifest()
            )

    def test_rejects_missing_required_package_file(self) -> None:
        helper = self.release_root / "codex-resources/codex-command-runner.exe"
        helper.unlink()
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "Required Codex package"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: self._manifest()
            )

    def test_rejects_symlinked_package_entry(self) -> None:
        helper = self.release_root / "codex-path/rg.exe"
        target = Path(self.temporary.name) / "outside-rg.exe"
        target.write_bytes(helper.read_bytes())
        helper.unlink()
        try:
            helper.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "reparse-linked"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: self._manifest()
            )

    def test_rejects_wrong_release_tag_or_duplicate_target_asset(self) -> None:
        wrong_tag = self._manifest()
        wrong_tag["tag_name"] = "rust-v0.146.0"
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "tag does not match"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: wrong_tag
            )

        duplicate = self._manifest()
        duplicate["assets"] = duplicate["assets"] * 2  # type: ignore[operator]
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "exactly one"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: duplicate
            )

    def test_rejects_nonofficial_asset_url(self) -> None:
        manifest = self._manifest()
        assets = manifest["assets"]
        assert isinstance(assets, list) and isinstance(assets[0], dict)
        assets[0]["browser_download_url"] = (
            f"https://evil.example/codex/{codex_artifact.RELEASE_ASSET_NAME}"
        )
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "official HTTPS endpoint"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: manifest
            )

    def test_rejects_executable_digest_mismatch(self) -> None:
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "SHA-256 does not match"
        ):
            codex_artifact.verify_windows_codex(
                self.binary,
                release_fetcher=lambda _url: self._manifest(digest="0" * 64),
            )

    def test_builtin_fetch_rejects_redirect_to_nonofficial_host(self) -> None:
        body = json.dumps(self._manifest()).encode("utf-8")
        response = FakeResponse(body, "https://evil.example/release.json")
        with mock.patch.object(
            codex_artifact, "_open_official_release", return_value=response
        ):
            with self.assertRaisesRegex(
                codex_artifact.CodexArtifactError, "official HTTPS endpoint"
            ):
                codex_artifact.verify_windows_codex(self.binary)

    def test_redirect_handler_rejects_each_nonofficial_hop(self) -> None:
        expected_path = f"/codex/releases/{self.VERSION}/release.json"
        handler = codex_artifact._OfficialRedirectHandler(expected_path)
        request = codex_artifact.urllib.request.Request(
            f"https://releases.openai.com{expected_path}"
        )
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "official HTTPS endpoint"
        ):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://evil.example/intermediate",
            )

    def test_builtin_fetch_rejects_oversized_declared_or_streamed_body(self) -> None:
        official_url = (
            "https://releases.openai.com/codex/releases/"
            f"{self.VERSION}/release.json"
        )
        declared = FakeResponse(
            b"{}",
            official_url,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(codex_artifact.RELEASE_MANIFEST_MAX_BYTES + 1),
            },
        )
        streamed = FakeResponse(
            b"x" * (codex_artifact.RELEASE_MANIFEST_MAX_BYTES + 1), official_url
        )
        for response in (declared, streamed):
            with self.subTest(response=response), mock.patch.object(
                codex_artifact, "_open_official_release", return_value=response
            ):
                with self.assertRaisesRegex(
                    codex_artifact.CodexArtifactError, "exceeds the size limit"
                ):
                    codex_artifact.verify_windows_codex(self.binary)

    def test_rejects_duplicate_json_keys_from_raw_fetcher(self) -> None:
        raw = b'{"tag_name":"rust-v0.147.0","tag_name":"rust-v0.147.0","assets":[]}'
        with self.assertRaisesRegex(
            codex_artifact.CodexArtifactError, "strict JSON"
        ):
            codex_artifact.verify_windows_codex(
                self.binary, release_fetcher=lambda _url: raw
            )


if __name__ == "__main__":
    unittest.main()

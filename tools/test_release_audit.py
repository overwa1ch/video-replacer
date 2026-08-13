#!/usr/bin/env python3
"""Regression tests for public payload and clean-root history auditing."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import release_audit


class ReleaseAuditHomePathTests(unittest.TestCase):
    def test_pinned_dreamina_version_metadata_is_required(self) -> None:
        self.assertIn(Path("tools/dreamina-version.json"), release_audit.REQUIRED_FILES)

    def test_payload_allows_placeholder_homes_and_rejects_real_users(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placeholder = root / "placeholder.md"
            private_posix = root / "private-posix.md"
            private_windows = root / "private-windows.md"
            placeholder.write_text(
                "/Users/example/project\n"
                "/home/user/project\n"
                "C:\\Users\\username\\project\n"
                "C:\\Users\\you\\project\n",
                encoding="utf-8",
            )
            private_posix.write_text(
                "/" + "Users" + "/alice/project\n", encoding="utf-8"
            )
            private_windows.write_text(
                "C:" + "\\" + "Users" + "\\bob\\project\n", encoding="utf-8"
            )

            with mock.patch.object(release_audit, "REPO_ROOT", root), mock.patch.object(
                release_audit, "REQUIRED_FILES", set()
            ):
                errors = release_audit.scan_paths(
                    [
                        placeholder.relative_to(root),
                        private_posix.relative_to(root),
                        private_windows.relative_to(root),
                    ]
                )

            self.assertNotIn(
                "private POSIX home path in placeholder.md", errors
            )
            self.assertNotIn(
                "private Windows home path in placeholder.md", errors
            )
            self.assertIn(
                "private POSIX home path in private-posix.md", errors
            )
            self.assertIn(
                "private Windows home path in private-windows.md", errors
            )

    def test_clean_root_history_allows_placeholder_homes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._initialize_repository(root)
            (root / "README.md").write_text(
                "/Users/example/project\n"
                "/home/user/project\n"
                "C:\\Users\\username\\project\n"
                "C:\\Users\\you\\project\n",
                encoding="utf-8",
            )
            self._commit_all(root, "Initial public release")

            with mock.patch.object(release_audit, "REPO_ROOT", root):
                self.assertEqual(release_audit.scan_history(), [])

    def test_history_rejects_real_posix_and_windows_home_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._initialize_repository(root)
            (root / "private-posix.md").write_text(
                "/" + "Users" + "/alice/project\n", encoding="utf-8"
            )
            (root / "private-windows.md").write_text(
                "C:" + "\\" + "Users" + "\\bob\\project\n", encoding="utf-8"
            )
            self._commit_all(root, "Unsafe root")

            with mock.patch.object(release_audit, "REPO_ROOT", root):
                errors = release_audit.scan_history()

            self.assertIn(
                "Git history contains a private home path: private-posix.md", errors
            )
            self.assertIn(
                "Git history contains a private home path: private-windows.md", errors
            )

    @staticmethod
    def _initialize_repository(root: Path) -> None:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "Release Audit Test"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "release-audit@example.invalid"],
            cwd=root,
            check=True,
        )

    @staticmethod
    def _commit_all(root: Path, message: str) -> None:
        subprocess.run(["git", "add", "--all"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main()

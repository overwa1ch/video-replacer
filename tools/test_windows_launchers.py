#!/usr/bin/env python3
"""Static and argv-routing tests for native Windows entrypoints."""

from __future__ import annotations

import unittest
import importlib.util
import tempfile
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(__file__).with_name("windows_launcher.py")
SPEC = importlib.util.spec_from_file_location("video_replacer_windows_launcher", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class WindowsLauncherTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (REPO_ROOT / name).read_text(encoding="utf-8")

    def test_entrypoints_are_native_cmd_without_powershell_policy_changes(self) -> None:
        for name in ("install.cmd", "video-replacer.cmd", "video-replacer-test.cmd"):
            text = self.read(name)
            self.assertIn("DisableDelayedExpansion", text)
            self.assertIn("%~dp0", text)
            self.assertNotIn("ExecutionPolicy", text)
            self.assertNotIn("powershell", text.casefold())
            self.assertNotIn("wsl", text.casefold())

    def test_public_workflow_entrypoint_uses_repo_venv_and_setup_gate(self) -> None:
        text = self.read("video-replacer.cmd")
        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("tools\\windows_launcher.py\" %*", text)

    def test_router_preserves_space_and_unicode_paths_as_argv(self) -> None:
        root = Path("C:/Users/example/视频 替换仓库")
        command = launcher.route(["status", "--json"], repo_root=root)
        self.assertEqual(command[0], "node")
        self.assertEqual(command[2:4], ["--project-root", str(root)])
        self.assertEqual(
            command[4:6], ["--root", str(root / "workspace" / "video-loop")]
        )
        self.assertEqual(command[-2:], ["status", "--json"])

    def test_router_rejects_infrastructure_overrides(self) -> None:
        for flag in launcher.RESERVED_FLAGS:
            with self.subTest(flag=flag), self.assertRaisesRegex(
                ValueError, "owns infrastructure options"
            ):
                launcher.route(["once", flag, "C:/attacker"])

    def test_router_sends_task_commands_through_live_setup_gate(self) -> None:
        root = Path("C:/repo")
        command = launcher.route(
            ["--confirm-paid", "submit-prepared", "batch-01"], repo_root=root
        )
        self.assertEqual(
            command,
            [
                str(root / ".venv" / "Scripts" / "python.exe"),
                str(root / "tools" / "setup.py"),
                "launch",
                "--",
                "--confirm-paid",
                "submit-prepared",
                "batch-01",
            ],
        )

    def test_main_runs_child_without_a_shell_and_returns_real_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "含空格 仓库"
            python = root / ".venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fixture")
            completed = mock.Mock(returncode=17)
            runner = mock.Mock(return_value=completed)
            result = launcher.main(
                ["setup", "status", "--json"], repo_root=root, runner=runner
            )
        self.assertEqual(result, 17)
        arguments, keywords = runner.call_args
        self.assertEqual(
            arguments[0],
            [str(python), str(root / "tools" / "setup.py"), "status", "--json"],
        )
        self.assertEqual(keywords, {"check": False})

    def test_windows_test_entrypoint_matches_public_suite(self) -> None:
        text = self.read("video-replacer-test.cmd")
        for phrase in (
            "unittest discover -s validation",
            "unittest discover -s tools",
            "--test tools\\test_video_batch_orchestrator.mjs",
            "--check tools\\video_batch_orchestrator.mjs",
            "tools\\release_audit.py",
        ):
            self.assertIn(phrase, text)

    def test_installer_has_ordered_python_candidates_and_no_stale_block_exit(self) -> None:
        text = self.read("install.cmd")
        positions = [
            text.index("VIDEO_REPLACER_BOOTSTRAP_PYTHON"),
            text.index("if exist \"%REPOSITORY_PYTHON%\""),
            text.index("py.exe -3.12"),
            text.index("python3.12.exe"),
            text.index("python.exe -c"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("exit /b %ERRORLEVEL%\n  )", text)

    def test_ci_negative_installer_test_uses_a_real_exe_in_unicode_path(self) -> None:
        workflow = self.read(".github/workflows/ci.yml")
        self.assertIn("'视频 替换 repo'", workflow)
        self.assertIn("Get-Command python.exe", workflow)
        self.assertIn("'raise SystemExit(23)'", workflow)
        self.assertIn("Start-Process -FilePath $env:ComSpec", workflow)
        self.assertIn("$result = $child.ExitCode", workflow)
        self.assertNotIn("video-replacer-fake-python.cmd", workflow)


if __name__ == "__main__":
    unittest.main()

"""Checks that the operator skill documents callable workflow interfaces."""

import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / ".agents" / "skills" / "video-replacer"


class DocCommandContractTest(unittest.TestCase):
    def test_operator_docs_describe_the_v3_profile_and_local_prepare_boundary(self) -> None:
        documents = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "workflows" / "video-replacement-batch-folder-loop-v1.md",
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "references" / "workflow-operator.md",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        for required in (
            '"schema_version": 3',
            "backend_profile",
            "dreamina_cli_seedance_2_5",
            "dreamina_cli_seedance_2_0",
            "200,000,000 bytes",
            "50,000,000 bytes",
            "190,000,000 bytes",
            "47,000,000 bytes",
            "upload-preparation.json",
            "source-upload-ready.mp4",
            "--confirm-paid",
        ):
            self.assertIn(required, text)
        self.assertIn("performs no backend upload", text)
        self.assertIn("should_compress", text)
        self.assertNotIn("volcengine_ark_seedance_2_5", text)

    def test_orchestrator_exposes_every_documented_operator_command(self) -> None:
        result = subprocess.run(
            ["node", str(PROJECT_ROOT / "tools" / "video_batch_orchestrator.mjs"), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        helptext = result.stdout + result.stderr
        for command in (
            "once",
            "check",
            "prepare",
            "retry-prepare",
            "submit-prepared",
            "status",
        ):
            self.assertIn(command, helptext)

    def test_operator_docs_define_native_platform_launchers(self) -> None:
        documents = [
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "AGENT-INSTALL-PROMPT.md",
            PROJECT_ROOT / "workflows" / "video-replacement-batch-folder-loop-v1.md",
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "references" / "first-run-setup.md",
            SKILL_ROOT / "references" / "workflow-operator.md",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        for required in (
            "./video-replacer",
            ".\\video-replacer.cmd",
            "./install --with-mosaic",
            ".\\install.cmd --with-mosaic",
            "Windows 11 x64",
            "setup login-dreamina",
            "WSL",
            "Git Bash",
            "PowerShell execution policy",
        ):
            self.assertIn(required, text)
        self.assertNotIn("Windows 代码路径仍属实验性", text)

    def test_workflow_mosaic_runner_exposes_minimal_flags(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "privacy" / "face_mosaic.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        helptext = result.stdout + result.stderr
        for flag in (
            "--video",
            "--output",
        ):
            self.assertIn(flag, helptext)
        for retired in ("--write-workflow-intake", "--review", "--preset"):
            self.assertNotIn(retired, helptext)


if __name__ == "__main__":
    unittest.main()

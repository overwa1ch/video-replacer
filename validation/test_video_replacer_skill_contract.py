from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "video-replacer"


class SkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.operator = (
            SKILL_DIR / "references" / "workflow-operator.md"
        ).read_text(encoding="utf-8")
        self.privacy = (
            SKILL_DIR / "references" / "face-anonymization.md"
        ).read_text(encoding="utf-8")
        self.interface = (SKILL_DIR / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )

    def test_skill_is_a_short_operator_contract(self) -> None:
        for sentence in (
            "Treat the Git repository containing this skill as the complete project",
            "workflow owns media analysis",
            "schema-v3 `job-bindings.json`",
            "wait on that same process",
        ):
            self.assertIn(sentence, self.skill)
        self.assertLess(len(self.skill.splitlines()), 100)

    def test_only_javascript_workflow_commands_are_exposed(self) -> None:
        self.assertIn("tools/video_batch_orchestrator.mjs", self.skill)
        self.assertIn('[LAUNCHER, "--confirm-paid", "submit-prepared", <batch>]', self.skill)
        self.assertIn("REPO_ROOT\\video-replacer.cmd", self.skill)
        self.assertIn("REPO_ROOT/video-replacer", self.skill)
        self.assertIn("[LAUNCHER, <command>, ...arguments]", self.skill)
        self.assertIn("Do not start a persistent watcher", self.skill)
        self.assertNotIn("scripts/face_mosaic.py", self.skill)
        self.assertNotIn("--execute", self.skill)
        self.assertNotIn("--auto-ready", self.skill)

    def test_first_run_contract_is_native_and_platform_aware(self) -> None:
        first_run = (
            SKILL_DIR / "references" / "first-run-setup.md"
        ).read_text(encoding="utf-8")
        for phrase in (
            "native Windows 11 x64",
            ".\\install.cmd --with-mosaic",
            ".\\video-replacer.cmd setup status --json",
            "setup login-dreamina",
            "never execute `dreamina` or `dreamina.exe` directly",
            "spaces or Chinese characters",
            "WSL/Git Bash",
            "PowerShell execution policy",
        ):
            self.assertIn(phrase, self.skill + first_run)

    def test_schema_v3_profile_and_privacy_mode_are_explicit(self) -> None:
        for sentence in (
            '"schema_version": 3',
            '"backend_profile": "dreamina_cli_seedance_2_5"',
            '"privacy_mode": "none"',
            "mosaic_required",
            "Natural-language wording does not control workflow behavior",
        ):
            self.assertIn(sentence, self.operator + self.privacy)
        for forbidden_agent_field in (
            "should_compress",
            "compression_policy",
            "final upload path",
        ):
            self.assertIn(forbidden_agent_field, self.skill + self.operator)
        self.assertIn("Never infer `privacy_mode: none` from silence", self.skill)
        self.assertIn("silence is not authorization to use `none`", self.operator)

    def test_mosaic_is_workflow_owned_and_fail_closed(self) -> None:
        for sentence in (
            "workflow invokes its own fixed tool",
            "output file exists",
            "does not fall back to the original video",
            "The original video is never used as a fallback",
        ):
            self.assertIn(sentence, self.operator + self.privacy)
        for retired in (
            "prepared-source.json",
            "privacy-review",
            "zero raw hits",
            "--write-workflow-intake",
        ):
            self.assertNotIn(retired, self.operator + self.privacy)

    def test_media_review_is_absent(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [
                SKILL_DIR / "SKILL.md",
                *(SKILL_DIR / "references").glob("*.md"),
            ]
        )
        for retired in (
            "VISUAL_REVIEW_PENDING",
            "human reviewer",
            "prepared-source.json",
            "privacy-review.json",
        ):
            self.assertNotIn(retired, combined)
        self.assertIn("Do not ask an Agent to score or approve", combined)

    def test_reference_generation_is_explicit_and_visible(self) -> None:
        for reference in (
            "static-asset-prompt-templates.md",
            "reference-image-generation.md",
        ):
            self.assertIn(reference, self.skill)
            self.assertTrue((SKILL_DIR / "references" / reference).is_file())
        self.assertIn("Generate a missing reference", self.skill)
        self.assertIn("视频替换任务", self.interface)
        self.assertIn("$video-replacer", self.interface)
        self.assertIn("explicit paid Gate", self.interface)

    def test_tree_contains_only_operator_resources(self) -> None:
        expected_references = {
            "first-run-setup.md",
            "workflow-operator.md",
            "static-asset-prompt-templates.md",
            "reference-image-generation.md",
            "face-anonymization.md",
        }
        actual_references = {
            path.name for path in (SKILL_DIR / "references").glob("*.md")
        }
        self.assertEqual(actual_references, expected_references)
        runtime_scripts = [
            path
            for path in (SKILL_DIR / "scripts").rglob("*")
            if path.is_file()
        ] if (SKILL_DIR / "scripts").exists() else []
        self.assertEqual(runtime_scripts, [], "workflow code must not live in the skill")

    def test_frontmatter_describes_the_operator_boundary(self) -> None:
        description = re.search(r'^description: "(.+)"$', self.skill, re.MULTILINE)
        self.assertIsNotNone(description)
        text = description.group(1)
        for phrase in (
            "downloads",
            "sets up",
            "repository-local video replacement jobs",
            "mask faces",
            "generate references",
            "privacy_mode",
            "backend_profile",
            "repository launcher",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()

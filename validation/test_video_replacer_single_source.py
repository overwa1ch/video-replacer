"""Guards the project-local single source for the operator skill."""

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = PROJECT_ROOT / ".agents" / "skills" / "video-replacer" / "SKILL.md"
class SingleSourceTest(unittest.TestCase):
    def test_canonical_skill_exists_only_in_the_project(self) -> None:
        self.assertTrue(CANONICAL.is_file())
        self.assertFalse(
            (PROJECT_ROOT / ".claude" / "skills" / "video-replacer").exists(),
            "a second project pointer would recreate a drifting source",
        )

    def test_description_routes_to_workflow_operation(self) -> None:
        text = CANONICAL.read_text(encoding="utf-8")
        match = re.search(r'^description: "(.+)"$', text, re.MULTILINE)
        self.assertIsNotNone(match)
        description = match.group(1)
        self.assertIn("repository-local video replacement jobs", description)
        self.assertIn("repository launcher", description)
        self.assertIn("privacy_mode", description)
        self.assertIn("backend_profile", description)

    def test_repo_agents_points_at_the_skill_without_copying_rules(self) -> None:
        agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        matching = [
            line for line in agents.splitlines()
            if "video-replacer" in line and ".agents/skills/video-replacer/SKILL.md" in line
        ]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main()

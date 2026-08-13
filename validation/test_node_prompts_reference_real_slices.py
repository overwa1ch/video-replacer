"""Pins the direct media-to-prompt node to its minimum context slice."""

import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOP_PATH = PROJECT_ROOT / "tools" / "video_batch_loop.py"
NODE_CONTRACT_ROOT = PROJECT_ROOT / "tools" / "video-replacement-node-contracts"


def load_loop():
    spec = importlib.util.spec_from_file_location("node_prompt_loop", LOOP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["node_prompt_loop"] = module
    spec.loader.exec_module(module)
    return module


class NodePromptSliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loop = load_loop()
        node_input = {
            "job_id": "V001",
            "sampled_frames": [
                {"image": "inputs/frames/frame-001.jpg", "timestamp_seconds": 0.0}
            ],
            "attachment_order": ["inputs/frames/frame-001.jpg"],
            "output_contract": {"result_field": "jobs[0].prompt"},
        }
        cls.rendered = {
            "video_to_prompt": cls.loop.video_to_prompt_node_prompt(
                "V001", node_input
            ),
        }

    def test_runtime_contracts_exist_inside_the_workflow_package(self) -> None:
        for filename in ("video-to-prompt.md",):
            with self.subTest(filename=filename):
                path = self.loop.node_contract_path(filename)
                self.assertTrue(path.is_file())
                self.assertTrue(path.is_relative_to(NODE_CONTRACT_ROOT.resolve()))

    def test_each_runtime_stage_receives_only_its_required_contract(self) -> None:
        expected = {
            "video_to_prompt": "video-to-prompt.md",
        }
        for name, text in self.rendered.items():
            with self.subTest(prompt=name):
                filename = expected[name]
                self.assertIn(f"节点合同（{filename}）", text)
                self.assertIn(self.loop.node_contract_text(filename), text)
                for other in {
                    "source-analysis.md",
                    "reference-analysis.md",
                    "prompt-writer.md",
                }:
                    self.assertNotIn(f"节点合同（{other}）", text)

    def test_combined_node_delivers_a_prompt_without_analysis_json_handoff(self) -> None:
        contract = self.loop.node_contract_text("video-to-prompt.md")
        prompt = self.rendered["video_to_prompt"]
        for text in (contract, prompt):
            with self.subTest(text="contract" if text == contract else "prompt"):
                self.assertIn("prompt.txt", text)
                self.assertNotIn("source-analysis.json", text)
                self.assertNotIn("reference-analysis.json", text)

    def test_runtime_nodes_do_not_name_interactive_entry_concepts(self) -> None:
        forbidden = {
            "SKILL.md",
            "$video-replacer",
            "Full Access",
            "标准模式",
            "渐进式披露",
            "入口资格判定",
            "/.agents/skills/",
            "video-replacer",
        }
        for name, text in self.rendered.items():
            for term in forbidden:
                with self.subTest(prompt=name, term=term):
                    self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()

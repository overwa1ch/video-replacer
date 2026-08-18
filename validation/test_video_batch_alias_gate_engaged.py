"""Keeps the parent-owned prompt-format Gate from becoming a silent no-op.

`validate_execution_prompt` can validate syntax from a reference count alone,
but only parent-owned semantic names prove that the node kept the declared
binding and actually used every requested reference in the body. A production
call site that omits those names quietly weakens the paid-path Gate.

Supplying the parent-owned reference count and ordered semantic names verifies
the complete first-line binding and body usage. Shot prose stays under the
direct Video-to-Prompt contract and is advisory to this paid-path Gate.
"""

import ast
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOP_PATH = PROJECT_ROOT / "tools" / "video_batch_loop.py"


def load_loop():
    spec = importlib.util.spec_from_file_location("alias_gate_loop", LOOP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["alias_gate_loop"] = module
    spec.loader.exec_module(module)
    return module


class AliasGateEngagedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loop = load_loop()

    def test_supplying_reference_count_requires_every_parent_bound_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prompt = Path(temporary) / "prompt.txt"
            prompt.write_text(
                "素材绑定：@视频1=原视频；@图片1=目标内饰。\n"
                "镜头1（0.0-1.0s）\n"
                "将车内饰替换为目标内饰。\n",
                encoding="utf-8",
            )
            self.loop.validate_execution_prompt(prompt)
            with self.assertRaisesRegex(self.loop.LoopError, "每张有序参考图"):
                self.loop.validate_execution_prompt(prompt, reference_count=2)

    def test_every_production_call_site_supplies_parent_semantic_names(self) -> None:
        tree = ast.parse(LOOP_PATH.read_text(encoding="utf-8"))
        weak = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name != "validate_execution_prompt":
                continue
            supplied = {kw.arg for kw in node.keywords}
            if not {
                "expected_reference_names",
                "expected_requirement_lines",
            }.issubset(supplied):
                weak.append(node.lineno)
        self.assertEqual(
            weak,
            [],
            f"{LOOP_PATH.name} calls validate_execution_prompt without parent "
            f"semantic names or immutable requirements at line(s) {weak}; those "
            "call sites cannot prove that the prompt preserved every hard "
            "requirement and used every declared reference.",
        )


if __name__ == "__main__":
    unittest.main()

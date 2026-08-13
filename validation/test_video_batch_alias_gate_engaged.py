"""Keeps the parent-owned prompt-format Gate from becoming a silent no-op.

`validate_execution_prompt(prompt_file, reference_count=None)` only runs
`_validate_reference_alias_contract` when a reference count is supplied. A call
site that omits it is valid Python, raises nothing, and quietly skips the gate
that checks the fixed prompt contract — so the failure mode is invisible.

Supplying the parent-owned reference count verifies the complete first-line
binding and the user-approved multi-shot format. Source facts stay bounded by
the direct Video-to-Prompt contract; this Gate validates format.
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

    def test_every_call_site_supplies_a_reference_count(self) -> None:
        tree = ast.parse(LOOP_PATH.read_text(encoding="utf-8"))
        bare = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name != "validate_execution_prompt":
                continue
            supplied = {kw.arg for kw in node.keywords} | {
                "positional" for _ in node.args[1:]
            }
            if not ({"reference_count", "positional"} & supplied):
                bare.append(node.lineno)
        self.assertEqual(
            bare,
            [],
            f"{LOOP_PATH.name} calls validate_execution_prompt without a "
            f"reference count at line(s) {bare}; the alias gate silently does "
            "nothing there. The node-refactor branch's call sites are all of "
            "this shape — porting them verbatim disables validation 266.",
        )


if __name__ == "__main__":
    unittest.main()

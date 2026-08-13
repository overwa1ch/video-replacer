"""Pins the direct video-to-prompt pipeline and deterministic parent gates.

Ordering failures do not necessarily crash or change corpus output; they can
surface only as money spent or a privacy request quietly dropped. These tests
keep those boundaries in deterministic code instead of prose.
"""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOP_PATH = PROJECT_ROOT / "tools" / "video_batch_loop.py"


def load_loop():
    spec = importlib.util.spec_from_file_location("node_invariants_loop", LOOP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["node_invariants_loop"] = module
    spec.loader.exec_module(module)
    return module


def function_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(LOOP_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is missing from {LOOP_PATH.name}")


def call_order(fn: ast.FunctionDef, names) -> list:
    """Line numbers of the first call to each named function, in file order."""
    seen = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called in names and called not in seen:
            seen[called] = node.lineno
    return sorted(seen.items(), key=lambda item: item[1])


class PipelineOrderingTest(unittest.TestCase):
    def test_parent_binding_precedes_direct_video_to_prompt_node(self) -> None:
        fn = function_body("prepare_streaming_job")
        order = [
            name
            for name, _ in call_order(
                fn,
                {
                    "select_job_reference_records",
                    "_write_parent_reference_binding",
                    "run_video_to_prompt_node",
                },
            )
        ]
        self.assertEqual(
            order,
            [
                "select_job_reference_records",
                "_write_parent_reference_binding",
                "run_video_to_prompt_node",
            ],
            "the parent must freeze the current Job's binding before one node "
            "can inspect that Job's media and write its prompt",
        )

    def test_preparation_does_not_split_media_analysis_from_prompt_writing(self) -> None:
        fn = function_body("prepare_streaming_job")
        legacy_nodes = {
            "run_source_analysis_node",
            "run_reference_analysis_node",
            "run_prompt_writer_node",
        }
        called = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(
            "run_video_to_prompt_node" in called,
            "prepare must invoke the media-capable node that writes prompt.txt",
        )
        self.assertTrue(
            legacy_nodes.isdisjoint(called),
            "no analysis JSON handoff node may run before a separate prompt writer",
        )

    def test_reference_binding_resolves_before_the_first_gate(self) -> None:
        fn = function_body("prepare_streaming_job")
        order = [
            name
            for name, _ in call_order(
                fn, {"resolve_reference_binding", "validate_execution_prompt"}
            )
        ]
        self.assertEqual(
            order[:2],
            ["resolve_reference_binding", "validate_execution_prompt"],
            "the parent-owned binding is the source of the reference count. "
            "Resolving it after the gate means the fixed binding check runs "
            "with nothing.",
        )

    def test_the_single_gate_is_the_last_prompt_step_before_preview(self) -> None:
        fn = function_body("prepare_streaming_job")
        order = [
            name
            for name, _ in call_order(
                fn,
                {
                    "run_video_to_prompt_node",
                    "resolve_reference_binding",
                    "validate_execution_prompt",
                    "prepare_active_video",
                },
            )
        ]
        self.assertEqual(
            order,
            [
                "run_video_to_prompt_node",
                "resolve_reference_binding",
                "validate_execution_prompt",
                "prepare_active_video",
            ],
            "the direct node must finish before the deterministic Gate blocks "
            "preview preparation",
        )

class ExplicitPrivacyModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loop = load_loop()

    def test_only_two_privacy_modes_are_accepted(self) -> None:
        self.assertEqual(self.loop.PRIVACY_MODES, {"none", "mosaic_required"})

    def test_explicit_mode_is_read_before_video_to_prompt(self) -> None:
        fn = function_body("prepare_streaming_job")
        order = [
            name
            for name, _ in call_order(
                fn, {"job_privacy_mode", "run_video_to_prompt_node"}
            )
        ]
        self.assertEqual(
            order,
            ["job_privacy_mode", "run_video_to_prompt_node"],
            "the explicit binding must be frozen before downstream work",
        )

    def test_mosaic_runs_after_prompt_gate_and_before_probe(self) -> None:
        fn = function_body("prepare_streaming_job")
        order = [
            name
            for name, _ in call_order(
                fn,
                {"validate_execution_prompt", "prepare_mosaic_video", "run_executor_probe"},
            )
        ]
        self.assertEqual(
            order,
            ["validate_execution_prompt", "prepare_mosaic_video", "run_executor_probe"],
        )


if __name__ == "__main__":
    unittest.main()

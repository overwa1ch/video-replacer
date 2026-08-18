#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import sys
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("video_batch_loop.py")
SPEC = importlib.util.spec_from_file_location("video_batch_loop", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
loop = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = loop
SPEC.loader.exec_module(loop)


class VideoBatchLoopDreaminaTests(unittest.TestCase):
    def test_shot_writing_gate_accepts_inline_change_paragraph(self) -> None:
        prompt = "\n".join(
            (
                "镜头1（0.0-2.5s）将驾驶位人物替换为目标人物，并保持原有动作。",
                "镜头2（2.5-5.0s）将车内饰替换为目标内饰，并保持车辆行驶状态。",
            )
        )
        self.assertEqual(loop._validate_shot_writing_contract(prompt), [])

    def test_shot_writing_gate_rejects_empty_heading(self) -> None:
        prompt = "镜头1（0.0-2.5s）\n镜头2（2.5-5.0s）\n保留原有动作。"
        self.assertEqual(
            loop._validate_shot_writing_contract(prompt),
            ["镜头1缺少逐镜变更内容"],
        )

    def test_reference_gate_does_not_block_shot_prose_variation(self) -> None:
        prompt = "\n".join(
            (
                "素材绑定：@视频1=原视频；@图片1=主驾人物；@图片2=前排右侧人物；@图片3=后排人物；@图片4=目标内饰。",
                "镜头四：将主驾人物、前排右侧人物、后排人物和目标内饰"
                "用于各自指定替换；车顶不得变成漏天玻璃，保持当前动作。",
            )
        )
        self.assertEqual(loop._validate_reference_alias_contract(prompt, 4), [])

    def test_default_executor_is_project_dreamina_adapter(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIDEO_REPLACEMENT_EXECUTOR", None)
            executor = loop.find_replacement_executor()
        self.assertEqual(executor.path.name, "dreamina_video.py")
        self.assertEqual(executor.path.parent, MODULE_PATH.parent.resolve())
        self.assertEqual(executor.transport, "dreamina_cli_local_upload")

    def test_legacy_pipeline_variable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replacement = Path(temporary) / "replacement_pipeline.py"
            replacement.write_text("# fixture\n", encoding="utf-8")
            with self.assertRaises(loop.LoopError):
                loop.find_replacement_executor(
                    environment={"VIDEO_REPLACER_PIPELINE": str(replacement)}
                )

    def test_video_to_prompt_node_has_one_continuous_media_to_prompt_contract(self) -> None:
        video_to_prompt = loop.video_to_prompt_node_prompt(
            "V001",
            {
                "job_id": "V001",
                "sampled_frames": [
                    {
                        "image": "inputs/frames/frame-001.jpg",
                        "timestamp_seconds": 0.0,
                    }
                ],
                "attachment_order": ["inputs/frames/frame-001.jpg"],
                "output_contract": {"result_field": "jobs[0].prompt"},
            },
        )
        contract = loop.node_contract_path("video-to-prompt.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "write_video_to_prompt_node_input",
            "video_to_prompt_node_prompt",
            "run_video_to_prompt_node",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(loop, name, None)))
        self.assertIn("video-to-prompt.md", video_to_prompt)
        self.assertIn("prompt.txt", contract)
        self.assertNotIn("source-analysis.json", contract)
        self.assertNotIn("reference-analysis.json", contract)
        self.assertIn("素材绑定：@视频1=原视频；@图片1=目标对象A", contract)
        self.assertIn("source-layout fact explicitly confirmed", contract)
        self.assertIn("midpoint of that observed interval", contract)
        self.assertIn("multi-shot source", contract)
        self.assertIn("Sora", contract)
        for prompt in (video_to_prompt,):
            self.assertNotIn("dreamina_video.py", prompt)
            self.assertNotIn("SKILL.md", prompt)
            self.assertNotIn("video-replacer", prompt)
        self.assertNotIn("task_id", contract)
        for downstream_term in (
            "preflight",
            "executor",
            "submission",
            "visual acceptance",
        ):
            self.assertNotIn(downstream_term, contract.casefold())
        # The submission name is no longer an instruction the child could get
        # wrong: the parent derives it in build_submission_plan and
        # load_submission_plan rejects a mismatch.
        self.assertNotIn("name` 必须逐字等于", video_to_prompt)
        source = inspect.getsource(loop.build_submission_plan)
        self.assertIn('"name": f"{batch.name}-{job_id}"', source)
        self.assertIn(
            'expected_name = f"{batch.name}-{job_id}"',
            inspect.getsource(loop.load_submission_plan),
        )

    def test_dreamina_task_record_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            task_dir = (
                project
                / "outputs"
                / "video-replacements"
                / "batch-V001"
                / "tasks"
            )
            task_dir.mkdir(parents=True)
            (task_dir / "fingerprint.json").write_text(
                json.dumps(
                    {
                        "backend": "dreamina-cli",
                        "task_id": "dreamina-submit-1",
                        "gen_status": "querying",
                    }
                ),
                encoding="utf-8",
            )
            task_id = loop.recorded_task_id(project, "batch", "V001")
        self.assertEqual(task_id, "dreamina-submit-1")


if __name__ == "__main__":
    unittest.main()

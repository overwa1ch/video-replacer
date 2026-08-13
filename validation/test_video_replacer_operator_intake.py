from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "tools" / "video_batch_loop.py"
SPEC = importlib.util.spec_from_file_location("operator_intake_loop", MODULE_PATH)
loop = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = loop
SPEC.loader.exec_module(loop)


class OperatorIntakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.loop_root = self.base / "video-loop"
        self.project_root = self.base / "project-shell"
        loop.ensure_layout(self.loop_root)
        self.project_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_batch(
        self,
        requirement: str,
        *,
        privacy_mode: str = "none",
        schema_version: int = 2,
        video_suffix: str = ".mp4",
    ) -> Path:
        incoming = self.loop_root / "inbox" / "operator-batch"
        (incoming / "videos").mkdir(parents=True)
        (incoming / "replacements").mkdir()
        (incoming / "videos" / f"001-source{video_suffix}").write_bytes(
            b"original-video"
        )
        (incoming / "replacements" / "target.png").write_bytes(b"reference")
        (incoming / "requirements.txt").write_text(
            f"V001：{requirement}\n", encoding="utf-8"
        )
        job = {
            "id": "V001",
            "references": [
                {
                    "relative_path": "replacements/target.png",
                    "semantic_name": "目标车内饰",
                }
            ],
        }
        if schema_version == 2:
            job["privacy_mode"] = privacy_mode
        (incoming / "job-bindings.json").write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "batch_id": "operator-batch",
                    "jobs": [job],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (incoming / "PAUSE").touch()
        return loop.prepare_inbox_batch(incoming, self.loop_root)

    def test_agent_binding_map_controls_order_and_semantic_name(self) -> None:
        batch = self.make_batch("将车内饰替换为目标参考")
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        records = loop.select_job_reference_records(batch, "V001")
        self.assertEqual([item["filename"] for item in records], ["target.png"])
        self.assertEqual([item["semantic_name"] for item in records], ["目标车内饰"])

    def test_privacy_mode_is_explicit_and_natural_language_does_not_override_it(self) -> None:
        batch = self.make_batch("给源视频人物脸部打马赛克", privacy_mode="none")
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        self.assertEqual(loop.job_privacy_mode(batch, "V001"), "none")

        path = batch / "job-bindings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["jobs"][0]["privacy_mode"] = "mosaic_required"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(loop.job_privacy_mode(batch, "V001"), "mosaic_required")

    def test_new_schema_v1_batch_is_rejected(self) -> None:
        batch = self.make_batch("替换车内饰", schema_version=1)
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        with self.assertRaisesRegex(loop.LoopError, "schema_version 3"):
            loop.select_job_reference_records(batch, "V001")

    def test_new_schema_v2_batch_is_rejected_before_preparation(self) -> None:
        batch = self.make_batch("替换车内饰", schema_version=2)
        with self.assertRaisesRegex(loop.LoopError, "schema_version 3"):
            loop.inspect_streaming_flow(
                batch,
                self.loop_root,
                self.project_root,
                preparation_only=True,
            )

    def test_unfinished_schema_v2_flow_is_rejected_before_new_prompt_pipeline(self) -> None:
        batch = self.make_batch("替换车内饰", schema_version=2)
        loop.atomic_write_json(
            batch / "streaming-flow.json",
            {
                "schema_version": 1,
                "batch_id": batch.name,
                "job_bindings_sha256": loop.sha256_file(batch / "job-bindings.json"),
                "flow_fingerprint": "b" * 64,
            },
        )
        with self.assertRaisesRegex(loop.LoopError, "拒绝混用提示词管线"):
            loop.inspect_streaming_flow(
                batch,
                self.loop_root,
                self.project_root,
                preparation_only=True,
            )

    def test_existing_schema_v1_streaming_flow_keeps_legacy_identity_shape(self) -> None:
        batch = self.make_batch("替换车内饰", schema_version=1)
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        loop.atomic_write_json(
            batch / "streaming-flow.json",
            {
                "schema_version": 1,
                "batch_id": batch.name,
                "job_bindings_sha256": loop.sha256_file(batch / "job-bindings.json"),
                "flow_fingerprint": "a" * 64,
            },
        )
        index = loop.read_index(batch)
        reference_index = loop.verify_reference_index(batch)
        identity = loop._streaming_flow_identity(
            batch, self.project_root, index, reference_index
        )
        self.assertEqual(identity["prepared_sources"], [])
        self.assertNotIn("privacy_modes", identity)
        self.assertEqual(loop.job_privacy_mode(batch, "V001"), "none")

    def test_mosaic_runner_uses_workflow_tool_and_returns_its_output(self) -> None:
        batch = self.make_batch("替换车内饰", privacy_mode="mosaic_required")

        def fake_run(command, **_kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"workflow-mosaic")
            return SimpleNamespace(returncode=0, stdout='{"output":"ok"}\n')

        ffmpeg = self.base / "ffmpeg"
        ffmpeg.write_text("fixture", encoding="utf-8")
        with mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=ffmpeg
        ), mock.patch.dict(
            loop.os.environ,
            {
                "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross",
                "VIDEO_REPLACER_TOS_SECRET_KEY": "must-not-cross",
                "OPENAI_API_KEY": "must-not-cross",
            },
            clear=False,
        ), mock.patch.object(loop.subprocess, "run", side_effect=fake_run) as run:
            output = loop.prepare_mosaic_video(batch, self.project_root, "V001")
        self.assertEqual(output.name, "source-face-mosaic.mp4")
        self.assertEqual(output.read_bytes(), b"workflow-mosaic")
        command = run.call_args.args[0]
        self.assertEqual(Path(command[1]), loop.FACE_MOSAIC_SCRIPT)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", environment)
        self.assertNotIn("VIDEO_REPLACER_TOS_SECRET_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["VIDEO_REPLACER_PYTHON"], loop.sys.executable)
        self.assertEqual(environment["FFMPEG"], str(ffmpeg))

    def test_none_mode_uses_an_exact_copy_of_the_original(self) -> None:
        batch = self.make_batch("替换车内饰", privacy_mode="none")
        indexed = loop.read_index(batch)["jobs"][0]
        source = batch / indexed["relative_path"]
        active = loop.prepare_active_video(batch, self.project_root, "V001")
        self.assertEqual(active.name, loop.ACTIVE_VIDEO_COPY_FILENAME)
        self.assertEqual(active.read_bytes(), source.read_bytes())
        self.assertEqual(loop.sha256_file(active), indexed["sha256"])

    def test_mov_source_keeps_its_container_extension_in_the_active_copy(self) -> None:
        batch = self.make_batch(
            "替换车内饰", privacy_mode="none", video_suffix=".MOV"
        )
        indexed = loop.read_index(batch)["jobs"][0]
        source = batch / indexed["relative_path"]
        active = loop.prepare_active_video(batch, self.project_root, "V001")
        self.assertEqual(active.name, "source-active.mov")
        self.assertEqual(active.read_bytes(), source.read_bytes())
        self.assertEqual(loop.sha256_file(active), indexed["sha256"])

    def test_mosaic_failure_blocks_without_original_fallback(self) -> None:
        batch = self.make_batch("替换车内饰", privacy_mode="mosaic_required")
        ffmpeg = self.base / "ffmpeg"
        ffmpeg.write_text("fixture", encoding="utf-8")
        with mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=ffmpeg
        ), mock.patch.object(
            loop.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="failed"),
        ):
            with self.assertRaisesRegex(loop.LoopError, "自动打码失败"):
                loop.prepare_mosaic_video(batch, self.project_root, "V001")
        output = loop.job_output_dir(self.project_root, batch, "V001") / "source-face-mosaic.mp4"
        self.assertFalse(output.exists())

    def test_binding_map_rejects_filename_shaped_semantic_name(self) -> None:
        batch = self.make_batch("将车内饰替换为目标参考")
        path = batch / "job-bindings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["jobs"][0]["references"][0]["semantic_name"] = "target.png"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        with self.assertRaisesRegex(loop.LoopError, "不能是文件名"):
            loop.select_job_reference_records(batch, "V001")


if __name__ == "__main__":
    unittest.main()

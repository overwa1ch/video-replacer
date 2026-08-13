import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import video_batch_loop as loop  # noqa: E402


class VideoBatchNodeIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.loop_root = self.temp / "video-loop"
        self.project_root = self.temp / "project"
        loop.ensure_layout(self.loop_root)

    def tearDown(self):
        self.temporary.cleanup()

    def _fake_frame_extractor(
        self, _source_video: Path, frame_root: Path, _analysis_tool: Path
    ):
        """Stage deterministic sampled evidence without requiring real media."""

        frame_root.mkdir(parents=True, exist_ok=False)
        sampled_frames = []
        for number, timestamp in ((1, 0.0), (2, 0.75)):
            path = frame_root / f"frame-{number:03d}.jpg"
            path.write_bytes(f"sampled-frame-{number}".encode("utf-8"))
            sampled_frames.append(
                {
                    "image": path.relative_to(frame_root.parents[1]).as_posix(),
                    "timestamp_seconds": timestamp,
                    "size_bytes": path.stat().st_size,
                    "sha256": loop.sha256_file(path),
                }
            )
        return {
            "video_metadata": {
                "duration_seconds": 2.0,
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
            },
            "sampled_frames": sampled_frames,
            "sampling_policy": {
                "method": "uniform_timestamp_interval",
                "interval_seconds": loop.VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS,
                "minimum_interval_seconds": (
                    loop.VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS
                ),
                "max_frames": loop.VIDEO_PROMPT_MAX_FRAMES,
            },
        }

    def _prepared_batch(self) -> Path:
        inbox_batch = self.loop_root / "inbox" / "node-isolation"
        videos = inbox_batch / "videos"
        replacements = inbox_batch / "replacements"
        videos.mkdir(parents=True)
        replacements.mkdir(parents=True)
        (videos / "current.mp4").write_bytes(b"current-job-video")
        (videos / "other-job-secret.mp4").write_bytes(b"other-job-video")
        (replacements / "escape-interior.png").write_bytes(b"reference")
        (replacements / "other-job-interior.png").write_bytes(
            b"other-reference-secret"
        )

        batch = loop.prepare_inbox_batch(inbox_batch, self.loop_root)
        loop.atomic_write_json(
            batch / "reference-index.json", loop.build_reference_index(batch)
        )
        loop.atomic_write_json(
            batch / "job-bindings.json",
            {
                "schema_version": 2,
                "batch_id": batch.name,
                "jobs": [
                    {
                        "id": "V001",
                        "privacy_mode": "none",
                        "references": [
                            {
                                "relative_path": "replacements/escape-interior.png",
                                "semantic_name": "车内饰",
                            }
                        ],
                    },
                    {
                        "id": "V002",
                        "privacy_mode": "none",
                        "references": [
                            {
                                "relative_path": "replacements/other-job-interior.png",
                                "semantic_name": "另一套车内饰",
                            }
                        ],
                    },
                ],
            },
        )
        loop.atomic_write_json(
            batch / "reference-classification.json",
            {
                "schema_version": 1,
                "references": [
                    {"id": "R001", "role": "车内饰"},
                    {"id": "R002", "role": "车内饰"},
                ],
                "role_bindings": {
                    "V002": "R002",
                    "OTHER_JOB_BINDING_SENTINEL": "R002",
                },
            },
        )
        (batch / "requirements.txt").write_text(
            "默认：保持原时长\n"
            "V001：替换 Ford Escape 内饰，绑定 replacements/escape-interior.png\n"
            "V002：OTHER_JOB_REQUIREMENT_SENTINEL\n",
            encoding="utf-8",
        )
        loop.atomic_write_json(
            batch / "later-stage-secret.json",
            {
                "executor": "EXECUTOR_SENTINEL",
                "task_id": "TASK_SENTINEL",
                "output_path": "OUTPUT_SENTINEL",
                "review": "REVIEW_SENTINEL",
            },
        )
        return batch

    def test_node_contracts_are_workflow_owned_not_skill_files(self):
        contract_root = loop.NODE_CONTRACT_ROOT.resolve()
        self.assertNotIn(".agents", contract_root.parts)
        self.assertNotIn("skills", contract_root.parts)
        paths = sorted(path.name for path in contract_root.iterdir() if path.is_file())
        self.assertEqual(paths, ["video-to-prompt.md"])
        path = loop.node_contract_path("video-to-prompt.md")
        self.assertEqual(path.parent, contract_root)
        self.assertNotIn(".agents", path.parts)
        self.assertNotIn("skills", path.parts)

        contract = loop.node_contract_text("video-to-prompt.md")
        self.assertIn("parent's timestamped visual samples", contract)
        self.assertIn("parent-bound reference images", contract)
        self.assertIn("exact order declared by `attachment_order`", contract)
        self.assertIn("Do not call a tool, read a path, write a file", contract)
        self.assertIn("no intermediate analysis artifact", contract)
        self.assertIn("素材绑定：@视频1=原视频；@图片1=", contract)
        self.assertIn("镜头N（0.0-2.5s）", contract)
        self.assertIn("unambiguous key action observed", contract)
        self.assertIn("Put the complete", contract)
        self.assertIn("`jobs[0].prompt`", contract)
        self.assertIn("parent alone persists", contract)
        for obsolete in (
            "source-analysis.md",
            "reference-analysis.md",
            "prompt-writer.md",
        ):
            with self.assertRaises(loop.LoopError):
                loop.node_contract_path(obsolete)

    def test_minimal_node_schema_has_no_parent_execution_fields(self):
        schema_path = TOOLS_ROOT / "video_batch_node_result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        serialized = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("task_id", serialized)
        self.assertNotIn("output_path", serialized)
        job_schema = schema["properties"]["jobs"]["items"]
        self.assertFalse(job_schema["additionalProperties"])
        self.assertEqual(
            set(job_schema["properties"]), {"id", "status", "blocker", "prompt"}
        )

    def test_node_batch_status_must_match_its_only_job_status(self):
        with self.assertRaisesRegex(
            loop.LoopError, "batch_status 必须等于唯一 Job status"
        ):
            loop.validate_node_result(
                {
                    "batch_status": "BLOCKED",
                    "summary": "inconsistent fixture",
                    "jobs": [
                        {
                            "id": "V001",
                            "status": "COMPLETE",
                            "blocker": None,
                            "prompt": "valid prompt fixture",
                        }
                    ],
                },
                "V001",
            )

    def test_stable_codex_home_rejects_instruction_sources(self):
        destination = self.temp / "codex-node-home"
        destination.mkdir(mode=0o700)
        auth = destination / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": "e30.e30.c2ln",
                        "access_token": "fixture-access-token-never-valid",
                        "refresh_token": "fixture-refresh-token-never-valid",
                        "account_id": "fixture-account-id-never-valid",
                    },
                    "last_refresh": "2026-08-13T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            auth.chmod(0o600)
        with mock.patch(
            "codex_node_home._temporary_roots", return_value=set()
        ), mock.patch(
            "codex_node_home.path_contains_link_like", return_value=False
        ), mock.patch(
            "codex_node_home._windows_verify_private_acl", return_value=None
        ):
            self.assertEqual(
                loop.validate_node_home(
                    destination,
                    repo_root=self.project_root,
                    require_auth=True,
                ),
                destination.resolve(),
            )
        (destination / "AGENTS.md").write_text(
            "GLOBAL_RULE_SENTINEL\n", encoding="utf-8"
        )
        with mock.patch(
            "codex_node_home._temporary_roots", return_value=set()
        ), mock.patch(
            "codex_node_home.path_contains_link_like", return_value=False
        ), mock.patch(
            "codex_node_home._windows_verify_private_acl", return_value=None
        ), self.assertRaisesRegex(loop.CodexNodeHomeError, "forbidden"):
            loop.validate_node_home(
                destination,
                repo_root=self.project_root,
                require_auth=True,
            )

    def test_video_to_prompt_input_contains_only_current_job_evidence(self):
        batch = self._prepared_batch()
        references = loop.select_job_reference_records(batch, "V001")
        workspace = self.temp / "video-to-prompt-workspace"
        analysis_tool = self.temp / "ffmpeg-fixture"
        analysis_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        analysis_tool.chmod(0o755)
        input_path = loop.write_video_to_prompt_node_input(
            batch,
            self.project_root,
            "V001",
            workspace,
            references,
            analysis_tool=analysis_tool,
            frame_extractor=self._fake_frame_extractor,
        )
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["job_id"], "V001")
        self.assertEqual(payload["source_video"]["filename"], "current.mp4")
        self.assertEqual(payload["source_video"]["size_bytes"], 17)
        self.assertEqual(
            payload["source_video"]["sha256"],
            loop.sha256_file(batch / "videos" / "current.mp4"),
        )
        self.assertEqual(payload["source_video"]["duration_seconds"], 2.0)
        self.assertEqual(len(payload["sampled_frames"]), 2)
        self.assertEqual(
            payload["sampling_policy"]["method"], "uniform_timestamp_interval"
        )
        self.assertEqual(payload["sampling_policy"]["interval_seconds"], 0.75)
        self.assertEqual(
            payload["sampling_policy"]["minimum_interval_seconds"], 0.75
        )
        self.assertNotIn("scene_threshold", payload["sampling_policy"])
        for frame in payload["sampled_frames"]:
            frame_path = workspace / frame["image"]
            self.assertTrue(frame_path.is_file())
            self.assertFalse(frame_path.is_symlink())
            self.assertFalse(Path(frame["image"]).is_absolute())
            self.assertEqual(loop.sha256_file(frame_path), frame["sha256"])
        self.assertEqual(len(payload["references"]), 1)
        staged_reference = payload["references"][0]
        self.assertEqual(staged_reference["reference_id"], "R001")
        self.assertEqual(staged_reference["semantic_name"], "车内饰")
        self.assertFalse(Path(staged_reference["image"]).is_absolute())
        self.assertEqual(
            (workspace / staged_reference["image"]).read_bytes(), b"reference"
        )
        self.assertEqual(
            loop.sha256_file(workspace / staged_reference["image"]),
            staged_reference["sha256"],
        )
        self.assertEqual(
            payload["requirements"],
            [
                "默认：保持原时长",
                "V001：替换 Ford Escape 内饰，绑定 replacements/escape-interior.png",
            ],
        )
        self.assertEqual(
            payload["material_bindings"],
            [
                {"handle": "@视频1", "semantic_name": "原视频"},
                {"handle": "@图片1", "semantic_name": "车内饰"},
            ],
        )
        self.assertEqual(
            payload["attachment_order"],
            [
                "inputs/frames/frame-001.jpg",
                "inputs/frames/frame-002.jpg",
                "inputs/references/R001.png",
            ],
        )
        self.assertNotIn("analysis_tools", payload)
        self.assertEqual(
            payload["output_contract"], {"result_field": "jobs[0].prompt"}
        )
        self.assertNotIn("jobs", payload)
        self.assertNotIn("source_analysis", payload)
        self.assertNotIn("reference_analysis", payload)
        for leaked_value in (
            "V002",
            "other-job-secret.mp4",
            "other-job-interior.png",
            "OTHER_JOB_REQUIREMENT_SENTINEL",
            "OTHER_JOB_BINDING_SENTINEL",
            "EXECUTOR_SENTINEL",
            "TASK_SENTINEL",
            "OUTPUT_SENTINEL",
            "REVIEW_SENTINEL",
        ):
            self.assertNotIn(leaked_value, serialized)
        for parent_owned_key in (
            "executor",
            "task_id",
            "output_path",
            "submission_plan",
        ):
            self.assertNotIn(parent_owned_key, serialized)
        self.assertNotIn(str(batch.resolve()), serialized)
        self.assertNotIn(str(self.project_root.resolve()), serialized)
        self.assertFalse((workspace / "inputs" / "source").exists())
        self.assertFalse((workspace / "tools").exists())
        self.assertFalse((workspace / "inputs" / "references" / "R002.png").exists())

    def test_parent_blocks_duplicate_material_names_for_name_only_body(self):
        batch = self._prepared_batch()
        workspace = self.temp / "duplicate-material-name-workspace"
        analysis_tool = self.temp / "ffmpeg-duplicate-name-fixture"
        analysis_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        analysis_tool.chmod(0o755)
        original = loop.select_job_reference_records(batch, "V001")[0]
        duplicate_a = dict(original, semantic_name="重复名称")
        duplicate_b = dict(original, id="R002", semantic_name="重复名称")
        with self.assertRaisesRegex(loop.LoopError, "素材名称必须非空且互不重复"):
            loop.write_video_to_prompt_node_input(
                batch,
                self.project_root,
                "V001",
                workspace,
                [duplicate_a, duplicate_b],
                analysis_tool=analysis_tool,
                frame_extractor=self._fake_frame_extractor,
            )

    def test_parent_uses_classified_natural_name_in_legacy_fallback(self):
        self.assertEqual(
            loop._reference_semantic_name(
                {"id": "R001", "filename": "interior-2026-ford-escape-platinum.png"},
                {"label_zh": "Ford Escape 车内饰"},
                set(),
            ),
            "Ford Escape 车内饰",
        )

    def test_parent_humanizes_unknown_reference_filename_in_legacy_fallback(self):
        self.assertEqual(
            loop._reference_semantic_name(
                {"id": "R001", "filename": "target-ford-f150-cabin.png"},
                {},
                set(),
            ),
            "target ford f150 cabin",
        )

    def test_explicit_binding_ignores_legacy_role_ambiguity(self):
        batch = self._prepared_batch()
        (batch / "requirements.txt").write_text(
            "V001：替换车内饰\nV002：跳过\n", encoding="utf-8"
        )
        loop.atomic_write_json(
            batch / "reference-classification.json",
            {"role_bindings": {"车内饰": ["R001", "R002"]}},
        )
        self.assertEqual(
            [item["id"] for item in loop.select_job_reference_records(batch, "V001")],
            ["R001"],
        )

    def test_explicit_binding_ignores_reference_names_in_requirement_text(self):
        batch = self._prepared_batch()
        (batch / "requirements.txt").write_text(
            "V001-V002：分别绑定 R001 与 R002\n", encoding="utf-8"
        )
        self.assertEqual(
            [item["id"] for item in loop.select_job_reference_records(batch, "V001")],
            ["R001"],
        )

    def test_parent_keeps_group_requirement_without_reference_binding(self):
        batch = self._prepared_batch()
        (batch / "requirements.txt").write_text(
            "V001-V002：保持原时长\n"
            "V001：绑定 replacements/escape-interior.png\n",
            encoding="utf-8",
        )
        self.assertEqual(
            [item["id"] for item in loop.select_job_reference_records(batch, "V001")],
            ["R001"],
        )

    def test_parent_explicit_filename_wins_without_generic_multi_binding(self):
        batch = self._prepared_batch()
        loop.atomic_write_json(
            batch / "reference-classification.json",
            {"role_bindings": {"内饰": ["R001", "R002"]}},
        )
        self.assertEqual(
            [item["id"] for item in loop.select_job_reference_records(batch, "V001")],
            ["R001"],
        )

    def test_parent_only_writes_its_own_selected_reference_binding(self):
        batch = self._prepared_batch()
        references = loop.select_job_reference_records(batch, "V001")
        loop._write_parent_reference_binding(
            batch, self.project_root, "V001", references
        )
        binding = json.loads(
            (
                loop.job_output_dir(self.project_root, batch, "V001")
                / loop.REFERENCE_BINDING_FILENAME
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(binding, ["R001"])

    def test_codex_command_disables_context_features_and_uses_external_cwd(self):
        batch = self.loop_root / "ready" / "node-isolation"
        batch.mkdir(parents=True)
        isolated_cwd = self.temp / "isolated-node-cwd"
        isolated_cwd.mkdir()
        command = loop.build_codex_command(
            "codex-test.exe",
            batch,
            self.project_root,
            TOOLS_ROOT / "video_batch_node_result.schema.json",
            batch / "node-result.json",
            node_workspace=isolated_cwd,
        )

        command_cwd = Path(command[command.index("--cd") + 1]).resolve()
        self.assertEqual(command_cwd, isolated_cwd.resolve())
        with self.assertRaises(ValueError):
            command_cwd.relative_to(self.project_root.resolve())
        disabled = {
            command[index + 1]
            for index, item in enumerate(command[:-1])
            if item == "--disable"
        }
        self.assertTrue(
            {
                "skill_search",
                "plugins",
                "hooks",
                "apps",
                "multi_agent",
                "shell_tool",
                "unified_exec",
                "shell_snapshot",
                "computer_use",
                "use_agent_identity",
            }.issubset(disabled)
        )
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("notify=[]", command)
        self.assertIn("skills.include_instructions=false", command)
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--add-dir", command)
        permission_config = next(
            item
            for item in command
            if item.startswith("permissions.node_isolated.filesystem=")
        )
        self.assertIn('"."="read"', permission_config)
        self.assertNotIn('="write"', permission_config)
        self.assertIn(
            "permissions.node_isolated.network.enabled=false", command
        )
        with self.assertRaises(loop.LoopError):
            loop.build_codex_command(
                "codex-test.exe",
                batch,
                self.project_root,
                TOOLS_ROOT / "video_batch_node_result.schema.json",
                batch / "node-result.json",
                node_workspace=isolated_cwd,
                writable_workspace_paths=[Path("delivery")],
            )

    def test_codex_command_attaches_only_workspace_images(self):
        batch = self.loop_root / "ready" / "node-image-attachment"
        batch.mkdir(parents=True)
        workspace = self.temp / "node-image-workspace"
        image_path = workspace / "inputs" / "references" / "R001.png"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image-fixture")
        command = loop.build_codex_command(
            "codex-test.exe",
            batch,
            self.project_root,
            TOOLS_ROOT / "video_batch_node_result.schema.json",
            batch / "node-result.json",
            node_workspace=workspace,
            image_paths=[image_path],
        )
        image_index = command.index("--image")
        self.assertEqual(Path(command[image_index + 1]), image_path.resolve())

        outside = self.temp / "outside.png"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(loop.LoopError, "节点图片越过工作区"):
            loop.build_codex_command(
                "codex-test.exe",
                batch,
                self.project_root,
                TOOLS_ROOT / "video_batch_node_result.schema.json",
                batch / "node-result.json",
                node_workspace=workspace,
                image_paths=[outside],
            )

    def test_parent_promotes_structured_prompt_result_to_prompt_txt(self):
        batch = self._prepared_batch()
        prompt = (
            "素材绑定：@视频1=原视频；@图片1=车内饰。\n\n"
            "镜头1（0.0-1.0s）\n替换为车内饰。\n"
        )

        loop.promote_prompt_result(batch, self.project_root, "V001", prompt)

        output_dir = loop.job_output_dir(self.project_root, batch, "V001")
        self.assertEqual(
            sorted(path.name for path in output_dir.iterdir()), ["prompt.txt"]
        )
        self.assertFalse((output_dir / "source-analysis.json").exists())
        self.assertFalse((output_dir / "reference-analysis.json").exists())
        self.assertEqual((output_dir / "prompt.txt").read_text(), prompt)

    def test_parent_rejects_empty_or_non_string_prompt_result(self):
        batch = self._prepared_batch()
        for invalid in (None, "", "   \n"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                loop.LoopError, "提示词交付为空"
            ):
                loop.promote_prompt_result(
                    batch, self.project_root, "V001", invalid
                )

    def test_video_to_prompt_runtime_stages_samples_for_read_only_model_turn(self):
        batch = self._prepared_batch()
        references = loop.select_job_reference_records(batch, "V001")
        analysis_tool = self.temp / "ffmpeg-runtime-fixture"
        analysis_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        analysis_tool.chmod(0o755)
        captured = {}
        real_writer = loop.write_video_to_prompt_node_input

        def write_with_sampled_fixture(*args, **kwargs):
            kwargs["frame_extractor"] = self._fake_frame_extractor
            return real_writer(*args, **kwargs)

        def fake_node(_batch, _loop_root, _project_root, _prompt, **kwargs):
            workspace = self.temp / "video-to-prompt-runtime-workspace"
            workspace.mkdir()
            kwargs["node_workspace_setup"](workspace)
            captured.update(kwargs)
            captured["workspace"] = workspace
            captured["prompt"] = _prompt(workspace)
            captured["input"] = json.loads(
                (workspace / "node-input.json").read_text(encoding="utf-8")
            )
            return {
                "batch_status": "BLOCKED",
                "summary": "fixture block",
                "jobs": [
                    {
                        "id": "V001",
                        "status": "BLOCKED",
                        "blocker": "fixture",
                        "prompt": None,
                    }
                ],
            }

        with mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=analysis_tool
        ), mock.patch.object(
            loop, "write_video_to_prompt_node_input", side_effect=write_with_sampled_fixture
        ), mock.patch.object(loop, "_run_codex_exec", side_effect=fake_node):
            result = loop.run_video_to_prompt_node(
                batch,
                self.loop_root,
                self.project_root,
                "V001",
                references,
            )

        self.assertEqual(result["jobs"][0]["status"], "BLOCKED")
        self.assertEqual(captured["stage"], "video-to-prompt")
        self.assertNotIn("writable_workspace_paths", captured)
        self.assertEqual(
            captured["attached_workspace_images"],
            [
                Path("inputs/frames/frame-001.jpg"),
                Path("inputs/frames/frame-002.jpg"),
                Path("inputs/references/R001.png"),
            ],
        )
        self.assertEqual(captured["input"]["source_video"]["filename"], "current.mp4")
        self.assertEqual(len(captured["input"]["sampled_frames"]), 2)
        self.assertEqual(captured["input"]["references"][0]["reference_id"], "R001")
        self.assertEqual(
            captured["input"]["output_contract"],
            {"result_field": "jobs[0].prompt"},
        )
        self.assertIn("不要调用任何工具", captured["prompt"])
        self.assertIn('"timestamp_seconds": 0.75', captured["prompt"])
        self.assertFalse((captured["workspace"] / "inputs" / "source").exists())
        self.assertFalse((captured["workspace"] / "tools").exists())
        self.assertFalse((captured["workspace"] / "scratch").exists())
        self.assertFalse((captured["workspace"] / "delivery").exists())
        self.assertNotIn("trusted_readable_paths", captured)

if __name__ == "__main__":
    unittest.main()

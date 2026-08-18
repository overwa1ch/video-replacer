import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(__file__).resolve().parent
TOOLS_ROOT = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

SCHEMA_PATH = TOOLS_ROOT / "video_batch_loop_result.schema.json"
if not SCHEMA_PATH.is_file():
    SCHEMA_PATH = SCRIPT_ROOT / "video_batch_loop_result.schema.json"

import video_batch_loop as loop  # noqa: E402


class VideoBatchLoopTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.root = self.temp / "video-loop"
        self.project = self.temp / "project"
        self.executor_path = self.project / "tools" / "dreamina_video.py"
        self.executor_path.parent.mkdir(parents=True)
        self.executor_path.write_text("# trusted test fixture\n", encoding="utf-8")
        self.executor = loop.ExecutorSpec(
            self.executor_path.resolve(), "dreamina_cli_local_upload"
        )
        loop.ensure_layout(self.root)
        self.now = 1_000.0

    def tearDown(self):
        self.temporary.cleanup()

    def observer(self):
        return loop.ObservationTracker(self.root, clock=lambda: self.now)

    @staticmethod
    def fake_source_evidence_extractor(_source, frame_root, _analysis_tool):
        frame_root.mkdir(parents=True, exist_ok=False)
        frame = frame_root / "frame-001.jpg"
        frame.write_bytes(b"deterministic-source-evidence")
        return {
            "video_metadata": {
                "duration_seconds": 1.0,
                "width": 1280,
                "height": 720,
                "fps": 30.0,
            },
            "sampled_frames": [
                {
                    "image": frame.relative_to(frame_root.parents[1]).as_posix(),
                    "timestamp_seconds": 0.0,
                    "size_bytes": frame.stat().st_size,
                    "sha256": loop.sha256_file(frame),
                }
            ],
            "sampling_policy": {
                "method": "uniform_timestamp_interval",
                "interval_seconds": loop.VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS,
                "minimum_interval_seconds": loop.VIDEO_PROMPT_MIN_FRAME_INTERVAL_SECONDS,
                "max_frames": loop.VIDEO_PROMPT_MAX_FRAMES,
            },
        }

    def freeze_source_evidence(self, batch):
        return loop.ensure_source_evidence_index(
            batch,
            loop.verify_batch_index(batch),
            frame_extractor=self.fake_source_evidence_extractor,
        )

    def codex_node_home(self, state_dir):
        home = state_dir / loop.NODE_HOME_DIRECTORY
        home.mkdir(parents=True, exist_ok=True)
        auth = home / "auth.json"
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
            home.chmod(0o700)
            auth.chmod(0o600)
        return home

    def test_batch_names_are_portable_to_windows(self):
        for name in ("batch-01", "batch.name", "A_1"):
            with self.subTest(name=name):
                self.assertTrue(loop.safe_batch_name(name))
        for name in ("CON", "con.txt", "PRN", "NUL", "COM1", "LPT9", "batch."):
            with self.subTest(name=name):
                self.assertFalse(loop.safe_batch_name(name))

    @unittest.skipUnless(os.name == "nt", "native Windows junction contract")
    def test_windows_junction_is_rejected_without_touching_target(self):
        batch = self.root / "inbox" / "batch-junction"
        (batch / "videos").mkdir(parents=True)
        external = self.temp / "external-material"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        junction = batch / "replacements"
        result = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(junction),
                str(external),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        with self.assertRaisesRegex(loop.LoopError, "junction"):
            loop.reject_symlinks(batch)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def create_inbox_batch(self, name="batch-a", count=2):
        batch = self.root / "inbox" / name
        videos = batch / "videos"
        videos.mkdir(parents=True)
        if count == 2:
            (videos / "10.mp4").write_bytes(b"video-ten")
            (videos / "2.mp4").write_bytes(b"video-two")
        else:
            for number in range(1, count + 1):
                (videos / f"{number:02d}.mp4").write_bytes(
                    f"video-{number}".encode("utf-8")
                )
        return batch

    def prepare(self, name="batch-a", count=2):
        batch = loop.prepare_inbox_batch(
            self.create_inbox_batch(name, count), self.root
        )
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
                        "id": job["id"],
                        "privacy_mode": "none",
                        "references": [],
                    }
                    for job in loop.read_index(batch)["jobs"]
                ],
            },
        )
        return batch

    def write_valid_plan(
        self,
        batch,
        job_id="V001",
        images=None,
        privacy=None,
        project_root=None,
        executor=None,
    ):
        project_root = project_root or self.project
        executor = executor or self.executor
        images = list(images or [])
        binding_payload = json.loads(
            (batch / "job-bindings.json").read_text(encoding="utf-8")
        )
        binding_job = next(
            item for item in binding_payload["jobs"] if item["id"] == job_id
        )
        if images and not binding_job["references"]:
            binding_job["references"] = [
                {
                    "relative_path": (
                        Path(value)
                        .resolve()
                        .relative_to(batch.resolve())
                        .as_posix()
                    ),
                    "semantic_name": f"目标素材角色{index}",
                }
                for index, value in enumerate(images, start=1)
            ]
            loop.atomic_write_json(batch / "job-bindings.json", binding_payload)
        semantic_names = [
            str(item["semantic_name"]) for item in binding_job["references"]
        ]
        if not loop.job_requirement_lines(batch, job_id):
            with (batch / "requirements.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"{job_id}：执行测试替换并保持原动作。\n")
        if semantic_names:
            applicable = "\n".join(loop.job_requirement_lines(batch, job_id))
            missing_handles = [
                f"@图片{index}"
                for index in range(1, len(semantic_names) + 1)
                if f"@图片{index}" not in applicable
            ]
            if missing_handles:
                with (batch / "requirements.txt").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(
                        f"{job_id}：分别使用{'、'.join(missing_handles)}"
                        "完成各自指定替换。\n"
                    )
        output = (
            project_root
            / "outputs"
            / "video-replacements"
            / f"{batch.name}-{job_id}"
        )
        output.mkdir(parents=True, exist_ok=True)
        mode = loop.job_privacy_mode(batch, job_id)
        source = output / (
            "source-face-mosaic.mp4" if mode == "mosaic_required" else "source.mp4"
        )
        prompt = output / "prompt.txt"
        preflight_path = output / "preflight.json"
        indexed_job = next(
            job for job in loop.read_index(batch)["jobs"] if job["id"] == job_id
        )
        indexed_source = batch / indexed_job["relative_path"]
        privacy_record = dict(
            privacy
            or {
                "status": "workflow-selected-input",
                "remote_upload_authorized": True,
                "paid_task_authorized": True,
            }
        )
        if mode == "mosaic_required":
            source.write_bytes(b"workflow-mosaic")
        else:
            source.write_bytes(indexed_source.read_bytes())
        if images:
            bindings = "；".join(
                f"@图片{index}={name}"
                for index, name in enumerate(semantic_names, start=1)
            )
            node_prompt = (
                f"素材绑定：@视频1=原视频；{bindings}。\n"
                "镜头1（0.0-1.0s）\n"
                f"将指定对象分别替换为{'、'.join(semantic_names)}。\n"
            )
        else:
            node_prompt = (
                "素材绑定：@视频1=原视频。\n"
                "镜头1（0.0-1.0s）\n"
                "执行指定替换。\n"
            )
        prompt.write_text(
            loop.compose_execution_prompt(
                batch,
                job_id,
                binding_job["references"],
                node_prompt,
            )
            + "\n",
            encoding="utf-8",
        )
        preflight = {
            "preflight_passed": True,
            "transport": executor.transport,
            "active_video": {
                "path": str(source.resolve()),
                "sha256": loop.sha256_file(source),
            },
            "input_bindings": loop._expected_input_bindings(
                prompt, [Path(value) for value in images]
            ),
            "privacy": privacy_record,
        }
        loop.atomic_write_json(preflight_path, preflight)
        plan = {
            "schema_version": 1,
            "batch_id": batch.name,
            "job_id": job_id,
            "video": str(source.resolve()),
            "preflight_manifest": str(preflight_path.resolve()),
            "prompt_file": str(prompt.resolve()),
            "images": [str(Path(value).resolve()) for value in images],
            "name": f"{batch.name}-{job_id}",
            "output_dir": str(output.resolve()),
        }
        loop.atomic_write_json(output / "submission-plan.json", plan)
        return plan, preflight

    def test_prepare_generates_natural_ids_and_private_material(self):
        destination = loop.prepare_inbox_batch(self.create_inbox_batch(), self.root)
        index = json.loads((destination / "batch-index.json").read_text())
        self.assertEqual(
            [(job["id"], job["filename"]) for job in index["jobs"]],
            [("V001", "2.mp4"), ("V002", "10.mp4")],
        )
        requirements = (destination / "requirements.txt").read_text()
        self.assertIn("V001：  # 2.mp4", requirements)
        self.assertIn("无需移动文件夹", requirements)
        self.assertIn("videos/2.mp4", (destination / "batch-preview.html").read_text())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((destination / "videos").stat().st_mode), 0o700
            )
            for video in (destination / "videos").iterdir():
                self.assertEqual(stat.S_IMODE(video.stat().st_mode), 0o600)
            for generated in (
                destination / "batch-index.json",
                destination / "requirements.txt",
                destination / "batch-preview.html",
                destination / "loop-state.json",
            ):
                self.assertEqual(stat.S_IMODE(generated.stat().st_mode), 0o600)

    def test_loose_files_require_two_watcher_observations_even_with_old_mtime(self):
        inbox = self.root / "inbox"
        video = inbox / "2.MP4"
        video.write_bytes(b"video")
        os.utime(video, (1, 1))
        observer = self.observer()
        first = observer.begin_scan()
        self.assertIsNone(loop.collect_loose_inbox_videos(self.root, 0, observer, first))
        second = observer.begin_scan()
        batch = loop.collect_loose_inbox_videos(self.root, 0, observer, second)
        self.assertIsNotNone(batch)
        self.assertTrue((batch / "videos" / "2.MP4").is_file())

    def test_complete_batch_folder_requires_two_scans(self):
        batch = self.create_inbox_batch()
        for path in batch.rglob("*"):
            if path.is_file():
                os.utime(path, (1, 1))
        observer = self.observer()
        loop.scan_once(self.root, self.project, 0, False, observer=observer)
        self.assertTrue(batch.exists())
        loop.scan_once(self.root, self.project, 0, False, observer=observer)
        self.assertTrue((self.root / "needs-input" / batch.name).exists())

    def test_reference_index_preserves_names_and_rejects_duplicate_basenames(self):
        destination = loop.prepare_inbox_batch(self.create_inbox_batch(), self.root)
        refs = destination / "replacements"
        (refs / "客户 B.png").write_bytes(b"b")
        (refs / "客户 A.jpg").write_bytes(b"a")
        index = loop.build_reference_index(destination)
        self.assertEqual(
            [item["filename"] for item in index["references"]],
            ["客户 A.jpg", "客户 B.png"],
        )
        (refs / "nested").mkdir()
        (refs / "nested" / "客户 A.jpg").write_bytes(b"duplicate")
        with self.assertRaises(loop.LoopError):
            loop.build_reference_index(destination)

    def test_check_validates_unfrozen_reference_index_in_memory_without_writing(self):
        batch = self.prepare("check-before-reference-index")
        self.upgrade_bindings_to_schema_v3(batch)
        (batch / "requirements.txt").write_text(
            "默认：保持原动作并替换产品\n", encoding="utf-8"
        )
        (batch / "reference-index.json").unlink()

        returncode = loop.main(
            [
                "--root", str(self.root),
                "--project-root", str(self.project),
                "--min-free-gib", "0",
                "check", batch.name,
            ]
        )

        self.assertEqual(returncode, 0)
        self.assertFalse((batch / "reference-index.json").exists())

    def test_check_rejects_new_schema_v2_without_freezing_indexes(self):
        batch = self.prepare("check-rejects-schema-v2")
        (batch / "requirements.txt").write_text(
            "默认：保持原动作并替换产品\n", encoding="utf-8"
        )
        (batch / "reference-index.json").unlink()

        returncode = loop.main(
            [
                "--root", str(self.root),
                "--project-root", str(self.project),
                "--min-free-gib", "0",
                "check", batch.name,
            ]
        )

        self.assertEqual(returncode, 1)
        self.assertFalse((batch / "reference-index.json").exists())
        self.assertFalse(
            (batch / loop.SOURCE_EVIDENCE_INDEX_FILENAME).exists()
        )

    def test_failed_new_flow_validation_does_not_freeze_any_index(self):
        batch = self.prepare("inspect-rejects-schema-v2")
        (batch / "requirements.txt").write_text(
            "默认：保持原动作并替换产品\n", encoding="utf-8"
        )
        (batch / "reference-index.json").unlink()
        (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")

        with self.assertRaisesRegex(loop.LoopError, "schema_version 3"):
            loop.inspect_streaming_flow(
                batch,
                self.root,
                self.project,
                preparation_only=True,
                minimum_free_bytes=0,
            )

        self.assertFalse((batch / "reference-index.json").exists())
        self.assertFalse(
            (batch / loop.SOURCE_EVIDENCE_INDEX_FILENAME).exists()
        )

    def test_partial_source_cache_does_not_strand_unfrozen_reference_index(self):
        batch = self.prepare("partial-source-cache-is-restartable")
        (batch / "reference-index.json").unlink()
        cache = (
            batch
            / "streaming-results"
            / "source-evidence"
            / ("a" * 64)
        )
        cache.mkdir(parents=True)
        loop.atomic_write_json(cache / "manifest.json", {"partial": True})

        self.assertFalse(loop._has_frozen_streaming_artifacts(batch))
        provisional = loop.reference_index_for_validation(
            batch, persist_if_unfrozen=False
        )
        self.assertEqual(provisional["batch_id"], batch.name)
        self.assertFalse((batch / "reference-index.json").exists())

    def test_frozen_flow_missing_reference_index_is_not_silently_rebuilt(self):
        batch, _flow = self.streaming_batch()
        (batch / "reference-index.json").unlink()

        with self.assertRaisesRegex(loop.LoopError, "冻结.*reference-index"):
            loop.inspect_streaming_flow(
                batch,
                self.root,
                self.project,
                minimum_free_bytes=0,
            )
        self.assertFalse((batch / "reference-index.json").exists())

    def test_requirements_coverage_default_missing_and_unknown(self):
        destination = self.prepare()
        requirements = destination / "requirements.txt"
        requirements.write_text("默认：替换产品\nV001：同时替换人物\n", encoding="utf-8")
        errors, coverage = loop.validate_requirements(destination)
        self.assertEqual(errors, [])
        self.assertEqual(coverage["covered"], ["V001", "V002"])
        requirements.write_text("V001：替换人物\nV099：替换产品\n", encoding="utf-8")
        errors, coverage = loop.validate_requirements(destination)
        self.assertTrue(any("V099" in error for error in errors))
        self.assertEqual(coverage["missing"], ["V002"])

    def test_auto_ready_requires_a_requirements_save_after_latest_input_observation(self):
        observer = self.observer()
        observed_at = observer.begin_scan()
        destination = loop.prepare_inbox_batch(
            self.create_inbox_batch(),
            self.root,
            observer=observer,
            observed_at=observed_at,
        )
        requirements = destination / "requirements.txt"
        requirements.write_text("V001：替换人物\nV002：替换人物\n", encoding="utf-8")
        (destination / "replacements" / "face.png").write_bytes(b"face")
        for _ in range(2):
            observed_at = observer.begin_scan()
            self.assertEqual(
                loop.promote_auto_ready_batches(
                    self.root, 0, observer, observed_at
                ),
                [],
            )
        requirements.write_text(
            "V001：替换人物\nV002：替换人物\n# 输入观察后再保存\n",
            encoding="utf-8",
        )
        observed_at = observer.begin_scan()
        self.assertEqual(
            loop.promote_auto_ready_batches(self.root, 0, observer, observed_at), []
        )
        observed_at = observer.begin_scan()
        promoted = loop.promote_auto_ready_batches(
            self.root, 0, observer, observed_at
        )
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0].parent.name, "ready")

    def test_auto_ready_does_not_arm_legacy_or_paused_batches(self):
        legacy = self.prepare("legacy")
        state = json.loads((legacy / "loop-state.json").read_text())
        state.pop("auto_ready")
        loop.atomic_write_json(legacy / "loop-state.json", state)
        paused = self.prepare("paused")
        (paused / "PAUSE").write_text("", encoding="utf-8")
        observer = self.observer()
        observed_at = observer.begin_scan()
        self.assertEqual(
            loop.promote_auto_ready_batches(self.root, 0, observer, observed_at), []
        )

    def test_dry_run_never_starts_codex_or_paid_work(self):
        destination = self.prepare()
        (destination / "requirements.txt").write_text(
            "V001：替换人物\nV002：跳过\n", encoding="utf-8"
        )
        ready = loop.move_batch(destination, self.root, "ready")
        with mock.patch.object(loop, "_run_codex_exec") as run_node:
            result = loop.process_ready_batch(
                ready, self.root, self.project, False, executor=self.executor
            )
        self.assertIsNone(result)
        run_node.assert_not_called()
        self.assertEqual(
            json.loads((ready / "loop-state.json").read_text())["state"],
            "READY_DRY_RUN",
        )

    def test_legacy_prepare_entry_is_permanently_disabled(self):
        destination = self.prepare()
        (destination / "requirements.txt").write_text(
            "V001：替换人物\nV002：跳过\n", encoding="utf-8"
        )
        with self.assertRaises(loop.LoopError):
            loop.prepare_batch_without_submission(
                destination, self.root, self.project, executor=self.executor
            )

        (destination / "PAUSE").write_text("", encoding="utf-8")
        with mock.patch.object(loop, "_run_codex_exec") as run_node:
            with self.assertRaisesRegex(loop.LoopError, "旧整批 prepare 入口已停用"):
                loop.prepare_batch_without_submission(
                destination, self.root, self.project, executor=self.executor
            )
        run_node.assert_not_called()

    def test_video_to_prompt_node_prompt_embeds_sampled_read_only_contract(self):
        node_input = {
            "job_id": "V001",
            "source_video": {
                "filename": "source.mp4",
                "duration_seconds": 1.5,
            },
            "sampled_frames": [
                {
                    "image": "inputs/frames/frame-001.jpg",
                    "timestamp_seconds": 0.0,
                }
            ],
            "attachment_order": ["inputs/frames/frame-001.jpg"],
            "requirements": ["V001：替换人物"],
        }
        node = loop.video_to_prompt_node_prompt("V001", node_input)
        self.assertIn("video-to-prompt.md", node)
        self.assertIn('"inputs/frames/frame-001.jpg"', node)
        self.assertIn('"timestamp_seconds": 0.0', node)
        self.assertIn("附图顺序严格等于 `attachment_order`", node)
        self.assertIn("不要调用任何工具、读取路径、创建文件", node)
        self.assertIn("`jobs[0].prompt`", node)
        self.assertNotIn("`node-input.json`", node)
        self.assertNotIn("`delivery`", node)
        self.assertNotIn("inputs/source", node)
        self.assertNotIn("ffmpeg", node.casefold())
        self.assertNotIn("source-analysis.json", node)
        self.assertNotIn("reference-analysis.json", node)
        self.assertNotIn("SKILL.md", node)
        self.assertNotIn("video-replacer", node)
        self.assertNotIn(str(self.project), node)
        self.assertNotIn(str(self.root), node)

    def test_direct_video_to_prompt_node_does_not_reuse_another_jobs_prompt(self):
        source = inspect.getsource(loop.run_video_to_prompt_node)
        self.assertNotIn("reuse_matching", source)

    def test_node_exec_uses_fresh_nonproject_workspace_and_minimal_features(self):
        batch = self.root / "ready" / "batch-a"
        batch.mkdir(parents=True)
        isolated_workspace = self.temp / "isolated-node"
        isolated_workspace.mkdir()
        command = loop.build_codex_command(
            "codex-test.exe" if os.name == "nt" else "codex-test",
            batch,
            self.project,
            SCHEMA_PATH,
            batch / "codex-result.json",
            node_workspace=isolated_workspace,
        )
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--skip-git-repo-check", command)
        for feature in (
            "skill_search",
            "plugins",
            "apps",
            "multi_agent",
            "shell_tool",
            "unified_exec",
            "shell_snapshot",
            "computer_use",
        ):
            self.assertIn(feature, command)
        self.assertIn("skills.include_instructions=false", command)
        self.assertIn('cli_auth_credentials_store="file"', command)
        self.assertEqual(command[-1], "-")
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

        calls = []

        def fake_run(command, *_args, **kwargs):
            result_path = Path(command[command.index("--output-last-message") + 1])
            result_path.write_text("{}", encoding="utf-8")
            calls.append(
                (
                    list(command),
                    kwargs["cwd"],
                    result_path,
                    Path(kwargs["env"]["CODEX_HOME"]),
                    Path(kwargs["env"]["HOME"]),
                    Path(kwargs["env"]["TMPDIR"]),
                    dict(kwargs["env"]),
                )
            )
            return SimpleNamespace(returncode=0)

        state_dir = self.temp / "external-node-state"
        codex_home = self.codex_node_home(state_dir)
        with mock.patch.object(
            loop,
            "find_codex",
            return_value="codex-test.exe" if os.name == "nt" else "codex-test",
        ), mock.patch.object(
            loop,
            "verify_windows_codex",
            return_value={"binary": Path("codex-test.exe")},
        ), mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir
        ), mock.patch.object(
            loop, "validate_node_home", return_value=codex_home
        ), mock.patch.object(
            loop, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(loop.subprocess, "run", side_effect=fake_run) as run, mock.patch.dict(
            os.environ,
            {
                "ARK_API_KEY": "secret",
                "TOS_SECRET": "secret",
                "CODEX_EXEC_SERVER_NOISE_FIXTURE": "secret",
                "PATH": os.defpath,
            },
            clear=False,
        ):
            loop._run_codex_exec(
                batch,
                self.root,
                self.project,
                "node prompt",
                schema_path=SCHEMA_PATH,
                job_ids=["V001"],
                stage="review-1",
            )
            loop._run_codex_exec(
                batch,
                self.root,
                self.project,
                "node prompt",
                schema_path=SCHEMA_PATH,
                job_ids=["V001"],
                stage="review-1",
            )
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[0][2], calls[1][2])
        self.assertEqual(calls[0][3], calls[1][3])
        for (
            node_command,
            cwd,
            result_path,
            isolated_home,
            shell_home,
            shell_tmp,
            node_environment,
        ) in calls:
            self.assertNotEqual(Path(cwd), self.project.resolve())
            self.assertFalse(Path(cwd).exists())
            self.assertIn("--skip-git-repo-check", node_command)
            self.assertNotIn("--sandbox", node_command)
            self.assertNotIn("--add-dir", node_command)
            with self.assertRaises(ValueError):
                result_path.relative_to(Path(cwd))
            self.assertFalse(result_path.exists())
            self.assertEqual(isolated_home, codex_home.resolve())
            self.assertTrue(isolated_home.exists())
            self.assertFalse(shell_home.exists())
            self.assertFalse(shell_tmp.exists())
            self.assertEqual(node_environment["CODEX_EXEC_SERVER_URL"], "none")
            self.assertFalse(
                any(
                    key.startswith("CODEX_EXEC_SERVER_NOISE_")
                    for key in node_environment
                )
            )
        persisted = list(
            batch.glob(
                "streaming-results/agents/V001-review-1/*/codex-result.json"
            )
        )
        self.assertEqual(len(persisted), 2)
        kwargs = run.call_args.kwargs
        self.assertNotIn("ARK_API_KEY", kwargs["env"])
        self.assertNotIn("TOS_SECRET", kwargs["env"])
        self.assertEqual(kwargs["env"]["PATH"], os.defpath)
        self.assertEqual(kwargs["env"]["CODEX_EXEC_SERVER_URL"], "none")

        process_records = list(
            batch.glob("streaming-results/agents/V001-review-1/*/codex-process.json")
        )
        self.assertEqual(len(process_records), 2)
        for process_path in process_records:
            process = json.loads(process_path.read_text(encoding="utf-8"))
            self.assertEqual(process["phase"], "SUCCEEDED")
            self.assertIn("result_validated_at", process)
            self.assertEqual(process["execution_environment"], "none")
            self.assertEqual(process["writable_workspace_paths"], [])
            self.assertTrue(process["shell_tools_disabled"])

    def test_node_exec_timeout_is_recorded_and_temporary_workspace_is_discarded(self):
        batch = self.root / "ready" / "node-timeout"
        batch.mkdir(parents=True)
        state_dir = self.temp / "external-timeout-state"
        codex_home = self.codex_node_home(state_dir)
        staged_workspaces = []

        def setup_workspace(workspace):
            staged_workspaces.append(workspace)
            (workspace / "sample.jpg").write_bytes(b"temporary sample")

        def fake_run(command, *_args, **kwargs):
            self.assertEqual(kwargs["timeout"], 7)
            raise loop.subprocess.TimeoutExpired(command, kwargs["timeout"])

        with mock.patch.object(
            loop,
            "find_codex",
            return_value="codex-test.exe" if os.name == "nt" else "codex-test",
        ), mock.patch.object(
            loop,
            "verify_windows_codex",
            return_value={"binary": Path("codex-test.exe")},
        ), mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir
        ), mock.patch.object(
            loop, "validate_node_home", return_value=codex_home
        ), mock.patch.object(
            loop, "locked_node_home", return_value=mock.MagicMock()
        ), mock.patch.object(
            loop.subprocess, "run", side_effect=fake_run
        ), mock.patch.dict(
            os.environ,
            {"VIDEO_LOOP_NODE_EXEC_TIMEOUT_SECONDS": "7"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                loop.LoopError, r"V001 prepare .* 7 秒"
            ):
                loop._run_codex_exec(
                    batch,
                    self.root,
                    self.project,
                    "node prompt",
                    schema_path=SCHEMA_PATH,
                    job_ids=["V001"],
                    stage="prepare",
                    node_workspace_setup=setup_workspace,
                )

        self.assertEqual(len(staged_workspaces), 1)
        self.assertFalse(staged_workspaces[0].exists())
        node_runs = state_dir / "node-runs"
        self.assertTrue(node_runs.is_dir())
        self.assertEqual(list(node_runs.iterdir()), [])
        records = list(
            batch.glob(
                "streaming-results/agents/V001-prepare/*/codex-process.json"
            )
        )
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertIsNone(record["returncode"])
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["timeout_seconds"], 7)
        self.assertEqual(record["stage"], "prepare")
        self.assertEqual(record["job_ids"], ["V001"])
        self.assertIn("started_at", record)
        self.assertIn("finished_at", record)
        self.assertEqual(record["phase"], "TIMED_OUT")
        self.assertIn("queued_at", record)
        self.assertIn("lock_acquired_at", record)
        self.assertIn("exec_started_at", record)
        self.assertIn("exec_finished_at", record)
        self.assertIsInstance(record["auth_lock_wait_seconds"], float)
        self.assertIsInstance(record["exec_seconds"], float)
        self.assertIsInstance(record["total_seconds"], float)
        self.assertFalse((records[0].parent / "codex-result.json").exists())

    def test_node_exec_timeout_environment_must_be_a_positive_integer(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIDEO_LOOP_NODE_EXEC_TIMEOUT_SECONDS", None)
            self.assertEqual(
                loop._node_exec_timeout_seconds(),
                loop.DEFAULT_NODE_EXEC_TIMEOUT_SECONDS,
            )

        for invalid in ("0", "-1", "not-an-integer"):
            with self.subTest(value=invalid), mock.patch.dict(
                os.environ,
                {"VIDEO_LOOP_NODE_EXEC_TIMEOUT_SECONDS": invalid},
                clear=False,
            ):
                with self.assertRaises(loop.LoopError):
                    loop._node_exec_timeout_seconds()

    def test_zero_exit_with_missing_or_invalid_result_is_not_recorded_as_success(self):
        for label, result_text, expected_phase, error_pattern in (
            ("missing", None, "RESULT_MISSING", "没有生成节点结果"),
            ("invalid", "not-json", "RESULT_INVALID", "不是有效 JSON"),
            ("array", "[]", "RESULT_INVALID", "不是 JSON 对象"),
        ):
            with self.subTest(label=label):
                batch = self.root / "ready" / f"node-result-{label}"
                batch.mkdir(parents=True)
                state_dir = self.temp / f"external-result-{label}-state"
                codex_home = self.codex_node_home(state_dir)

                def fake_run(command, *_args, **_kwargs):
                    if result_text is not None:
                        output = Path(
                            command[command.index("--output-last-message") + 1]
                        )
                        output.write_text(result_text, encoding="utf-8")
                    return SimpleNamespace(returncode=0)

                with mock.patch.object(
                    loop,
                    "find_codex",
                    return_value="codex-test.exe" if os.name == "nt" else "codex-test",
                ), mock.patch.object(
                    loop,
                    "verify_windows_codex",
                    return_value={"binary": Path("codex-test.exe")},
                ), mock.patch.object(
                    loop, "executor_state_dir", return_value=state_dir
                ), mock.patch.object(
                    loop, "validate_node_home", return_value=codex_home
                ), mock.patch.object(
                    loop, "locked_node_home", return_value=mock.MagicMock()
                ), mock.patch.object(loop.subprocess, "run", side_effect=fake_run):
                    with self.assertRaisesRegex(loop.LoopError, error_pattern):
                        loop._run_codex_exec(
                            batch,
                            self.root,
                            self.project,
                            "node prompt",
                            schema_path=SCHEMA_PATH,
                            job_ids=["V001"],
                        )

                records = list(
                    batch.glob(
                        "streaming-results/agents/V001-prepare/*/codex-process.json"
                    )
                )
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text())
                self.assertEqual(record["phase"], expected_phase)
                self.assertNotEqual(record["phase"], "SUCCEEDED")
                self.assertIn("error", record)

    def test_node_home_setup_failure_writes_terminal_process_record(self):
        batch = self.root / "ready" / "node-home-setup-failure"
        batch.mkdir(parents=True)
        state_dir = self.temp / "external-setup-failure-state"
        with mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir
        ), mock.patch.object(
            loop,
            "validate_node_home",
            side_effect=loop.CodexNodeHomeError("fixture node home invalid"),
        ):
            with self.assertRaisesRegex(loop.LoopError, "node home invalid"):
                loop._run_codex_exec(
                    batch,
                    self.root,
                    self.project,
                    "node prompt",
                    schema_path=SCHEMA_PATH,
                    job_ids=["V001"],
                )
        records = list(
            batch.glob("streaming-results/agents/V001-prepare/*/codex-process.json")
        )
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["phase"], "SETUP_FAILED")
        self.assertIn("fixture node home invalid", record["error"])

    def test_node_prompt_and_attachment_setup_failures_are_terminal(self):
        cases = (
            ("empty-prompt", "   ", (), "节点 prompt 为空"),
            (
                "missing-attachment",
                "node prompt",
                (Path("missing.jpg"),),
                "节点附加图片不存在",
            ),
        )
        for label, prompt, attachments, error_pattern in cases:
            with self.subTest(label=label):
                batch = self.root / "ready" / f"node-setup-{label}"
                batch.mkdir(parents=True)
                state_dir = self.temp / f"external-{label}-state"
                codex_home = self.codex_node_home(state_dir)
                with mock.patch.object(
                    loop, "executor_state_dir", return_value=state_dir
                ), mock.patch.object(
                    loop, "validate_node_home", return_value=codex_home
                ):
                    with self.assertRaisesRegex(loop.LoopError, error_pattern):
                        loop._run_codex_exec(
                            batch,
                            self.root,
                            self.project,
                            prompt,
                            schema_path=SCHEMA_PATH,
                            job_ids=["V001"],
                            attached_workspace_images=attachments,
                        )
                records = list(
                    batch.glob(
                        "streaming-results/agents/V001-prepare/*/codex-process.json"
                    )
                )
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual(record["phase"], "SETUP_FAILED")
                self.assertIn("finished_at", record)
                self.assertIn(error_pattern, record["error"])

    def test_node_auth_lock_failure_is_recorded_before_subprocess(self):
        batch = self.root / "ready" / "node-lock-failure"
        batch.mkdir(parents=True)
        state_dir = self.temp / "external-lock-failure-state"
        codex_home = self.codex_node_home(state_dir)

        @contextmanager
        def fail_lock(*_args, **_kwargs):
            raise loop.CodexNodeHomeError("fixture auth lock timeout")
            yield

        with mock.patch.object(
            loop,
            "find_codex",
            return_value="codex-test.exe" if os.name == "nt" else "codex-test",
        ), mock.patch.object(
            loop,
            "verify_windows_codex",
            return_value={"binary": Path("codex-test.exe")},
        ), mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir
        ), mock.patch.object(
            loop, "validate_node_home", return_value=codex_home
        ), mock.patch.object(
            loop, "locked_node_home", side_effect=fail_lock
        ), mock.patch.object(loop.subprocess, "run") as run:
            with self.assertRaisesRegex(loop.LoopError, "auth lock timeout"):
                loop._run_codex_exec(
                    batch,
                    self.root,
                    self.project,
                    "node prompt",
                    schema_path=SCHEMA_PATH,
                    job_ids=["V001"],
                    stage="prepare",
                )

        run.assert_not_called()
        records = list(
            batch.glob(
                "streaming-results/agents/V001-prepare/*/codex-process.json"
            )
        )
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["phase"], "LOCK_FAILED")
        self.assertIsNone(record["lock_acquired_at"])
        self.assertIsNone(record["exec_started_at"])
        self.assertEqual(record["exec_seconds"], 0.0)
        self.assertIn("fixture auth lock timeout", record["error"])

    def test_windows_prompt_node_requires_native_exe_without_shell_wrappers(self):
        for wrapper in (
            r"D:\Tools\codex.cmd",
            r"D:\Tools\codex.bat",
            r"D:\Tools\codex.ps1",
        ):
            with self.subTest(wrapper=wrapper), self.assertRaisesRegex(
                loop.LoopError, "native codex.exe"
            ):
                loop.codex_launcher(wrapper, platform_name="nt")
        launcher = loop.codex_launcher(r"D:\Tools\codex.exe", platform_name="nt")
        self.assertEqual(launcher, [r"D:\Tools\codex.exe"])
        with mock.patch.object(loop, "codex_launcher", return_value=launcher):
            command = loop.build_codex_command(
                r"D:\Tools\codex.exe",
                self.root,
                self.project,
                self.root / "s.json",
                self.root / "r.json",
                node_workspace=self.temp / "windows-node",
                platform_name="nt",
            )
        self.assertEqual(command[0], r"D:\Tools\codex.exe")
        self.assertEqual(command[-1], "-")
        self.assertIn('windows.sandbox="unelevated"', command)
        self.assertNotIn("--sandbox", command)
        permission_config = next(
            item
            for item in command
            if item.startswith("permissions.node_isolated.filesystem=")
        )
        self.assertIn('"."="read"', permission_config)
        self.assertNotIn('="write"', permission_config)
        for feature in ("shell_tool", "unified_exec", "shell_snapshot"):
            self.assertIn(feature, command)

    def test_executor_selection_is_dreamina_only_and_basename_allowlisted(self):
        replacement = self.temp / "replacement_pipeline.py"
        replacement.write_text("# fixture", encoding="utf-8")
        with self.assertRaises(loop.LoopError):
            loop.find_replacement_executor(
                self.project, {"VIDEO_REPLACER_PIPELINE": str(replacement)}
            )
        selected = loop.find_replacement_executor(
            self.project,
            {
                "VIDEO_REPLACEMENT_EXECUTOR": str(self.executor_path),
                "VIDEO_REPLACER_PIPELINE": str(replacement),
            },
        )
        self.assertEqual(selected.path, self.executor_path.resolve())
        evil = self.temp / "evil.py"
        evil.write_text("# fixture", encoding="utf-8")
        with self.assertRaises(loop.LoopError):
            loop.find_replacement_executor(
                self.project, {"VIDEO_REPLACEMENT_EXECUTOR": str(evil)}
            )

    def test_state_dir_is_external_and_explicit_value_must_be_absolute(self):
        external = Path.home() / ".local" / "state" / "video-replacer-test-fixture"
        if os.name == "nt":
            with self.assertRaisesRegex(loop.LoopError, "LocalAppData"):
                loop.executor_state_dir(
                    self.root, {"VIDEO_REPLACER_STATE_DIR": str(external)}
                )
        else:
            self.assertEqual(
                loop.executor_state_dir(
                    self.root, {"VIDEO_REPLACER_STATE_DIR": str(external)}
                ),
                external.resolve(),
            )
        with self.assertRaises(loop.LoopError):
            loop.executor_state_dir(self.root, {"VIDEO_REPLACER_STATE_DIR": "relative"})
        with self.assertRaises(loop.LoopError):
            loop.executor_state_dir(
                self.root,
                {"VIDEO_REPLACER_STATE_DIR": str(self.temp / "unsafe-state")},
            )
        with self.assertRaises(loop.LoopError):
            loop.require_external_state_dir(self.root / "state", self.root, self.project)

    def test_startup_cleanup_removes_only_orphaned_node_run_directories(self):
        state_dir = self.temp / "external-state"
        node_runs = state_dir / "node-runs"
        orphan_a = node_runs / "video-loop-source-V001-fixture"
        orphan_b = node_runs / "video-loop-prompt-V002-fixture"
        retained = node_runs / "unrelated"
        for directory in (orphan_a, orphan_b, retained):
            directory.mkdir(parents=True)
        (orphan_a / "auth.json").write_text("secret", encoding="utf-8")
        (orphan_b / "source.mp4").write_bytes(b"source")
        symlink = node_runs / "video-loop-symlink"
        symlink.symlink_to(retained, target_is_directory=True)

        with mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir
        ), mock.patch.object(loop, "require_external_state_dir"):
            result = loop.cleanup_orphaned_node_runs(self.root, self.project)

        self.assertEqual(
            result,
            {
                "removed_count": 2,
                "removed": sorted([orphan_a.name, orphan_b.name]),
            },
        )
        self.assertFalse(orphan_a.exists())
        self.assertFalse(orphan_b.exists())
        self.assertTrue(retained.is_dir())
        self.assertTrue(symlink.is_symlink())

    def test_submission_plan_confines_and_rebinds_indexed_images(self):
        batch = self.prepare()
        image = batch / "replacements" / "face.png"
        image.write_bytes(b"image")
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        original = str(image.resolve())
        self.write_valid_plan(batch, images=[image])
        loaded = loop.load_submission_plan(batch, self.project, "V001", executor=self.executor)
        self.assertEqual(loaded["images"], [original])
        review = loop.move_batch(batch, self.root, "review")
        loaded = loop.load_submission_plan(review, self.project, "V001", executor=self.executor)
        self.assertEqual(
            loaded["images"],
            [str((review / "replacements" / "face.png").resolve())],
        )
        plan_path = self.project / "outputs" / "video-replacements" / "batch-a-V001" / "submission-plan.json"
        plan = json.loads(plan_path.read_text())
        outside = self.temp / "outside.png"
        outside.write_bytes(b"outside")
        plan["images"] = [str(outside)]
        loop.atomic_write_json(plan_path, plan)
        with self.assertRaises(loop.LoopError):
            loop.load_submission_plan(review, self.project, "V001", executor=self.executor)

    def test_zero_reference_images_are_valid_and_bound(self):
        batch = self.prepare()
        self.write_valid_plan(batch, images=[])
        loaded = loop.load_submission_plan(batch, self.project, "V001", executor=self.executor)
        self.assertEqual(loaded["images"], [])

    def test_preflight_rehash_rejects_prompt_and_reference_tampering(self):
        batch = self.prepare()
        image = batch / "replacements" / "product.png"
        image.write_bytes(b"image")
        loop.atomic_write_json(batch / "reference-index.json", loop.build_reference_index(batch))
        plan, _ = self.write_valid_plan(batch, images=[image])
        Path(plan["prompt_file"]).write_text("tampered", encoding="utf-8")
        with self.assertRaises(loop.LoopError):
            loop.load_submission_plan(batch, self.project, "V001", executor=self.executor)

    def test_retired_privacy_status_is_rejected(self):
        batch = self.prepare()
        privacy = {
            "status": "anonymized",
            "remote_upload_authorized": True,
            "paid_task_authorized": True,
        }
        self.write_valid_plan(batch, privacy=privacy)
        with self.assertRaises(loop.LoopError):
            loop.load_submission_plan(batch, self.project, "V001", executor=self.executor)

    def test_workflow_selected_none_input_loads_without_review(self):
        batch = self.prepare()
        self.write_valid_plan(batch)
        loaded = loop.load_submission_plan(
            batch,
            self.project,
            "V001",
            executor=self.executor,
        )
        self.assertEqual(loaded["job_id"], "V001")

    def test_mosaic_mode_requires_fixed_workflow_output(self):
        batch = self.prepare()
        bindings = json.loads((batch / "job-bindings.json").read_text(encoding="utf-8"))
        bindings["jobs"][0]["privacy_mode"] = "mosaic_required"
        loop.atomic_write_json(batch / "job-bindings.json", bindings)
        self.write_valid_plan(batch)
        loaded = loop.load_submission_plan(
            batch,
            self.project,
            "V001",
            executor=self.executor,
        )
        self.assertEqual(loaded["job_id"], "V001")

    def test_active_video_cannot_be_swapped_between_v_ids(self):
        batch = self.prepare()
        plan, _ = self.write_valid_plan(batch, job_id="V001")
        index = loop.read_index(batch)
        second_source = batch / index["jobs"][1]["relative_path"]
        active = Path(plan["video"])
        active.write_bytes(second_source.read_bytes())
        preflight_path = Path(plan["preflight_manifest"])
        preflight = json.loads(preflight_path.read_text())
        preflight["active_video"]["sha256"] = loop.sha256_file(active)
        loop.atomic_write_json(preflight_path, preflight)
        with self.assertRaises(loop.LoopError):
            loop.load_submission_plan(
                batch, self.project, "V001", executor=self.executor
            )

    def test_source_preparation_manifest_binds_original_and_video_stream(self):
        batch = self.prepare()
        indexed = loop.read_index(batch)["jobs"][0]
        source = batch / indexed["relative_path"]
        output = (
            self.project
            / "outputs"
            / "video-replacements"
            / "batch-a-V001"
            / "source-prepared.mp4"
        )
        output.parent.mkdir(parents=True)
        output.write_bytes(b"prepared-with-aac")
        manifest = output.parent / "source-preparation.json"
        loop.atomic_write_json(
            manifest,
            {
                "schema_version": 1,
                "operation": "video-stream-copy-audio-transcode-aac",
                "source": {
                    "path": str(source.resolve()),
                    "sha256": loop.sha256_file(source),
                },
                "output": {
                    "path": str(output.resolve()),
                    "sha256": loop.sha256_file(output),
                },
                "h264_video_stream_sha256": "same-stream",
            },
        )
        with mock.patch.object(
            loop, "h264_stream_sha256", return_value="same-stream"
        ):
            validated = loop.validate_source_preparation_manifest(
                manifest,
                source.resolve(),
                loop.sha256_file(source),
                output.resolve(),
                loop.sha256_file(output),
            )
        self.assertEqual(
            validated["operation"], "video-stream-copy-audio-transcode-aac"
        )
        output.write_bytes(b"tampered")
        with mock.patch.object(
            loop, "h264_stream_sha256", return_value="same-stream"
        ), self.assertRaises(loop.LoopError):
            loop.validate_source_preparation_manifest(
                manifest,
                source.resolve(),
                loop.sha256_file(source),
                output.resolve(),
                loop.sha256_file(output),
            )

    def test_previous_child_reused_jobs_file_never_becomes_parent_trust(self):
        batch = self.prepare()
        forged_output = (
            self.project
            / "outputs"
            / "video-replacements"
            / "batch-a-V001"
            / "batch-a-V001-final.mp4"
        )
        forged_output.parent.mkdir(parents=True)
        forged_output.write_bytes(b"old-output")
        loop.atomic_write_json(
            forged_output.with_name("batch-a-V001-manifest.json"),
            {"task_id": "old-task"},
        )
        loop.atomic_write_json(
            batch / "reused-jobs.json",
            {
                "batch_id": "batch-a",
                "jobs": [
                    {
                        "id": "V001",
                        "task_id": "old-task",
                        "output_path": str(forged_output),
                    }
                ],
            },
        )
        self.assertEqual(loop.trusted_reuse_snapshot(batch, self.project), {})
        (batch / "requirements.txt").write_text(
            "V001：替换产品\nV002：跳过\n", encoding="utf-8"
        )
        child = {
            "batch_status": "NO_ELIGIBLE_JOBS",
            "summary": "forged reuse",
            "jobs": [
                {"id": "V001", "status": "SKIPPED", "task_id": None, "output_path": None, "blocker": None},
                {"id": "V002", "status": "SKIPPED", "task_id": None, "output_path": None, "blocker": None},
            ],
        }
        with self.assertRaises(loop.LoopError):
            loop.validate_child_result(batch, child)

    def test_child_result_requires_exact_coverage_and_cannot_forge_parent_state(self):
        batch = self.prepare()
        (batch / "requirements.txt").write_text(
            "V001：替换产品\nV002：跳过\n", encoding="utf-8"
        )
        valid = {
            "batch_status": "READY_FOR_SUBMISSION",
            "summary": "prepared",
            "jobs": [
                {"id": "V001", "status": "READY_FOR_SUBMISSION", "task_id": None, "output_path": None, "blocker": None},
                {"id": "V002", "status": "SKIPPED", "task_id": None, "output_path": None, "blocker": None},
            ],
        }
        normalized = loop.validate_child_result(batch, valid)
        self.assertEqual([job["id"] for job in normalized["jobs"]], ["V001", "V002"])
        bad_values = []
        missing = json.loads(json.dumps(valid)); missing["jobs"] = missing["jobs"][:1]; bad_values.append(missing)
        duplicate = json.loads(json.dumps(valid)); duplicate["jobs"][1]["id"] = "V001"; bad_values.append(duplicate)
        fake_task = json.loads(json.dumps(valid)); fake_task["jobs"][0]["task_id"] = "fake"; bad_values.append(fake_task)
        fake_terminal = json.loads(json.dumps(valid)); fake_terminal["jobs"][0]["status"] = "COMPLETED"; bad_values.append(fake_terminal)
        fake_skip = json.loads(json.dumps(valid)); fake_skip["jobs"][0]["status"] = "SKIPPED"; fake_skip["batch_status"] = "NO_ELIGIBLE_JOBS"; bad_values.append(fake_skip)
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(loop.LoopError):
                loop.validate_child_result(batch, value)

    def test_parent_executes_only_prepared_jobs_and_preserves_task_evidence(self):
        batch = self.prepare()
        (batch / "requirements.txt").write_text(
            "V001：替换产品\nV002：跳过\n", encoding="utf-8"
        )
        running = loop.move_batch(batch, self.root, "running")
        prepared = {
            "batch_status": "READY_FOR_SUBMISSION",
            "summary": "prepared",
            "jobs": [
                {"id": "V001", "status": "READY_FOR_SUBMISSION", "task_id": None, "output_path": None, "blocker": None},
                {"id": "V002", "status": "SKIPPED", "task_id": None, "output_path": None, "blocker": None},
            ],
        }
        manifest = {"task_id": "task-1", "final_output": "/tmp/final.mp4"}
        with mock.patch.object(loop, "execute_submission_plan", return_value=manifest) as execute:
            result = loop.execute_prepared_jobs(
                running,
                self.project,
                prepared,
                executor=self.executor,
                paid_execution_authorized=True,
            )
        self.assertEqual(execute.call_args.args[:3], (running, self.project, "V001"))
        self.assertTrue(execute.call_args.kwargs["paid_execution_authorized"])
        self.assertEqual(result["batch_status"], "COMPLETED")

        task_dir = self.project / "outputs" / "video-replacements" / "batch-a-V001" / "tasks"
        task_dir.mkdir(parents=True)
        loop.atomic_write_json(task_dir / "record.json", {"task_id": "created-before-failure"})
        with mock.patch.object(
            loop, "execute_submission_plan", side_effect=loop.LoopError("failed")
        ):
            failed = loop.execute_prepared_jobs(
                running,
                self.project,
                prepared,
                executor=self.executor,
                paid_execution_authorized=True,
            )
        self.assertEqual(failed["jobs"][0]["task_id"], "created-before-failure")

    def test_fifteen_is_supported_and_sixteen_is_rejected(self):
        batch = self.prepare("batch-fifteen", 15)
        index = loop.read_index(batch)
        self.assertEqual(index["jobs"][-1]["id"], "V015")
        running = loop.move_batch(batch, self.root, "running")
        prepared = {
            "batch_status": "READY_FOR_SUBMISSION",
            "summary": "prepared",
            "jobs": [
                {"id": f"V{number:03d}", "status": "READY_FOR_SUBMISSION", "task_id": None, "output_path": None, "blocker": None}
                for number in range(1, 16)
            ],
        }

        def manifest_for(_batch, _project, job_id, **_kwargs):
            return {"task_id": f"task-{job_id}", "final_output": f"/tmp/{job_id}.mp4"}

        with mock.patch.object(loop, "execute_submission_plan", side_effect=manifest_for) as execute:
            result = loop.execute_prepared_jobs(
                running,
                self.project,
                prepared,
                executor=self.executor,
                paid_execution_authorized=True,
            )
        self.assertEqual(execute.call_count, 15)
        self.assertEqual(result["batch_status"], "COMPLETED")
        with self.assertRaises(loop.LoopError):
            loop.prepare_inbox_batch(
                self.create_inbox_batch("batch-sixteen", 16), self.root
            )

    def test_running_recovery_is_fail_closed_and_never_resubmits(self):
        running = loop.move_batch(self.prepare(), self.root, "running")
        observer = self.observer()
        with mock.patch.object(loop, "_run_codex_exec") as run_node, mock.patch.object(
            loop, "execute_submission_plan"
        ) as execute:
            counts = loop.scan_once(
                self.root, self.project, 0, True, observer=observer
            )
        run_node.assert_not_called()
        execute.assert_not_called()
        blocked = self.root / "blocked" / running.name
        self.assertTrue(blocked.exists())
        self.assertEqual(counts["running_recovered"], 1)
        recovery = json.loads((blocked / "recovery-state.json").read_text())
        self.assertFalse(recovery["automatic_new_submission_allowed"])

    def test_daily_paid_limit_and_cross_batch_semantic_ledger_block(self):
        state_dir = self.temp / "state"
        batch = self.root / "ready" / "batch-ledger"
        batch.mkdir()
        for number in range(15):
            loop.reserve_parent_submission(
                state_dir, f"{number:064x}", batch, f"V{number + 1:03d}", 15
            )
        first_record = json.loads(
            (
                state_dir
                / "video-batch-loop-submissions"
                / f"{0:064x}.json"
            ).read_text()
        )
        self.assertEqual(first_record["local_date"], loop.local_date_now())
        with self.assertRaises(loop.LoopError):
            loop.reserve_parent_submission(state_dir, "f" * 64, batch, "V016", 15)
        with self.assertRaises(loop.LoopError):
            loop.reserve_parent_submission(state_dir, f"{0:064x}", batch, "V001", 0)

    def test_source_reference_and_hash_time_of_check_changes_are_rejected(self):
        batch = self.prepare()
        (batch / "videos" / "2.mp4").write_bytes(b"tampered")
        with self.assertRaises(loop.LoopError):
            loop.verify_batch_integrity(batch)

        other = self.prepare("reference-tamper")
        image = other / "replacements" / "product.png"
        image.write_bytes(b"original")
        loop.atomic_write_json(other / "reference-index.json", loop.build_reference_index(other))
        image.write_bytes(b"tampered")
        with self.assertRaises(loop.LoopError):
            loop.verify_reference_index(other)

        stable_file = self.temp / "stable.bin"
        stable_file.write_bytes(b"x")
        with mock.patch.object(
            loop.ObservationTracker,
            "_stat_signature",
            side_effect=[{"size": 1}, {"size": 2}],
        ), self.assertRaises(loop.LoopError):
            loop._hash_without_change(stable_file)

    def test_prompt_format_gate_rejects_missing_complete_material_binding(self):
        prompt = self.temp / "missing-material-binding.txt"
        prompt.write_text(
            "编辑要求：保持原视频固定广角机位、构图、运镜；"
            "仅将车内饰和人物替换；镜头1车外替换为住宅。\n"
            "动作约束：开场遮阳挡已完整安装；随后折收并取下；"
            "结尾重新展开并安装。\n"
            "一致性：安装完成前侧窗有直射光；安装完成后直射光消失。\n"
            "禁止：禁止遮阳挡改变位置、形变、折叠方式、展开方向、"
            "固定方式或手部交互。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(loop.LoopError, "提示词格式 Gate 未通过") as caught:
            loop.validate_execution_prompt(prompt, reference_count=0)
        message = str(caught.exception)
        self.assertIn("首个非空行必须是完整的素材绑定", message)

    def test_prompt_format_gate_accepts_shot_qualified_state_timeline(self):
        prompt = self.temp / "qualified-prompt.txt"
        prompt.write_text(
            "最高优先级：替换车内饰、三名人物、镜头1住宅外景和镜头5 "
            "Costco 外景；保持原片固定广角机位，无运镜。\n"
            "镜头1：0–2秒\n开场遮阳挡已完整安装，侧窗无明显直射；人物按原动作"
            "折收并取下后，侧窗明显直射出现。\n"
            "镜头2：2–4秒\n保持未安装状态、原动作和侧窗直射。\n"
            "镜头3：4–6秒\n保持未安装状态、原动作和侧窗直射。\n"
            "镜头4：6–8秒\n保持未安装状态、原动作和侧窗直射。\n"
            "镜头5：8–10秒\n按原动作重新固定遮阳挡；固定完成后，侧窗明显直射消失。\n"
            "全局锁定：同一个遮阳挡按原片逐时刻轨迹完成安装、折收、取下、"
            "展开和重新固定。\n"
            "禁止：禁止复制、瞬移、偏离原片动作轨迹或在原片可见时丢失。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt)

    def test_prompt_gate_accepts_generic_installation_before_after_shape(self):
        prompt = self.temp / "generic-installation-shape.txt"
        prompt.write_text(
            "动作约束：开场遮阳挡已完整安装；随后按原轨迹取下，"
            "结尾按原轨迹重新安装。\n"
            "一致性：保持遮阳挡材质、轮廓、展开方向和安装前后形态。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt)

    def test_prompt_gate_accepts_shot_scoped_installation_phases(self):
        prompt = self.temp / "shot-scoped-installation-phases.txt"
        prompt.write_text(
            "动作约束：开场遮阳挡已完整安装；随后按原轨迹取下，"
            "结尾按原轨迹重新安装。\n"
            "镜头4（约16–24秒）：人物协作展开遮阳挡，安装完成前保持侧窗自然光。\n"
            "镜头5（约24–30秒）：遮阳挡完成固定，安装完成后保持柔和环境光。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt)

    def test_prompt_gate_allows_nonblocking_timecode_variation(self):
        prompt = self.temp / "invalid-shot-writing.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n"
            "镜头1（0-1秒）：编辑人物。\n"
            "保持：用户明确要求的人物动作和本镜可见人物与产品的原有关系。\n"
            "其余画面信息保持原视频本镜头不变。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=0)

    def test_prompt_gate_accepts_plain_shot_change_paragraph(self):
        prompt = self.temp / "inline-shot-writing.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n"
            "镜头1（0.0-1.0s）\n"
            "将人物替换为指定人物。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=0)

    def test_prompt_gate_allows_nonblocking_overbroad_prose(self):
        prompt = self.temp / "legacy-keep-shot-writing.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n"
            "镜头1（0.0-1.0s）：编辑人物。\n"
            "保持：用户明确要求的人物动作和本镜可见人物与产品的原有关系。\n"
            "除上述编辑和重点保持事项外，保持原视频本镜头的构图、机位、景别、运镜、时长和剪辑节奏不变；"
            "保持原有车辆、人物、产品和环境的数量、位置、动作、视线、状态、接触、遮挡与空间关系不变；"
            "保持原有光线、曝光和明暗变化逻辑不变。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=0)

    def test_prompt_gate_accepts_explicit_removal_instruction(self):
        prompt = self.temp / "negative-absence-instruction.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n"
            "镜头1（0.0-1.0s）\n"
            "将人物替换为指定人物，并移除所有遮阳产品。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=0)

    def test_prompt_gate_allows_nonblocking_negative_sentence(self):
        prompt = self.temp / "offscreen-person-negative.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n"
            "镜头4（16.8-23.9s）\n"
            "驾驶位女性不进入镜头4；将画面人物替换为副驾驶成年男性和后排年轻女性。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=0)

    def test_prompt_reference_alias_gate_accepts_once_binding_and_name_only_body(self):
        prompt = self.temp / "reference-alias-valid.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频；@图片1=Volkswagen目标内饰；"
            "@图片2=驾驶位年长女性。\n"
            "镜头1（0.0-1.0s）\n"
            "将车内饰替换为Volkswagen目标内饰，将驾驶位人物替换为驾驶位年长女性。\n",
            encoding="utf-8",
        )
        loop.validate_execution_prompt(prompt, reference_count=2)

    def test_prompt_format_gate_rejects_repeated_handles_in_body(self):
        prompt = self.temp / "reference-alias-repeated.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频；@图片1=目标内饰。\n"
            "编辑要求：把车内饰替换为@图片1中的目标内饰。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            loop.LoopError, "素材句柄只能出现在首行素材绑定"
        ):
            loop.validate_execution_prompt(prompt, reference_count=1)

    def test_prompt_format_gate_rejects_duplicate_semantic_names(self):
        prompt = self.temp / "reference-alias-duplicate.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频；@图片1=目标人物；@图片2=目标人物。\n"
            "编辑要求：替换目标人物。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(loop.LoopError, "素材名称必须互不重复"):
            loop.validate_execution_prompt(prompt, reference_count=2)

    def test_prompt_format_gate_rejects_filename_like_binding_name(self):
        prompt = self.temp / "reference-alias-filename.txt"
        prompt.write_text(
            "素材绑定：@视频1=原视频；@图片1=interior-2026-ford-escape-platinum.png。\n"
            "编辑要求：替换车内饰。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(loop.LoopError, "自然、可读的名称"):
            loop.validate_execution_prompt(prompt, reference_count=1)

    def test_load_submission_plan_enforces_prompt_format_gate(self):
        batch = self.prepare()
        plan, preflight = self.write_valid_plan(batch)
        prompt = Path(plan["prompt_file"])
        prompt.write_text(
            "编辑要求：保持固定机位、构图和运镜，仅将人物替换；"
            "镜头1外景替换为住宅。\n",
            encoding="utf-8",
        )
        preflight["input_bindings"] = loop._expected_input_bindings(prompt, [])
        loop.atomic_write_json(Path(plan["preflight_manifest"]), preflight)
        with self.assertRaisesRegex(loop.LoopError, "提示词格式 Gate 未通过"):
            loop.load_submission_plan(
                batch,
                self.project,
                "V001",
                executor=self.executor,
            )

    def test_direct_legacy_execute_blocks_before_agent_or_submission(self):
        batch = self.prepare()
        (batch / "requirements.txt").write_text("默认：替换产品\n", encoding="utf-8")
        ready = loop.move_batch(batch, self.root, "ready")
        with mock.patch.object(loop, "_run_codex_exec") as run_node:
            with self.assertRaisesRegex(loop.LoopError, "旧整批 Agent 执行入口已停用"):
                loop.process_ready_batch(
                    ready,
                    self.root,
                    self.project,
                    True,
                    executor=self.executor,
                    minimum_free_bytes=2,
                )
        run_node.assert_not_called()

    def test_stale_assembly_is_explicitly_recovered_after_two_scans(self):
        staging = self.root / "inbox" / ".assembling-batch-recover"
        (staging / "videos").mkdir(parents=True)
        (staging / "replacements").mkdir()
        (staging / "videos" / "a.mp4").write_bytes(b"video")
        observer = self.observer()
        first = observer.begin_scan()
        self.assertEqual(
            loop.recover_stale_assembling_batches(self.root, 0, observer, first),
            (0, 0),
        )
        second = observer.begin_scan()
        self.assertEqual(
            loop.recover_stale_assembling_batches(self.root, 0, observer, second),
            (1, 0),
        )
        self.assertTrue((self.root / "inbox" / "a.mp4").is_file())

    def test_executor_always_receives_external_state_dir_and_parent_env(self):
        batch = self.prepare()
        self.write_valid_plan(batch)
        state_dir = self.temp / "external-state"
        ffmpeg = self.temp / "ffmpeg"
        ffmpeg.write_text("fixture\n", encoding="utf-8")
        ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IXUSR)

        def fake_generate(command, **kwargs):
            output = Path(command[command.index("--output-dir") + 1])
            name = command[command.index("--name") + 1]
            loop.atomic_write_json(
                output / f"{name}-manifest.json",
                {"task_id": "offline-task", "final_output": str(output / f"{name}-final.mp4")},
            )
            (output / f"{name}-final.mp4").write_bytes(b"offline-result")
            self.assertEqual(
                kwargs["env"]["VIDEO_REPLACER_STATE_DIR"], str(state_dir.resolve())
            )
            self.assertEqual(kwargs["env"]["FFMPEG"], str(ffmpeg.resolve()))
            self.assertNotIn(loop.PAYMENT_AUTH_TOKEN_ENV, kwargs["env"])
            self.assertEqual(
                command[command.index("--state-dir") + 1], str(state_dir.resolve())
            )
            self.assertIn("--confirm-paid", command)
            return SimpleNamespace(returncode=0, stdout="offline fixture\n")

        with mock.patch.object(
            loop, "executor_state_dir", return_value=state_dir.resolve()
        ), mock.patch.object(
            loop, "_reject_temporary_state_dir"
        ), mock.patch.dict(
            os.environ,
            {
                "VIDEO_REPLACER_STATE_DIR": str(state_dir),
                loop.PAYMENT_AUTH_TOKEN_ENV: "a" * 64,
            },
            clear=False,
        ), mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=ffmpeg.resolve()
        ), mock.patch.object(loop.subprocess, "run", side_effect=fake_generate) as run:
            manifest = loop.execute_submission_plan(
                batch,
                self.project,
                "V001",
                loop_root=self.root,
                executor=self.executor,
                minimum_free_bytes=0,
                paid_execution_authorized=True,
            )
        self.assertEqual(manifest["task_id"], "offline-task")
        self.assertEqual(run.call_count, 1)
        ledger_records = list(
            (state_dir / "video-batch-loop-submissions").glob("*.json")
        )
        self.assertEqual(len(ledger_records), 1)
        ledger = json.loads(ledger_records[0].read_text())
        self.assertEqual(ledger["state"], "completed")
        self.assertEqual(ledger["task_id"], "offline-task")

    def test_stale_output_cannot_bypass_parent_semantic_ledger(self):
        batch = self.prepare()
        plan, _ = self.write_valid_plan(batch)
        output = Path(plan["output_dir"])
        name = str(plan["name"])
        loop.atomic_write_json(output / f"{name}-manifest.json", {"task_id": "stale"})
        (output / f"{name}-final.mp4").write_bytes(b"stale")
        state_dir = self.temp / "external-state"
        with mock.patch.object(
            loop, "_reject_temporary_state_dir"
        ), mock.patch.dict(
            os.environ, {"VIDEO_REPLACER_STATE_DIR": str(state_dir)}, clear=False
        ), mock.patch.object(loop.subprocess, "run") as run, self.assertRaises(
            loop.LoopError
        ):
            loop.execute_submission_plan(
                batch,
                self.project,
                "V001",
                loop_root=self.root,
                executor=self.executor,
                paid_execution_authorized=True,
            )
        run.assert_not_called()

    def test_direct_submission_defaults_to_blocked_before_ledger_or_process(self):
        batch = self.prepare("direct-paid-gate")
        with mock.patch.object(
            loop, "reserve_parent_submission"
        ) as reserve, mock.patch.object(
            loop.subprocess, "run"
        ) as run, self.assertRaisesRegex(
            loop.LoopError, "明确付费授权"
        ):
            loop.execute_submission_plan(
                batch,
                self.project,
                "V001",
                loop_root=self.root,
                executor=self.executor,
            )
        reserve.assert_not_called()
        run.assert_not_called()

    def test_true_once_cli_regression_and_watch_holds_one_lifetime_lock(self):
        with mock.patch.object(
            loop, "scan_once", return_value={"prepared": 0}
        ) as scan:
            returncode = loop.main(
                [
                    "--root",
                    str(self.root),
                    "--project-root",
                    str(self.project),
                    "--min-free-gib",
                    "0",
                    "once",
                ]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(scan.call_args.args[:2], (self.root.resolve(), self.project.resolve()))

        events = []

        @contextmanager
        def fake_lock(_root):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with mock.patch.object(loop, "exclusive_loop_lock", side_effect=fake_lock), mock.patch.object(
            loop, "scan_once", side_effect=[{}, KeyboardInterrupt]
        ) as watch_scan, mock.patch.object(loop.time, "sleep"):
            returncode = loop.main(
                [
                    "--root",
                    str(self.root),
                    "--project-root",
                    str(self.project),
                    "--min-free-gib",
                    "0",
                    "watch",
                    "--interval",
                    "1",
                ]
            )
        self.assertEqual(returncode, 130)
        self.assertEqual(watch_scan.call_count, 2)
        self.assertEqual(events, ["enter", "exit"])

    def test_child_schema_forbids_parent_owned_fields(self):
        schema = json.loads(
            SCHEMA_PATH.read_text()
        )
        self.assertNotIn("VISUAL_REVIEW_PENDING", schema["properties"]["batch_status"]["enum"])
        job = schema["properties"]["jobs"]["items"]["properties"]
        self.assertEqual(job["task_id"]["type"], "null")
        self.assertEqual(job["output_path"]["type"], "null")

    def streaming_batch(self):
        batch = self.prepare()
        self.upgrade_bindings_to_schema_v3(batch)
        (batch / "requirements.txt").write_text(
            "默认：替换产品，重点保留动作和接触关系\n",
            encoding="utf-8",
        )
        ready = loop.move_batch(batch, self.root, "ready")
        self.freeze_source_evidence(ready)
        flow = loop.inspect_streaming_flow(
            ready,
            self.root,
            self.project,
            minimum_free_bytes=0,
        )
        return ready, flow

    def streaming_batch_with_reference(self, name="batch-with-reference"):
        batch = self.prepare(name)
        reference = batch / "replacements" / "target-interior.png"
        reference.write_bytes(b"reference-image-fixture")
        loop.atomic_write_json(
            batch / "reference-index.json", loop.build_reference_index(batch)
        )
        bindings = json.loads((batch / "job-bindings.json").read_text())
        bindings["jobs"][0]["references"] = [
            {
                "relative_path": "replacements/target-interior.png",
                "semantic_name": "目标车内饰",
            }
        ]
        bindings["schema_version"] = 3
        bindings["backend_profile"] = "dreamina_cli_seedance_2_5"
        loop.atomic_write_json(batch / "job-bindings.json", bindings)
        (batch / "requirements.txt").write_text(
            "默认：保持源片动作\nV001：将内饰替换为@图片1\n",
            encoding="utf-8",
        )
        ready = loop.move_batch(batch, self.root, "ready")
        self.freeze_source_evidence(ready)
        flow = loop.inspect_streaming_flow(
            ready,
            self.root,
            self.project,
            minimum_free_bytes=0,
        )
        return ready, flow

    @staticmethod
    def upgrade_bindings_to_schema_v3(batch):
        bindings = json.loads(
            (batch / "job-bindings.json").read_text(encoding="utf-8")
        )
        bindings["schema_version"] = 3
        bindings["backend_profile"] = "dreamina_cli_seedance_2_5"
        loop.atomic_write_json(batch / "job-bindings.json", bindings)

    def test_schema_v3_profile_must_match_live_setup_profile(self):
        batch = self.prepare("profile-mismatch")
        self.upgrade_bindings_to_schema_v3(batch)
        index, reference_index = loop.verify_batch_integrity(batch)
        with mock.patch.dict(
            os.environ,
            {"VIDEO_REPLACER_ACTIVE_PROFILE": "dreamina_cli_seedance_2_0"},
            clear=False,
        ):
            with self.assertRaisesRegex(loop.LoopError, "setup profile"):
                loop.validate_job_bindings(batch, index, reference_index)

    def test_repository_runtime_schema_v3_requires_ready_launcher(self):
        project = self.temp / "standalone-repository"
        runtime = project / "workspace" / "video-loop"
        loop.ensure_layout(runtime)
        inbox = runtime / "inbox" / "missing-ready-launcher"
        videos = inbox / "videos"
        videos.mkdir(parents=True)
        (videos / "source.mp4").write_bytes(b"fixture")
        batch = loop.prepare_inbox_batch(inbox, runtime)
        loop.atomic_write_json(
            batch / "reference-index.json", loop.build_reference_index(batch)
        )
        index = loop.read_index(batch)
        loop.atomic_write_json(
            batch / "job-bindings.json",
            {
                "schema_version": 3,
                "batch_id": batch.name,
                "backend_profile": "dreamina_cli_seedance_2_5",
                "jobs": [
                    {
                        "id": job["id"],
                        "privacy_mode": "none",
                        "references": [],
                    }
                    for job in index["jobs"]
                ],
            },
        )
        index, reference_index = loop.verify_batch_integrity(batch)
        with mock.patch.object(loop, "PROJECT_ROOT", project), mock.patch.dict(
            os.environ, {"VIDEO_REPLACER_ACTIVE_PROFILE": ""}, clear=False
        ):
            with self.assertRaisesRegex(loop.LoopError, "READY Gate"):
                loop.validate_job_bindings(batch, index, reference_index)

    def schema_v3_ready_batch(self, name, *, skip_second=False):
        batch = self.prepare(name)
        self.upgrade_bindings_to_schema_v3(batch)
        requirements = (
            "V001：替换产品\nV002：跳过\n"
            if skip_second
            else "默认：替换产品\n"
        )
        (batch / "requirements.txt").write_text(requirements, encoding="utf-8")
        return loop.move_batch(batch, self.root, "ready")

    def write_retired_schema_v3_flow(self, batch):
        self.freeze_source_evidence(batch)
        current = loop.inspect_streaming_flow(
            batch,
            self.root,
            self.project,
            minimum_free_bytes=0,
        )
        index, reference_index = loop.verify_batch_integrity(batch)
        retired_identity = loop._streaming_flow_identity(
            batch, self.project, index, reference_index
        )
        self.assertEqual(
            retired_identity["prompt_pipeline"],
            "video-to-prompt-v3-parent-composed",
        )
        retired_identity["prompt_pipeline"] = "video-to-prompt-v1"
        retired_identity["video_to_prompt_contract_sha256"] = "e" * 64
        retired = dict(current)
        retired["prompt_pipeline"] = "video-to-prompt-v1"
        retired["video_to_prompt_contract_sha256"] = "e" * 64
        retired["flow_fingerprint"] = loop._streaming_flow_fingerprint(
            retired_identity
        )
        loop.atomic_write_json(batch / "streaming-flow.json", retired)
        state = json.loads((batch / "loop-state.json").read_text(encoding="utf-8"))
        state["flow_fingerprint"] = retired["flow_fingerprint"]
        loop.atomic_write_json(batch / "loop-state.json", state)
        return retired

    @staticmethod
    def freeze_legacy_flow(batch):
        loop.atomic_write_json(
            batch / "streaming-flow.json",
            {
                "schema_version": 1,
                "batch_id": batch.name,
                "job_bindings_sha256": loop.sha256_file(batch / "job-bindings.json"),
                "flow_fingerprint": "f" * 64,
            },
        )

    def legacy_schema_ready_batch(
        self, name, binding_schema_version, *, skip_second=False
    ):
        batch = self.prepare(name)
        bindings = json.loads(
            (batch / "job-bindings.json").read_text(encoding="utf-8")
        )
        bindings["schema_version"] = binding_schema_version
        if binding_schema_version == 1:
            for job in bindings["jobs"]:
                job.pop("privacy_mode")
        loop.atomic_write_json(batch / "job-bindings.json", bindings)
        requirements = (
            "V001：替换产品\nV002：跳过\n"
            if skip_second
            else "默认：替换产品\n"
        )
        (batch / "requirements.txt").write_text(requirements, encoding="utf-8")
        ready = loop.move_batch(batch, self.root, "ready")

        # schema v1 validates only as an already-frozen batch. Seed the old
        # envelope first, then replace it with the exact historical identity.
        self.freeze_legacy_flow(ready)
        index, reference_index = loop.verify_batch_integrity(ready)
        identity = loop._streaming_flow_identity(
            ready, self.project, index, reference_index
        )
        skipped = loop.explicitly_skipped_ids(ready)
        jobs = [
            {
                "id": str(job["id"]),
                "filename": str(job["filename"]),
                "skipped": str(job["id"]) in skipped,
            }
            for job in index["jobs"]
        ]
        flow = {
            **identity,
            "flow_fingerprint": loop._streaming_flow_fingerprint(identity),
            "preparation_only": False,
            "created_at": loop.utc_now(),
            "jobs": jobs,
        }
        loop.atomic_write_json(ready / "streaming-flow.json", flow)
        return ready, flow

    def write_complete_n1_fixture(self, batch, prompt_text):
        output_dir = loop.job_output_dir(self.project, batch, "V001")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / loop.PROMPT_FILENAME).write_text(
            prompt_text, encoding="utf-8"
        )
        return (
            {
                "batch_status": "COMPLETE",
                "summary": "ready",
                "jobs": [
                    {
                        "id": "V001",
                        "status": "COMPLETE",
                        "blocker": None,
                        "prompt": prompt_text,
                    }
                ],
            },
            output_dir,
        )

    def write_streaming_result(self, batch, flow, stage, job):
        path = loop._streaming_job_result_path(batch, stage, job["id"])
        loop.atomic_write_json(
            path,
            {
                "schema_version": 1,
                "batch_id": batch.name,
                "job_id": job["id"],
                "flow_fingerprint": flow["flow_fingerprint"],
                "updated_at": loop.utc_now(),
                "job": job,
            },
        )

    def write_payment_checkpoint(
        self,
        batch,
        flow,
        *,
        token="a" * 64,
        parent_pid=None,
        authorized_at=None,
        expires_at=None,
    ):
        authorized_at = authorized_at or datetime.now(timezone.utc)
        expires_at = expires_at or (
            authorized_at
            + timedelta(seconds=loop.PAYMENT_CHECKPOINT_TTL_SECONDS)
        )
        jobs = flow.get("jobs", [])
        checkpoint = {
            "schema_version": 2,
            "batch_id": batch.name,
            "flow_fingerprint": flow["flow_fingerprint"],
            "planned_paid_tasks": sum(
                item.get("skipped") is not True
                for item in jobs
                if isinstance(item, dict)
            ),
            "explicit_payment_approval_received": True,
            "authorization_scope": "current-batch",
            "orchestrator_pid": parent_pid or os.getppid(),
            "authorization_nonce": "b" * 32,
            "authorized_at": authorized_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        checkpoint["authorization_binding_sha256"] = (
            loop.payment_authorization_binding(token, checkpoint)
        )
        loop.atomic_write_json(batch / "payment-checkpoint.json", checkpoint)
        return token, checkpoint

    def test_scoped_child_result_accepts_exactly_one_worker_job(self):
        batch = self.prepare()
        result = {
            "batch_status": "READY_FOR_SUBMISSION",
            "summary": "V001 ready",
            "jobs": [
                {
                    "id": "V001",
                    "status": "READY_FOR_SUBMISSION",
                    "task_id": None,
                    "output_path": None,
                    "blocker": None,
                }
            ],
        }
        normalized = loop.validate_child_result(
            batch, result, expected_job_ids=["V001"]
        )
        self.assertEqual([job["id"] for job in normalized["jobs"]], ["V001"])
        with self.assertRaisesRegex(loop.LoopError, "范围外"):
            loop.validate_child_result(
                batch,
                {
                    **result,
                    "jobs": [
                        {
                            "id": "V002",
                            "status": "READY_FOR_SUBMISSION",
                            "task_id": None,
                            "output_path": None,
                            "blocker": None,
                        }
                    ],
                },
                expected_job_ids=["V001"],
            )

    def test_streaming_inspection_binds_all_input_hashes(self):
        batch, flow = self.streaming_batch()
        self.assertEqual([job["id"] for job in flow["jobs"]], ["V001", "V002"])
        self.assertRegex(flow["flow_fingerprint"], r"^[0-9a-f]{64}$")
        (batch / "requirements.txt").write_text(
            "默认：替换人物\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(loop.LoopError, "输入哈希已变化"):
            loop.verify_streaming_flow(batch)

    def test_frozen_flow_rejects_any_job_manifest_tampering(self):
        batch, flow = self.streaming_batch()
        self.assertEqual(
            flow["flow_jobs_sha256"],
            loop._streaming_flow_jobs_sha256(flow["jobs"]),
        )
        flow_path = batch / "streaming-flow.json"
        original = flow_path.read_text(encoding="utf-8")
        mutations = {
            "reordered": lambda jobs: jobs.reverse(),
            "duplicated": lambda jobs: jobs.__setitem__(
                slice(None), [jobs[0], jobs[0]]
            ),
            "filename": lambda jobs: jobs[0].__setitem__("filename", "other.mp4"),
            "skipped": lambda jobs: jobs[0].__setitem__("skipped", True),
            "extra-field": lambda jobs: jobs[0].__setitem__("extra", "tampered"),
        }
        for entrypoint in ("inspect", "verify"):
            for label, mutate in mutations.items():
                with self.subTest(entrypoint=entrypoint, mutation=label):
                    candidate = json.loads(original)
                    mutate(candidate["jobs"])
                    loop.atomic_write_json(flow_path, candidate)
                    tampered = flow_path.read_text(encoding="utf-8")
                    with self.assertRaisesRegex(loop.LoopError, "冻结 Job 清单"):
                        if entrypoint == "inspect":
                            loop.inspect_streaming_flow(
                                batch,
                                self.root,
                                self.project,
                                minimum_free_bytes=0,
                            )
                        else:
                            loop.verify_streaming_flow(batch, self.project)
                    self.assertEqual(flow_path.read_text(encoding="utf-8"), tampered)
        loop.atomic_write_text(flow_path, original)

    def test_streaming_flow_rejects_sampler_or_ffmpeg_identity_change(self):
        batch, _flow = self.streaming_batch()
        with mock.patch.object(
            loop, "VIDEO_PROMPT_SAMPLER_RECIPE", "changed-sampler-recipe"
        ):
            with self.assertRaises(loop.LoopError):
                loop.verify_streaming_flow(batch, self.project)

        different_tool = self.temp / "different-ffmpeg"
        different_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        different_tool.chmod(0o755)
        with mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=different_tool
        ):
            with self.assertRaisesRegex(loop.LoopError, "工具不匹配"):
                loop.verify_streaming_flow(batch, self.project)

    def test_new_schema_v3_flow_binds_the_parent_composed_prompt_pipeline(self):
        batch = self.schema_v3_ready_batch("schema-v3-new-prompt-pipeline")
        self.freeze_source_evidence(batch)
        index, reference_index = loop.verify_batch_integrity(batch)
        identity = loop._streaming_flow_identity(
            batch, self.project, index, reference_index
        )

        self.assertEqual(
            identity["prompt_pipeline"], "video-to-prompt-v3-parent-composed"
        )
        self.assertEqual(identity["video_to_prompt_model"], "gpt-5.6-terra")
        self.assertEqual(
            identity["video_to_prompt_contract_sha256"],
            loop.sha256_file(loop.node_contract_path("video-to-prompt.md")),
        )
        self.assertEqual(
            identity["source_evidence_index_sha256"],
            loop.sha256_file(batch / loop.SOURCE_EVIDENCE_INDEX_FILENAME),
        )
        self.assertEqual(
            identity["source_evidence_sampler_recipe"],
            loop.VIDEO_PROMPT_SAMPLER_RECIPE,
        )
        expected_fingerprint = loop._streaming_flow_fingerprint(identity)
        flow = loop.inspect_streaming_flow(
            batch,
            self.root,
            self.project,
            minimum_free_bytes=0,
        )
        persisted = json.loads(
            (batch / "streaming-flow.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            flow["prompt_pipeline"], "video-to-prompt-v3-parent-composed"
        )
        self.assertEqual(flow["video_to_prompt_model"], "gpt-5.6-terra")
        self.assertEqual(
            flow["video_to_prompt_contract_sha256"],
            identity["video_to_prompt_contract_sha256"],
        )
        self.assertEqual(flow["flow_fingerprint"], expected_fingerprint)
        self.assertEqual(
            persisted["prompt_pipeline"], "video-to-prompt-v3-parent-composed"
        )
        self.assertEqual(
            loop.verify_streaming_flow(batch)["flow_fingerprint"],
            expected_fingerprint,
        )

    def test_schema_v3_flow_rejects_unknown_prompt_pipeline_or_contract_hash(self):
        mutations = {
            "unknown-pipeline-marker": {
                "prompt_pipeline": "video-to-prompt-unknown",
            },
            "changed-contract-sha": {
                "video_to_prompt_contract_sha256": "f" * 64,
            },
        }
        for mutation_name, mutation in mutations.items():
            for entrypoint in ("inspect", "verify"):
                with self.subTest(
                    mutation=mutation_name,
                    entrypoint=entrypoint,
                ):
                    batch = self.schema_v3_ready_batch(
                        f"schema-v3-{mutation_name}-{entrypoint}"
                    )
                    self.freeze_source_evidence(batch)
                    flow = loop.inspect_streaming_flow(
                        batch,
                        self.root,
                        self.project,
                        minimum_free_bytes=0,
                    )
                    mutated_identity = {
                        key: flow[key]
                        for key in (
                            "schema_version",
                            "batch_id",
                            "requirements_sha256",
                            "batch_index_sha256",
                            "reference_index_sha256",
                            "job_bindings_sha256",
                            "job_ids",
                            "reference_ids",
                            "privacy_modes",
                            "prompt_pipeline",
                            "video_to_prompt_model",
                            "video_to_prompt_contract_sha256",
                            "backend_profile",
                            "backend_profile_constraints_sha256",
                        )
                        if key in flow
                    }
                    mutated_identity.update(mutation)
                    flow.update(mutation)
                    flow["flow_fingerprint"] = (
                        loop._streaming_flow_fingerprint(mutated_identity)
                    )
                    loop.atomic_write_json(batch / "streaming-flow.json", flow)

                    with self.assertRaisesRegex(
                        loop.LoopError, "未知或已退役的提示词管线"
                    ):
                        if entrypoint == "inspect":
                            loop.inspect_streaming_flow(
                                batch,
                                self.root,
                                self.project,
                                minimum_free_bytes=0,
                            )
                        else:
                            loop.verify_streaming_flow(batch, self.project)

    def test_unfinished_retired_schema_v3_flow_cannot_mix_prompt_pipelines(self):
        for preparation_state in ("none", "partial"):
            for entrypoint in ("inspect", "verify"):
                with self.subTest(
                    preparation_state=preparation_state,
                    entrypoint=entrypoint,
                ):
                    batch = self.schema_v3_ready_batch(
                        f"retired-v3-{preparation_state}-{entrypoint}"
                    )
                    retired = self.write_retired_schema_v3_flow(batch)
                    if preparation_state == "partial":
                        self.write_streaming_result(
                            batch,
                            retired,
                            "preparation",
                            {
                                "id": "V001",
                                "status": "READY_FOR_SUBMISSION",
                                "task_id": None,
                                "output_path": None,
                                "blocker": None,
                            },
                        )
                    original_flow = (batch / "streaming-flow.json").read_text(
                        encoding="utf-8"
                    )

                    with self.assertRaises(loop.LoopError):
                        if entrypoint == "inspect":
                            loop.inspect_streaming_flow(
                                batch,
                                self.root,
                                self.project,
                                minimum_free_bytes=0,
                            )
                        else:
                            loop.verify_streaming_flow(batch, self.project)

                    self.assertEqual(
                        (batch / "streaming-flow.json").read_text(encoding="utf-8"),
                        original_flow,
                    )

    def test_submission_preflight_validates_every_eligible_plan_before_ready(self):
        flow = {
            "schema_version": 1,
            "batch_id": "preflight-all-eligible",
            "flow_fingerprint": "a" * 64,
            "jobs": [
                {"id": "V001", "skipped": False},
                {"id": "V002", "skipped": True},
                {"id": "V003", "skipped": False},
            ],
        }
        batch = self.root / "ready" / flow["batch_id"]
        batch.mkdir(parents=True)

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop,
            "_read_streaming_job_result",
            side_effect=lambda _batch, _stage, job_id, _fingerprint: {
                "job": {"id": job_id, "status": loop.PREPARED_STATUS}
            },
        ), mock.patch.object(
            loop, "load_submission_plan", return_value={}
        ) as load_plan:
            result = loop.preflight_streaming_submission(
                batch,
                self.project,
                max_batch_videos=7,
            )

        self.assertTrue(result["ready_for_paid_submission"])
        self.assertEqual(result["eligible_job_ids"], ["V001", "V003"])
        self.assertEqual(
            [call.args[2] for call in load_plan.call_args_list],
            ["V001", "V003"],
        )
        for call in load_plan.call_args_list:
            self.assertEqual(call.kwargs["max_batch_videos"], 7)

    def test_partial_local_preparation_is_blocked_before_payment_approval(self):
        batch = self.root / "needs-input" / "partial-local-preparation"
        batch.mkdir(parents=True)
        flow = {
            "flow_fingerprint": "f" * 64,
            "jobs": [{"id": "V001"}, {"id": "V002"}],
        }
        jobs = [
            {"id": "V001", "status": loop.PREPARED_STATUS},
            {"id": "V002", "status": "BLOCKED", "blocker": "retry me"},
        ]

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop, "_collect_streaming_preparation", return_value=jobs
        ):
            result = loop.finalize_streaming_preparation(batch, self.project)

        self.assertEqual(result["batch_status"], "PARTIAL")
        self.assertEqual(result["workflow_state"], "LOCAL_PREPARATION_BLOCKED")
        self.assertFalse(result["payment_approval_required"])
        state = json.loads((batch / "loop-state.json").read_text())
        self.assertEqual(state["state"], "LOCAL_PREPARATION_BLOCKED")
        self.assertFalse(state["payment_approval_required"])

    def test_submission_preflight_blocks_if_any_eligible_plan_is_invalid(self):
        flow = {
            "schema_version": 1,
            "batch_id": "preflight-invalid-plan",
            "flow_fingerprint": "b" * 64,
            "jobs": [
                {"id": "V001", "skipped": False},
                {"id": "V002", "skipped": False},
                {"id": "V003", "skipped": False},
            ],
        }
        batch = self.root / "ready" / flow["batch_id"]
        batch.mkdir(parents=True)

        def load_plan(_batch, _project_root, job_id, **_kwargs):
            if job_id == "V002":
                raise loop.LoopError("V002 submission plan 无效")
            return {}

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop,
            "_read_streaming_job_result",
            side_effect=lambda _batch, _stage, job_id, _fingerprint: {
                "job": {"id": job_id, "status": loop.PREPARED_STATUS}
            },
        ), mock.patch.object(
            loop, "load_submission_plan", side_effect=load_plan
        ) as mocked_load_plan:
            with self.assertRaisesRegex(loop.LoopError, "V002.*无效"):
                loop.preflight_streaming_submission(batch, self.project)

        self.assertEqual(
            [call.args[2] for call in mocked_load_plan.call_args_list],
            ["V001", "V002", "V003"],
        )

    def test_stale_ready_plan_is_demoted_and_payment_state_is_cleared(self):
        batch, flow = self.streaming_batch()
        ready_job = {
            "id": "V001",
            "status": loop.PREPARED_STATUS,
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
        self.write_streaming_result(batch, flow, "preparation", ready_job)
        loop.atomic_write_json(
            batch / "loop-state.json",
            {
                "batch_id": batch.name,
                "state": "LOCAL_PREPARED_AWAITING_APPROVAL",
                "flow_fingerprint": flow["flow_fingerprint"],
                "payment_approval_required": True,
            },
        )

        with mock.patch.object(
            loop,
            "load_submission_plan",
            side_effect=loop.LoopError("submission plan missing"),
        ), mock.patch.object(loop, "run_video_to_prompt_node") as node:
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "BLOCKED")
        self.assertIn("submission plan missing", result["job"]["blocker"])
        self.assertEqual(
            result["invalidated_prepared_job"]["status"], loop.PREPARED_STATUS
        )
        persisted = json.loads(
            loop._streaming_job_result_path(
                batch, "preparation", "V001"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["job"]["status"], "BLOCKED")
        state = json.loads((batch / "loop-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "LOCAL_PREPARATION_BLOCKED")
        self.assertFalse(state["payment_approval_required"])
        self.assertFalse((batch / "payment-checkpoint.json").exists())
        node.assert_not_called()

    def test_preflight_plan_race_demotes_ready_result_and_finalizes_blocked(self):
        batch, flow = self.streaming_batch()
        for job_id in ("V001", "V002"):
            self.write_streaming_result(
                batch,
                flow,
                "preparation",
                {
                    "id": job_id,
                    "status": loop.PREPARED_STATUS,
                    "task_id": None,
                    "output_path": None,
                    "blocker": None,
                },
            )
        loop.atomic_write_json(
            batch / "loop-state.json",
            {
                "batch_id": batch.name,
                "state": "LOCAL_PREPARED_AWAITING_APPROVAL",
                "flow_fingerprint": flow["flow_fingerprint"],
                "payment_approval_required": True,
            },
        )

        def load_plan(_batch, _project_root, job_id, **_kwargs):
            if job_id == "V001":
                raise loop.LoopError("submission plan changed after preparation")
            return {}

        with mock.patch.object(loop, "load_submission_plan", side_effect=load_plan):
            with self.assertRaisesRegex(loop.LoopError, "V001.*计划复核失败"):
                loop.preflight_streaming_submission(batch, self.project)

        persisted = json.loads(
            loop._streaming_job_result_path(
                batch, "preparation", "V001"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["job"]["status"], "BLOCKED")
        state = json.loads((batch / "loop-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "LOCAL_PREPARATION_BLOCKED")
        self.assertFalse(state["payment_approval_required"])
        self.assertFalse((batch / "payment-checkpoint.json").exists())

    def test_submission_preflight_rejects_stale_plan_when_canonical_preparation_is_blocked(self):
        flow = {
            "schema_version": 1,
            "batch_id": "preflight-stale-plan",
            "flow_fingerprint": "c" * 64,
            "jobs": [{"id": "V001", "skipped": False}],
        }
        batch = self.root / "ready" / flow["batch_id"]
        batch.mkdir(parents=True)
        blocked = {
            "schema_version": 1,
            "batch_id": batch.name,
            "job_id": "V001",
            "flow_fingerprint": flow["flow_fingerprint"],
            "updated_at": loop.utc_now(),
            "job": {
                "id": "V001",
                "status": "BLOCKED",
                "task_id": None,
                "output_path": None,
                "blocker": "latest attempt failed",
            },
        }

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop, "_read_streaming_job_result", return_value=blocked
        ), mock.patch.object(
            loop, "load_submission_plan", return_value={}
        ) as load_plan:
            with self.assertRaisesRegex(loop.LoopError, "V001.*BLOCKED"):
                loop.preflight_streaming_submission(batch, self.project)

        load_plan.assert_not_called()

    def test_retry_prepare_archives_blocked_attempt_and_reruns_only_target(self):
        batch = self.prepare("retry-local-blocked")
        self.upgrade_bindings_to_schema_v3(batch)
        (batch / "requirements.txt").write_text(
            "默认：替换产品\n", encoding="utf-8"
        )
        (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")
        self.freeze_source_evidence(batch)
        flow = loop.inspect_streaming_flow(
            batch,
            self.root,
            self.project,
            preparation_only=True,
            minimum_free_bytes=0,
        )
        old_result = {
            "schema_version": 1,
            "batch_id": batch.name,
            "job_id": "V001",
            "flow_fingerprint": flow["flow_fingerprint"],
            "updated_at": loop.utc_now(),
            "job": {
                "id": "V001",
                "status": "BLOCKED",
                "task_id": None,
                "output_path": None,
                "blocker": "transient local failure",
            },
        }
        result_path = loop._streaming_job_result_path(
            batch, "preparation", "V001"
        )
        loop.atomic_write_json(result_path, old_result)
        retried = dict(old_result)
        retried["job"] = dict(
            old_result["job"], status=loop.PREPARED_STATUS, blocker=None
        )

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop, "recorded_task_id", return_value=None
        ), mock.patch.object(
            loop, "prepare_streaming_job", return_value=retried
        ) as prepare_job:
            result = loop.retry_streaming_preparation_job(
                batch, self.root, self.project, "V001"
            )

        self.assertEqual(result["job"]["status"], loop.PREPARED_STATUS)
        prepare_job.assert_called_once()
        self.assertTrue(result_path.is_file())
        self.assertEqual(json.loads(result_path.read_text()), retried)
        history = list(
            (batch / "streaming-results" / "preparation-history" / "V001").glob(
                "attempt-*.json"
            )
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text()), old_result)

    def test_retry_prepare_restores_canonical_result_when_rerun_raises(self):
        batch = self.root / "needs-input" / "retry-transaction-restore"
        batch.mkdir(parents=True)
        (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")
        flow = {
            "schema_version": 1,
            "batch_id": batch.name,
            "flow_fingerprint": "a" * 64,
            "jobs": [{"id": "V001", "skipped": False}],
        }
        old_result = {
            "schema_version": 1,
            "batch_id": batch.name,
            "job_id": "V001",
            "flow_fingerprint": flow["flow_fingerprint"],
            "updated_at": loop.utc_now(),
            "job": {
                "id": "V001",
                "status": "BLOCKED",
                "task_id": None,
                "output_path": None,
                "blocker": "transient local failure",
            },
        }
        result_path = loop._streaming_job_result_path(
            batch, "preparation", "V001"
        )
        loop.atomic_write_json(result_path, old_result)

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop, "recorded_task_id", return_value=None
        ), mock.patch.object(
            loop,
            "prepare_streaming_job",
            side_effect=RuntimeError("unexpected rerun crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected rerun crash"):
                loop.retry_streaming_preparation_job(
                    batch, self.root, self.project, "V001"
                )

        self.assertEqual(json.loads(result_path.read_text()), old_result)
        history = list(
            (batch / "streaming-results" / "preparation-history" / "V001").glob(
                "attempt-*.json"
            )
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text()), old_result)

    def test_retry_prepare_can_rebuild_ready_job_with_invalid_plan(self):
        batch = self.root / "needs-input" / "retry-stale-ready-plan"
        batch.mkdir(parents=True)
        (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")
        flow = {
            "schema_version": 1,
            "batch_id": batch.name,
            "flow_fingerprint": "b" * 64,
            "jobs": [{"id": "V001", "skipped": False}],
        }
        ready = {
            "schema_version": 1,
            "batch_id": batch.name,
            "job_id": "V001",
            "flow_fingerprint": flow["flow_fingerprint"],
            "updated_at": loop.utc_now(),
            "job": {
                "id": "V001",
                "status": loop.PREPARED_STATUS,
                "task_id": None,
                "output_path": None,
                "blocker": None,
            },
        }
        result_path = loop._streaming_job_result_path(
            batch, "preparation", "V001"
        )
        loop.atomic_write_json(result_path, ready)
        rebuilt = dict(ready)

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(
            loop,
            "load_submission_plan",
            side_effect=loop.LoopError("submission plan missing"),
        ), mock.patch.object(
            loop, "recorded_task_id", return_value=None
        ), mock.patch.object(
            loop, "prepare_streaming_job", return_value=rebuilt
        ) as prepare_job:
            result = loop.retry_streaming_preparation_job(
                batch, self.root, self.project, "V001"
            )

        self.assertEqual(result, rebuilt)
        prepare_job.assert_called_once()
        self.assertTrue(result_path.is_file())
        history = list(
            (batch / "streaming-results" / "preparation-history" / "V001").glob(
                "attempt-*.json"
            )
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text()), ready)

    def test_retry_prepare_rejects_ready_or_paid_evidence(self):
        batch = self.root / "needs-input" / "retry-refusals"
        batch.mkdir(parents=True)
        (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")
        flow = {
            "schema_version": 1,
            "batch_id": batch.name,
            "flow_fingerprint": "d" * 64,
            "jobs": [{"id": "V001", "skipped": False}],
        }
        result_path = loop._streaming_job_result_path(
            batch, "preparation", "V001"
        )

        def write_status(status):
            loop.atomic_write_json(
                result_path,
                {
                    "schema_version": 1,
                    "batch_id": batch.name,
                    "job_id": "V001",
                    "flow_fingerprint": flow["flow_fingerprint"],
                    "updated_at": loop.utc_now(),
                    "job": {
                        "id": "V001",
                        "status": status,
                        "task_id": None,
                        "output_path": None,
                        "blocker": "failed" if status == "BLOCKED" else None,
                    },
                },
            )

        with mock.patch.object(
            loop, "verify_streaming_flow", return_value=flow
        ), mock.patch.object(loop, "load_submission_plan", return_value={}):
            write_status(loop.PREPARED_STATUS)
            with self.assertRaisesRegex(loop.LoopError, "提交计划有效"):
                loop.retry_streaming_preparation_job(
                    batch, self.root, self.project, "V001"
                )

            write_status("BLOCKED")
            submission = batch / "streaming-results" / "submission" / "V001.json"
            submission.parent.mkdir(parents=True)
            submission.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                loop, "recorded_task_id", return_value=None
            ), self.assertRaisesRegex(loop.LoopError, "提交、付费授权或远端任务"):
                loop.retry_streaming_preparation_job(
                    batch, self.root, self.project, "V001"
                )

        self.assertTrue(result_path.is_file())

    def test_retry_prepare_rejects_paid_evidence_for_any_flow_job(self):
        flow = {
            "schema_version": 1,
            "flow_fingerprint": "e" * 64,
            "jobs": [
                {"id": "V001", "skipped": False},
                {"id": "V002", "skipped": False},
            ],
        }

        for kind in ("submission", "checkpoint", "task"):
            with self.subTest(kind=kind):
                batch = self.root / "needs-input" / f"retry-paid-{kind}"
                batch.mkdir(parents=True)
                (batch / "PAUSE").write_text("local-only\n", encoding="utf-8")
                flow["batch_id"] = batch.name
                result_path = loop._streaming_job_result_path(
                    batch, "preparation", "V001"
                )
                loop.atomic_write_json(
                    result_path,
                    {
                        "schema_version": 1,
                        "batch_id": batch.name,
                        "job_id": "V001",
                        "flow_fingerprint": flow["flow_fingerprint"],
                        "updated_at": loop.utc_now(),
                        "job": {
                            "id": "V001",
                            "status": "BLOCKED",
                            "task_id": None,
                            "output_path": None,
                            "blocker": "local failure",
                        },
                    },
                )
                if kind == "submission":
                    submission = (
                        batch / "streaming-results" / "submission" / "V002.json"
                    )
                    submission.parent.mkdir(parents=True)
                    submission.write_text("{}\n", encoding="utf-8")
                elif kind == "checkpoint":
                    (batch / "payment-checkpoint-V002.json").write_text(
                        "{}\n", encoding="utf-8"
                    )

                def recorded_task(
                    _project_root, _batch_name, candidate_job_id
                ):
                    if kind == "task" and candidate_job_id == "V002":
                        return "remote-v002"
                    return None

                with mock.patch.object(
                    loop, "verify_streaming_flow", return_value=flow
                ), mock.patch.object(
                    loop, "recorded_task_id", side_effect=recorded_task
                ), mock.patch.object(
                    loop, "prepare_streaming_job"
                ) as prepare_job, self.assertRaisesRegex(
                    loop.LoopError, "提交、付费授权或远端任务"
                ):
                    loop.retry_streaming_preparation_job(
                        batch, self.root, self.project, "V001"
                    )

                prepare_job.assert_not_called()
                self.assertFalse(
                    (
                        batch
                        / "streaming-results"
                        / "preparation-history"
                        / "V001"
                    ).exists()
                )

    def test_fully_prepared_retired_schema_v3_flow_is_frozen_and_reused(self):
        batch = self.schema_v3_ready_batch(
            "retired-v3-fully-prepared", skip_second=True
        )
        retired = self.write_retired_schema_v3_flow(batch)
        self.write_streaming_result(
            batch,
            retired,
            "preparation",
            {
                "id": "V001",
                "status": "READY_FOR_SUBMISSION",
                "task_id": None,
                "output_path": None,
                "blocker": None,
            },
        )
        self.write_streaming_result(
            batch,
            retired,
            "preparation",
            {
                "id": "V002",
                "status": "SKIPPED",
                "task_id": None,
                "output_path": None,
                "blocker": None,
            },
        )
        original_flow = json.loads(
            (batch / "streaming-flow.json").read_text(encoding="utf-8")
        )

        with mock.patch.object(loop, "load_submission_plan", return_value={}):
            inspected = loop.inspect_streaming_flow(
                batch,
                self.root,
                self.project,
                minimum_free_bytes=0,
            )
            verified = loop.verify_streaming_flow(batch, self.project)
        persisted = json.loads(
            (batch / "streaming-flow.json").read_text(encoding="utf-8")
        )

        self.assertEqual(inspected, original_flow)
        self.assertEqual(verified, original_flow)
        self.assertEqual(persisted, original_flow)
        self.assertEqual(persisted["prompt_pipeline"], "video-to-prompt-v1")
        self.assertEqual(
            persisted["flow_fingerprint"], retired["flow_fingerprint"]
        )

        with mock.patch.object(
            loop, "run_video_to_prompt_node"
        ) as video_to_prompt, mock.patch.object(
            loop, "load_submission_plan", return_value={}
        ) as load_plan:
            prepared = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )
            skipped = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V002",
                executor=self.executor,
            )

        self.assertEqual(prepared["job"]["status"], "READY_FOR_SUBMISSION")
        self.assertEqual(skipped["job"]["status"], "SKIPPED")
        self.assertGreaterEqual(load_plan.call_count, 1)
        video_to_prompt.assert_not_called()

    def test_unfinished_frozen_schema_v1_v2_flow_never_enters_new_prompt_node(self):
        for binding_schema_version in (1, 2):
            for preparation_state in ("none", "partial"):
                for entrypoint in ("inspect", "verify", "prepare"):
                    with self.subTest(
                        binding_schema_version=binding_schema_version,
                        preparation_state=preparation_state,
                        entrypoint=entrypoint,
                    ):
                        batch, frozen = self.legacy_schema_ready_batch(
                            "legacy-v"
                            f"{binding_schema_version}-{preparation_state}-{entrypoint}",
                            binding_schema_version,
                        )
                        if preparation_state == "partial":
                            self.write_streaming_result(
                                batch,
                                frozen,
                                "preparation",
                                {
                                    "id": "V001",
                                    "status": "READY_FOR_SUBMISSION",
                                    "task_id": None,
                                    "output_path": None,
                                    "blocker": None,
                                },
                            )
                        original_flow = (
                            batch / "streaming-flow.json"
                        ).read_text(encoding="utf-8")

                        with mock.patch.object(
                            loop, "run_video_to_prompt_node"
                        ) as video_to_prompt, self.assertRaises(loop.LoopError):
                            if entrypoint == "inspect":
                                loop.inspect_streaming_flow(
                                    batch,
                                    self.root,
                                    self.project,
                                    minimum_free_bytes=0,
                                )
                            elif entrypoint == "verify":
                                loop.verify_streaming_flow(batch, self.project)
                            else:
                                loop.prepare_streaming_job(
                                    batch,
                                    self.root,
                                    self.project,
                                    "V002",
                                    executor=self.executor,
                                )

                        video_to_prompt.assert_not_called()
                        self.assertEqual(
                            (batch / "streaming-flow.json").read_text(
                                encoding="utf-8"
                            ),
                            original_flow,
                        )

    def test_fully_prepared_frozen_schema_v1_v2_flow_is_reused(self):
        for binding_schema_version in (1, 2):
            with self.subTest(binding_schema_version=binding_schema_version):
                batch, frozen = self.legacy_schema_ready_batch(
                    f"legacy-v{binding_schema_version}-fully-prepared",
                    binding_schema_version,
                    skip_second=True,
                )
                self.write_streaming_result(
                    batch,
                    frozen,
                    "preparation",
                    {
                        "id": "V001",
                        "status": "READY_FOR_SUBMISSION",
                        "task_id": None,
                        "output_path": None,
                        "blocker": None,
                    },
                )
                self.write_streaming_result(
                    batch,
                    frozen,
                    "preparation",
                    {
                        "id": "V002",
                        "status": "SKIPPED",
                        "task_id": None,
                        "output_path": None,
                        "blocker": None,
                    },
                )
                self.write_valid_plan(batch, "V001")
                original_flow = json.loads(
                    (batch / "streaming-flow.json").read_text(encoding="utf-8")
                )

                inspected = loop.inspect_streaming_flow(
                    batch,
                    self.root,
                    self.project,
                    minimum_free_bytes=0,
                )
                verified = loop.verify_streaming_flow(batch, self.project)
                persisted = json.loads(
                    (batch / "streaming-flow.json").read_text(encoding="utf-8")
                )

                self.assertEqual(inspected, original_flow)
                self.assertEqual(verified, original_flow)
                self.assertEqual(persisted, original_flow)
                self.assertNotIn("prompt_pipeline", persisted)

                with mock.patch.object(
                    loop, "run_video_to_prompt_node"
                ) as video_to_prompt, mock.patch.object(
                    loop, "load_submission_plan", return_value={}
                ) as load_plan:
                    prepared = loop.prepare_streaming_job(
                        batch,
                        self.root,
                        self.project,
                        "V001",
                        executor=self.executor,
                    )
                    skipped = loop.prepare_streaming_job(
                        batch,
                        self.root,
                        self.project,
                        "V002",
                        executor=self.executor,
                    )

                self.assertEqual(
                    prepared["job"]["status"], "READY_FOR_SUBMISSION"
                )
                self.assertEqual(skipped["job"]["status"], "SKIPPED")
                self.assertGreaterEqual(load_plan.call_count, 1)
                video_to_prompt.assert_not_called()

    def test_repeated_streaming_inspection_preserves_reference_index_fingerprint(self):
        batch, flow = self.streaming_batch()
        reference_index = json.loads((batch / "reference-index.json").read_text())
        with mock.patch.object(loop, "utc_now", return_value="2099-01-01T00:00:00+00:00"):
            repeated = loop.inspect_streaming_flow(
                batch,
                self.root,
                self.project,
                minimum_free_bytes=0,
            )
        rewritten = json.loads((batch / "reference-index.json").read_text())
        self.assertEqual(repeated["flow_fingerprint"], flow["flow_fingerprint"])
        self.assertEqual(
            repeated["reference_index_sha256"], flow["reference_index_sha256"]
        )
        self.assertEqual(rewritten["created_at"], reference_index["created_at"])

    def test_reinspection_of_existing_flow_preserves_visible_batch_state(self):
        batch, flow = self.streaming_batch()
        state_path = batch / "loop-state.json"
        prepared_state = {
            "batch_id": batch.name,
            "state": "LOCAL_PREPARED_AWAITING_APPROVAL",
            "updated_at": "2099-01-01T00:00:00+00:00",
            "flow_fingerprint": flow["flow_fingerprint"],
            "payment_approval_required": True,
        }
        loop.atomic_write_json(state_path, prepared_state)
        original_bytes = state_path.read_bytes()

        inspected = loop.inspect_streaming_flow(
            batch,
            self.root,
            self.project,
            minimum_free_bytes=0,
        )

        self.assertEqual(inspected, flow)
        self.assertEqual(state_path.read_bytes(), original_bytes)

    def test_streaming_inspection_never_overwrites_changed_frozen_reference_index(self):
        batch, _flow = self.streaming_batch_with_reference(
            "frozen-reference-index-change"
        )
        index_path = batch / "reference-index.json"
        original_index = index_path.read_bytes()
        (batch / "replacements" / "target-interior.png").write_bytes(
            b"changed-reference"
        )

        with self.assertRaisesRegex(loop.LoopError, "参考素材在索引后发生变化"):
            loop.inspect_streaming_flow(
                batch,
                self.root,
                self.project,
                minimum_free_bytes=0,
            )

        self.assertEqual(index_path.read_bytes(), original_index)

    def test_prepare_streaming_job_is_scoped_and_writes_one_result(self):
        batch, flow = self.streaming_batch()
        child = {
            "batch_status": "COMPLETE",
            "summary": "ready",
            "jobs": [
                {
                    "id": "V001",
                    "status": "COMPLETE",
                    "blocker": None,
                    "prompt": "素材绑定：@视频1=原视频。\n替换人物。",
                }
            ],
        }
        output_dir = loop.job_output_dir(self.project, batch, "V001")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / loop.PROMPT_FILENAME).write_text(
            loop.compose_execution_prompt(
                batch,
                "V001",
                [],
                "素材绑定：@视频1=原视频。\n编辑要求：替换人物。\n",
            )
            + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            loop, "run_video_to_prompt_node", return_value=child
        ) as combined_run, mock.patch.object(
            loop, "select_job_reference_records", return_value=[]
        ), mock.patch.object(
            loop, "resolve_reference_binding", return_value=[]
        ), mock.patch.object(
            loop, "prepare_active_video", return_value=output_dir / "source-active.mp4"
        ), mock.patch.object(
            loop,
            "prepare_upload_for_profile",
            return_value=(
                output_dir / "source-active.mp4",
                output_dir / "upload-preparation.json",
            ),
        ), mock.patch.object(
            loop, "run_executor_probe", return_value=output_dir / "preflight.json"
        ), mock.patch.object(
            loop, "build_submission_plan", return_value={}
        ), mock.patch.object(
            loop, "execute_submission_plan"
        ) as execute:
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
        )
        self.assertEqual(result["flow_fingerprint"], flow["flow_fingerprint"])
        self.assertEqual(result["job"]["status"], "READY_FOR_SUBMISSION")
        self.assertEqual(
            combined_run.call_args.args,
            (batch, self.root, self.project, "V001", []),
        )
        execute.assert_not_called()
        self.assertTrue(
            loop._streaming_job_result_path(batch, "preparation", "V001").is_file()
        )

    def test_prepare_passes_parent_bound_references_directly_to_combined_node(self):
        batch, _flow = self.streaming_batch_with_reference()
        child = {
            "batch_status": "COMPLETE",
            "summary": "ready",
            "jobs": [
                {
                    "id": "V001",
                    "status": "COMPLETE",
                    "blocker": None,
                    "prompt": (
                        "素材绑定：@视频1=原视频；@图片1=目标车内饰。\n"
                        "将汽车内饰替换为目标车内饰。"
                    ),
                }
            ],
        }
        output_dir = loop.job_output_dir(self.project, batch, "V001")
        output_dir.mkdir(parents=True, exist_ok=True)
        references = loop.select_job_reference_records(batch, "V001")
        (output_dir / loop.PROMPT_FILENAME).write_text(
            loop.compose_execution_prompt(
                batch,
                "V001",
                references,
                "素材绑定：@视频1=原视频；@图片1=目标车内饰。\n"
                "将汽车内饰替换为目标车内饰。\n",
            )
            + "\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            loop, "run_video_to_prompt_node", return_value=child
        ) as combined_run, mock.patch.object(
            loop,
            "resolve_reference_binding",
            return_value=[batch / "replacements" / "target-interior.png"],
        ), mock.patch.object(
            loop, "validate_execution_prompt"
        ), mock.patch.object(
            loop, "prepare_active_video", return_value=output_dir / "source-active.mp4"
        ), mock.patch.object(
            loop,
            "prepare_upload_for_profile",
            return_value=(
                output_dir / "source-active.mp4",
                output_dir / "upload-preparation.json",
            ),
        ), mock.patch.object(
            loop, "run_executor_probe", return_value=output_dir / "preflight.json"
        ), mock.patch.object(
            loop, "build_submission_plan", return_value={}
        ):
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "READY_FOR_SUBMISSION")
        self.assertEqual(combined_run.call_count, 1)
        self.assertEqual(combined_run.call_args.args[-2], "V001")
        self.assertEqual(combined_run.call_args.args[-1], references)

    def test_combined_node_block_stops_before_video_preparation_and_probe(self):
        batch, _flow = self.streaming_batch_with_reference("combined-block")
        combined_blocked = {
            "batch_status": "BLOCKED",
            "summary": "media unavailable",
            "jobs": [
                {
                    "id": "V001",
                    "status": "BLOCKED",
                    "blocker": "无法确定源片动作",
                    "prompt": None,
                }
            ],
        }
        with mock.patch.object(
            loop, "run_video_to_prompt_node", return_value=combined_blocked
        ) as combined_run, mock.patch.object(
            loop, "prepare_active_video"
        ) as active_video, mock.patch.object(
            loop, "run_executor_probe"
        ) as probe:
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "BLOCKED")
        self.assertEqual(result["job"]["blocker"], "无法确定源片动作")
        combined_run.assert_called_once()
        active_video.assert_not_called()
        probe.assert_not_called()

    def test_existing_prepared_job_bypasses_combined_node(self):
        batch, flow = self.streaming_batch_with_reference("prepared-combined")
        reference = batch / "replacements" / "target-interior.png"
        self.write_valid_plan(batch, "V001", images=[reference])
        ready_job = {
            "id": "V001",
            "status": "READY_FOR_SUBMISSION",
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
        self.write_streaming_result(batch, flow, "preparation", ready_job)
        output_dir = loop.job_output_dir(self.project, batch, "V001")
        self.assertFalse((output_dir / "source-analysis.json").exists())
        self.assertFalse((output_dir / "reference-analysis.json").exists())

        with mock.patch.object(
            loop, "run_video_to_prompt_node"
        ) as combined_run, mock.patch.object(
            loop, "load_submission_plan", return_value={}
        ):
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "READY_FOR_SUBMISSION")
        combined_run.assert_not_called()

    def test_prepare_blocks_prompt_gate_error_before_probe(self):
        batch, _flow = self.streaming_batch()
        child, output_dir = self.write_complete_n1_fixture(
            batch, "素材绑定\n@视频1=原视频。\n"
        )
        gate_error = loop.PROMPT_GATE_ERROR_PREFIX + "fixture"
        events = []

        def gate(*_args, **_kwargs):
            events.append("gate")
            raise loop.LoopError(gate_error)

        with mock.patch.object(
            loop, "run_video_to_prompt_node", return_value=child
        ), mock.patch.object(
            loop, "select_job_reference_records", return_value=[]
        ), mock.patch.object(
            loop, "resolve_reference_binding", return_value=[]
        ), mock.patch.object(
            loop, "validate_execution_prompt", side_effect=gate
        ), mock.patch.object(
            loop,
            "run_executor_probe",
        ) as probe:
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "BLOCKED")
        self.assertIn(gate_error, result["job"]["blocker"])
        self.assertEqual(events, ["gate"])
        probe.assert_not_called()

    def test_prepare_blocks_non_gate_failures_before_probe(self):
        batch, _flow = self.streaming_batch()
        child, _output_dir = self.write_complete_n1_fixture(
            batch, "待检查提示词\n"
        )
        with mock.patch.object(
            loop, "run_video_to_prompt_node", return_value=child
        ), mock.patch.object(
            loop, "select_job_reference_records", return_value=[]
        ), mock.patch.object(
            loop, "resolve_reference_binding", return_value=[]
        ), mock.patch.object(
            loop,
            "validate_execution_prompt",
            side_effect=loop.LoopError("无法读取执行提示词：fixture"),
        ), mock.patch.object(
            loop, "run_executor_probe"
        ) as probe:
            result = loop.prepare_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )

        self.assertEqual(result["job"]["status"], "BLOCKED")
        self.assertIn("无法读取执行提示词", result["job"]["blocker"])
        probe.assert_not_called()

    def test_submit_streaming_job_isolates_a_job_failure(self):
        batch, flow = self.streaming_batch()
        ready_job = {
            "id": "V001",
            "status": "READY_FOR_SUBMISSION",
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
        self.write_streaming_result(batch, flow, "preparation", ready_job)
        token, _checkpoint = self.write_payment_checkpoint(batch, flow)
        with mock.patch.dict(
            os.environ, {loop.PAYMENT_AUTH_TOKEN_ENV: token}, clear=False
        ), mock.patch.object(
            loop, "execute_submission_plan", side_effect=loop.LoopError("fixture fail")
        ) as execute, mock.patch.object(loop, "recorded_task_id", return_value=None):
            result = loop.submit_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )
            self.assertNotIn(loop.PAYMENT_AUTH_TOKEN_ENV, os.environ)
        self.assertEqual(result["job"]["status"], "BLOCKED")
        self.assertIn("fixture fail", result["job"]["blocker"])
        self.assertTrue(execute.call_args.kwargs["paid_execution_authorized"])

    def test_direct_submit_job_rejects_legacy_checkpoint_before_execution(self):
        batch, flow = self.streaming_batch()
        ready_job = {
            "id": "V001",
            "status": "READY_FOR_SUBMISSION",
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
        self.write_streaming_result(batch, flow, "preparation", ready_job)
        loop.atomic_write_json(
            batch / "payment-checkpoint.json",
            {
                "schema_version": 1,
                "batch_id": batch.name,
                "flow_fingerprint": flow["flow_fingerprint"],
                "planned_paid_tasks": 2,
                "explicit_payment_approval_received": True,
                "authorization_scope": "current-js-process",
                "orchestrator_pid": os.getppid(),
            },
        )
        with mock.patch.object(loop, "execute_submission_plan") as execute, self.assertRaisesRegex(
            loop.LoopError, "payment-checkpoint"
        ):
            loop.submit_streaming_job(
                batch,
                self.root,
                self.project,
                "V001",
                executor=self.executor,
            )
        execute.assert_not_called()

    def test_handwritten_or_expired_checkpoint_cannot_replay(self):
        batch, flow = self.streaming_batch()
        token, checkpoint = self.write_payment_checkpoint(batch, flow)
        checkpoint["authorization_binding_sha256"] = "0" * 64
        loop.atomic_write_json(batch / "payment-checkpoint.json", checkpoint)
        with self.assertRaisesRegex(loop.LoopError, "capability"):
            loop._validate_streaming_payment_checkpoint(
                batch,
                flow,
                environment={loop.PAYMENT_AUTH_TOKEN_ENV: token},
                parent_pid=os.getppid(),
            )

        now = datetime.now(timezone.utc)
        token, _checkpoint = self.write_payment_checkpoint(
            batch,
            flow,
            authorized_at=now - timedelta(hours=3),
            expires_at=now - timedelta(hours=1),
        )
        with self.assertRaisesRegex(loop.LoopError, "过期"):
            loop._validate_streaming_payment_checkpoint(
                batch,
                flow,
                environment={loop.PAYMENT_AUTH_TOKEN_ENV: token},
                parent_pid=os.getppid(),
                observed_at=now,
            )

    def test_direct_submit_prepared_rejects_replayed_checkpoint(self):
        batch, flow = self.streaming_batch()
        paused = loop.move_batch(batch, self.root, "needs-input")
        (paused / "PAUSE").touch()
        loop.atomic_write_json(
            paused / "payment-checkpoint.json",
            {
                "schema_version": 1,
                "batch_id": paused.name,
                "flow_fingerprint": flow["flow_fingerprint"],
                "planned_paid_tasks": 2,
                "explicit_payment_approval_received": True,
            },
        )
        with mock.patch.object(loop, "execute_prepared_jobs") as execute, self.assertRaisesRegex(
            loop.LoopError, "payment-checkpoint"
        ):
            loop.submit_prepared_batch(
                paused,
                self.root,
                self.project,
                executor=self.executor,
            )
        execute.assert_not_called()

    def test_direct_retry_sequential_rejects_replayed_checkpoint(self):
        batch, flow = self.streaming_batch()
        blocked = loop.move_batch(batch, self.root, "blocked")
        (blocked / "PAUSE").touch()
        loop.atomic_write_json(
            blocked / "payment-checkpoint.json",
            {
                "schema_version": 1,
                "batch_id": blocked.name,
                "flow_fingerprint": flow["flow_fingerprint"],
                "planned_paid_tasks": 2,
                "explicit_payment_approval_received": True,
            },
        )
        with self.assertRaisesRegex(loop.LoopError, "payment-checkpoint"):
            loop.retry_failed_jobs_sequentially(
                blocked,
                self.root,
                self.project,
                self.temp / "missing-retry-authorization.json",
                executor=self.executor,
            )

    def test_finalize_streaming_flow_moves_partial_batch_to_blocked(self):
        batch, flow = self.streaming_batch()
        downloaded = self.temp / "V001-final.mp4"
        downloaded.write_bytes(b"downloaded")
        ready_job = {
            "id": "V001",
            "status": "READY_FOR_SUBMISSION",
            "task_id": None,
            "output_path": None,
            "blocker": None,
        }
        blocked_job = {
            "id": "V002",
            "status": "BLOCKED",
            "task_id": None,
            "output_path": None,
            "blocker": "fixture blocker",
        }
        success_job = {
            "id": "V001",
            "status": "COMPLETED",
            "task_id": "task-1",
            "output_path": str(downloaded),
            "blocker": None,
        }
        self.write_streaming_result(batch, flow, "preparation", ready_job)
        self.write_streaming_result(batch, flow, "preparation", blocked_job)
        self.write_streaming_result(batch, flow, "submission", success_job)
        destination = loop.finalize_streaming_flow(batch, self.root)
        self.assertEqual(destination.parent.name, "blocked")
        result = json.loads((destination / "streaming-result.json").read_text())
        self.assertEqual(result["batch_status"], "PARTIAL")

    def test_finalize_streaming_flow_moves_all_success_to_completed(self):
        batch, flow = self.streaming_batch()
        for job in flow["jobs"]:
            downloaded = self.temp / f"{job['id']}.mp4"
            downloaded.write_bytes(b"downloaded")
            ready = {
                "id": job["id"],
                "status": "READY_FOR_SUBMISSION",
                "task_id": None,
                "output_path": None,
                "blocker": None,
            }
            completed = {
                "id": job["id"],
                "status": "COMPLETED",
                "task_id": f"task-{job['id']}",
                "output_path": str(downloaded),
                "blocker": None,
            }
            self.write_streaming_result(batch, flow, "preparation", ready)
            self.write_streaming_result(batch, flow, "submission", completed)
        destination = loop.finalize_streaming_flow(batch, self.root)
        self.assertEqual(destination.parent.name, "completed")
        result = json.loads((destination / "streaming-result.json").read_text())
        self.assertEqual(result["batch_status"], "COMPLETED")

    def test_finalize_streaming_flow_blocks_completed_record_without_file(self):
        batch, flow = self.streaming_batch()
        for job in flow["jobs"]:
            ready = {
                "id": job["id"],
                "status": "READY_FOR_SUBMISSION",
                "task_id": None,
                "output_path": None,
                "blocker": None,
            }
            missing = {
                "id": job["id"],
                "status": "COMPLETED",
                "task_id": f"task-{job['id']}",
                "output_path": str(self.temp / f"missing-{job['id']}.mp4"),
                "blocker": None,
            }
            self.write_streaming_result(batch, flow, "preparation", ready)
            self.write_streaming_result(batch, flow, "submission", missing)
        destination = loop.finalize_streaming_flow(batch, self.root)
        self.assertEqual(destination.parent.name, "blocked")
        result = json.loads((destination / "streaming-result.json").read_text())
        self.assertEqual(result["batch_status"], "BLOCKED")
        self.assertTrue(all(job["blocker"] == "下载文件不存在" for job in result["jobs"]))


class EngineIsRunnableAsAScriptTest(unittest.TestCase):
    """The JavaScript control plane spawns [python, video_batch_loop.py].

    Every other test in this file imports the module, which exercises a
    different code path in CPython. Past roughly 200 KB this file trips a 3.9
    tokenizer buffer-boundary bug and refuses to run as a script unless it
    carries an encoding declaration — while importing keeps working. macOS
    ships 3.9 as python3, so the whole suite can stay green while the live
    pipeline dies at launch.
    """

    def test_engine_runs_under_the_interpreter_that_launches_it(self) -> None:
        import subprocess

        engine = Path(loop.__file__).resolve()
        completed = subprocess.run(
            [sys.executable, str(engine), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"{engine.name} cannot be executed directly:\n{completed.stderr}",
        )
        self.assertIn("usage:", completed.stdout)

    def test_engine_declares_its_encoding(self) -> None:
        engine = Path(loop.__file__).resolve()
        header = engine.read_text(encoding="utf-8").splitlines()[:3]
        self.assertTrue(
            any("coding" in line for line in header),
            "the encoding declaration must stay within the first two lines; "
            "without it this file stops being executable once it grows",
        )


if __name__ == "__main__":
    unittest.main()

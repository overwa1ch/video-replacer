"""Regression coverage for schema-v3 backend selection and upload lineage.

All media in this module is deliberately tiny fake bytes.  The parent media
Gate is mocked only after the manifest's hash/identity checks are in scope, so
these tests cannot create an actual remote task or require FFmpeg.
"""

from __future__ import annotations

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
from upload_preparation import constraints_digest  # noqa: E402


class BackendProfileWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "video-loop"
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir(parents=True)
        loop.ensure_layout(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_batch(self, profile_id: str = "dreamina_cli_seedance_2_5") -> Path:
        incoming = self.root / "inbox" / "batch-profile"
        (incoming / "videos").mkdir(parents=True)
        (incoming / "replacements").mkdir()
        (incoming / "videos" / "001-source.mp4").write_bytes(b"source-bytes")
        (incoming / "requirements.txt").write_text(
            "V001：替换目标对象\n", encoding="utf-8"
        )
        batch = loop.prepare_inbox_batch(incoming, self.root)
        loop.atomic_write_json(
            batch / "reference-index.json", loop.build_reference_index(batch)
        )
        loop.atomic_write_json(
            batch / "job-bindings.json",
            {
                "schema_version": 3,
                "batch_id": batch.name,
                "backend_profile": profile_id,
                "jobs": [
                    {"id": "V001", "privacy_mode": "none", "references": []}
                ],
            },
        )
        return batch

    def validated_indexes(self, batch: Path):
        return loop.verify_batch_integrity(batch)

    def test_schema_v3_accepts_only_profile_and_agent_fields(self) -> None:
        batch = self.make_batch()
        index, reference_index = self.validated_indexes(batch)
        bindings = loop.validate_job_bindings(batch, index, reference_index)
        self.assertEqual(bindings["V001"]["privacy_mode"], "none")
        profile, managed = loop.backend_profile_for_batch(batch, index, reference_index)
        self.assertTrue(managed)
        self.assertEqual(profile.profile_id, "dreamina_cli_seedance_2_5")

        path = batch / "job-bindings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["should_compress"] = True
        loop.atomic_write_json(path, payload)
        with self.assertRaisesRegex(loop.LoopError, "顶层字段无效"):
            loop.validate_job_bindings(batch, index, reference_index)

        payload.pop("should_compress")
        payload["jobs"][0]["compression_policy"] = "lossless"
        loop.atomic_write_json(path, payload)
        with self.assertRaisesRegex(loop.LoopError, "素材绑定字段无效"):
            loop.validate_job_bindings(batch, index, reference_index)

    def test_provider_credentials_do_not_cross_backend_or_local_tool_boundaries(self) -> None:
        state = Path(self.temporary.name) / "state"
        source = {
            "PATH": os.defpath,
            "HOME": str(Path(self.temporary.name) / "home"),
            "OPENAI_API_KEY": "openai-must-not-cross",
            "GITHUB_TOKEN": "github-must-not-cross",
            "VIDEO_REPLACER_ARK_API_KEY": "ark-secret",
            "VIDEO_REPLACER_TOS_ACCESS_KEY": "tos-access",
            "VIDEO_REPLACER_TOS_SECRET_KEY": "tos-secret",
            "VIDEO_REPLACER_TOS_ENDPOINT": "tos.example.test",
            "VIDEO_REPLACER_TOS_REGION": "test-region",
            "VIDEO_REPLACER_TOS_BUCKET": "test-bucket",
            "VIDEO_REPLACER_TOS_LIFECYCLE_RULE_ID": "expiry-rule",
            "VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID": "model-test",
        }
        dreamina = loop.get_backend_profile("dreamina_cli_seedance_2_5")
        ffmpeg = Path(self.temporary.name) / "ffmpeg-test"
        with mock.patch.dict(os.environ, source, clear=True), mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=ffmpeg
        ):
            environment = loop.execution_environment_for_profile(dreamina, state)
        self.assertEqual(environment["PATH"], os.defpath)
        self.assertEqual(environment["VIDEO_REPLACER_STATE_DIR"], str(state))
        for forbidden in (
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "VIDEO_REPLACER_ARK_API_KEY",
            "VIDEO_REPLACER_TOS_ACCESS_KEY",
            "VIDEO_REPLACER_TOS_SECRET_KEY",
        ):
            self.assertNotIn(forbidden, environment)

        ark = loop.get_backend_profile("volcengine_ark_seedance_2_5")
        with mock.patch.dict(os.environ, source, clear=True), mock.patch.object(
            loop, "_trusted_ffmpeg_executable", return_value=ffmpeg
        ):
            execution = loop.execution_environment_for_profile(ark, state)
            probe = loop.probe_environment_for_profile(ark)
        self.assertEqual(execution["VIDEO_REPLACER_ARK_API_KEY"], "ark-secret")
        self.assertEqual(execution["VIDEO_REPLACER_TOS_SECRET_KEY"], "tos-secret")
        self.assertNotIn("OPENAI_API_KEY", execution)
        self.assertNotIn("GITHUB_TOKEN", execution)
        self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", probe)
        self.assertNotIn("VIDEO_REPLACER_TOS_SECRET_KEY", probe)

    def test_schema_v3_rejects_missing_or_unknown_profile(self) -> None:
        batch = self.make_batch()
        index, reference_index = self.validated_indexes(batch)
        path = batch / "job-bindings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("backend_profile")
        loop.atomic_write_json(path, payload)
        with self.assertRaisesRegex(loop.LoopError, "顶层字段无效"):
            loop.validate_job_bindings(batch, index, reference_index)

        payload["backend_profile"] = "unreviewed_backend"
        loop.atomic_write_json(path, payload)
        with self.assertRaisesRegex(loop.LoopError, "未知 backend_profile"):
            loop.validate_job_bindings(batch, index, reference_index)

    def test_profile_changes_streaming_identity(self) -> None:
        batch = self.make_batch("dreamina_cli_seedance_2_5")
        index, reference_index = self.validated_indexes(batch)
        first = loop._streaming_flow_identity(batch, self.project, index, reference_index)
        payload = json.loads((batch / "job-bindings.json").read_text(encoding="utf-8"))
        payload["backend_profile"] = "dreamina_cli_seedance_2_0"
        loop.atomic_write_json(batch / "job-bindings.json", payload)
        second = loop._streaming_flow_identity(batch, self.project, index, reference_index)
        self.assertNotEqual(first, second)
        self.assertNotEqual(
            loop._streaming_flow_fingerprint(first),
            loop._streaming_flow_fingerprint(second),
        )

    def test_registry_resolves_only_the_profile_adapter_and_ark_model(self) -> None:
        ark_profile = loop.get_backend_profile("volcengine_ark_seedance_2_5")
        with self.assertRaisesRegex(loop.LoopError, "VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID"):
            loop.find_replacement_executor(
                PROJECT_ROOT,
                environment={},
                profile=ark_profile,
            )
        executor = loop.find_replacement_executor(
            PROJECT_ROOT,
            environment={"VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID": "ep-seedance-test"},
            profile=ark_profile,
        )
        self.assertEqual(executor.path.name, "ark_video.py")
        self.assertEqual(executor.transport, ark_profile.transport)
        self.assertEqual(executor.model_version, "ep-seedance-test")
        self.assertFalse(executor.supports_authorized_retry)
        with self.assertRaisesRegex(loop.LoopError, "受控注册表"):
            loop.find_replacement_executor(
                PROJECT_ROOT,
                environment={
                    "VIDEO_REPLACER_ARK_SEEDANCE_2_5_MODEL_ID": "ep-seedance-test",
                    "VIDEO_REPLACEMENT_EXECUTOR": str(
                        self.temporary.name + "/unreviewed/ark_video.py"
                    ),
                },
                profile=ark_profile,
            )

    def test_paid_execution_resolves_ark_from_the_batch_profile(self) -> None:
        """A submit path must not silently fall back to Dreamina for Ark."""

        batch = self.make_batch("volcengine_ark_seedance_2_5")
        profile = loop.get_backend_profile("volcengine_ark_seedance_2_5")
        ark_executor = loop.ExecutorSpec(
            PROJECT_ROOT / "tools" / "ark_video.py",
            profile.transport,
            profile.profile_id,
            "ep-seedance-test",
            False,
        )
        with mock.patch.object(
            loop, "find_replacement_executor", return_value=ark_executor
        ) as discover:
            result = loop.execute_prepared_jobs(
                batch,
                self.project,
                {"jobs": []},
                already_validated=True,
        )
        self.assertEqual(result["jobs"], [])
        discover.assert_called_once_with(self.project, profile=profile)

    def test_seedance_2_0_profile_has_its_own_working_size_constraints(self) -> None:
        profile = loop.get_backend_profile("dreamina_cli_seedance_2_0")
        constraints = loop.upload_constraints_for_profile(profile)
        self.assertEqual(constraints.limit_bytes, 50_000_000)
        self.assertEqual(constraints.target_bytes, 47_000_000)
        self.assertEqual(constraints.min_pixels, 40_000)
        self.assertEqual(set(constraints.allowed_containers), {"mp4", "mov"})

    def test_v3_plan_binds_profile_preflight_and_upload_manifest(self) -> None:
        batch = self.make_batch()
        profile = loop.get_backend_profile("dreamina_cli_seedance_2_5")
        output = loop.job_output_dir(self.project, batch, "V001")
        output.mkdir(parents=True)
        source_index = loop.read_index(batch)["jobs"][0]
        original = batch / source_index["relative_path"]
        preliminary = output / loop.ACTIVE_VIDEO_COPY_FILENAME
        preliminary.write_bytes(original.read_bytes())
        metadata = {
            "container": "mp4",
            "video_codec": "h264",
            "duration_seconds": 5.0,
            "width": 1280,
            "height": 720,
            "fps": 30.0,
            "has_audio": True,
        }
        constraints = loop.upload_constraints_for_profile(profile)
        upload_manifest = output / loop.UPLOAD_PREPARATION_FILENAME
        loop.atomic_write_json(
            upload_manifest,
            {
                "schema_version": 1,
                "created_at": "2026-08-12T00:00:00+00:00",
                "backend_profile": profile.profile_id,
                "constraints_digest": constraints_digest(constraints),
                "action": "unchanged",
                "source": {
                    "path": str(preliminary.resolve()),
                    "sha256": loop.sha256_file(preliminary),
                    "size_bytes": preliminary.stat().st_size,
                    "metadata": metadata,
                },
                "output": {
                    "path": str(preliminary.resolve()),
                    "sha256": loop.sha256_file(preliminary),
                    "size_bytes": preliminary.stat().st_size,
                    "metadata": metadata,
                },
                "limit_bytes": profile.limit_bytes,
                "target_bytes": profile.target_bytes,
                "full_decode_passed": True,
                "ffmpeg": {"path": "fake", "version": "fake", "commands": []},
            },
        )
        prompt = output / loop.PROMPT_FILENAME
        prompt.write_text(
            "素材绑定：@视频1=原视频。\n执行指定替换。\n", encoding="utf-8"
        )
        preflight_path = output / "preflight.json"
        executor = loop.ExecutorSpec(
            PROJECT_ROOT / "tools" / "dreamina_video.py",
            profile.transport,
            profile.profile_id,
            "seedance2.5",
            True,
        )
        preflight = {
            "preflight_passed": True,
            "transport": profile.transport,
            "backend_profile": profile.profile_id,
            "constraints_digest": profile.constraints_digest,
            "backend_profile_constraints_sha256": profile.constraints_digest,
            "model_version": "seedance2.5",
            "upload_preparation_manifest": str(upload_manifest.resolve()),
            "upload_preparation_manifest_sha256": loop.sha256_file(upload_manifest),
            "active_video": {
                "path": str(preliminary.resolve()),
                "sha256": loop.sha256_file(preliminary),
            },
            "input_bindings": loop._expected_input_bindings(prompt, []),
            "privacy": {
                "status": "workflow-selected-input",
                "active_video_sha256": loop.sha256_file(preliminary),
                "remote_upload_authorized": True,
                "paid_task_authorized": True,
            },
        }
        loop.atomic_write_json(preflight_path, preflight)
        plan = {
            "schema_version": 2,
            "batch_id": batch.name,
            "job_id": "V001",
            "name": f"{batch.name}-V001",
            "output_dir": str(output.resolve()),
            "prompt_file": str(prompt.resolve()),
            "video": str(preliminary.resolve()),
            "preflight_manifest": str(preflight_path.resolve()),
            "preflight_manifest_sha256": loop.sha256_file(preflight_path),
            "upload_preparation_manifest": str(upload_manifest.resolve()),
            "backend_profile": profile.profile_id,
            "backend_profile_constraints_sha256": profile.constraints_digest,
            "images": [],
        }
        loop.atomic_write_json(output / "submission-plan.json", plan)
        with mock.patch.object(loop, "probe_video_metadata", return_value=metadata), mock.patch.object(
            loop, "validate_video_metadata"
        ), mock.patch.object(loop, "validate_full_decode"):
            loaded = loop.load_submission_plan(
                batch, self.project, "V001", executor=executor
            )
        self.assertEqual(loaded["backend_profile"], profile.profile_id)

        # A v3 paid approval carries the final-media identity, not merely the
        # flow fingerprint.  The same local proof is recomputed before submit.
        flow = {
            "flow_fingerprint": "profile-managed-flow",
            "jobs": [{"id": "V001"}],
        }
        with mock.patch.object(loop, "probe_video_metadata", return_value=metadata), mock.patch.object(
            loop, "validate_video_metadata"
        ), mock.patch.object(loop, "validate_full_decode"):
            identity = loop.submission_authorization_identity(
                batch, self.project, "V001", executor=executor
            )
        token = "c" * 64
        authorized_at = loop.datetime.now(loop.timezone.utc)
        checkpoint = {
            "schema_version": 3,
            "batch_id": batch.name,
            "flow_fingerprint": flow["flow_fingerprint"],
            "planned_paid_tasks": 1,
            "explicit_payment_approval_received": True,
            "authorization_scope": "current-batch-single-job",
            "authorized_job_ids": ["V001"],
            "orchestrator_pid": os.getppid(),
            "authorization_nonce": "d" * 32,
            "authorized_at": authorized_at.isoformat(),
            "expires_at": (
                authorized_at
                + loop.timedelta(seconds=loop.PAYMENT_CHECKPOINT_TTL_SECONDS)
            ).isoformat(),
            "submission_identities": [identity],
        }
        checkpoint["authorization_binding_sha256"] = loop.payment_authorization_binding(
            token, checkpoint
        )
        loop.atomic_write_json(batch / "payment-checkpoint-V001.json", checkpoint)
        with mock.patch.object(loop, "probe_video_metadata", return_value=metadata), mock.patch.object(
            loop, "validate_video_metadata"
        ), mock.patch.object(loop, "validate_full_decode"):
            verified_checkpoint = loop._validate_streaming_payment_checkpoint(
                batch,
                flow,
                project_root=self.project,
                executor=executor,
                environment={loop.PAYMENT_AUTH_TOKEN_ENV: token},
                parent_pid=os.getppid(),
                expected_job_id="V001",
            )
        self.assertEqual(
            loop.payment_checkpoint_identity_map(verified_checkpoint),
            {"V001": identity},
        )

        # A profile/provenance mutation cannot reuse the old plan/preflight.
        altered = json.loads(upload_manifest.read_text(encoding="utf-8"))
        altered["target_bytes"] = profile.target_bytes - 1
        loop.atomic_write_json(upload_manifest, altered)
        with mock.patch.object(loop, "probe_video_metadata", return_value=metadata), mock.patch.object(
            loop, "validate_video_metadata"
        ), mock.patch.object(loop, "validate_full_decode"):
            with self.assertRaisesRegex(loop.LoopError, "upload-preparation manifest"):
                loop.load_submission_plan(batch, self.project, "V001", executor=executor)
            with self.assertRaisesRegex(loop.LoopError, "upload-preparation manifest"):
                loop._validate_streaming_payment_checkpoint(
                    batch,
                    flow,
                    project_root=self.project,
                    executor=executor,
                    environment={loop.PAYMENT_AUTH_TOKEN_ENV: token},
                    parent_pid=os.getppid(),
                    expected_job_id="V001",
                )

    def test_upload_preparation_is_between_mosaic_and_probe(self) -> None:
        source = Path(loop.__file__).read_text(encoding="utf-8")
        body = source[source.index("def prepare_streaming_job("):source.index("def _payment_checkpoint_timestamp(")]
        self.assertLess(body.index("prepare_mosaic_video("), body.index("prepare_upload_for_profile("))
        self.assertLess(body.index("prepare_upload_for_profile("), body.index("run_executor_probe("))


if __name__ == "__main__":
    unittest.main()

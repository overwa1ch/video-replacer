"""Offline regression tests for the Ark adapter safety boundary.

These tests deliberately exercise no TOS or Ark network path.  The adapter's
remote calls are covered through its small pure helpers so that a test run can
never create a paid task.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).with_name("ark_video.py")
SPEC = importlib.util.spec_from_file_location("ark_video", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ark_video = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ark_video
SPEC.loader.exec_module(ark_video)


class ArkVideoSafetyTests(unittest.TestCase):
    def test_environment_rejects_non_official_ark_and_tos_hosts(self) -> None:
        required = {
            "VIDEO_REPLACER_ARK_API_KEY": "ark-key",
            "VIDEO_REPLACER_TOS_ACCESS_KEY": "access-key",
            "VIDEO_REPLACER_TOS_SECRET_KEY": "secret-key",
            "VIDEO_REPLACER_TOS_ENDPOINT": "tos-cn-beijing.volces.com",
            "VIDEO_REPLACER_TOS_REGION": "cn-beijing",
            "VIDEO_REPLACER_TOS_BUCKET": "private-bucket",
            "VIDEO_REPLACER_TOS_LIFECYCLE_RULE_ID": "expire-rule",
        }
        with mock.patch.dict(
            os.environ,
            {**required, "VIDEO_REPLACER_ARK_API_BASE_URL": "https://evil.invalid/api/v3"},
            clear=True,
        ):
            with self.assertRaisesRegex(ark_video.ArkPipelineError, "官方火山 Ark"):
                ark_video.load_ark_environment()
        with mock.patch.dict(
            os.environ,
            {**required, "VIDEO_REPLACER_TOS_ENDPOINT": "tos.evil.invalid"},
            clear=True,
        ):
            with self.assertRaisesRegex(ark_video.ArkPipelineError, "官方火山 TOS"):
                ark_video.load_ark_environment()

    def test_profile_requires_the_reviewed_ark_adapter(self) -> None:
        args = argparse.Namespace(
            backend_profile="dreamina_cli_seedance_2_5",
            model_version="seedance2.5",
        )
        with self.assertRaisesRegex(ark_video.ArkPipelineError, "Ark"):
            ark_video.validate_ark_profile(args)

    def test_model_version_is_explicit_and_not_read_from_environment(self) -> None:
        args = argparse.Namespace(
            backend_profile="volcengine_ark_seedance_2_5",
            model_version="",
        )
        with self.assertRaisesRegex(ark_video.ArkPipelineError, "--model-version"):
            ark_video.validate_ark_profile(args)

    def test_generate_refuses_before_any_credential_read(self) -> None:
        args = argparse.Namespace(confirm_paid=False)
        with mock.patch.object(ark_video, "load_ark_environment") as environment:
            with self.assertRaisesRegex(ark_video.ArkPipelineError, "confirm-paid"):
                ark_video.command_generate(args)
        environment.assert_not_called()

    def test_remote_summary_never_exposes_object_keys(self) -> None:
        raw_key = "private/user-prefix/very-secret-video.mp4"
        summary = ark_video.safe_remote_summary(
            [{"key": raw_key, "sha256": "a" * 64, "size_bytes": 12}]
        )
        rendered = str(summary)
        self.assertNotIn(raw_key, rendered)
        self.assertEqual(summary["remote_object_count"], 1)
        self.assertEqual(len(summary["remote_object_key_digests"]), 1)

    def test_queued_report_never_exposes_a_signed_url(self) -> None:
        profile = ark_video.get_backend_profile("volcengine_ark_seedance_2_5")
        signed_url = (
            "https://private.example.invalid/object?"
            "X-Tos-Algorithm=TOS4-HMAC-SHA256&X-Tos-Signature=very-secret"
        )
        raw_key = "private/video-replacer/object.mp4"
        report = ark_video._safe_queued_report(
            profile,
            {"duration": 4, "video_resolution": "720p", "ratio": "16:9"},
            "f" * 64,
            "task-1",
            [{"key": raw_key, "signed_url": signed_url}],
        )
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(signed_url, rendered)
        self.assertNotIn(raw_key, rendered)

    def test_terminal_cleanup_deletes_uploaded_private_objects(self) -> None:
        environment = ark_video.ArkEnvironment(
            ark_api_key="ark-key",
            tos_access_key="access-key",
            tos_secret_key="secret-key",
            tos_endpoint="tos.example.invalid",
            tos_region="cn-beijing",
            tos_bucket="private-bucket",
        )
        record = {
            "staged_objects": [
                {"key": "private/video.mp4", "upload_state": "uploaded"}
            ]
        }
        client = mock.Mock()
        persist = mock.Mock()
        status = ark_video.cleanup_staged_objects(client, environment, record, persist)
        self.assertEqual(status, "complete")
        client.delete_object.assert_called_once_with("private-bucket", "private/video.mp4")
        self.assertEqual(record["staged_objects"][0]["upload_state"], "deleted")
        self.assertEqual(record["tos_cleanup_status"], "complete")

    def test_parent_recovery_marker_never_exposes_object_keys(self) -> None:
        profile = ark_video.get_backend_profile("volcengine_ark_seedance_2_5")
        raw_key = "private/user-prefix/very-secret-video.mp4"
        marker = ark_video._safe_task_record(
            profile,
            {"model_version_sha256": "m" * 64},
            "f" * 64,
            {
                "task_id": "task-1",
                "task_status": "queued",
                "submission_state": "submitted",
                "tos_cleanup_status": "not_started",
                "staged_objects": [{"key": raw_key}],
            },
        )
        self.assertNotIn(raw_key, json.dumps(marker, ensure_ascii=False))
        self.assertEqual(marker["task_id"], "task-1")

    def test_external_record_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            ark_video.atomic_write_private_json(path, {"key": "private/object"})
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_lifecycle_fallback_is_verified_before_any_staging(self) -> None:
        environment = ark_video.ArkEnvironment(
            ark_api_key="ark-key",
            tos_access_key="access-key",
            tos_secret_key="secret-key",
            tos_endpoint="tos.example.invalid",
            tos_region="cn-beijing",
            tos_bucket="private-bucket",
            tos_prefix="video-replacer",
            tos_lifecycle_rule_id="expire-video-replacer",
        )
        client = mock.Mock()
        client.get_bucket_lifecycle.return_value = SimpleNamespace(
            rules=[
                SimpleNamespace(
                    id="expire-video-replacer",
                    status=SimpleNamespace(value="Enabled"),
                    expiration=SimpleNamespace(days=2),
                    prefix="video-replacer/video-replacer/",
                )
            ]
        )
        ark_video.verify_tos_lifecycle_fallback(client, environment)
        client.get_bucket_lifecycle.assert_called_once_with("private-bucket")

        client.get_bucket_lifecycle.return_value = SimpleNamespace(
            rules=[
                SimpleNamespace(
                    id="expire-video-replacer",
                    status=SimpleNamespace(value="Enabled"),
                    expiration=SimpleNamespace(days=1),
                    prefix="video-replacer/video-replacer/",
                )
            ]
        )
        with self.assertRaisesRegex(ark_video.ArkPipelineError, "至少保留"):
            ark_video.verify_tos_lifecycle_fallback(client, environment)

    def test_generated_url_never_falls_back_to_input_url(self) -> None:
        payload = {
            "content": [{"type": "video_url", "video_url": {"url": "https://input.invalid/signed"}}],
            "data": {"video_url": "https://output.invalid/final.mp4"},
        }
        self.assertEqual(
            ark_video.extract_video_url(payload), "https://output.invalid/final.mp4"
        )

    def test_probe_binds_the_profile_fields_used_by_parent_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            upload_manifest = root / "upload-preparation.json"
            video.write_bytes(b"local-video")
            prompt.write_text("replace", encoding="utf-8")
            upload_manifest.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                backend_profile="volcengine_ark_seedance_2_5",
                model_version="ep-test-model",
                video=str(video),
                prompt_file=str(prompt),
                image=[],
                privacy_status="workflow-selected-input",
                name="job",
                output_dir=str(root / "out"),
                upload_preparation_manifest=str(upload_manifest),
            )
            metadata = {
                "path": str(video),
                "size_bytes": video.stat().st_size,
                "container": "mp4",
                "duration_seconds": 4.0,
                "width": 1280,
                "height": 720,
                "fps": 24.0,
                "video_codec": "h264",
                "audio_codec": "aac",
                "has_audio": True,
            }
            stdout = io.StringIO()
            with mock.patch.object(ark_video, "probe_video", return_value=metadata), redirect_stdout(stdout):
                self.assertEqual(ark_video.command_probe(args), 0)
            report = json.loads(stdout.getvalue())
            profile = ark_video.get_backend_profile(args.backend_profile)
            self.assertEqual(report["constraints_digest"], profile.constraints_digest)
            self.assertEqual(
                report["backend_profile_constraints_sha256"], profile.constraints_digest
            )
            self.assertEqual(report["model_version"], args.model_version)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("dreamina_video.py")
SPEC = importlib.util.spec_from_file_location("dreamina_video", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dreamina = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dreamina)


class DreaminaVideoTests(unittest.TestCase):
    def test_cli_environment_excludes_other_provider_credentials(self) -> None:
        environment = dreamina.cli_environment(
            {
                "PATH": os.defpath,
                "LOCALAPPDATA": "C:/Users/example/AppData/Local",
                "OPENAI_API_KEY": "must-not-cross",
                "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross",
                "VIDEO_REPLACER_TOS_SECRET_KEY": "must-not-cross",
            }
        )
        self.assertEqual(environment["LOCALAPPDATA"], "C:/Users/example/AppData/Local")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", environment)
        self.assertNotIn("VIDEO_REPLACER_TOS_SECRET_KEY", environment)

    def test_non_preview_generate_requires_explicit_paid_confirmation_first(self) -> None:
        args = Namespace(preview=False, confirm_paid=False)
        with mock.patch.object(dreamina, "find_dreamina") as find_binary, mock.patch.object(
            dreamina, "request_state_root"
        ) as state_root, mock.patch.object(
            dreamina, "atomic_write_json"
        ) as write_json, self.assertRaisesRegex(
            dreamina.DreaminaPipelineError, "--confirm-paid"
        ):
            dreamina.command_generate(args)
        find_binary.assert_not_called()
        state_root.assert_not_called()
        write_json.assert_not_called()

    def test_default_model_uses_seedance25_vip_lane(self) -> None:
        self.assertEqual(dreamina.DEFAULT_MODEL, "seedance2.5")

    def test_executor_has_no_skill_or_privacy_review_dependency(self) -> None:
        for retired in (
            "resolve_skill_root",
            "validate_anonymization_manifest",
            "validate_privacy_review_manifest",
            "validate_privacy_direction_manifest",
        ):
            self.assertFalse(hasattr(dreamina, retired))

    def test_parse_cli_result_finds_json_after_progress_text(self) -> None:
        parsed = dreamina.parse_cli_result(
            'uploading...\n{"submit_id":"submit-1","gen_status":"querying"}\n'
        )
        self.assertEqual(parsed["submit_id"], "submit-1")
        self.assertEqual(parsed["gen_status"], "querying")

    def test_parse_cli_result_reads_nested_terminal_failure(self) -> None:
        parsed = dreamina.parse_cli_result(
            json.dumps(
                {
                    "data": {
                        "submit_id": "submit-2",
                        "gen_status": "fail",
                        "fail_reason": "Invalid media input",
                    }
                }
            )
        )
        self.assertEqual(parsed["submit_id"], "submit-2")
        self.assertEqual(parsed["gen_status"], "fail")
        self.assertEqual(parsed["fail_reason"], "Invalid media input")

    def test_fingerprint_is_path_independent_and_content_sensitive(self) -> None:
        request = {
            "backend": "dreamina-cli",
            "video_sha256": "a",
            "prompt_sha256": "b",
            "image_sha256": ["c"],
        }
        first = dreamina.request_fingerprint(request)
        second = dreamina.request_fingerprint(dict(reversed(list(request.items()))))
        changed = dreamina.request_fingerprint({**request, "prompt_sha256": "different"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_duration_and_ratio_match_source_profile(self) -> None:
        self.assertEqual(dreamina.api_duration(13.57, None), 14)
        self.assertEqual(dreamina.api_duration(2.1, None), 4)
        self.assertEqual(dreamina.api_duration(28.0, 28, "seedance2.5"), 28)
        self.assertEqual(
            dreamina.api_duration(28.0, None, dreamina.DEFAULT_MODEL), 28
        )
        self.assertEqual(dreamina.infer_ratio(1280, 720), "16:9")
        self.assertEqual(dreamina.infer_ratio(720, 1280), "9:16")

    def test_seedance25_request_accepts_720p(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            image = root / "image.png"
            video.write_bytes(b"video")
            prompt.write_text("prompt", encoding="utf-8")
            image.write_bytes(b"image")
            args = Namespace(
                model_version="seedance2.5",
                resolution="720p",
                duration=4,
                ratio="3:4",
                generate_audio=False,
            )
            request = dreamina.planned_request(
                video,
                prompt,
                [image],
                {
                    "duration_seconds": 2.0,
                    "width": 960,
                    "height": 1280,
                },
                args,
            )
            self.assertEqual(request["model_version"], "seedance2.5")
            self.assertEqual(request["video_resolution"], "720p")
            self.assertEqual(request["duration"], 4)

    def test_legacy_model_rejects_source_longer_than_15_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            video.write_bytes(b"video")
            prompt.write_text("prompt", encoding="utf-8")
            args = Namespace(
                model_version="seedance2.0fast_vip",
                resolution="720p",
                duration=None,
                ratio="16:9",
                generate_audio=False,
            )
            with self.assertRaises(dreamina.DreaminaPipelineError):
                dreamina.planned_request(
                    video,
                    prompt,
                    [],
                    {
                        "duration_seconds": 28.0,
                        "width": 1280,
                        "height": 720,
                    },
                    args,
                )

    def test_submit_command_uses_official_cli_local_files(self) -> None:
        request = {
            "model_version": "seedance2.0fast",
            "duration": 5,
            "ratio": "9:16",
            "video_resolution": "720p",
        }
        command = dreamina.submit_command(
            Path("/opt/dreamina"),
            Path("/tmp/source.mp4"),
            "replace the product",
            [Path("/tmp/ref-1.png"), Path("/tmp/ref-2.png")],
            request,
            poll=30,
        )
        self.assertEqual(command[1], "multimodal2video")
        self.assertEqual(command.count("--image"), 2)
        self.assertIn("--video", command)
        self.assertNotIn("ARK_API_KEY", " ".join(command))
        self.assertNotIn("TOS_", " ".join(command))

    def test_workflow_selected_input_record_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "active.mp4"
            video.write_bytes(b"active-video")
            record = dreamina.build_privacy_record(
                "workflow-selected-input", video.resolve()
            )
            self.assertEqual(record["status"], "workflow-selected-input")
            self.assertEqual(
                record["active_video_sha256"], dreamina.sha256_file(video)
            )
            self.assertTrue(record["remote_upload_authorized"])
            self.assertTrue(record["paid_task_authorized"])

    def test_fast_model_rejects_non_720p_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            image = root / "image.png"
            video.write_bytes(b"video")
            prompt.write_text("prompt", encoding="utf-8")
            image.write_bytes(b"image")
            args = Namespace(
                model_version="seedance2.0fast",
                resolution="1080p",
                duration=None,
                ratio=None,
                generate_audio=False,
            )
            with self.assertRaises(dreamina.DreaminaPipelineError):
                dreamina.planned_request(
                    video,
                    prompt,
                    [image],
                    {
                        "duration_seconds": 5.0,
                        "width": 1280,
                        "height": 720,
                    },
                    args,
                )

    def test_retired_privacy_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "source.mp4"
            video.write_bytes(b"source")
            with self.assertRaises(dreamina.DreaminaPipelineError):
                dreamina.build_privacy_record("user-directed-unmasked", video.resolve())

    def test_retry_requires_external_user_authorization_and_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary).resolve()
            authorization_root = state_root / "retry-authorizations"
            authorization_root.mkdir()
            authorization = authorization_root / "batch-a.json"
            authorization.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "batch_id": "batch-a",
                        "decision": dreamina.RETRY_AUTHORIZATION_DECISION,
                        "issued_by": "user-in-chat",
                        "authorization_text": "逐条重试",
                        "paid_retry_authorized": True,
                        "mode": "wait-terminal-before-next",
                        "model_version": "seedance2.0fast_vip",
                        "max_new_tasks": 1,
                        "jobs": [
                            {
                                "job_id": "V002",
                                "original_submit_id": "failed-submit-2",
                                "retry_attempt": 2,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            identity = dreamina.validate_retry_authorization(
                str(authorization),
                state_root,
                "batch-a-V002",
                2,
                "seedance2.0fast_vip",
            )
            self.assertEqual(identity["original_submit_id"], "failed-submit-2")
            original = dreamina.request_fingerprint({"request": "same"})
            retry = dreamina.request_fingerprint(
                {"request": "same", "retry": identity}
            )
            self.assertNotEqual(original, retry)

    def test_retry_authorization_outside_state_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary).resolve()
            unauthorized = state_root / "workspace-auth.json"
            unauthorized.write_text("{}", encoding="utf-8")
            with self.assertRaises(dreamina.DreaminaPipelineError):
                dreamina.validate_retry_authorization(
                    str(unauthorized),
                    state_root,
                    "batch-a-V002",
                    2,
                    "seedance2.0fast_vip",
                )

    def test_controlled_probe_records_profile_model_and_constraints_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            output = root / "output"
            upload_manifest = root / "upload-preparation.json"
            video.write_bytes(b"source")
            upload_manifest.write_text("{}", encoding="utf-8")
            args = Namespace(
                video=str(video),
                prompt_file=None,
                image=[],
                privacy_status="workflow-selected-input",
                name="batch-V001",
                output_dir=str(output),
                backend_profile="dreamina_cli_seedance_2_0",
                model_version="seedance2.0",
                upload_preparation_manifest=str(upload_manifest),
            )
            with mock.patch.object(
                dreamina,
                "probe_video",
                return_value={
                    "duration_seconds": 8.0,
                    "width": 1280,
                    "height": 720,
                },
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(dreamina.command_probe(args), 0)
            manifest = json.loads((output / "batch-V001-preflight.json").read_text())
            profile = dreamina.get_backend_profile("dreamina_cli_seedance_2_0")
            self.assertEqual(manifest["backend_profile"], profile.profile_id)
            self.assertEqual(manifest["constraints_digest"], profile.constraints_digest)
            self.assertEqual(manifest["model_version"], "seedance2.0")

    def test_controlled_profile_cannot_substitute_model_or_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            video.write_bytes(b"video")
            prompt.write_text("prompt", encoding="utf-8")
            args = Namespace(
                backend_profile="dreamina_cli_seedance_2_0",
                model_version="seedance2.0",
                resolution="720p",
                duration=4,
                ratio="16:9",
                generate_audio=False,
            )
            request = dreamina.planned_request(
                video,
                prompt,
                [],
                {"duration_seconds": 4.0, "width": 1280, "height": 720},
                args,
            )
            self.assertEqual(request["backend_profile"], "dreamina_cli_seedance_2_0")
            self.assertIn("constraints_digest", request)
            args.model_version = "seedance2.5"
            with self.assertRaisesRegex(dreamina.DreaminaPipelineError, "固定使用"):
                dreamina.planned_request(
                    video,
                    prompt,
                    [],
                    {"duration_seconds": 4.0, "width": 1280, "height": 720},
                    args,
                )
            args.backend_profile = "volcengine_ark_seedance_2_5"
            args.model_version = None
            with self.assertRaisesRegex(dreamina.DreaminaPipelineError, "ark_video.py"):
                dreamina.planned_request(
                    video,
                    prompt,
                    [],
                    {"duration_seconds": 4.0, "width": 1280, "height": 720},
                    args,
                )

    def test_controlled_preflight_requires_the_same_profile_at_generate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            manifest_path = root / "preflight.json"
            upload_manifest = root / "upload-preparation.json"
            video.write_bytes(b"source")
            upload_manifest.write_text("{}", encoding="utf-8")
            profile = dreamina.get_backend_profile("dreamina_cli_seedance_2_5")
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": dreamina.PREFLIGHT_SCHEMA_VERSION,
                        "preflight_passed": True,
                        "transport": dreamina.DREAMINA_TRANSPORT,
                        "backend_profile": profile.profile_id,
                        "constraints_digest": profile.constraints_digest,
                        "model_version": "seedance2.5",
                        "upload_preparation_manifest": str(upload_manifest.resolve()),
                        "upload_preparation_manifest_sha256": dreamina.sha256_file(upload_manifest),
                        "active_video": {
                            "path": str(video.resolve()),
                            "sha256": dreamina.sha256_file(video),
                            "metadata": {"duration_seconds": 4.0},
                        },
                        "privacy": {
                            "status": "workflow-selected-input",
                            "active_video_sha256": dreamina.sha256_file(video),
                            "remote_upload_authorized": True,
                            "paid_task_authorized": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            dreamina.validate_preflight_manifest(
                manifest_path,
                video.resolve(),
                profile=profile,
                model_version="seedance2.5",
            )
            with self.assertRaisesRegex(dreamina.DreaminaPipelineError, "同一个 --backend-profile"):
                dreamina.validate_preflight_manifest(manifest_path, video.resolve())

    def test_probe_and_generate_parsers_accept_profile_arguments(self) -> None:
        parser = dreamina.build_parser()
        probe = parser.parse_args(
            [
                "probe",
                "--video",
                "/tmp/source.mp4",
                "--privacy-status",
                "workflow-selected-input",
                "--backend-profile",
                "dreamina_cli_seedance_2_5",
                "--model-version",
                "seedance2.5",
            ]
        )
        generate = parser.parse_args(
            [
                "generate",
                "--video",
                "/tmp/source.mp4",
                "--preflight-manifest",
                "/tmp/preflight.json",
                "--prompt-file",
                "/tmp/prompt.txt",
                "--backend-profile",
                "dreamina_cli_seedance_2_5",
                "--model-version",
                "seedance2.5",
            ]
        )
        self.assertEqual(probe.backend_profile, "dreamina_cli_seedance_2_5")
        self.assertEqual(generate.model_version, "seedance2.5")


if __name__ == "__main__":
    unittest.main()

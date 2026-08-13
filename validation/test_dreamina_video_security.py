import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import dreamina_video as dreamina  # noqa: E402


class DreaminaVideoSecurityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_global_state_override_is_exact_absolute_path(self):
        state = self.root / "global-state"
        with mock.patch.dict(
            os.environ, {dreamina.STATE_OVERRIDE_NAME: str(state)}, clear=True
        ):
            self.assertEqual(dreamina.request_state_root(None), state.resolve())
        with mock.patch.dict(
            os.environ, {dreamina.STATE_OVERRIDE_NAME: "relative-state"}, clear=True
        ):
            with self.assertRaises(dreamina.DreaminaPipelineError):
                dreamina.request_state_root(None)

    def test_input_bindings_cover_prompt_and_ordered_reference_bytes(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("replace product", encoding="utf-8")
        first = self.root / "first.png"
        second = self.root / "second.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        preflight = {
            "input_bindings": dreamina.build_input_bindings(prompt, [first, second])
        }
        dreamina.validate_input_bindings(
            preflight, prompt, [first, second], required=True
        )
        with self.assertRaises(dreamina.DreaminaPipelineError):
            dreamina.validate_input_bindings(
                preflight, prompt, [second, first], required=True
            )
        first.write_bytes(b"changed")
        with self.assertRaises(dreamina.DreaminaPipelineError):
            dreamina.validate_input_bindings(
                preflight, prompt, [first, second], required=True
            )
        self.assertEqual(
            dreamina.build_input_bindings(prompt, [])["reference_images"], []
        )

    def test_content_request_identity_is_path_independent(self):
        args = argparse.Namespace(
            model_version=None,
            resolution=None,
            duration=None,
            ratio=None,
            generate_audio=False,
        )
        metadata = {"duration_seconds": 5.0, "width": 1280, "height": 720}

        def make_inputs(name):
            root = self.root / name
            root.mkdir()
            video = root / "source.mp4"
            prompt = root / "prompt.txt"
            image = root / "reference.png"
            video.write_bytes(b"same-video")
            prompt.write_text("same prompt", encoding="utf-8")
            image.write_bytes(b"same-image")
            return video, prompt, image

        first = make_inputs("batch-a")
        second = make_inputs("batch-b")
        request_a = dreamina.planned_request(*first[:2], [first[2]], metadata, args)
        request_b = dreamina.planned_request(*second[:2], [second[2]], metadata, args)
        self.assertEqual(request_a, request_b)
        self.assertNotIn(str(self.root), json.dumps(request_a))

    def test_workflow_selected_input_is_authorized(self):
        video = self.root / "source.mp4"
        video.write_bytes(b"workflow-selected-source")
        record = dreamina.build_privacy_record(
            "workflow-selected-input",
            video.resolve(),
        )
        self.assertEqual(record["status"], "workflow-selected-input")
        self.assertEqual(record["active_video_sha256"], dreamina.sha256_file(video))
        self.assertTrue(record["remote_upload_authorized"])
        self.assertTrue(record["paid_task_authorized"])

    def test_retired_privacy_status_is_rejected(self):
        video = self.root / "source.mp4"
        video.write_bytes(b"source")
        with self.assertRaises(dreamina.DreaminaPipelineError):
            dreamina.build_privacy_record("anonymized", video.resolve())


if __name__ == "__main__":
    unittest.main()

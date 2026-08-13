#!/usr/bin/env python3
"""Deterministic decision tests for local upload-size preparation.

The tests deliberately replace FFmpeg probing/decoding with small local fakes.
They exercise the preparation state machine without reading or writing a real
media stream, starting an encoder, or contacting a backend.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("upload_preparation.py")
SPEC = importlib.util.spec_from_file_location("upload_preparation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
upload = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upload
SPEC.loader.exec_module(upload)


class UploadPreparationTests(unittest.TestCase):
    constraints = upload.UploadConstraints(
        limit_bytes=100,
        target_bytes=90,
        allowed_containers=("mp4",),
        allowed_video_codecs=("h264",),
        min_duration_seconds=1,
        max_duration_seconds=30,
        min_fps=24,
        max_fps=60,
        min_width=320,
        max_width=1920,
        min_height=240,
        max_height=1080,
    )

    def _metadata(self, path: Path) -> dict:
        resolved = path.resolve()
        return {
            "path": str(resolved),
            "size_bytes": resolved.stat().st_size,
            "size_mb": round(resolved.stat().st_size / 1_000_000, 3),
            "container": "mp4",
            "format_names": ["mp4"],
            "duration_seconds": 10.0,
            "width": 1280,
            "height": 720,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "has_audio": True,
        }

    @staticmethod
    def _write(path: Path, size: int, byte: bytes = b"x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(byte * size)

    def _prepare_with_fakes(self, source: Path, output_dir: Path, runner):
        with mock.patch.object(upload, "_ffmpeg_version", return_value="ffmpeg fake 1"), mock.patch.object(
            upload,
            "probe_video_metadata",
            side_effect=lambda path, *_args, **_kwargs: self._metadata(Path(path)),
        ), mock.patch.object(upload, "validate_full_decode", return_value=True):
            return upload.prepare_upload_video(
                source,
                output_dir,
                "test_profile",
                self.constraints,
                "fake-ffmpeg",
                runner=runner,
            )

    @staticmethod
    def _command_has(command: list[str], option: str, value: str | None = None) -> bool:
        if option not in command:
            return False
        return value is None or command[command.index(option) + 1] == value

    def test_under_and_equal_limit_use_original_file_unchanged(self) -> None:
        for size in (99, 100):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.mp4"
                output_dir = root / "prepared"
                original = b"s" * size
                source.write_bytes(original)
                commands: list[list[str]] = []

                def runner(command, *, timeout=None):
                    commands.append(list(command))
                    return (0, "", "")

                result = self._prepare_with_fakes(source, output_dir, runner)

                self.assertEqual(result["action"], "unchanged")
                self.assertEqual(Path(result["output_path"]), source.resolve())
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse((output_dir / upload.OUTPUT_FILENAME).exists())
                self.assertEqual(result["output"]["size_bytes"], size)
                self.assertTrue(result["full_decode_passed"])
                self.assertTrue(Path(result["manifest_path"]).is_file())
                self.assertFalse(any("-pass" in command for command in commands))

    def test_constraints_digest_is_independent_of_collection_order(self) -> None:
        first = upload.UploadConstraints(
            limit_bytes=100,
            target_bytes=90,
            allowed_containers=("mov", "mp4", "mov"),
            allowed_video_codecs=("hevc", "h264"),
            allowed_audio_codecs=("mp3", "aac"),
            allowed_heights=(720, 480),
        )
        second = upload.UploadConstraints(
            limit_bytes=100,
            target_bytes=90,
            allowed_containers=("mp4", "mov"),
            allowed_video_codecs=("h264", "hevc"),
            allowed_audio_codecs=("aac", "mp3"),
            allowed_heights=(480, 720),
        )
        self.assertEqual(upload.constraints_digest(first), upload.constraints_digest(second))

    def test_oversized_input_uses_remux_when_actual_result_fits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output_dir = root / "prepared"
            # Exactly one byte over the hard limit must take the preparation
            # path rather than silently reusing the input.
            original = b"s" * 101
            source.write_bytes(original)
            commands: list[list[str]] = []

            def runner(command, *, timeout=None):
                command = list(command)
                commands.append(command)
                if self._command_has(command, "-c", "copy"):
                    self._write(Path(command[-1]), 90, b"r")
                return (0, "", "")

            result = self._prepare_with_fakes(source, output_dir, runner)

            output = output_dir / upload.OUTPUT_FILENAME
            self.assertEqual(result["action"], "remuxed")
            self.assertEqual(Path(result["output_path"]), output.resolve())
            self.assertEqual(output.stat().st_size, 90)
            self.assertEqual(source.read_bytes(), original)
            self.assertNotEqual(output.resolve(), source.resolve())
            self.assertTrue(any(self._command_has(command, "-c", "copy") for command in commands))
            self.assertFalse(any(self._command_has(command, "-pass") for command in commands))

    def test_oversized_input_reencodes_after_remux_remains_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output_dir = root / "prepared"
            original = b"s" * 140
            source.write_bytes(original)
            commands: list[list[str]] = []

            def runner(command, *, timeout=None):
                command = list(command)
                commands.append(command)
                if self._command_has(command, "-c", "copy"):
                    self._write(Path(command[-1]), 120, b"r")
                elif self._command_has(command, "-pass", "2"):
                    self._write(Path(command[-1]), 90, b"e")
                return (0, "", "")

            result = self._prepare_with_fakes(source, output_dir, runner)

            self.assertEqual(result["action"], "reencoded")
            self.assertEqual((output_dir / upload.OUTPUT_FILENAME).stat().st_size, 90)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(
                sum(self._command_has(command, "-pass", "1") for command in commands), 1
            )
            self.assertEqual(
                sum(self._command_has(command, "-pass", "2") for command in commands), 1
            )
            self.assertFalse(list(output_dir.glob(".source-upload-ready-*.mp4")))

    def test_three_reencode_attempts_that_remain_over_limit_block_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output_dir = root / "prepared"
            source.write_bytes(b"s" * 150)
            commands: list[list[str]] = []

            def runner(command, *, timeout=None):
                command = list(command)
                commands.append(command)
                if self._command_has(command, "-c", "copy"):
                    self._write(Path(command[-1]), 130, b"r")
                elif self._command_has(command, "-pass", "2"):
                    self._write(Path(command[-1]), 120, b"e")
                return (0, "", "")

            with self.assertRaisesRegex(upload.UploadPreparationError, "三次受限重编码") as raised:
                self._prepare_with_fakes(source, output_dir, runner)

            self.assertEqual(
                sum(self._command_has(command, "-pass", "1") for command in commands), 3
            )
            self.assertEqual(
                sum(self._command_has(command, "-pass", "2") for command in commands), 3
            )
            self.assertFalse((output_dir / upload.OUTPUT_FILENAME).exists())
            manifest_path = Path(raised.exception.manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["action"], "blocked")
            self.assertFalse(manifest["full_decode_passed"])
            self.assertIn("三次受限重编码", manifest["error"])

    def test_decode_failure_blocks_even_an_under_limit_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output_dir = root / "prepared"
            source.write_bytes(b"s" * 99)

            with mock.patch.object(upload, "_ffmpeg_version", return_value="ffmpeg fake 1"), mock.patch.object(
                upload,
                "probe_video_metadata",
                side_effect=lambda path, *_args, **_kwargs: self._metadata(Path(path)),
            ), mock.patch.object(
                upload,
                "validate_full_decode",
                side_effect=upload.UploadPreparationError("FFmpeg 完整解码校验失败"),
            ):
                with self.assertRaisesRegex(upload.UploadPreparationError, "完整解码"):
                    upload.prepare_upload_video(
                        source,
                        output_dir,
                        "test_profile",
                        self.constraints,
                        "fake-ffmpeg",
                        runner=lambda *_args, **_kwargs: (0, "", ""),
                    )

            self.assertFalse((output_dir / upload.OUTPUT_FILENAME).exists())
            manifest = json.loads(
                (output_dir / upload.MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["action"], "blocked")
            self.assertFalse(manifest["full_decode_passed"])

    def test_existing_frozen_output_or_manifest_fails_closed_without_running_ffmpeg(self) -> None:
        for existing_name in (upload.OUTPUT_FILENAME, upload.MANIFEST_FILENAME):
            with self.subTest(existing_name=existing_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.mp4"
                output_dir = root / "prepared"
                output_dir.mkdir()
                source.write_bytes(b"s" * 99)
                existing = output_dir / existing_name
                existing.write_text("frozen", encoding="utf-8")
                runner = mock.Mock(return_value=(0, "", ""))

                with self.assertRaisesRegex(upload.UploadPreparationError, "拒绝覆盖"):
                    upload.prepare_upload_video(
                        source,
                        output_dir,
                        "test_profile",
                        self.constraints,
                        "fake-ffmpeg",
                        runner=runner,
                    )

                runner.assert_not_called()
                self.assertEqual(existing.read_text(encoding="utf-8"), "frozen")

    def test_source_named_as_upload_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / upload.OUTPUT_FILENAME
            original = b"original-source-must-survive"
            source.write_bytes(original)
            runner = mock.Mock(return_value=(0, "", ""))

            with self.assertRaisesRegex(upload.UploadPreparationError, "拒绝覆盖"):
                upload.prepare_upload_video(
                    source,
                    root,
                    "test_profile",
                    self.constraints,
                    "fake-ffmpeg",
                    runner=runner,
                )

            runner.assert_not_called()
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((root / upload.MANIFEST_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()

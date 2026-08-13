#!/usr/bin/env python3
"""Prepare a PCM-audio H.264 source for the current Dreamina input profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dreamina_video import (
    DreaminaPipelineError,
    ffmpeg_executable,
    probe_video,
    sha256_file,
    validate_full_decode,
    validate_reference_video,
)


class PreparationError(RuntimeError):
    pass


def h264_stream_sha256(path: Path) -> str:
    process = subprocess.Popen(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "h264",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    digest = hashlib.sha256()
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    _, stderr = process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise PreparationError(f"无法提取 H.264 视频流：{detail}")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="保持 H.264 视频流不变，只把源音轨转为 AAC。"
    )
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.video).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if not source.is_file():
        raise PreparationError(f"找不到源视频：{source}")
    if source == output:
        raise PreparationError("技术准备输出不能覆盖源视频")
    if output.exists():
        raise PreparationError(f"输出已存在：{output}")

    source_meta = probe_video(source)
    if source_meta.get("video_codec") != "h264":
        raise PreparationError("当前技术准备只接受 H.264 源视频")
    if not source_meta.get("has_audio") or source_meta.get("audio_codec") == "aac":
        raise PreparationError("源视频没有需要转为 AAC 的非 AAC 音轨")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".tmp.mp4")
    command = [
        ffmpeg_executable(),
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise PreparationError(
            f"音轨转 AAC 失败：{completed.stderr.strip() or completed.returncode}"
        )
    os.replace(temporary, output)
    output_meta = validate_reference_video(output, probe_video(output))
    validate_full_decode(output)
    if abs(
        float(source_meta["duration_seconds"])
        - float(output_meta["duration_seconds"])
    ) > 0.1:
        raise PreparationError("技术准备前后时长误差超过 0.1 秒")
    if (
        source_meta["width"],
        source_meta["height"],
        source_meta["fps"],
    ) != (
        output_meta["width"],
        output_meta["height"],
        output_meta["fps"],
    ):
        raise PreparationError("技术准备改变了分辨率或帧率")
    stream_sha = h264_stream_sha256(source)
    if h264_stream_sha256(output) != stream_sha:
        raise PreparationError("技术准备改变了 H.264 视频流")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "operation": "video-stream-copy-audio-transcode-aac",
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "metadata": source_meta,
        },
        "output": {
            "path": str(output),
            "sha256": sha256_file(output),
            "metadata": output_meta,
        },
        "h264_video_stream_sha256": stream_sha,
        "technical_validation_passed": True,
        "remote_submission_authorized": False,
    }
    manifest_path = output.parent / "source-preparation.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        output.chmod(0o600)
        manifest_path.chmod(0o600)
    print(
        json.dumps(
            {**manifest, "manifest_path": str(manifest_path)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PreparationError, DreaminaPipelineError) as exc:
        print(f"错误：{exc}", file=os.sys.stderr)
        raise SystemExit(1)

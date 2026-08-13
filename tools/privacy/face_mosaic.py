#!/usr/bin/env python3
"""Run the workflow-owned, explicitly installed OpenScrub face mosaic."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, List, Mapping, Optional


LOCK_FILE = Path(__file__).with_name("openscrub-requirements.lock.txt")
INSTALL_MANIFEST = "video-replacer-install.json"
INSTALL_SCHEMA_VERSION = 3
YUNET_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
    "main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
INSTALL_MANIFEST_FIELDS = {
    "schema_version",
    "requirements_sha256",
    "python",
    "python_version",
    "openscrub_sha256",
    "runtime_smoke",
    "yunet_sha256",
}
SAFE_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LOCALAPPDATA",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
}


class MosaicError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def child_environment(source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Keep runtime basics while excluding provider credentials and tokens."""

    source_env = os.environ if source is None else source
    allowed = {name.casefold() for name in SAFE_ENV_NAMES}
    environment = {
        key: value
        for key, value in source_env.items()
        if key.casefold() in allowed or key.upper().startswith("LC_")
    }
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def path_contains_symlink(path: Path) -> bool:
    """Compatibility name: reject symlinks and Windows junctions."""

    absolute = lexical_absolute(path)
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if is_link_like(cursor):
            return True
    return False


def default_cache_root() -> Path:
    configured = os.getenv("VIDEO_REPLACER_CACHE_DIR", "").strip()
    if configured:
        return lexical_absolute(Path(configured))
    lock_id = sha256_file(LOCK_FILE)[:16]
    if os.name == "nt":
        base = Path(
            os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
        return lexical_absolute(base / "video-replacer" / "cache" / f"openscrub-{lock_id}")
    return lexical_absolute(Path.home() / ".cache" / "video-replacer" / f"openscrub-{lock_id}")


def python312() -> Path:
    configured = os.getenv("VIDEO_REPLACER_PYTHON", "").strip()
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        Path(value)
        for name in ("python3.12", "python3", "python")
        if (value := shutil.which(name))
    )
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [str(candidate), "-c", "import sys; print(sys.version_info >= (3, 12))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            env=child_environment(),
        )
        if result.returncode == 0 and result.stdout.strip() == "True":
            return candidate.resolve()
    raise MosaicError("找不到 Python 3.12；请让仓库 Agent 重新安装本地马赛克能力。")


def venv_command(venv: Path, name: str) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def interpreter_version(python: Path) -> Optional[List[int]]:
    try:
        result = subprocess.run(
            [str(python), "-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=15,
            env=child_environment(),
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(not isinstance(part, int) or isinstance(part, bool) for part in value)
    ):
        return None
    return value


def openscrub_smoke(executable: Path) -> bool:
    try:
        result = subprocess.run(
            [str(executable), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=120,
            env=child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "usage:" in result.stdout.casefold()


def install_identity(venv: Path, executable: Path) -> Dict[str, object]:
    python = venv_command(venv, "python")
    version = interpreter_version(python)
    if version is None:
        raise MosaicError("OpenScrub venv Python 身份校验失败。")
    return {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "requirements_sha256": sha256_file(LOCK_FILE),
        "python": str(python.resolve()),
        "python_version": version,
        "openscrub_sha256": sha256_file(executable),
        "runtime_smoke": True,
        "yunet_sha256": YUNET_SHA256,
    }


def verified_openscrub(cache: Path) -> Optional[Path]:
    cache = lexical_absolute(cache)
    if path_contains_symlink(cache):
        return None
    venv = cache / "venv"
    executable = venv_command(venv, "openscrub")
    python = venv_command(venv, "python")
    marker = venv / INSTALL_MANIFEST
    model = cache / "openscrub-home" / ".openscrub" / "models" / YUNET_FILENAME
    if (
        is_link_like(cache)
        or is_link_like(venv)
        or is_link_like(executable)
        or is_link_like(marker)
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or not python.is_file()
        or not marker.is_file()
        or not model.is_file()
    ):
        return None
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(recorded, dict):
        return None
    if set(recorded) != INSTALL_MANIFEST_FIELDS:
        return None
    if recorded.get("schema_version") != INSTALL_SCHEMA_VERSION:
        return None
    if recorded.get("requirements_sha256") != sha256_file(LOCK_FILE):
        return None
    if recorded.get("python") != str(python.resolve()):
        return None
    if recorded.get("python_version") != interpreter_version(python):
        return None
    if recorded.get("openscrub_sha256") != sha256_file(executable):
        return None
    if recorded.get("runtime_smoke") is not True:
        return None
    if recorded.get("yunet_sha256") != YUNET_SHA256:
        return None
    if sha256_file(model) != YUNET_SHA256:
        return None
    if not openscrub_smoke(executable):
        return None
    return executable.resolve()


def install_openscrub(cache: Path) -> Path:
    """Install the pinned optional dependency only after an explicit setup call."""

    cache = lexical_absolute(cache)
    if path_contains_symlink(cache):
        raise MosaicError(f"OpenScrub cache 不能是符号链接：{cache}")
    cache.mkdir(parents=True, exist_ok=True)
    existing = verified_openscrub(cache)
    if existing:
        return existing

    final_venv = cache / "venv"
    if is_link_like(final_venv):
        raise MosaicError(f"拒绝替换符号链接目录：{final_venv}")
    if final_venv.exists():
        shutil.rmtree(final_venv)

    interpreter = python312()
    environment = child_environment()
    try:
        subprocess.run(
            [str(interpreter), "-m", "venv", str(final_venv)],
            check=True,
            env=environment,
        )
        installer = venv_command(final_venv, "python")
        result = subprocess.run(
            [
                str(installer),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(LOCK_FILE),
            ],
            check=False,
            env=environment,
        )
        executable = venv_command(final_venv, "openscrub")
        if (
            result.returncode != 0
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
            or not openscrub_smoke(executable)
        ):
            raise MosaicError("OpenScrub 安装失败。")
        model = cache / "openscrub-home" / ".openscrub" / "models" / YUNET_FILENAME
        model.parent.mkdir(parents=True, exist_ok=True)
        candidate = model.with_name(f".{model.name}.{os.getpid()}.download")
        request = urllib.request.Request(
            YUNET_URL, headers={"User-Agent": "video-replacer-installer/1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response, candidate.open("xb") as handle:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 4 * 1024 * 1024:
                        raise MosaicError("YuNet 人脸模型下载超过大小上限。")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if sha256_file(candidate) != YUNET_SHA256:
                raise MosaicError("YuNet 人脸模型 SHA-256 校验失败。")
            os.replace(candidate, model)
        finally:
            candidate.unlink(missing_ok=True)
        runtime_smoke(cache, executable)
        marker = final_venv / INSTALL_MANIFEST
        marker.write_text(
            json.dumps(install_identity(final_venv, executable), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        if final_venv.exists() and not is_link_like(final_venv):
            shutil.rmtree(final_venv)
        raise

    installed = verified_openscrub(cache)
    if not installed:
        raise MosaicError("OpenScrub 安装校验失败。")
    return installed


def ensure_openscrub(cache: Path) -> Path:
    executable = verified_openscrub(cache)
    if executable:
        return executable
    raise MosaicError("OpenScrub 尚未安装或合同已变化；请让仓库 Agent 重新安装马赛克能力。")


def ffmpeg_shim_name(platform_name: Optional[str] = None) -> str:
    platform_name = os.name if platform_name is None else platform_name
    return "ffmpeg.exe" if platform_name == "nt" else "ffmpeg"


def materialize_ffmpeg_shim(
    target: Path, shim: Path, platform_name: Optional[str] = None
) -> None:
    platform_name = os.name if platform_name is None else platform_name
    if shim.exists() or is_link_like(shim):
        shim.unlink()
    if platform_name == "nt":
        try:
            os.link(target, shim)
        except OSError:
            shutil.copy2(target, shim)
        if sha256_file(shim) != sha256_file(target):
            shim.unlink(missing_ok=True)
            raise MosaicError("FFmpeg Windows shim 身份校验失败。")
    else:
        shim.symlink_to(target)


def ffmpeg_directory(cache: Path) -> Path:
    configured = os.getenv("FFMPEG", "").strip()
    found = configured if configured and Path(configured).is_file() else shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg  # type: ignore

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise MosaicError("找不到 ffmpeg。") from exc
    target = Path(found).resolve()
    native_name = ffmpeg_shim_name()
    if target.name.casefold() == native_name.casefold():
        return target.parent
    bin_dir = cache / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if path_contains_symlink(bin_dir):
        raise MosaicError("FFmpeg shim 目录不能使用链接或 junction。")
    shim = bin_dir / native_name
    materialize_ffmpeg_shim(target, shim)
    return bin_dir


def runtime_smoke(cache: Path, executable: Path) -> None:
    """Exercise OpenScrub, YuNet, and FFmpeg with generated no-user media."""

    ffmpeg_dir = ffmpeg_directory(cache)
    ffmpeg = ffmpeg_dir / ffmpeg_shim_name()
    environment = child_environment()
    environment["PATH"] = str(ffmpeg_dir) + os.pathsep + environment.get("PATH", "")
    environment["HOME"] = str(cache / "openscrub-home")
    environment["USERPROFILE"] = str(cache / "openscrub-home")
    environment["XDG_CACHE_HOME"] = str(cache / "runtime-cache")
    with tempfile.TemporaryDirectory(prefix=".mosaic-smoke-", dir=cache) as temporary:
        temporary_root = Path(temporary)
        source = temporary_root / "input.mp4"
        output = temporary_root / "output.mp4"
        generated = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=5:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
            env=environment,
        )
        if generated.returncode != 0 or not source.is_file():
            raise MosaicError("FFmpeg 合成 smoke 视频失败。")
        checked = subprocess.run(
            [
                str(executable),
                str(source),
                "-o",
                str(output),
                "--no-ner",
                "--categories",
                "face",
                "--mode",
                "mosaic",
                "--device",
                "cpu",
                "--encoder",
                "x264",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=300,
            env=environment,
        )
        if checked.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise MosaicError("OpenScrub 合成视频 smoke 失败。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or run the workflow-owned best-effort face mosaic."
    )
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--video")
    parser.add_argument("--output")
    parser.add_argument("--cache-dir")
    args = parser.parse_args()

    cache = (
        lexical_absolute(Path(args.cache_dir))
        if args.cache_dir
        else default_cache_root()
    )
    if args.install_only:
        executable = install_openscrub(cache)
        print(json.dumps({"installed": str(executable), "cache": str(cache)}, ensure_ascii=False))
        return 0
    if not args.video or not args.output:
        raise MosaicError("运行打码需要同时提供 --video 和 --output。")

    source = Path(args.video).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if not source.is_file():
        raise MosaicError(f"找不到源视频：{source}")
    if output == source:
        raise MosaicError("打码输出不能覆盖源视频。")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    executable = ensure_openscrub(cache)
    command = [
        str(executable),
        str(source),
        "-o",
        str(output),
        "--no-ner",
        "--categories",
        "face",
        "--mode",
        "mosaic",
        "--face-threshold",
        "0.6",
        "--face-expand",
        "0.15",
        "--device",
        "cpu",
        "--encoder",
        "x264",
    ]
    environment = child_environment()
    environment["HOME"] = str(cache / "openscrub-home")
    environment["USERPROFILE"] = str(cache / "openscrub-home")
    environment["PATH"] = str(ffmpeg_directory(cache)) + os.pathsep + environment.get("PATH", "")
    environment["XDG_CACHE_HOME"] = str(cache / "runtime-cache")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        check=False,
    )
    log_path = output.with_suffix(".openscrub.log")
    log_path.write_text(result.stdout, encoding="utf-8")
    if os.name != "nt":
        log_path.chmod(0o600)
    if result.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise MosaicError(f"OpenScrub 处理失败；日志：{log_path}")
    print(json.dumps({"output": str(output), "log": str(log_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MosaicError, subprocess.CalledProcessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)

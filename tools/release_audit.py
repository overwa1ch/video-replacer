#!/usr/bin/env python3
"""Fail a release when public files cross the repository privacy boundary."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    Path(".gitattributes"),
    Path("AGENTS.md"),
    Path("AGENT-INSTALL-PROMPT.md"),
    Path("ARCHITECTURE.md"),
    Path("CONTRIBUTING.md"),
    Path("LICENSE.txt"),
    Path("NOTICE.txt"),
    Path("README.md"),
    Path("RELEASE.md"),
    Path("SECURITY.md"),
    Path("install.cmd"),
    Path("video-replacer.cmd"),
    Path("video-replacer-test.cmd"),
    Path(".agents/skills/video-replacer/SKILL.md"),
    Path("tools/bootstrap.py"),
    Path("tools/codex_node_home.py"),
    Path("tools/codex_artifact.py"),
    Path("tools/codex_wire_attestation.py"),
    Path("tools/doctor.py"),
    Path("tools/dreamina-install-manifest.json"),
    Path("tools/dreamina-version.json"),
    Path("tools/install_dreamina.py"),
    Path("tools/setup.py"),
    Path("tools/state_paths.py"),
    Path("tools/windows_launcher.py"),
    Path("tools/video_batch_orchestrator.mjs"),
    Path("tools/video_batch_loop.py"),
    Path("tools/video-to-prompt-model-catalog.json"),
}
MEDIA_SUFFIXES = {
    ".aac",
    ".avi",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
}
SENSITIVE_NAMES = {
    ".env",
    "cookies.json",
    "credentials",
    "credentials.json",
    "auth.json",
}
FORBIDDEN_TRACKED_ROOTS = {
    ".video-replacer",
    "assets/reference-images/catalog.json",
    "outputs",
    "workspace",
}
PRIVATE_HOME_RE = re.compile(
    r"(?:/" + "Users" + r"/|/" + "home" + r"/)([^/\s\"']+)/"
)
WINDOWS_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\([^\\\s\"']+)\\")
PUBLIC_HOME_PLACEHOLDERS = frozenset({"example", "user", "username", "you"})
TASK_ID_RE = re.compile(r"\bcgt-\d{14}-[a-z0-9]+\b", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")
PRESIGNED_RE = re.compile(
    r"https://[^\s\"'<>]+[?&](?:x-tos|x-amz)-(?:algorithm|credential|signature|security-token)=",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|GITHUB_TOKEN|VIDEO_REPLACER_ARK_API_KEY|"
    r"VIDEO_REPLACER_TOS_ACCESS_KEY|VIDEO_REPLACER_TOS_SECRET_KEY|"
    r"VIDEO_REPLACER_TOS_SECURITY_TOKEN)\b\s*[:=]\s*[\"']?([^\s\"',;]{12,})"
)


class AuditError(RuntimeError):
    pass


def contains_private_home_path(text: str, pattern: re.Pattern[str]) -> bool:
    """Return whether ``text`` contains a non-placeholder home directory."""

    return any(
        match.group(1).casefold() not in PUBLIC_HOME_PLACEHOLDERS
        for match in pattern.finditer(text)
    )


def git_paths() -> List[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AuditError(result.stderr.decode("utf-8", errors="replace").strip())
    paths = (Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value)
    # A dirty pre-release worktree can still have tracked deletions in the
    # index. Audit the payload that actually exists, not those retired paths.
    return sorted(
        path for path in paths if (REPO_ROOT / path).exists() or (REPO_ROOT / path).is_symlink()
    )


def placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    return normalized.startswith(
        ("<", "${", "__", "example", "fake", "placeholder", "replace-with", "test", "your-")
    ) or set(normalized) <= {"a", "b", "c", "d", "e", "f", "0", "1"}


def scan_paths(paths: Sequence[Path]) -> List[str]:
    errors: List[str] = []
    for required in sorted(REQUIRED_FILES):
        if not (REPO_ROOT / required).is_file():
            errors.append(f"missing required public file: {required}")
    for relative in paths:
        path = REPO_ROOT / relative
        if not path.is_file() or path.is_symlink():
            if path.is_symlink():
                errors.append(f"symlink is not allowed in release payload: {relative}")
            continue
        as_posix = relative.as_posix()
        if relative.suffix.casefold() in MEDIA_SUFFIXES:
            errors.append(f"tracked media is forbidden: {relative}")
        name = relative.name.casefold()
        if name in SENSITIVE_NAMES or (name.startswith(".env.") and name != ".env.example"):
            errors.append(f"credential-like filename is forbidden: {relative}")
        if any(as_posix == root or as_posix.startswith(root + "/") for root in FORBIDDEN_TRACKED_ROOTS):
            errors.append(f"runtime path is forbidden in release payload: {relative}")
        if path.stat().st_size > 5 * 1024 * 1024:
            errors.append(f"file exceeds 5 MiB release limit: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if contains_private_home_path(text, PRIVATE_HOME_RE):
            errors.append(f"private POSIX home path in {relative}")
        if contains_private_home_path(text, WINDOWS_HOME_RE):
            errors.append(f"private Windows home path in {relative}")
        if TASK_ID_RE.search(text):
            errors.append(f"remote task id in {relative}")
        if "test" not in relative.parts and "examples" not in relative.parts and UUID_RE.search(text):
            errors.append(f"UUID-shaped remote task id in {relative}")
        if PRIVATE_KEY_RE.search(text):
            errors.append(f"private key material in {relative}")
        if PRESIGNED_RE.search(text):
            errors.append(f"presigned storage URL in {relative}")
        if not ("test" in relative.parts or relative.name.startswith("test_")):
            for match in SECRET_ASSIGNMENT_RE.finditer(text):
                if not placeholder(match.group(1)):
                    errors.append(f"credential assignment in {relative}")
                    break
    return errors


def scan_history() -> List[str]:
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if objects.returncode != 0:
        return ["unable to enumerate Git history: " + objects.stderr.strip()]

    object_paths: dict[str, str] = {}
    for line in objects.stdout.splitlines():
        object_id, separator, path = line.partition(" ")
        if separator and path:
            object_paths.setdefault(object_id, path)
    if not object_paths:
        return []

    batch_check = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=REPO_ROOT,
        input="\n".join(object_paths) + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if batch_check.returncode != 0:
        return ["unable to inspect Git history objects: " + batch_check.stderr.strip()]

    errors: List[str] = []
    retired_launchagent = "/".join(("automation", "com" + "." + "deco" + ".video-batch-loop.plist"))
    retired_distribution = "/".join(("distribution", "video-replacement-loop")) + "/"
    for line in batch_check.stdout.splitlines():
        try:
            object_id, object_type, raw_size = line.split(" ", 2)
            size = int(raw_size)
        except ValueError:
            errors.append(f"unable to parse Git object metadata: {line}")
            continue
        if object_type != "blob":
            continue
        path = object_paths.get(object_id, "<unknown>")
        relative = Path(path)
        name = relative.name.casefold()
        if relative.suffix.casefold() in MEDIA_SUFFIXES:
            errors.append(f"Git history contains media: {path}")
        if name in SENSITIVE_NAMES or (name.startswith(".env.") and name != ".env.example"):
            errors.append(f"Git history contains credential-like file: {path}")
        if path == retired_launchagent:
            errors.append("Git history contains the retired personal LaunchAgent")
        if path.startswith(retired_distribution):
            errors.append("Git history contains the retired distribution tree")
        if size > 5 * 1024 * 1024:
            errors.append(f"Git history contains a blob over 5 MiB: {path}")
            continue
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if blob.returncode != 0:
            errors.append(f"unable to read historical blob: {path}")
            continue
        try:
            text = blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if contains_private_home_path(text, PRIVATE_HOME_RE) or contains_private_home_path(
            text, WINDOWS_HOME_RE
        ):
            errors.append(f"Git history contains a private home path: {path}")
        if TASK_ID_RE.search(text) or (
            "test" not in relative.parts
            and "examples" not in relative.parts
            and UUID_RE.search(text)
        ):
            errors.append(f"Git history contains a remote task id: {path}")
        if PRIVATE_KEY_RE.search(text):
            errors.append(f"Git history contains private key material: {path}")
        if PRESIGNED_RE.search(text):
            errors.append(f"Git history contains a presigned storage URL: {path}")
        if "test" not in relative.parts and not relative.name.startswith("test_"):
            for match in SECRET_ASSIGNMENT_RE.finditer(text):
                if not placeholder(match.group(1)):
                    errors.append(f"Git history contains a credential assignment: {path}")
                    break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit public release files and optional history.")
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--list-files",
        action="store_true",
        help="print the exact audited payload paths, one per line",
    )
    args = parser.parse_args()
    paths = git_paths()
    errors = scan_paths(paths)
    if args.history:
        errors.extend(scan_history())
    if errors:
        for error in sorted(set(errors)):
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    if args.list_files:
        for path in paths:
            print(path.as_posix())
    else:
        print(f"release audit passed: {len(paths)} public files")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuditError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

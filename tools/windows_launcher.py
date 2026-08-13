#!/usr/bin/env python3
"""Native Windows public launcher routing with argv-safe child execution."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
RESERVED_FLAGS = {"--config", "--project-root", "--root", "--python", "--engine"}


def overridden_flags(arguments: Sequence[str]) -> list[str]:
    return sorted(
        {
            token.split("=", 1)[0]
            for token in arguments
            if token.split("=", 1)[0] in RESERVED_FLAGS
        }
    )


def route(
    arguments: Sequence[str],
    *,
    repo_root: Path = REPO_ROOT,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    environment = os.environ if environment is None else environment
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    setup = repo_root / "tools" / "setup.py"
    orchestrator = repo_root / "tools" / "video_batch_orchestrator.mjs"
    node = environment.get("VIDEO_REPLACER_NODE", "").strip() or "node"

    overridden = overridden_flags(arguments)
    if overridden:
        raise ValueError(
            "public launcher owns infrastructure options: " + ", ".join(overridden)
        )

    if arguments and arguments[0] == "setup":
        return [str(python), str(setup), *arguments[1:]]

    if arguments and arguments[0] == "status":
        remaining = list(arguments[1:])
        if remaining not in ([], ["--json"]):
            raise ValueError("public status accepts only the optional --json flag")
        return [
            node,
            str(orchestrator),
            "--project-root",
            str(repo_root),
            "--root",
            str(repo_root / "workspace" / "video-loop"),
            "--python",
            str(python),
            "status",
            *remaining,
        ]

    if arguments and arguments[0] in {"help", "--help", "-h"}:
        if len(arguments) != 1:
            raise ValueError("public help does not accept infrastructure options")
        return [
            node,
            str(orchestrator),
            "--project-root",
            str(repo_root),
            "--root",
            str(repo_root / "workspace" / "video-loop"),
            "--python",
            str(python),
            "help",
        ]

    return [str(python), str(setup), "launch", "--", *arguments]


def main(
    arguments: Sequence[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        print("ERROR: repository runtime is missing; run install.cmd first.", file=sys.stderr)
        return 2
    try:
        command = route(arguments, repo_root=repo_root, environment=environment)
    except ValueError as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    result = runner(command, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

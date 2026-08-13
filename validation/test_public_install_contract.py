"""Guards the standalone clone/install/public-release contract."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = load_module("public_bootstrap", REPO_ROOT / "tools" / "bootstrap.py")
doctor = load_module("public_doctor", REPO_ROOT / "tools" / "doctor.py")
mosaic = load_module(
    "public_face_mosaic", REPO_ROOT / "tools" / "privacy" / "face_mosaic.py"
)


class PublicInstallContractTests(unittest.TestCase):
    def test_repository_is_the_complete_project(self) -> None:
        documents = [
            REPO_ROOT / "README.md",
            REPO_ROOT / ".agents" / "skills" / "video-replacer" / "SKILL.md",
            REPO_ROOT / "workflows" / "video-replacement-batch-folder-loop-v1.md",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        self.assertIn("REPO_ROOT/workspace/video-loop", text)
        self.assertIn("REPO_ROOT/outputs/video-replacements", text)
        for retired in ("PROJECT_SHELL", "HANDOFF.md", "../../workspace"):
            self.assertNotIn(retired, text)

    def test_install_surface_and_public_files_exist(self) -> None:
        required = (
            "install",
            "install.cmd",
            "video-replacer",
            "video-replacer.cmd",
            "video-replacer-test",
            "video-replacer-test.cmd",
            ".gitattributes",
            "AGENT-INSTALL-PROMPT.md",
            "tools/bootstrap.py",
            "tools/doctor.py",
            "tools/install_dreamina.py",
            "tools/dreamina_environment.py",
            "tools/dreamina-install-manifest.json",
            "tools/dreamina-version.json",
            "tools/setup.py",
            "tools/state_paths.py",
            "tools/release_audit.py",
            "requirements-core.lock.txt",
            ".env.example",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "ARCHITECTURE.md",
            ".github/workflows/ci.yml",
        )
        for relative in required:
            self.assertTrue((REPO_ROOT / relative).is_file(), relative)

    def test_install_entrypoints_reuse_the_repository_runtime_for_repairs(self) -> None:
        posix = (REPO_ROOT / "install").read_text(encoding="utf-8")
        windows = (REPO_ROOT / "install.cmd").read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python3", posix)
        self.assertLess(posix.index(".venv/bin/python3"), posix.index("python3.12 python3"))
        self.assertIn(".venv\\Scripts\\python.exe", windows)
        self.assertLess(
            windows.index('if exist "%REPOSITORY_PYTHON%"'),
            windows.index("py.exe -3.12"),
        )
        self.assertFalse(any((REPO_ROOT / "automation").glob("*.plist")))

    def test_runtime_and_media_paths_are_ignored(self) -> None:
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            "/workspace/",
            "/outputs/",
            "/assets/reference-images/*",
            ".video-replacer/",
            ".env",
            "credentials.json",
        ):
            self.assertIn(pattern, ignore)

    def test_readme_makes_setup_an_agent_owned_installation(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill = (
            REPO_ROOT / ".agents" / "skills" / "video-replacer" / "SKILL.md"
        ).read_text(encoding="utf-8")
        combined = "\n".join((readme, agents, skill))
        for phrase in (
            "不要让我运行命令或手工编辑配置",
            "First-run setup Gate",
            "setup status --json",
            "ready: true",
            "first-run-setup.md",
        ):
            self.assertIn(phrase, combined)
        self.assertIn('"schema_version": 3', readme)
        self.assertNotIn('"schema_version": 2', readme)
        self.assertNotIn("cp .env.example .env.local", readme)

    def test_download_prompt_requires_same_task_live_ready(self) -> None:
        prompt = (REPO_ROOT / "AGENT-INSTALL-PROMPT.md").read_text(encoding="utf-8")
        for phrase in (
            "同一个连续任务",
            "全部必需依赖（包括本地马赛克能力）",
            "实时返回 ready: true",
            "不要让我运行命令",
            "不要上传任何媒体",
            "install.cmd",
            "video-replacer.cmd",
            "Windows 11 x64",
            "不得改用 WSL/Git Bash",
            "永久改变 PowerShell execution policy",
            "含空格或中文",
        ):
            self.assertIn(phrase, prompt)

    def test_windows_ci_exercises_every_native_entrypoint(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "windows-2022",
            ".\\install.cmd --with-mosaic",
            ".\\video-replacer.cmd status --json",
            ".\\video-replacer-test.cmd",
            ".\\tools\\install_dreamina.py",
        ):
            self.assertIn(phrase, workflow)
        self.assertNotIn("windows-latest", workflow)
        self.assertNotIn(".\\.video-replacer\\bin\\dreamina.exe version", workflow)

    def test_windows_release_label_has_a_real_machine_hard_gate(self) -> None:
        release = (REPO_ROOT / "RELEASE.md").read_text(encoding="utf-8")
        for phrase in (
            "Windows 11 x64 hard Gate",
            "fresh ordinary local user",
            "spaces and Chinese characters",
            "Dreamina OAuth/device login",
            "second Agent session",
            'preferred `windows.sandbox="elevated"` mode',
            "preferred `elevated` sandbox must perform the complete",
            "separate `unelevated` fallback task",
            "must not run prompt preparation",
            "UAC/administrator package-install process",
            "stop after `prepare`",
            "no media upload, generation task, or credit use",
            "does not claim that the current candidate has passed it",
        ):
            self.assertIn(phrase, release)

    def test_windows_ready_requires_native_codex_executable(self) -> None:
        public_contract = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                REPO_ROOT / "README.md",
                REPO_ROOT / "AGENT-INSTALL-PROMPT.md",
                REPO_ROOT
                / ".agents"
                / "skills"
                / "video-replacer"
                / "references"
                / "first-run-setup.md",
                REPO_ROOT / "SECURITY.md",
            )
        )
        self.assertIn("native standalone `codex.exe`", public_contract)
        self.assertIn("`codex.cmd`", public_contract)
        self.assertIn("script wrappers are not accepted", (REPO_ROOT / "tools" / "doctor.py").read_text(encoding="utf-8"))

    def test_windows_public_state_root_and_split_wire_checks_are_explicit(self) -> None:
        public_contract = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                REPO_ROOT / "README.md",
                REPO_ROOT / "SECURITY.md",
                REPO_ROOT / "ARCHITECTURE.md",
                REPO_ROOT / "AGENT-INSTALL-PROMPT.md",
                REPO_ROOT / "RELEASE.md",
                REPO_ROOT
                / ".agents"
                / "skills"
                / "video-replacer"
                / "references"
                / "first-run-setup.md",
            )
        )
        for phrase in (
            "FOLDERID_LocalAppData",
            "LocalAppData/video-replacer",
            "VIDEO_REPLACER_STATE_DIR",
            "mapped",
            "reparse",
            "codex-wire",
            "codex-file-auth-wire",
        ):
            self.assertIn(phrase, public_contract)

    def test_release_history_audit_runs_in_an_independent_clean_root(self) -> None:
        release = (REPO_ROOT / "RELEASE.md").read_text(encoding="utf-8")
        self.assertIn("independent Git repository", release)
        self.assertIn("without `.git`", release)
        self.assertIn("Do not use an orphan or release branch inside the legacy repository", release)
        self.assertNotIn("separate reviewed release branch", release)
        self.assertLess(release.index("independent Git repository"), release.index("tools/release_audit.py --history"))

    def test_dreamina_install_contract_never_pipes_remote_shell(self) -> None:
        installer = (REPO_ROOT / "tools" / "install_dreamina.py").read_text(
            encoding="utf-8"
        )
        first_run = (
            REPO_ROOT
            / ".agents"
            / "skills"
            / "video-replacer"
            / "references"
            / "first-run-setup.md"
        ).read_text(encoding="utf-8")
        self.assertIn("SHA-256", installer + first_run)
        self.assertNotIn("curl -fsSL https://jimeng.jianying.com/cli | bash", installer)
        self.assertIn("does not execute the provider's remote shell installer", first_run)

    def test_dreamina_version_metadata_write_boundary_is_public(self) -> None:
        documents = (
            REPO_ROOT / "README.md",
            REPO_ROOT / "SECURITY.md",
            REPO_ROOT / "ARCHITECTURE.md",
            REPO_ROOT
            / ".agents"
            / "skills"
            / "video-replacer"
            / "references"
            / "first-run-setup.md",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        for phrase in (
            "tools/dreamina-version.json",
            "~/.dreamina_cli/version.json",
            r"%USERPROFILE%\.dreamina_cli\version.json",
            "non-sensitive",
            "credential files",
            "link-like",
        ):
            self.assertIn(phrase, text)

    def test_windows_dreamina_probe_mitigation_is_shared_and_bounded(self) -> None:
        helper = (REPO_ROOT / "tools" / "dreamina_environment.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("GetWindowsDirectoryW", helper)
        self.assertIn("GetSystemDirectoryW", helper)
        self.assertIn("NODEFAULTCURRENTDIRECTORYINEXEPATH", helper)
        self.assertIn("powershell.exe", helper)
        self.assertIn("pwsh.exe", helper)
        for relative in (
            "tools/install_dreamina.py",
            "tools/doctor.py",
            "tools/setup.py",
            "tools/dreamina_video.py",
        ):
            self.assertIn(
                "dreamina_environment",
                (REPO_ROOT / relative).read_text(encoding="utf-8"),
                relative,
            )
        public_text = "\n".join(
            (REPO_ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "README.md",
                "SECURITY.md",
                "ARCHITECTURE.md",
                ".agents/skills/video-replacer/references/first-run-setup.md",
                "workflows/video-replacement-batch-folder-loop-v1.md",
                "RELEASE.md",
            )
        )
        for phrase in (
            "NoDefaultCurrentDirectoryInExePath",
            "PowerShell/CIM",
            "not a general subprocess sandbox",
        ):
            self.assertIn(phrase, public_text)

    def test_bootstrap_environment_does_not_forward_provider_credentials(self) -> None:
        source = {
            "PATH": os.defpath,
            "HOME": "/home/example",
            "OPENAI_API_KEY": "must-not-cross",
            "GITHUB_TOKEN": "must-not-cross",
            "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross",
            "VIDEO_REPLACER_TOS_SECRET_KEY": "must-not-cross",
        }
        environment = bootstrap.safe_environment(source)
        self.assertEqual(environment["PATH"], os.defpath)
        for forbidden in (
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "VIDEO_REPLACER_ARK_API_KEY",
            "VIDEO_REPLACER_TOS_SECRET_KEY",
        ):
            self.assertNotIn(forbidden, environment)

    def test_mosaic_child_environment_is_credential_minimized(self) -> None:
        source = {
            "PATH": os.defpath,
            "HOME": "/home/example",
            "OPENAI_API_KEY": "must-not-cross",
            "VIDEO_REPLACER_ARK_API_KEY": "must-not-cross",
            "VIDEO_REPLACER_TOS_SECRET_KEY": "must-not-cross",
        }
        environment = mosaic.child_environment(source)
        self.assertEqual(environment["PATH"], os.defpath)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", environment)
        self.assertNotIn("VIDEO_REPLACER_TOS_SECRET_KEY", environment)

    def test_mosaic_never_installs_implicitly_during_a_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            with self.assertRaisesRegex(mosaic.MosaicError, "Agent.*重新安装"):
                mosaic.ensure_openscrub(cache)
            self.assertFalse(cache.exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable-bit contract")
    def test_mosaic_rejects_non_executable_or_tampered_install(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            cache = Path(temporary) / "cache"
            venv = cache / "venv"
            binary_dir = venv / "bin"
            binary_dir.mkdir(parents=True)
            python = binary_dir / "python"
            python.symlink_to(Path(sys.executable).resolve())
            executable = binary_dir / "openscrub"
            executable.write_text("#!/bin/sh\nprintf 'usage: openscrub\\n'\n", encoding="utf-8")
            executable.chmod(0o600)
            model = (
                cache
                / "openscrub-home"
                / ".openscrub"
                / "models"
                / mosaic.YUNET_FILENAME
            )
            model.parent.mkdir(parents=True)
            model.write_bytes(b"test model fixture")
            marker = venv / mosaic.INSTALL_MANIFEST
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": mosaic.INSTALL_SCHEMA_VERSION,
                        "requirements_sha256": mosaic.sha256_file(mosaic.LOCK_FILE),
                        "python": str(python.resolve()),
                        "python_version": mosaic.interpreter_version(python),
                        "openscrub_sha256": mosaic.sha256_file(executable),
                        "runtime_smoke": True,
                        "yunet_sha256": mosaic.YUNET_SHA256,
                    }
                ),
                encoding="utf-8",
            )
            actual_sha256_file = mosaic.sha256_file

            def fixture_sha256(path: Path) -> str:
                if Path(path) == model:
                    return mosaic.YUNET_SHA256
                return actual_sha256_file(Path(path))

            with mock.patch.object(mosaic, "sha256_file", side_effect=fixture_sha256):
                self.assertIsNone(mosaic.verified_openscrub(cache))
                executable.chmod(0o700)
                self.assertEqual(mosaic.verified_openscrub(cache), executable.resolve())
                executable.write_text("#!/bin/sh\nprintf 'tampered\\n'\n", encoding="utf-8")
                self.assertIsNone(mosaic.verified_openscrub(cache))

    @unittest.skipIf(os.name == "nt", "symlink fixture is POSIX-specific")
    def test_mosaic_installer_rejects_symlinked_cache_ancestors(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            fixture = Path(temporary)
            target = fixture / "target"
            target.mkdir()
            linked_parent = fixture / "linked-parent"
            linked_parent.symlink_to(target, target_is_directory=True)
            cache = linked_parent / "cache"
            with self.assertRaisesRegex(mosaic.MosaicError, "符号链接"):
                mosaic.install_openscrub(cache)
            self.assertFalse((target / "cache").exists())

    @unittest.skipIf(os.name == "nt", "symlink fixture is POSIX-specific")
    def test_doctor_rejects_symlinked_repository_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            repository = fixture / "repository"
            repository.mkdir()
            external_runtime = fixture / "external-runtime"
            for name in (
                "inbox",
                "needs-input",
                "ready",
                "running",
                "review",
                "blocked",
                "completed",
                "logs",
            ):
                (external_runtime / name).mkdir(parents=True, exist_ok=True)
            (repository / "workspace").mkdir()
            (repository / "workspace" / "video-loop").symlink_to(
                external_runtime, target_is_directory=True
            )
            (repository / "outputs" / "video-replacements").mkdir(parents=True)
            with mock.patch.object(doctor, "REPO_ROOT", repository):
                checks = {item.name: item for item in doctor.check_layout()}
            self.assertEqual(checks["runtime-layout"].status, "fail")

    def test_doctor_rejects_temporary_external_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            with mock.patch.dict(
                os.environ, {"VIDEO_REPLACER_STATE_DIR": str(state)}, clear=False
            ):
                checks = {item.name: item for item in doctor.check_layout()}
            self.assertEqual(checks["external-state"].status, "fail")

    def test_public_diagnostic_commands_reject_path_overrides(self) -> None:
        launcher = (
            REPO_ROOT / "video-replacer.cmd"
            if os.name == "nt"
            else REPO_ROOT / "video-replacer"
        )
        for arguments in (
            ("status", "--root", "/tmp/attacker"),
            ("help", "--engine", "/tmp/attacker.py"),
        ):
            result = subprocess.run(
                [str(launcher), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)

    def test_doctor_never_echoes_ark_secret_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {name: f"private-{index}" for index, name in enumerate(doctor.ARK_REQUIRED_ENV)},
            clear=True,
        ), mock.patch.object(doctor.importlib.util, "find_spec", return_value=object()):
            checks = doctor.check_ark(online=False)
        rendered = json.dumps([doctor.asdict(item) for item in checks])
        self.assertNotIn("private-", rendered)
        self.assertIn("all required names are present", rendered)

    def test_release_audit_passes_current_public_worktree(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "release_audit.py")],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()

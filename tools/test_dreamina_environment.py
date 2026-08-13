#!/usr/bin/env python3
"""Tests for the shared Dreamina subprocess environment."""

from __future__ import annotations

import unittest
from unittest import mock

from tools import dreamina_environment


class DreaminaEnvironmentTests(unittest.TestCase):
    def test_posix_environment_is_credential_minimized(self) -> None:
        result = dreamina_environment.dreamina_environment(
            {
                "HOME": "/home/example",
                "PATH": "/usr/bin:/bin",
                "VIDEO_REPLACER_ARK_API_KEY": "secret",
                "OPENAI_API_KEY": "secret",
            },
            windows=False,
        )
        self.assertEqual(result["HOME"], "/home/example")
        self.assertEqual(result["PATH"], "/usr/bin:/bin")
        self.assertNotIn("VIDEO_REPLACER_ARK_API_KEY", result)
        self.assertNotIn("OPENAI_API_KEY", result)

    def test_windows_removes_shell_probe_directories_and_cwd_lookup(self) -> None:
        source = {
            "USERPROFILE": "C:\\Users\\example",
            "SYSTEMROOT": "C:\\Windows",
            "WINDIR": "C:\\Windows",
            "COMSPEC": "C:\\attacker\\powershell.exe",
            "SHELL": "C:\\attacker\\pwsh.exe",
            "PATH": (
                "C:\\Windows\\System32\\WindowsPowerShell\\v1.0;"
                "C:\\Program Files\\PowerShell\\7;C:\\custom"
            ),
        }
        with mock.patch.object(
            dreamina_environment.shutil, "which", return_value=None
        ) as which:
            result = dreamina_environment.dreamina_environment(source, windows=True)
        self.assertEqual(
            result["PATH"], "C:\\Windows\\System32;C:\\Windows"
        )
        self.assertEqual(result["NODEFAULTCURRENTDIRECTORYINEXEPATH"], "1")
        self.assertEqual(result["COMSPEC"], "C:\\Windows\\System32\\cmd.exe")
        self.assertNotIn("SHELL", result)
        self.assertNotIn("PowerShell", result["PATH"])
        self.assertEqual(which.call_count, 2)

    def test_windows_requires_an_absolute_system_root(self) -> None:
        for value in ("", "Windows", "\\Windows"):
            with self.subTest(value=value), self.assertRaises(
                dreamina_environment.DreaminaEnvironmentError
            ):
                dreamina_environment.dreamina_environment(
                    {"USERPROFILE": "C:\\Users\\example", "SYSTEMROOT": value},
                    windows=True,
                )
        with self.assertRaisesRegex(
            dreamina_environment.DreaminaEnvironmentError, "disagree"
        ):
            dreamina_environment.dreamina_environment(
                {
                    "USERPROFILE": "C:\\Users\\example",
                    "SYSTEMROOT": "C:\\Windows",
                    "WINDIR": "D:\\Windows",
                },
                windows=True,
            )

    def test_windows_fails_if_a_shell_remains_resolvable(self) -> None:
        with mock.patch.object(
            dreamina_environment.shutil,
            "which",
            side_effect=lambda name, path: "C:/unexpected/" + name,
        ):
            with self.assertRaises(dreamina_environment.DreaminaEnvironmentError):
                dreamina_environment.dreamina_environment(
                    {
                        "USERPROFILE": "C:\\Users\\example",
                        "SYSTEMROOT": "C:\\Windows",
                    },
                    windows=True,
                )

    @unittest.skipUnless(__import__("os").name == "nt", "native Windows only")
    def test_native_windows_path_keeps_cmd_but_hides_powershell(self) -> None:
        environment = dreamina_environment.dreamina_environment()
        self.assertIsNotNone(
            dreamina_environment.shutil.which("cmd.exe", path=environment["PATH"])
        )
        for executable in dreamina_environment.WINDOWS_AGENT_DETECT_EXECUTABLES:
            self.assertIsNone(
                dreamina_environment.shutil.which(
                    executable, path=environment["PATH"]
                )
            )


if __name__ == "__main__":
    unittest.main()

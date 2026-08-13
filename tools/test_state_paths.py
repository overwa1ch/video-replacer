#!/usr/bin/env python3
"""Tests for the single external state-root authority."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_paths
import bootstrap
import codex_node_home
import doctor
import video_batch_loop

SETUP_MODULE_PATH = Path(__file__).with_name("setup.py")


class StatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_posix_absolute_override_semantics_are_preserved(self) -> None:
        expected = self.root / "operator-state"
        with mock.patch.object(state_paths, "_is_windows_host", return_value=False):
            self.assertEqual(
                state_paths.resolve_state_root(
                    {"VIDEO_REPLACER_STATE_DIR": str(expected)}
                ),
                expected,
            )
            with self.assertRaisesRegex(state_paths.StatePathError, "absolute"):
                state_paths.resolve_state_root(
                    {"VIDEO_REPLACER_STATE_DIR": "relative-state"}
                )

    def test_posix_xdg_default_semantics_are_preserved(self) -> None:
        base = self.root / "xdg"
        with mock.patch.object(state_paths, "_is_windows_host", return_value=False):
            self.assertEqual(
                state_paths.resolve_state_root({"XDG_STATE_HOME": str(base)}),
                base / "video-replacer",
            )

    def test_windows_ignores_environment_localappdata_and_accepts_only_known_root(self) -> None:
        known = self.root / "Known LocalAppData"
        known.mkdir()
        expected = (known / "video-replacer").resolve()
        unsafe = self.root / "shared-state"
        with mock.patch.object(state_paths, "_is_windows_host", return_value=True), mock.patch.object(
            state_paths, "_windows_known_local_app_data", return_value=known
        ), mock.patch.object(
            state_paths,
            "_windows_canonical_safe_path",
            side_effect=lambda path: Path(path).resolve(strict=False),
        ):
            self.assertEqual(
                state_paths.resolve_state_root({"LOCALAPPDATA": str(unsafe)}),
                expected,
            )
            self.assertEqual(
                state_paths.resolve_state_root(
                    {"VIDEO_REPLACER_STATE_DIR": str(expected)}
                ),
                expected,
            )
            with self.assertRaisesRegex(
                state_paths.StatePathError, "LocalAppData/video-replacer"
            ):
                state_paths.resolve_state_root(
                    {"VIDEO_REPLACER_STATE_DIR": str(unsafe)}
                )

    def test_all_entrypoints_delegate_to_the_same_authoritative_root(self) -> None:
        expected = self.root / "authoritative" / "video-replacer"
        source = {
            "LOCALAPPDATA": str(self.root / "forged-localappdata"),
            "VIDEO_REPLACER_STATE_DIR": str(expected),
        }
        with mock.patch.object(
            bootstrap, "resolve_state_root", return_value=expected
        ) as bootstrap_resolver, mock.patch.object(
            doctor, "resolve_state_root", return_value=expected
        ) as doctor_resolver, mock.patch.object(
            codex_node_home, "resolve_state_root", return_value=expected
        ) as codex_resolver, mock.patch.object(
            video_batch_loop, "resolve_state_root", return_value=expected
        ) as loop_resolver, mock.patch.object(
            video_batch_loop, "_reject_temporary_state_dir"
        ):
            with mock.patch.dict(os.environ, source, clear=False):
                self.assertEqual(bootstrap.external_state_root(), expected)
                self.assertEqual(doctor.external_state_path(), expected)
            self.assertEqual(
                codex_node_home.node_home_path(source),
                expected / codex_node_home.NODE_HOME_DIRECTORY,
            )
            self.assertEqual(
                video_batch_loop.executor_state_dir(self.root, source),
                expected.resolve(),
            )
        bootstrap_resolver.assert_called_once_with()
        doctor_resolver.assert_called_once_with()
        codex_resolver.assert_called_once_with(source)
        loop_resolver.assert_called_once_with(source)

    def test_setup_expected_state_root_uses_the_same_resolver(self) -> None:
        import importlib.util

        specification = importlib.util.spec_from_file_location(
            "state_paths_setup_contract", SETUP_MODULE_PATH
        )
        assert specification is not None and specification.loader is not None
        setup_module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(setup_module)
        expected = self.root / "authoritative" / "video-replacer"
        with mock.patch.object(
            setup_module, "resolve_state_root", return_value=expected
        ) as resolver:
            self.assertEqual(setup_module.expected_state_root(), expected)
        resolver.assert_called_once_with()

    def test_windows_reparse_or_nonfixed_path_fails_closed(self) -> None:
        known = self.root / "Known LocalAppData"
        known.mkdir()
        with mock.patch.object(state_paths, "_is_windows_host", return_value=True), mock.patch.object(
            state_paths, "_windows_known_local_app_data", return_value=known
        ), mock.patch.object(
            state_paths,
            "_windows_canonical_safe_path",
            side_effect=state_paths.StatePathError("reparse point"),
        ):
            with self.assertRaisesRegex(state_paths.StatePathError, "reparse"):
                state_paths.resolve_state_root({})

    @unittest.skipUnless(os.name == "nt", "native Windows Known Folder Gate")
    def test_native_windows_known_folder_gate(self) -> None:
        known = state_paths._windows_canonical_safe_path(
            state_paths._windows_known_local_app_data()
        )
        expected = state_paths.resolve_state_root({})
        self.assertEqual(expected, (known / "video-replacer").resolve(strict=False))
        self.assertEqual(
            state_paths.resolve_state_root(
                {"VIDEO_REPLACER_STATE_DIR": str(expected)}
            ),
            expected,
        )
        with self.assertRaises(state_paths.StatePathError):
            state_paths.resolve_state_root(
                {"VIDEO_REPLACER_STATE_DIR": tempfile.gettempdir()}
            )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the stable prompt-node credential and locking boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_node_home as node_home


FIXTURE_ID_TOKEN = (
    "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
    "eyJzdWIiOiJ2aWRlby1yZXBsYWNlci10ZXN0LWZpeHR1cmUiLCJodHRwczovL2FwaS5vcGVu"
    "YWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiZml4dHVyZS1hY2NvdW50LWlk"
    "LW5ldmVyLXZhbGlkIiwiY2hhdGdwdF91c2VyX2lkIjoiZml4dHVyZS11c2VyLWlkLW5ldmVy"
    "LXZhbGlkIn19."
    "c2lnbmF0dXJl"
)
FIXTURE_ACCESS_TOKEN = "fixture-access-token-never-valid"
FIXTURE_REFRESH_TOKEN = "fixture-refresh-token-never-valid"
FIXTURE_ACCOUNT_ID = "fixture-account-id-never-valid"


class CodexNodeHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _auth_payload() -> dict[str, object]:
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": FIXTURE_ID_TOKEN,
                "access_token": FIXTURE_ACCESS_TOKEN,
                "refresh_token": FIXTURE_REFRESH_TOKEN,
                "account_id": FIXTURE_ACCOUNT_ID,
            },
            "last_refresh": "2026-08-13T00:00:00Z",
        }

    @staticmethod
    def _write_auth(home: Path, payload: object) -> Path:
        auth = home / "auth.json"
        auth.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        return auth

    def _home(self) -> Path:
        home = self.root / "state" / node_home.NODE_HOME_DIRECTORY
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(node_home, "_temporary_roots", return_value=set()):
            result = node_home.ensure_node_home(home, repo_root=self.repo, source={})
        self._write_auth(result, self._auth_payload())
        return result

    def test_home_is_external_file_auth_only(self) -> None:
        home = self._home()
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(node_home, "_temporary_roots", return_value=set()):
            self.assertEqual(
                node_home.validate_node_home(
                    home, repo_root=self.repo, require_auth=True, source={}
                ),
                home.resolve(),
            )
        (home / "config.toml").write_text("model='unsafe'\n", encoding="utf-8")
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), self.assertRaisesRegex(node_home.CodexNodeHomeError, "forbidden"):
            node_home.validate_node_home(
                home, repo_root=self.repo, require_auth=True, source={}
            )

    def test_file_auth_accepts_supported_managed_identity_record(self) -> None:
        home = self._home()
        payload = self._auth_payload()
        payload["last_refresh"] = "2026-08-13T00:00:00.123456789Z"
        payload["agent_identity"] = {
            "agent_runtime_id": "fixture-runtime-id",
            "agent_private_key": "fixture-private-key-never-valid",
            "account_id": FIXTURE_ACCOUNT_ID,
            "chatgpt_user_id": "fixture-user-id",
            "email": "",
            "plan_type": "enterprise",
            "chatgpt_account_is_fedramp": False,
            "task_id": None,
        }
        auth = self._write_auth(home, payload)
        node_home.validate_file_auth(auth)

    def test_file_auth_digest_is_from_the_validated_snapshot_only(self) -> None:
        home = self._home()
        auth = home / "auth.json"
        self.assertEqual(
            node_home.file_auth_access_token_sha256(auth),
            hashlib.sha256(FIXTURE_ACCESS_TOKEN.encode("utf-8")).digest(),
        )

    @unittest.skipIf(os.name == "nt", "open-file replacement semantics differ on Windows")
    def test_file_auth_digest_rejects_path_swap_during_read(self) -> None:
        home = self._home()
        auth = home / "auth.json"
        replacement_token = "replacement-access-token-never-valid"
        replacement_payload = self._auth_payload()
        assert isinstance(replacement_payload["tokens"], dict)
        replacement_payload["tokens"]["access_token"] = replacement_token
        replacement = home / "replacement.json"
        replacement.write_text(
            json.dumps(replacement_payload) + "\n", encoding="utf-8"
        )
        replacement.chmod(0o600)

        real_fstat = os.fstat
        calls = 0

        def swap_after_read(descriptor: int):
            nonlocal calls
            metadata = real_fstat(descriptor)
            calls += 1
            if calls == 2:
                os.replace(replacement, auth)
            return metadata

        with mock.patch.object(
            node_home.os, "fstat", side_effect=swap_after_read
        ), self.assertRaises(node_home.CodexNodeHomeError) as caught:
            node_home.file_auth_access_token_sha256(auth)
        message = str(caught.exception)
        self.assertNotIn(FIXTURE_ACCESS_TOKEN, message)
        self.assertNotIn(replacement_token, message)

    def test_file_auth_rejects_empty_unknown_or_incomplete_payloads(self) -> None:
        home = self._home()
        invalid: list[tuple[str, object]] = [("empty", {})]

        payload = self._auth_payload()
        payload["unexpected"] = "fixture"
        invalid.append(("unknown top-level field", payload))

        payload = self._auth_payload()
        del payload["last_refresh"]
        invalid.append(("missing required top-level field", payload))

        payload = self._auth_payload()
        payload["auth_mode"] = "apikey"
        invalid.append(("wrong auth mode", payload))

        payload = self._auth_payload()
        payload["OPENAI_API_KEY"] = "fixture-api-key-never-valid"
        invalid.append(("api key material", payload))

        payload = self._auth_payload()
        assert isinstance(payload["tokens"], dict)
        del payload["tokens"]["refresh_token"]
        invalid.append(("missing token field", payload))

        payload = self._auth_payload()
        assert isinstance(payload["tokens"], dict)
        payload["tokens"]["extra"] = "fixture"
        invalid.append(("unknown token field", payload))

        payload = self._auth_payload()
        assert isinstance(payload["tokens"], dict)
        payload["tokens"]["access_token"] = "  "
        invalid.append(("empty access token", payload))

        payload = self._auth_payload()
        assert isinstance(payload["tokens"], dict)
        payload["tokens"]["account_id"] = None
        invalid.append(("empty account id", payload))

        payload = self._auth_payload()
        assert isinstance(payload["tokens"], dict)
        payload["tokens"]["id_token"] = "not-a-jwt"
        invalid.append(("invalid id token", payload))

        payload = self._auth_payload()
        payload["last_refresh"] = "2026-08-13"
        invalid.append(("timestamp without timezone", payload))

        payload = self._auth_payload()
        payload["personal_access_token"] = "fixture-pat-never-valid"
        invalid.append(("alternate auth material", payload))

        payload = self._auth_payload()
        payload["agent_identity"] = "fixture-agent-jwt-never-valid"
        invalid.append(("unsupported agent identity form", payload))

        for label, candidate in invalid:
            with self.subTest(label=label):
                auth = self._write_auth(home, candidate)
                with self.assertRaisesRegex(
                    node_home.CodexNodeHomeError, "supported ChatGPT file-auth schema"
                ):
                    node_home.validate_file_auth(auth)

    def test_file_auth_rejects_duplicate_keys_and_never_echoes_tokens(self) -> None:
        home = self._home()
        payload = self._auth_payload()
        serialized = json.dumps(payload)
        duplicate = serialized.replace(
            '"auth_mode": "chatgpt"',
            '"auth_mode": "chatgpt", "auth_mode": "chatgpt"',
            1,
        )
        auth = home / "auth.json"
        auth.write_text(duplicate, encoding="utf-8")
        if os.name != "nt":
            auth.chmod(0o600)
        with self.assertRaises(node_home.CodexNodeHomeError) as caught:
            node_home.validate_file_auth(auth)
        message = str(caught.exception)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(FIXTURE_ACCESS_TOKEN, message)
        self.assertNotIn(FIXTURE_REFRESH_TOKEN, message)
        self.assertNotIn(FIXTURE_ID_TOKEN, message)

    def test_ensure_home_rejects_existing_malformed_auth_before_login(self) -> None:
        home = self._home()
        self._write_auth(home, {})
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), self.assertRaisesRegex(
            node_home.CodexNodeHomeError, "supported ChatGPT file-auth schema"
        ):
            node_home.ensure_node_home(home, repo_root=self.repo, source={})

    def test_structure_validation_does_not_parse_auth_content(self) -> None:
        home = self._home()
        auth = self._write_auth(home, {})
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(node_home, "_temporary_roots", return_value=set()):
            self.assertEqual(
                node_home.validate_node_home_structure(
                    home,
                    repo_root=self.repo,
                    require_auth=True,
                    source={},
                ),
                home.resolve(),
            )
        with self.assertRaisesRegex(
            node_home.CodexNodeHomeError, "supported ChatGPT file-auth schema"
        ):
            node_home.validate_file_auth(auth)

    def test_windows_ensure_hardens_and_live_verifies_home_and_data(self) -> None:
        home = self.root / "state" / node_home.NODE_HOME_DIRECTORY
        home.mkdir(parents=True)
        self._write_auth(home, self._auth_payload())
        lock = home / node_home.LOCK_FILENAME
        lock.write_bytes(b"\0")
        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home, "_windows_set_process_default_owner_to_current_user"
        ), mock.patch.object(
            node_home, "_windows_require_local_fixed_path"
        ) as require_local, mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "is_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), mock.patch.object(
            node_home, "_windows_apply_private_acl"
        ) as apply_acl, mock.patch.object(
            node_home, "_windows_verify_private_acl"
        ) as verify_acl:
            result = node_home.ensure_node_home(
                home, repo_root=self.repo, source={}
            )
        self.assertEqual(result, home.resolve())
        self.assertGreaterEqual(require_local.call_count, 2)
        apply_acl.assert_has_calls(
            [
                mock.call(home, is_directory=True),
                mock.call(home / "auth.json", is_directory=False),
                mock.call(lock, is_directory=False),
            ]
        )
        verify_acl.assert_has_calls(
            [
                mock.call(home.resolve(), is_directory=True),
                mock.call(home.resolve() / "auth.json", is_directory=False),
                mock.call(home.resolve() / node_home.LOCK_FILENAME, is_directory=False),
                mock.call(home.resolve() / "auth.json", is_directory=False),
            ]
        )

    def test_windows_validation_fails_closed_on_unsafe_acl(self) -> None:
        home = self._home()
        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home, "_windows_set_process_default_owner_to_current_user"
        ), mock.patch.object(
            node_home, "_windows_require_local_fixed_path"
        ), mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "is_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), mock.patch.object(
            node_home,
            "_windows_verify_private_acl",
            side_effect=node_home.CodexNodeHomeError("unexpected SID"),
        ), self.assertRaisesRegex(node_home.CodexNodeHomeError, "unexpected SID"):
            node_home.validate_node_home(
                home, repo_root=self.repo, require_auth=True, source={}
            )

    def test_windows_owner_mismatch_blocks_before_acl_mutation(self) -> None:
        candidate = self.root / "foreign-owner"
        with mock.patch.object(
            node_home,
            "_windows_require_current_user_owner",
            side_effect=node_home.CodexNodeHomeError(
                "owner must be the current user"
            ),
        ) as require_owner, mock.patch.object(
            node_home, "_windows_security_api"
        ) as security_api:
            with self.assertRaisesRegex(
                node_home.CodexNodeHomeError, "owner must be the current user"
            ):
                node_home._windows_apply_private_acl(
                    candidate, is_directory=True
                )
        require_owner.assert_called_once_with(candidate)
        security_api.assert_not_called()

    def test_windows_sets_current_user_default_owner_before_creating_home(self) -> None:
        home = self.root / "new-state" / node_home.NODE_HOME_DIRECTORY

        def require_before_create() -> None:
            self.assertFalse(home.exists())

        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home,
            "_windows_set_process_default_owner_to_current_user",
            side_effect=require_before_create,
        ) as set_default_owner, mock.patch.object(
            node_home, "_windows_require_local_fixed_path"
        ), mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "is_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), mock.patch.object(
            node_home, "_windows_apply_private_acl"
        ), mock.patch.object(
            node_home, "_windows_verify_private_acl"
        ):
            result = node_home.ensure_node_home(
                home, repo_root=self.repo, source={}
            )

        self.assertEqual(result, home.resolve())
        set_default_owner.assert_called_once_with()

    def test_windows_default_owner_failure_blocks_before_creating_home(self) -> None:
        home = self.root / "blocked-state" / node_home.NODE_HOME_DIRECTORY
        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home,
            "_windows_set_process_default_owner_to_current_user",
            side_effect=node_home.CodexNodeHomeError("default-owner update failed"),
        ), self.assertRaisesRegex(
            node_home.CodexNodeHomeError, "default-owner update failed"
        ):
            node_home.ensure_node_home(home, repo_root=self.repo, source={})

        self.assertFalse(home.exists())

    def test_windows_lock_sets_current_user_default_owner_before_open(self) -> None:
        home = self._home()
        lock = home / node_home.LOCK_FILENAME
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=mock.Mock(),
        )

        def require_before_open() -> None:
            self.assertFalse(lock.exists())

        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home,
            "_windows_set_process_default_owner_to_current_user",
            side_effect=require_before_open,
        ) as set_default_owner, mock.patch.object(
            node_home, "_windows_require_local_fixed_path"
        ), mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "is_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_windows_apply_private_acl"
        ), mock.patch.object(
            node_home, "_windows_verify_private_acl"
        ), mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            with node_home.locked_node_home(home, timeout_seconds=1):
                pass

        set_default_owner.assert_called_once_with()

    def test_windows_link_check_uses_native_reparse_attributes(self) -> None:
        candidate = self.root / "junction"
        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            Path, "is_symlink", return_value=False
        ), mock.patch.object(
            node_home, "_windows_path_is_reparse", return_value=True
        ) as reparse:
            self.assertTrue(node_home.is_link_like(candidate))
        reparse.assert_called_once_with(candidate)

    def test_windows_lock_file_is_hardened_before_locking(self) -> None:
        home = self._home()
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=mock.Mock(),
        )
        with mock.patch.object(
            node_home, "_is_windows_host", return_value=True
        ), mock.patch.object(
            node_home, "_windows_set_process_default_owner_to_current_user"
        ), mock.patch.object(
            node_home, "_windows_require_local_fixed_path"
        ), mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "is_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_windows_apply_private_acl"
        ) as apply_acl, mock.patch.object(
            node_home, "_windows_verify_private_acl"
        ) as verify_acl, mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            with node_home.locked_node_home(home, timeout_seconds=1):
                pass
        lock = home / node_home.LOCK_FILENAME
        apply_acl.assert_called_once_with(lock, is_directory=False)
        self.assertIn(mock.call(home, is_directory=True), verify_acl.call_args_list)
        self.assertIn(mock.call(lock, is_directory=False), verify_acl.call_args_list)
        self.assertEqual(fake_msvcrt.locking.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "native Windows ACL Gate")
    def test_native_windows_acl_gate(self) -> None:
        home = self.root / "state" / node_home.NODE_HOME_DIRECTORY
        with mock.patch.object(node_home, "_temporary_roots", return_value=set()):
            result = node_home.ensure_node_home(
                home, repo_root=self.repo, source={}
            )
            self._write_auth(result, self._auth_payload())
            node_home.validate_node_home(
                result, repo_root=self.repo, require_auth=True, source={}
            )
            with node_home.locked_node_home(result, timeout_seconds=5):
                pass
            node_home.validate_node_home_structure(
                result, repo_root=self.repo, require_auth=True, source={}
            )

    def test_missing_file_auth_fails_closed(self) -> None:
        home = self._home()
        (home / "auth.json").unlink()
        with mock.patch.object(
            node_home, "path_contains_link_like", return_value=False
        ), mock.patch.object(
            node_home, "_temporary_roots", return_value=set()
        ), self.assertRaisesRegex(node_home.CodexNodeHomeError, "login-codex-node"):
            node_home.validate_node_home(
                home, repo_root=self.repo, require_auth=True, source={}
            )

    def test_lock_serializes_threads_using_the_same_auth_store(self) -> None:
        home = self._home()
        active = 0
        maximum = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal active, maximum
            with node_home.locked_node_home(home, timeout_seconds=5):
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(maximum, 1)

    def test_lock_rejects_linked_home_before_creating_lock_file(self) -> None:
        target = self._home()
        linked = self.root / "linked-node-home"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(
            node_home.CodexNodeHomeError, "unsafe before locking"
        ):
            with node_home.locked_node_home(linked, timeout_seconds=1):
                self.fail("linked credential home reached the lock body")
        self.assertFalse((target / node_home.LOCK_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()

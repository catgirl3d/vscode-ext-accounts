from __future__ import annotations

import base64
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import account_services
from vscode_inject import backups
from vscode_inject import codex_accounts
from vscode_inject import gui_app
from vscode_inject import ide_context
from vscode_inject import kilo_new_accounts
from vscode_inject import refresh_scheduler
from vscode_inject import saved_accounts
from vscode_inject import state_db


class UserFacingError(RuntimeError):
    pass


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2024, 1, 2, 3, 4, 5, tzinfo=tz)


def make_jwt(exp: int) -> str:
    payload = base64.b64encode(json.dumps({"exp": exp}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SavedAccountsModuleTests(TempDirTestCase):
    codex_key = "codex://auth"

    def test_saved_account_kind_listing_and_filters(self):
        accounts_dir = self.root / "accounts"
        self.write_json(accounts_dir / "ide.json", {"kind": "ide", "entries": [{"key": "secret://ide", "value": {}}]})
        self.write_json(accounts_dir / "codex.json", {"entries": [{"key": self.codex_key, "value": {}}]})
        self.write_text(accounts_dir / "broken.json", "{")

        self.assertEqual(saved_accounts.saved_account_kind({"kind": "codex"}, self.codex_key), "codex")
        self.assertEqual(saved_accounts.saved_account_kind({"entries": [{"key": self.codex_key}]}, self.codex_key), "codex")
        self.assertEqual(saved_accounts.saved_account_kind({"entries": [{"key": "other"}]}, self.codex_key), "ide")

        records = saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key)
        by_name = {record["name"]: record for record in records}

        self.assertTrue(by_name["ide"]["readable"])
        self.assertEqual(by_name["ide"]["kind"], "ide")
        self.assertTrue(by_name["codex"]["readable"])
        self.assertEqual(by_name["codex"]["kind"], "codex")
        self.assertFalse(by_name["broken"]["readable"])
        self.assertIsNone(by_name["broken"]["kind"])
        self.assertIsNone(by_name["broken"]["data"])

        self.assertEqual(
            [record["name"] for record in saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key, kind="ide")],
            ["ide"],
        )
        self.assertEqual(
            [record["name"] for record in saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key, kind="codex")],
            ["codex"],
        )

    def test_load_saved_account_and_write_account_file_validate_kinds(self):
        accounts_dir = self.root / "accounts"
        self.write_json(accounts_dir / "codex.json", {"entries": [{"key": self.codex_key, "value": {}}]})
        self.write_text(accounts_dir / "broken.json", "{")
        self.write_json(accounts_dir / "ide.json", {"kind": "ide", "entries": []})

        with self.assertRaises(FileNotFoundError):
            saved_accounts.load_saved_account(str(accounts_dir), "missing", self.codex_key)

        path, data, kind = saved_accounts.load_saved_account(str(accounts_dir), "codex", self.codex_key, expected_kind="codex")
        self.assertEqual(Path(path), accounts_dir / "codex.json")
        self.assertEqual(kind, "codex")
        self.assertEqual(data["entries"][0]["key"], self.codex_key)

        with self.assertRaises(saved_accounts.SavedAccountKindMismatchError) as ctx:
            saved_accounts.load_saved_account(str(accounts_dir), "codex", self.codex_key, expected_kind="ide")
        self.assertEqual(ctx.exception.actual_kind, "codex")

        with self.assertRaisesRegex(ValueError, "Cannot overwrite unreadable account file"):
            saved_accounts.write_account_file(str(accounts_dir), self.codex_key, "broken", "ide", "kilocode", [])

        with self.assertRaisesRegex(ValueError, "already exists as ide"):
            saved_accounts.write_account_file(str(accounts_dir), self.codex_key, "ide", "codex", "codex", [])

        with patch("vscode_inject.saved_accounts.datetime.datetime", FixedDateTime):
            out = saved_accounts.write_account_file(
                str(accounts_dir),
                self.codex_key,
                "fresh",
                "ide",
                "kilocode",
                [{"key": "secret://ide", "value": {"accountId": "acct-1"}}],
            )

        written = self.read_json(Path(out))
        self.assertEqual(written["name"], "fresh")
        self.assertEqual(written["kind"], "ide")
        self.assertEqual(written["ext"], "kilocode")
        self.assertEqual(written["saved_at"], "2024-01-02T03:04:05")

    def test_write_saved_account_data_removes_staged_file_on_replace_failure(self):
        target = self.root / "accounts" / "new.json"

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_data(str(target), {"name": "new"})

        self.assertTrue(target.parent.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_saved_account_helpers_clean_up_temp_files_on_failure_paths(self):
        target_dir = self.root / "accounts"
        target = target_dir / "target.json"

        with patch("vscode_inject.saved_accounts.json.dump", side_effect=RuntimeError("dump failed")):
            with self.assertRaisesRegex(RuntimeError, "dump failed"):
                saved_accounts._stage_saved_account_data(str(target), {"name": "broken"})

        self.assertEqual(list(target_dir.iterdir()), [])

        missing = target_dir / "missing.json"
        saved_accounts._restore_account_file_bytes(str(missing), None)
        self.assertFalse(missing.exists())

        existing = target_dir / "existing.json"
        self.write_json(existing, {"name": "existing", "value": 1})
        with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts._restore_account_file_bytes(str(existing), b'{"name":"restored","value":2}')

        self.assertEqual(self.read_json(existing), {"name": "existing", "value": 1})
        self.assertEqual(sorted(path.name for path in target_dir.iterdir()), ["existing.json"])

    def test_write_saved_account_data_ignores_missing_staged_file_and_empty_batch(self):
        target = self.root / "accounts" / "late-failure.json"

        def delete_and_fail(src: str, dst: str):
            os.unlink(src)
            raise OSError("replace failed")

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=delete_and_fail):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_data(str(target), {"name": "late-failure"})

        self.assertEqual(list(target.parent.iterdir()), [])
        saved_accounts.write_saved_account_batch({})

    def test_write_saved_account_batch_rolls_back_new_files_when_replace_fails(self):
        created = self.root / "accounts" / "created.json"
        existing = self.root / "accounts" / "existing.json"
        self.write_json(existing, {"name": "existing", "value": 1})
        real_replace = os.replace
        replace_calls = {"count": 0}

        def flaky_replace(src: str, dst: str):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_batch(
                    [
                        (str(created), {"name": "created", "value": 10}),
                        (str(existing), {"name": "existing", "value": 20}),
                    ]
                )

        self.assertFalse(created.exists())
        self.assertEqual(self.read_json(existing), {"name": "existing", "value": 1})

    def test_write_saved_account_batch_surfaces_rollback_errors(self):
        first = self.root / "accounts" / "first.json"
        second = self.root / "accounts" / "second.json"
        self.write_json(first, {"name": "first", "value": 1})
        self.write_json(second, {"name": "second", "value": 2})
        real_replace = os.replace
        replace_calls = {"count": 0}

        def flaky_replace(src: str, dst: str):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=flaky_replace):
            with patch("vscode_inject.saved_accounts._restore_account_file_bytes", side_effect=OSError("rollback failed")):
                with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                    saved_accounts.write_saved_account_batch(
                        [
                            (str(first), {"name": "first", "value": 10}),
                            (str(second), {"name": "second", "value": 20}),
                        ]
                    )


class CodexAccountsModuleTests(TempDirTestCase):
    def test_decode_jwt_exp_ms_handles_valid_and_invalid_tokens(self):
        self.assertEqual(codex_accounts.decode_jwt_exp_ms(None), 0)
        self.assertEqual(codex_accounts.decode_jwt_exp_ms("not-a-jwt"), 0)
        self.assertEqual(codex_accounts.decode_jwt_exp_ms(make_jwt(1700000000)), 1700000000000)

    def test_codex_auth_read_write_and_current_account_contract(self):
        auth_path = self.root / "codex" / "auth.json"
        access_token = make_jwt(1700000000)
        raw_auth = {
            "tokens": {
                "access_token": access_token,
                "refresh_token": "refresh-1",
                "account_id": "acct-1",
                "id_token": "id-1",
            }
        }

        self.assertEqual(codex_accounts.read_codex_auth(str(auth_path)), {})
        codex_accounts.write_codex_auth(str(auth_path), raw_auth)
        self.assertEqual(codex_accounts.read_codex_auth(str(auth_path)), raw_auth)

        current = codex_accounts.read_current_codex_account(
            str(auth_path),
            "codex://auth",
            lambda value: value.get("refresh_token") or None,
        )
        self.assertEqual(
            current,
            {
                "codex://auth": {
                    "accountId": "acct-1",
                    "fingerprint": "refresh-1",
                    "expires": 1700000000000,
                }
            },
        )
        self.assertEqual(codex_accounts.read_current_codex_account(str(auth_path), "codex://auth", lambda _value: None), {})

    def test_to_codex_format_reuses_existing_id_token_and_preserves_existing_fields(self):
        with patch("vscode_inject.codex_accounts.datetime.datetime", FixedDateTime):
            formatted = codex_accounts.to_codex_format(
                {
                    "accountId": "acct-1",
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                },
                existing={"keep": "me", "tokens": {"account_id": "acct-1", "id_token": "id-1"}},
            )

        self.assertEqual(formatted["keep"], "me")
        self.assertEqual(formatted["auth_mode"], "chatgpt")
        self.assertIsNone(formatted["OPENAI_API_KEY"])
        self.assertEqual(
            formatted["tokens"],
            {
                "id_token": "id-1",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "account_id": "acct-1",
            },
        )
        self.assertEqual(formatted["last_refresh"], "2024-01-02T03:04:05Z")

        without_existing_tokens = codex_accounts.to_codex_format(
            {
                "accountId": "acct-2",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
            existing={"tokens": "invalid"},
        )
        self.assertEqual(without_existing_tokens["tokens"]["id_token"], "id-2")

        with self.assertRaisesRegex(ValueError, "requires id_token"):
            codex_accounts.to_codex_format(
                {"accountId": "acct-2", "access_token": "access-2", "refresh_token": "refresh-2"},
                existing={"tokens": {"account_id": "acct-1", "id_token": "id-1"}},
            )

    def test_from_codex_format_normalizes_nested_and_top_level_fields(self):
        access_token = make_jwt(1700000000)
        nested = codex_accounts.from_codex_format(
            {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "refresh-1",
                    "account_id": "acct-1",
                    "id_token": "id-1",
                }
            }
        )
        self.assertEqual(
            nested,
            {
                "type": "openai-codex",
                "access_token": access_token,
                "refresh_token": "refresh-1",
                "expires": 1700000000000,
                "accountId": "acct-1",
                "id_token": "id-1",
            },
        )

        top_level = codex_accounts.from_codex_format(
            {
                "tokens": {
                    "access_token": 123,
                    "refresh_token": 456,
                    "account_id": 789,
                    "id_token": 999,
                },
                "access": "top-access",
                "refresh": "top-refresh",
                "accountId": "top-acct",
                "id_token": "top-id",
                "expires": 321,
            }
        )
        self.assertEqual(
            top_level,
            {
                "type": "openai-codex",
                "access_token": "top-access",
                "refresh_token": "top-refresh",
                "expires": 321,
                "accountId": "top-acct",
                "id_token": "top-id",
            },
        )

        raw_tokens_is_not_dict = codex_accounts.from_codex_format(
            {
                "tokens": "invalid",
                "access_token": "access-4",
                "refresh_token": "refresh-4",
                "accountId": "acct-4",
                "id_token": "id-4",
                "expires": 654,
            }
        )
        self.assertEqual(raw_tokens_is_not_dict["accountId"], "acct-4")


class KiloNewAccountsModuleTests(TempDirTestCase):
    def test_kilo_new_read_write_and_conversion_helpers(self):
        auth_path = self.root / "kilo" / "auth.json"
        self.assertEqual(kilo_new_accounts.read_kilo_auth(str(auth_path)), {})

        auth = {"openai": {"access": "access-1", "refresh": "refresh-1", "expires": 123, "accountId": "acct-1"}}
        kilo_new_accounts.write_kilo_auth(str(auth_path), auth)
        self.assertEqual(kilo_new_accounts.read_kilo_auth(str(auth_path)), auth)

        self.assertEqual(
            kilo_new_accounts.to_kilo_new_format(
                {
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires": 456,
                    "accountId": "acct-2",
                }
            ),
            {
                "type": "oauth",
                "access": "access-2",
                "refresh": "refresh-2",
                "expires": 456,
                "accountId": "acct-2",
            },
        )
        self.assertEqual(
            kilo_new_accounts.from_kilo_new_format(
                {"access": "access-3", "refresh": "refresh-3", "expires": 789, "accountId": "acct-3"}
            ),
            {
                "type": "openai-codex",
                "access_token": "access-3",
                "refresh_token": "refresh-3",
                "expires": 789,
                "accountId": "acct-3",
            },
        )

    def test_kilo_new_fingerprint_and_current_account_contract(self):
        auth_path = self.root / "kilo" / "auth.json"
        auth = {"openai": {"access": "access-1", "refresh": "refresh-1", "expires": 123, "accountId": "acct-1"}}
        kilo_new_accounts.write_kilo_auth(str(auth_path), auth)

        expected = hashlib.sha256(b"refresh-1").hexdigest()
        self.assertEqual(kilo_new_accounts.get_kilo_new_fingerprint(str(auth_path)), expected)
        self.assertEqual(
            kilo_new_accounts.read_current_kilo_new_account(
                str(auth_path),
                "kilo-new://openai",
                lambda value: f"fp:{value['refresh']}",
            ),
            {
                "kilo-new://openai": {
                    "accountId": "acct-1",
                    "fingerprint": "fp:refresh-1",
                    "expires": 123,
                }
            },
        )

        self.write_json(auth_path, {"openai": "invalid"})
        self.assertIsNone(kilo_new_accounts.get_kilo_new_fingerprint(str(auth_path)))
        self.assertEqual(
            kilo_new_accounts.read_current_kilo_new_account(
                str(auth_path),
                "kilo-new://openai",
                lambda value: "unused",
            ),
            {},
        )


class BackupsModuleTests(TempDirTestCase):
    def test_prewrite_backup_targets_include_codex_when_requested(self):
        targets = backups.prewrite_backup_targets(
            {"vscode": {"label": "VSCode", "db": "db.vscdb", "local_state": "Local State"}},
            "vscode",
            "kilo/auth.json",
            "codex/auth.json",
            include_db=False,
            include_kilo=False,
            include_codex=True,
        )

        self.assertEqual(
            targets,
            [
                {
                    "source": "codex/auth.json",
                    "archive_path": "prewrite/shared/codex/auth.json",
                    "label": "Codex auth.json",
                    "required": False,
                }
            ],
        )

    def test_create_backup_archive_appends_zip_extension_and_rejects_empty_targets(self):
        source = self.root / "source.txt"
        self.write_text(source, "backup me")
        messages: list[str] = []

        result = backups.create_backup_archive(
            str(self.root),
            "vscode",
            [{"source": str(source), "archive_path": "state/source.txt", "label": "Source", "required": True}],
            out_path=str(self.root / "archive-no-ext"),
            backup_kind="manual",
            print_fn=messages.append,
        )

        self.assertTrue(result["path"].endswith(".zip"))
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(messages[-2:], [f"Backup saved: {result['path']}", "Files included: 1/1"])

        with self.assertRaisesRegex(RuntimeError, "none of the target files exist"):
            backups.create_backup_archive(
                str(self.root),
                "vscode",
                [{"source": str(self.root / "missing.txt"), "archive_path": "missing.txt", "label": "Missing", "required": False}],
                backup_kind="manual",
                print_fn=lambda _message: None,
            )

    def test_refresh_recovery_snapshot_cleanup_handles_dump_failures_and_unlink_errors(self):
        with patch("vscode_inject.backups.json.dump", side_effect=RuntimeError("dump failed")):
            with self.assertRaisesRegex(RuntimeError, "dump failed"):
                backups.write_refresh_recovery_snapshot(
                    str(self.root),
                    {"account.json": {"entries": []}},
                    subject_label="saved account 'alice'",
                    account_names=("alice",),
                    providers=("openai",),
                    operation="manual-refresh",
                    created_at="2024-01-02T03:04:05",
                )

        recovery_dir = self.root / "backups" / "refresh_recovery"
        self.assertEqual(list(recovery_dir.iterdir()), [])

        messages: list[str] = []
        backups.cleanup_refresh_recovery_snapshot(str(recovery_dir / "missing.json"), print_fn=messages.append)

        snapshot = recovery_dir / "snapshot.json"
        self.write_text(snapshot, "{}")
        with patch("vscode_inject.backups.os.unlink", side_effect=OSError("busy")):
            backups.cleanup_refresh_recovery_snapshot(str(snapshot), print_fn=messages.append)

        self.assertEqual(
            messages,
            [f"WARNING: Failed to remove refresh recovery snapshot {snapshot}: busy"],
        )

    def test_refresh_recovery_snapshot_ignores_missing_temp_file_during_failed_write(self):
        with patch("vscode_inject.backups.json.dump", side_effect=RuntimeError("dump failed")):
            with patch("vscode_inject.backups.os.unlink", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "dump failed"):
                    backups.write_refresh_recovery_snapshot(
                        str(self.root),
                        {"account.json": {"entries": []}},
                        subject_label="saved account 'alice'",
                        account_names=("alice",),
                        providers=("openai",),
                        operation="manual-refresh",
                        created_at="2024-01-02T03:04:05",
                    )


class StateDbModuleTests(TempDirTestCase):
    def test_get_aes_key_success_and_failure_paths(self):
        local_state_path = self.root / "Local State"
        self.write_json(
            local_state_path,
            {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPIencrypted").decode("ascii")}},
        )
        secret = b"secret-key"
        buffer = ctypes.create_string_buffer(secret)

        def fake_unprotect(_p_in, _desc, _opt1, _opt2, _opt3, _flags, p_out):
            p_out._obj.cbData = len(secret)
            p_out._obj.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
            return 1

        fake_windll = SimpleNamespace(
            crypt32=SimpleNamespace(CryptUnprotectData=fake_unprotect),
            kernel32=SimpleNamespace(LocalFree=lambda _ptr: None),
        )
        messages: list[str] = []

        with patch.object(state_db.ctypes, "windll", fake_windll):
            self.assertEqual(state_db.get_aes_key(str(local_state_path), print_fn=messages.append), secret)
        self.assertEqual(messages, [])

        missing_messages: list[str] = []
        self.assertIsNone(state_db.get_aes_key(str(self.root / "missing.json"), print_fn=missing_messages.append))
        self.assertEqual(len(missing_messages), 1)
        self.assertIn("Could not get AES key", missing_messages[0])

    def test_decrypt_decode_and_serialize_helpers(self):
        aes_key = b"0" * 32
        encrypted = state_db.encrypt_value("secret-value", aes_key)

        self.assertEqual(state_db.decrypt_value(b"", None), "")
        self.assertEqual(state_db.decrypt_value(b"plain-text", None), "plain-text")
        self.assertTrue(state_db.decrypt_value(b"\xff\xfe", None).startswith("b'"))
        self.assertIn("DPAPI key unavailable", state_db.decrypt_value(encrypted, None))
        self.assertEqual(state_db.decrypt_value(encrypted, aes_key), "secret-value")
        self.assertTrue(state_db.decrypt_value(encrypted[:-1] + b"x", aes_key).startswith("<decrypt failed:"))

        self.assertEqual(
            state_db.decode_entry(b"raw-bytes", None, decrypt_value_fn=lambda raw, _aes_key: raw.decode("utf-8").upper()),
            "RAW-BYTES",
        )
        self.assertEqual(
            state_db.decode_entry(
                json.dumps({"type": "Buffer", "data": list(b"abc")}),
                None,
                decrypt_value_fn=lambda raw, _aes_key: raw.decode("utf-8").upper(),
            ),
            "ABC",
        )
        self.assertEqual(state_db.decode_entry("not-json", None), "not-json")
        self.assertEqual(state_db.decode_entry(None, None), "")

        self.assertEqual(
            json.loads(state_db.serialize_entry_value("secret://key", "", aes_key)),
            {"type": "Buffer", "data": []},
        )

        encrypted_calls: list[tuple[str, bytes]] = []

        def fake_encrypt_value(plaintext: str, key: bytes) -> bytes:
            encrypted_calls.append((plaintext, key))
            return b"ENC"

        self.assertEqual(
            json.loads(
                state_db.serialize_entry_value(
                    "secret://key",
                    {"accountId": "acct-1"},
                    aes_key,
                    encrypt_value_fn=fake_encrypt_value,
                )
            ),
            {"type": "Buffer", "data": [69, 78, 67]},
        )
        self.assertEqual(
            encrypted_calls,
            [(json.dumps({"accountId": "acct-1"}, ensure_ascii=False), aes_key)],
        )
        self.assertEqual(state_db.serialize_entry_value("workbench.colorTheme", {"name": "dark"}, aes_key), '{"name": "dark"}')
        self.assertEqual(state_db.serialize_entry_value("workbench.items", [1, 2], aes_key), "[1, 2]")
        self.assertEqual(state_db.serialize_entry_value("workbench.empty", None, aes_key), "")

    def test_secret_key_helpers_and_read_shortcuts(self):
        self.assertEqual(state_db._extension_id_from_secret_key("window.zoomLevel"), "")
        self.assertEqual(state_db._extension_id_from_secret_key("secret://{bad-json"), "")
        self.assertEqual(state_db._extension_id_from_secret_key('secret://{"extensionId": 5, "key": "oauth"}'), "")
        self.assertEqual(
            state_db._secret_storage_key("kilocode.kilo-code", "oauth-key"),
            'secret://{"extensionId":"kilocode.kilo-code","key":"oauth-key"}',
        )
        self.assertEqual(state_db._escape_like_fragment("100%_done"), "100\\%\\_done")
        self.assertEqual(state_db._escape_like_fragment(r"dir\name"), r"dir\\name")

        self.assertEqual(
            state_db.read_current_accounts(
                str(self.root / "missing.vscdb"),
                str(self.root / "Local State"),
                "oauth-key",
                get_aes_key_fn=lambda _path: b"aes-key",
                decode_entry_fn=lambda value, _aes_key: value,
                account_fingerprint=lambda value: value.get("refresh_token"),
            ),
            {},
        )
        self.assertEqual(
            state_db.read_entries_for_extension_ids(
                str(self.root / "missing.vscdb"),
                str(self.root / "Local State"),
                "oauth-key",
                ["kilocode.kilo-code"],
                get_aes_key_fn=lambda _path: b"aes-key",
                decode_entry_fn=lambda value, _aes_key: value,
            ),
            [],
        )
        self.assertEqual(
            state_db.read_entries_for_extension_ids(
                str(self.root / "state.vscdb"),
                str(self.root / "Local State"),
                "oauth-key",
                [],
                get_aes_key_fn=lambda _path: b"aes-key",
                decode_entry_fn=lambda value, _aes_key: value,
            ),
            [],
        )

    def test_write_entries_to_db_counts_failures_and_logs_errors(self):
        messages: list[str] = []

        class FakeConnection:
            def __init__(self) -> None:
                self.committed = False
                self.closed = False

            def execute(self, _query: str, params: tuple[str, str]) -> None:
                if params[0] == "fail":
                    raise sqlite3.OperationalError("boom")

            def commit(self) -> None:
                self.committed = True

            def close(self) -> None:
                self.closed = True

        connection = FakeConnection()

        with patch("vscode_inject.state_db.sqlite3.connect", return_value=connection):
            restored, skipped = state_db.write_entries_to_db(
                "ignored.vscdb",
                [{"key": "ok", "value": "first"}, {"key": "fail", "value": "second"}],
                b"aes-key",
                print_fn=messages.append,
            )

        self.assertEqual((restored, skipped), (1, 1))
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertEqual(messages, ["  [OK] ok", "  [FAIL] fail: boom"])


class AccountServicesModuleTests(TempDirTestCase):
    kilo_new_key = "kilo-new://openai"

    def test_selection_and_entry_key_helpers_validate_edge_cases(self):
        self.assertEqual(
            account_services.entry_key_for_ext(self.kilo_new_key, "oauth-key", self.kilo_new_key),
            self.kilo_new_key,
        )
        self.assertEqual(account_services._extension_id_from_entry_key("secret://{bad-json"), "")

        with self.assertRaisesRegex(ValueError, "Select at least one extension"):
            account_services.normalize_ide_ext_selection(
                [],
                {"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key, "both": None},
                self.kilo_new_key,
            )

    def test_read_current_ide_entries_for_selection_includes_kilo_new_account(self):
        entries = account_services.read_current_ide_entries_for_selection(
            ["kilocode", "kilo-new"],
            ide_extensions={"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key},
            kilo_new_key=self.kilo_new_key,
            read_db_entries=lambda ext_ids: [{"key": ext_ids[0], "value": {"accountId": "acct-db"}}],
            read_kilo_auth=lambda: {"openai": {"accountId": "acct-kilo", "refresh": "refresh-kilo"}},
            from_kilo_new_format=lambda value: {"converted": value["accountId"]},
        )

        self.assertEqual(
            entries,
            [
                {"key": "kilocode.kilo-code", "value": {"accountId": "acct-db"}},
                {"key": self.kilo_new_key, "value": {"converted": "acct-kilo"}},
            ],
        )

    def test_save_ide_and_codex_account_raise_user_facing_errors_for_missing_data(self):
        with self.assertRaisesRegex(UserFacingError, "No matching account entries found"):
            account_services.save_ide_account(
                "alice",
                "kilocode",
                normalize_selection=lambda ext: (["kilocode"], "kilocode"),
                read_current_ide_entries_for_selection=lambda ext_names: [],
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "missing access_token or refresh_token"):
            account_services.save_codex_account(
                "alice",
                codex_key="codex://auth",
                read_codex_auth=lambda: {},
                from_codex_format=lambda _value: {},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.save_codex_account(
                "alice",
                codex_key="codex://auth",
                read_codex_auth=lambda: {},
                from_codex_format=lambda _value: {"access_token": "access-1", "refresh_token": "refresh-1"},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

    def test_refresh_saved_account_rejects_invalid_entries_payload(self):
        with self.assertRaisesRegex(ValueError, "invalid entries payload"):
            account_services.refresh_saved_account(
                "alice",
                operation_lock=contextlib.nullcontext(),
                load_saved_account_data=lambda name: ("account.json", {"entries": "invalid"}, "ide"),
                oauth_refresh_module=SimpleNamespace(),
                write_saved_account_batch=lambda updates: None,
                persist_refreshed_saved_account_batch=lambda *args, **kwargs: None,
                saved_account_refresh_error_cls=UserFacingError,
                persistence_error_cls=RuntimeError,
            )

    def test_use_ide_account_validates_kind_and_handles_running_kilo_new(self):
        base_kwargs = {
            "normalize_selection": lambda ext: (["kilocode"], "kilocode"),
            "ide_extensions": {"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key},
            "code_key": "code-key",
            "kilo_new_key": self.kilo_new_key,
            "ide_paths": {"vscode": {"label": "VSCode"}, "antigravity": {"label": "Antigravity"}},
            "guard_current_ide_closed": lambda: None,
            "is_ide_running": lambda ide: False,
            "create_prewrite_backup": lambda **kwargs: None,
            "apply_db_entries": lambda entries: None,
            "entry_key_for_ext_fn": lambda ext_id: f"secret://{ext_id}",
            "to_kilo_new_format": lambda value: {"accountId": value.get("accountId", "?")},
            "read_kilo_auth": lambda: {},
            "write_kilo_auth": lambda auth: None,
            "kilo_auth_path": "kilo/auth.json",
            "user_facing_error_cls": UserFacingError,
            "print_fn": lambda msg: None,
        }

        with self.assertRaisesRegex(UserFacingError, "Codex-only"):
            account_services.use_ide_account(
                "alice",
                load_saved_account_data=lambda name: ("account.json", {}, "codex"),
                **base_kwargs,
            )

        with self.assertRaisesRegex(UserFacingError, "No IDE entries"):
            account_services.use_ide_account(
                "alice",
                load_saved_account_data=lambda name: ("account.json", {"entries": [{"key": "code-key", "value": {}}]}, "ide"),
                **base_kwargs,
            )

        messages: list[str] = []
        backups: list[dict] = []
        written: list[dict] = []
        account_services.use_ide_account(
            "alice",
            ext=["kilo-new"],
            allow_kilo_new_while_running=True,
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": self.kilo_new_key,
                            "value": {
                                "accountId": "acct-kilo",
                                "access_token": "access-kilo",
                                "refresh_token": "refresh-kilo",
                            },
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["kilo-new"], "kilo-new"),
            ide_extensions=base_kwargs["ide_extensions"],
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths=base_kwargs["ide_paths"],
            guard_current_ide_closed=base_kwargs["guard_current_ide_closed"],
            is_ide_running=lambda ide: ide == "vscode",
            create_prewrite_backup=lambda **kwargs: backups.append(kwargs),
            apply_db_entries=base_kwargs["apply_db_entries"],
            entry_key_for_ext_fn=base_kwargs["entry_key_for_ext_fn"],
            to_kilo_new_format=lambda value: {
                "accountId": value["accountId"],
                "access": value["access_token"],
                "refresh": value["refresh_token"],
                "expires": value.get("expires", 0),
            },
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: written.append(auth),
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(backups, [{"include_db": False, "include_kilo": True, "note": "before applying IDE account 'alice'"}])
        self.assertIn("WARNING: Writing shared Kilo New auth while IDEs are running (VSCode)", messages[0])
        self.assertEqual(messages[-2:], ["[kilo-new] Written to kilo/auth.json", "  accountId: acct-kilo"])
        self.assertEqual(
            written,
            [{"openai": {"accountId": "acct-kilo", "access": "access-kilo", "refresh": "refresh-kilo", "expires": 0}}],
        )

    def test_use_ide_account_reuses_exact_db_entry_without_cross_extension_remap(self):
        applied_entries: list[list[dict]] = []
        messages: list[str] = []
        entry_key = 'secret://{"extensionId":"rooveterinaryinc.roo-cline","key":"oauth"}'

        account_services.use_ide_account(
            "alice",
            ext=["roo-cline"],
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": entry_key,
                            "value": {"accountId": "acct-roo", "refresh_token": "refresh-roo"},
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["roo-cline"], "roo-cline"),
            ide_extensions={"roo-cline": "rooveterinaryinc.roo-cline", "kilo-new": self.kilo_new_key},
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths={"vscode": {"label": "VSCode"}},
            guard_current_ide_closed=lambda: None,
            is_ide_running=lambda ide: False,
            create_prewrite_backup=lambda **kwargs: None,
            apply_db_entries=lambda entries: applied_entries.append(entries),
            entry_key_for_ext_fn=lambda ext_id: f'secret://{{"extensionId":"{ext_id}","key":"oauth"}}',
            to_kilo_new_format=lambda value: value,
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: None,
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(
            applied_entries,
            [[{"key": entry_key, "value": {"accountId": "acct-roo", "refresh_token": "refresh-roo"}}]],
        )
        self.assertNotIn("[cross-ext]", "\n".join(messages))

    def test_use_ide_account_can_remap_db_target_from_kilo_new_source(self):
        applied_entries: list[list[dict]] = []
        messages: list[str] = []

        account_services.use_ide_account(
            "alice",
            ext=["roo-cline"],
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": self.kilo_new_key,
                            "value": {"accountId": "acct-kilo", "refresh_token": "refresh-kilo"},
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["roo-cline"], "roo-cline"),
            ide_extensions={"roo-cline": "rooveterinaryinc.roo-cline", "kilo-new": self.kilo_new_key},
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths={"vscode": {"label": "VSCode"}},
            guard_current_ide_closed=lambda: None,
            is_ide_running=lambda ide: False,
            create_prewrite_backup=lambda **kwargs: None,
            apply_db_entries=lambda entries: applied_entries.append(entries),
            entry_key_for_ext_fn=lambda ext_id: f'secret://{{"extensionId":"{ext_id}","key":"oauth"}}',
            to_kilo_new_format=lambda value: value,
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: None,
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(
            applied_entries,
            [[
                {
                    "key": 'secret://{"extensionId":"rooveterinaryinc.roo-cline","key":"oauth"}',
                    "value": {"accountId": "acct-kilo", "refresh_token": "refresh-kilo"},
                }
            ]],
        )
        self.assertIn("[cross-ext] No 'rooveterinaryinc.roo-cline' key", messages[1])

    def test_use_codex_account_and_import_codex_account_validate_inputs(self):
        with self.assertRaisesRegex(UserFacingError, "does not contain a Codex entry"):
            account_services.use_codex_account(
                "alice",
                load_saved_account_data=lambda name, expected_kind=None: ("account.json", {}, "codex"),
                saved_codex_entry=lambda data: None,
                create_prewrite_backup=lambda **kwargs: None,
                to_codex_format=lambda value, existing: existing,
                read_codex_auth=lambda: {},
                write_codex_auth=lambda auth: None,
                codex_auth_path="codex/auth.json",
                user_facing_error_cls=UserFacingError,
                print_fn=lambda msg: None,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.use_codex_account(
                "alice",
                load_saved_account_data=lambda name, expected_kind=None: ("account.json", {}, "codex"),
                saved_codex_entry=lambda data: {"value": {"accountId": "acct-1"}},
                create_prewrite_backup=lambda **kwargs: None,
                to_codex_format=lambda value, existing: (_ for _ in ()).throw(ValueError("requires id_token")),
                read_codex_auth=lambda: {},
                write_codex_auth=lambda auth: None,
                codex_auth_path="codex/auth.json",
                user_facing_error_cls=UserFacingError,
                print_fn=lambda msg: None,
            )

        auth_path = self.root / "codex" / "import.json"
        self.write_json(auth_path, {"source": "auth"})

        with self.assertRaisesRegex(UserFacingError, "access_token or refresh_token missing"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {"accountId": "acct-1", "expires": 123, "id_token": "id-1"},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "could not decode access token expiry"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "accountId": "acct-1",
                    "expires": 0,
                    "id_token": "id-1",
                },
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "accountId": "acct-1",
                    "expires": 123,
                },
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

    def test_print_saved_entries_includes_expiration_only_when_present(self):
        messages: list[str] = []
        account_services.print_saved_entries(
            [
                {"value": {"accountId": "acct-1"}},
                {"value": {"accountId": "acct-2", "expires": 1700000000000}},
            ],
            print_fn=messages.append,
        )

        self.assertEqual(messages[0], "  accountId: acct-1")
        self.assertEqual(messages[1], "  accountId: acct-2")
        self.assertTrue(messages[2].startswith("  expires:   "))


class IdeContextModuleTests(unittest.TestCase):
    def test_tupled_context_and_candidate_helpers_cover_edge_cases(self):
        self.assertEqual(ide_context._tupled("code"), ("code",))
        self.assertEqual(ide_context._tupled(None), ())
        self.assertEqual(ide_context._tupled(5), ())
        self.assertEqual(ide_context._tupled(["code", 7]), ("code", "7"))

        with self.assertRaisesRegex(ValueError, "Unknown IDE"):
            ide_context.resolve_context("cursor", {})

        context = ide_context.resolve_context(
            "vscode",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "state.vscdb",
                    "local_state": "Local State",
                    "process": "Code.exe",
                    "launch_env": "CODE_EXE",
                    "launch_commands": ["code"],
                    "launch_paths": ["C:/Code.exe"],
                }
            },
        )
        self.assertEqual(context.launch_commands, ("code",))
        self.assertEqual(context.launch_paths, ("C:/Code.exe",))

        overridden = ide_context.override_context(context, db_path="alt.vscdb")
        self.assertEqual(overridden.db_path, "alt.vscdb")
        self.assertEqual(overridden.local_state_path, "Local State")

        deduped = ide_context.dedupe_candidate_paths(["", ".\\Code.exe", ".\\Code.exe", "./Code.exe"])
        self.assertEqual(len(deduped), 1)

    def test_windows_path_command_launch_and_running_helpers(self):
        class FakeKey:
            def __init__(self, value: str):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        registry_values = {
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe"): "C:/VSCode/Code.exe",
            ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Code.exe"): "C:/VSCode/Code.exe",
        }

        def open_key(hive, subkey):
            value = registry_values.get((hive, subkey))
            if value is None:
                raise OSError("missing")
            return FakeKey(value)

        fake_winreg = SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            HKEY_LOCAL_MACHINE="HKLM",
            OpenKey=open_key,
            QueryValueEx=lambda key, _name: (key.value, 1),
        )

        with patch.object(ide_context, "winreg", fake_winreg):
            self.assertEqual(ide_context.windows_app_path_candidates("Code.exe"), [os.path.normpath("C:/VSCode/Code.exe")])

        with patch("vscode_inject.ide_context.shutil.which", side_effect=lambda name: "C:/bin/code.cmd" if name == "code" else None):
            self.assertEqual(ide_context.path_command_candidates(["", "code", "missing"]), [os.path.normpath("C:/bin/code.cmd")])

        context = ide_context.IDEContext(
            name="vscode",
            label="VSCode",
            process_name="Code.exe",
            launch_env="CODE_EXE",
            launch_commands=("code",),
            launch_paths=("C:/Configured/Code.exe",),
        )
        candidates = ide_context.ide_executable_candidates(
            context,
            environ={"CODE_EXE": "C:/Env/Code.exe"},
            windows_app_path_candidates_fn=lambda exe: ["C:/Registry/Code.exe"] if exe == "Code.exe" else [],
            path_command_candidates_fn=lambda commands: ["C:/Path/code.cmd"] if "code" in commands else [],
        )
        self.assertEqual(
            candidates,
            [
                os.path.normpath("C:/Env/Code.exe"),
                os.path.normpath("C:/Configured/Code.exe"),
                os.path.normpath("C:/Registry/Code.exe"),
                os.path.normpath("C:/Path/code.cmd"),
            ],
        )

        self.assertIsNone(
            ide_context.resolve_ide_executable_path(
                context,
                executable_candidates=lambda ctx: ["C:/missing.exe"],
                isfile=lambda path: False,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "no candidate paths configured"):
            ide_context.launch_ide(
                ide_context.IDEContext(name="plain", label="Plain IDE"),
                resolve_executable_path=lambda ctx: None,
                executable_candidates=lambda ctx: [],
            )

        self.assertFalse(ide_context.is_ide_running(ide_context.IDEContext(name="plain", label="Plain IDE")))
        self.assertTrue(
            ide_context.is_ide_running(
                ide_context.IDEContext(name="vscode", label="VSCode", process_name="Code.exe"),
                run=lambda *args, **kwargs: SimpleNamespace(stdout='"Code.exe","123","Console","1","42 K"'),
            )
        )


class GuiAppModuleTests(unittest.TestCase):
    def test_execute_guarded_call_handles_system_exit_and_poll_swallow_exceptions(self):
        with patch("builtins.print") as print_mock:
            message, ok = gui_app.execute_guarded_call(lambda: (_ for _ in ()).throw(SystemExit(7)))

        self.assertFalse(ok)
        self.assertEqual(message, "Aborted (code 7)")
        print_mock.assert_called_once_with("Aborted (code 7)")

        root = Mock()
        notebook = Mock()
        notebook.select.side_effect = RuntimeError("tab lookup failed")
        ide_tab = SimpleNamespace(frame=".ide", refresh_runtime_state=Mock())

        gui_app.poll_ide_runtime_state(root, notebook, ide_tab)

        ide_tab.refresh_runtime_state.assert_not_called()
        root.after.assert_called_once_with(gui_app.POLL_INTERVAL_MS, gui_app.poll_ide_runtime_state, root, notebook, ide_tab, gui_app.POLL_INTERVAL_MS)

    def test_log_auto_refresh_result_ignores_empty_message_and_mapping_batch_delegates(self):
        with patch("builtins.print") as print_mock:
            gui_app.log_auto_refresh_result(refresh_scheduler.AutoRefreshResult(next_delay_ms=1, message=None))
        print_mock.assert_not_called()

        with patch("vscode_inject.gui_app.db.write_saved_account_batch") as write_batch:
            gui_app.write_saved_account_mapping_batch({"account.json": {"entries": []}})
        write_batch.assert_called_once_with({"account.json": {"entries": []}})

    def test_main_wires_gui_refresh_and_auto_refresh_loops_with_fakes(self):
        class FakeRoot:
            def __init__(self):
                self.after_calls: list[tuple[int, object, tuple[object, ...]]] = []
                self.geometry_value = None
                self.mainloop_called = False
                self.update_called = False
                self.title_args = None
                self.resizable_args = None
                self.configure_kwargs = None

            def title(self, value):
                self.title_args = value

            def resizable(self, width, height):
                self.resizable_args = (width, height)

            def configure(self, **kwargs):
                self.configure_kwargs = kwargs

            def after(self, delay, callback, *args):
                self.after_calls.append((delay, callback, args))

            def update_idletasks(self):
                self.update_called = True

            def geometry(self, value):
                self.geometry_value = value

            def winfo_reqwidth(self):
                return 900

            def winfo_reqheight(self):
                return 480

            def mainloop(self):
                self.mainloop_called = True

        class FakeStringVar:
            def __init__(self, value=""):
                self.value = value

            def set(self, value):
                self.value = value

        class FakeLabel:
            instances: list["FakeLabel"] = []

            def __init__(self, root, **kwargs):
                self.root = root
                self.kwargs = kwargs
                self.config_calls: list[dict] = []
                self.pack_calls: list[dict] = []
                FakeLabel.instances.append(self)

            def config(self, **kwargs):
                self.config_calls.append(kwargs)

            def pack(self, **kwargs):
                self.pack_calls.append(kwargs)

        class FakeStyle:
            def theme_use(self, _name):
                pass

            def configure(self, *_args, **_kwargs):
                pass

            def map(self, *_args, **_kwargs):
                pass

        class FakeNotebook:
            def __init__(self, root):
                self.root = root
                self.pack_calls: list[dict] = []
                self.selected = ".ide"

            def pack(self, **kwargs):
                self.pack_calls.append(kwargs)

            def select(self):
                return self.selected

        class FakeIdeTab:
            instances: list["FakeIdeTab"] = []

            def __init__(self, notebook, services):
                self.notebook = notebook
                self.services = services
                self.frame = ".ide"
                self.refresh_calls = 0
                self.refresh_runtime_state_calls = 0
                FakeIdeTab.instances.append(self)

            def refresh(self):
                self.refresh_calls += 1

            def refresh_runtime_state(self):
                self.refresh_runtime_state_calls += 1

        class FakeCodexTab:
            instances: list["FakeCodexTab"] = []

            def __init__(self, notebook, services):
                self.notebook = notebook
                self.services = services
                self.refresh_calls = 0
                FakeCodexTab.instances.append(self)

            def refresh(self):
                self.refresh_calls += 1

        class InlineThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                if self.target is not None:
                    self.target()

        class FakeAutoRefreshScheduler:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.policy = SimpleNamespace(min_delay_ms=321, scan_interval_ms=654)

            def run_once(self):
                return refresh_scheduler.AutoRefreshResult(
                    next_delay_ms=555,
                    ok=True,
                    refresh_ui=True,
                    message="auto refreshed",
                )

        def get_after_callback(root: FakeRoot, name: str):
            for _delay, callback, args in root.after_calls:
                if getattr(callback, "__name__", None) == name:
                    return callback, args
            raise AssertionError(f"Callback not scheduled: {name}")

        root = FakeRoot()
        auto_refresh_logs: list[refresh_scheduler.AutoRefreshResult] = []

        with patch("vscode_inject.gui_app.tk.Tk", return_value=root), \
            patch("vscode_inject.gui_app.tk.StringVar", FakeStringVar), \
            patch("vscode_inject.gui_app.tk.Label", FakeLabel), \
            patch("vscode_inject.gui_app.ttk.Style", FakeStyle), \
            patch("vscode_inject.gui_app.ttk.Notebook", FakeNotebook), \
            patch("vscode_inject.gui_app.IdeAccountsTab", FakeIdeTab), \
            patch("vscode_inject.gui_app.CodexTab", FakeCodexTab), \
            patch("vscode_inject.gui_app.refresh_scheduler.AutoRefreshScheduler", FakeAutoRefreshScheduler), \
            patch("vscode_inject.gui_app.threading.Thread", InlineThread), \
            patch("vscode_inject.gui_app.log_auto_refresh_result", side_effect=auto_refresh_logs.append):
            gui_app.main()

            ide_tab = FakeIdeTab.instances[0]
            codex_tab = FakeCodexTab.instances[0]
            status_label = FakeLabel.instances[0]
            status_var = status_label.kwargs["textvariable"]

            self.assertEqual(root.title_args, "Account Manager")
            self.assertEqual(root.resizable_args, (False, False))
            self.assertEqual(root.configure_kwargs, {"bg": gui_app.BG})
            self.assertEqual(root.geometry_value, f"{gui_app.WINDOW_WIDTH}x480")
            self.assertTrue(root.update_called)
            self.assertTrue(root.mainloop_called)
            self.assertEqual(ide_tab.refresh_calls, 1)
            self.assertEqual(codex_tab.refresh_calls, 1)
            self.assertEqual(ide_tab.refresh_runtime_state_calls, 1)
            self.assertTrue(callable(ide_tab.services.refresh_all))

            ide_tab.services.run_guarded(lambda: "manual success")
            process_ui_queue, ui_args = get_after_callback(root, "process_ui_queue")
            process_ui_queue(*ui_args)

            self.assertEqual(status_var.value, "manual success")
            self.assertEqual(status_label.config_calls[-1], {"fg": "#2d8a4e"})
            self.assertEqual(ide_tab.refresh_calls, 2)
            self.assertEqual(codex_tab.refresh_calls, 2)

            request_auto_refresh, request_args = get_after_callback(root, "request_auto_refresh")
            request_auto_refresh(*request_args)
            process_auto_refresh_queue, auto_args = get_after_callback(root, "process_auto_refresh_queue")
            process_auto_refresh_queue(*auto_args)

            self.assertEqual(status_var.value, "auto refreshed")
            self.assertEqual(ide_tab.refresh_calls, 3)
            self.assertEqual(codex_tab.refresh_calls, 3)
            self.assertEqual(len(auto_refresh_logs), 1)
            self.assertEqual(auto_refresh_logs[0].message, "auto refreshed")
            self.assertIn((gui_app.AUTO_REFRESH_START_DELAY_MS, request_auto_refresh, ()), root.after_calls)

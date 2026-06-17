# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import sqlite3
import sys
import tempfile
import tkinter as tk
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import parse_vscdb as db
from vscode_inject import gui_tabs
from vscode_inject.gui_app import (
    AUTO_REFRESH_START_DELAY_MS,
    POLL_INTERVAL_MS,
    execute_auto_refresh_tick,
    execute_guarded_call,
    log_auto_refresh_result,
    poll_ide_runtime_state,
    start_auto_refresh_worker,
)
from vscode_inject import refresh_scheduler
from vscode_inject.gui_tabs import CodexTab, EXPIRED_ROW_TAG, GuiServices, IdeAccountsTab


def create_state_db(path: Path, rows: list[tuple[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        if rows:
            con.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", rows)
        con.commit()
    finally:
        con.close()


def read_state_rows(path: Path) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT key, value FROM ItemTable ORDER BY key"))
    finally:
        con.close()


def oauth_key(ext_id: str) -> str:
    return f'secret://{{"extensionId":"{ext_id}","key":"{db.OAUTH_KEY}"}}'


class ParseVscdbTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def patch_db(self, name: str, value):
        patcher = patch.object(db, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_file(self, path: Path, content: str = "stub") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def capture_output(self, fn, *args, **kwargs) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            fn(*args, **kwargs)
        return output.getvalue()

    def test_account_fingerprint_prefers_refresh_then_account_id(self):
        self.assertEqual(
            db.account_fingerprint({"refresh_token": "refresh-1", "accountId": "acct-1"}),
            hashlib.sha256(b"refresh-1").hexdigest(),
        )
        self.assertEqual(
            db.account_fingerprint({"accountId": "acct-2"}),
            hashlib.sha256(b"acct-2").hexdigest(),
        )
        self.assertIsNone(db.account_fingerprint({"expires": 123}))
        self.assertIsNone(db.account_fingerprint("not-a-dict"))

    def test_account_fingerprint_supports_nested_tokens_and_non_string_values(self):
        self.assertEqual(
            db.account_fingerprint({"tokens": {"refresh_token": "nested-refresh"}}),
            hashlib.sha256(b"nested-refresh").hexdigest(),
        )
        self.assertEqual(
            db.account_fingerprint({"tokens": {"account_id": 12345}}),
            hashlib.sha256(b"12345").hexdigest(),
        )

    def test_set_ide_updates_selected_paths_and_rejects_unknown_name(self):
        self.patch_db("CURRENT_IDE", db.CURRENT_IDE)
        self.patch_db("DB_PATH", db.DB_PATH)
        self.patch_db("LOCAL_STATE_PATH", db.LOCAL_STATE_PATH)
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                },
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                },
            },
        )

        db.set_ide("antigravity")

        self.assertEqual(db.CURRENT_IDE, "antigravity")
        self.assertEqual(db.DB_PATH, "db-antigravity")
        self.assertEqual(db.LOCAL_STATE_PATH, "local-antigravity")

        with self.assertRaisesRegex(ValueError, "Unknown IDE"):
            db.set_ide("cursor")

    def test_resolve_ide_executable_path_prefers_env_override(self):
        env_exe = self.root / "env" / "Code.exe"
        fallback_exe = self.root / "fallback" / "Code.exe"
        self.write_file(env_exe)
        self.write_file(fallback_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_env": "VSCODE_INJECT_VSCODE_EXE",
                    "launch_paths": [str(fallback_exe)],
                }
            },
        )

        with patch.dict("os.environ", {"VSCODE_INJECT_VSCODE_EXE": str(env_exe)}, clear=False):
            self.assertEqual(db.resolve_ide_executable_path("vscode"), str(env_exe))

    def test_resolve_ide_executable_path_falls_back_to_windows_app_paths(self):
        registry_exe = self.root / "registry" / "Antigravity.exe"
        self.write_file(registry_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                    "launch_paths": [str(self.root / "missing" / "Antigravity.exe")],
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [str(registry_exe)] if exe_name == "Antigravity.exe" else [])

        self.assertEqual(db.resolve_ide_executable_path("antigravity"), str(registry_exe))

    def test_resolve_ide_executable_path_falls_back_to_path_commands(self):
        path_exe = self.root / "path" / "code.cmd"
        self.write_file(path_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_commands": ["code"],
                    "launch_paths": [str(self.root / "missing" / "Code.exe")],
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [])
        self.patch_db("_path_command_candidates", lambda command_names: [str(path_exe)] if "code" in command_names else [])

        self.assertEqual(db.resolve_ide_executable_path("vscode"), str(path_exe))

    def test_resolve_ide_executable_path_accepts_iterable_launch_config_values(self):
        path_exe = self.root / "iterable" / "Code.exe"
        self.write_file(path_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_commands": {"code"},
                    "launch_paths": {str(path_exe)},
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [])
        self.patch_db("_path_command_candidates", lambda command_names: [])

        self.assertEqual(db.resolve_ide_executable_path("vscode"), str(path_exe))

    def test_launch_ide_starts_resolved_executable(self):
        exe_path = str(self.root / "launch" / "Code.exe")

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("resolve_ide_executable_path", lambda ide=None: exe_path)

        with patch("subprocess.Popen") as popen:
            message = db.launch_ide("vscode")

        popen.assert_called_once_with([exe_path], close_fds=True)
        self.assertEqual(message, "Started VSCode")

    def test_launch_ide_reports_checked_paths_when_executable_missing(self):
        checked_paths = [r"%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe"]

        self.patch_db(
            "IDE_PATHS",
            {
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                    "launch_env": "VSCODE_INJECT_ANTIGRAVITY_EXE",
                    "launch_paths": checked_paths,
                }
            },
        )
        self.patch_db("resolve_ide_executable_path", lambda ide=None: None)
        self.patch_db("ide_executable_candidates", lambda ide=None: list(checked_paths))

        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Antigravity executable not found") as exc:
                db.launch_ide("antigravity")

        self.assertIn("%LOCALAPPDATA%\\Programs\\Antigravity\\Antigravity.exe", str(exc.exception))
        self.assertIn("VSCODE_INJECT_ANTIGRAVITY_EXE", str(exc.exception))

    def test_normalize_ide_ext_selection_preserves_current_contract(self):
        names, label = db._normalize_ide_ext_selection(None)
        self.assertEqual(names, ["kilocode", "roo-cline"])
        self.assertEqual(label, "both")

        names, label = db._normalize_ide_ext_selection(["kilo-new", "both"])
        self.assertEqual(names, ["kilo-new", "kilocode", "roo-cline"])
        self.assertEqual(label, "kilo-new+kilocode+roo-cline")

        with self.assertRaisesRegex(ValueError, "Unknown extension"):
            db._normalize_ide_ext_selection("unknown-ext")

    def test_read_current_accounts_reads_oauth_rows_from_state_db(self):
        db_path = self.root / "state.vscdb"
        create_state_db(
            db_path,
            [
                (
                    oauth_key("kilocode.kilo-code"),
                    json.dumps({
                        "accountId": "acct-kilo",
                        "refresh_token": "refresh-kilo",
                        "expires": 111,
                    }),
                ),
                (
                    oauth_key("rooveterinaryinc.roo-cline"),
                    json.dumps({
                        "accountId": "acct-roo",
                        "refresh_token": "refresh-roo",
                        "expires": 222,
                    }),
                ),
                ("window.zoomLevel", "1"),
            ],
        )

        self.patch_db("get_aes_key", lambda local_state_path=None: b"ignored")

        accounts = db.read_current_accounts(str(db_path), str(self.root / "Local State"))

        self.assertEqual(
            accounts,
            {
                "kilocode.kilo-code": {
                    "accountId": "acct-kilo",
                    "fingerprint": hashlib.sha256(b"refresh-kilo").hexdigest(),
                    "expires": 111,
                },
                "rooveterinaryinc.roo-cline": {
                    "accountId": "acct-roo",
                    "fingerprint": hashlib.sha256(b"refresh-roo").hexdigest(),
                    "expires": 222,
                },
            },
        )

    def test_read_current_accounts_decodes_buffer_wrapped_secret_entries(self):
        db_path = self.root / "buffer_state.vscdb"
        create_state_db(
            db_path,
            [
                (
                    oauth_key("kilocode.kilo-code"),
                    json.dumps({"type": "Buffer", "data": [1, 2, 3]}),
                ),
            ],
        )

        def fake_decrypt_value(raw: bytes, aes_key: bytes | None) -> str:
            self.assertEqual(raw, b"\x01\x02\x03")
            self.assertEqual(aes_key, b"aes-key")
            return json.dumps({
                "accountId": "acct-buffer",
                "refresh_token": "refresh-buffer",
                "expires": 777,
            })

        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("decrypt_value", fake_decrypt_value)

        accounts = db.read_current_accounts(str(db_path), str(self.root / "Local State"))

        self.assertEqual(
            accounts,
            {
                "kilocode.kilo-code": {
                    "accountId": "acct-buffer",
                    "fingerprint": hashlib.sha256(b"refresh-buffer").hexdigest(),
                    "expires": 777,
                }
            },
        )

    def test_read_current_accounts_for_antigravity_merges_kilo_new_auth(self):
        db_path = self.root / "antigravity" / "state.vscdb"
        create_state_db(
            db_path,
            [
                (
                    oauth_key("kilocode.kilo-code"),
                    json.dumps({
                        "accountId": "acct-kilo",
                        "refresh_token": "refresh-kilo",
                        "expires": 111,
                    }),
                ),
            ],
        )

        kilo_auth = self.root / "kilo" / "auth.json"
        self.write_json(
            kilo_auth,
            {
                "openai": {
                    "refresh": "refresh-kilo-new",
                    "accountId": "acct-kilo-new",
                    "expires": 333,
                }
            },
        )

        self.patch_db(
            "IDE_PATHS",
            {
                "antigravity": {
                    "label": "Antigravity",
                    "db": str(db_path),
                    "local_state": str(self.root / "antigravity" / "Local State"),
                    "process": "Antigravity.exe",
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("get_aes_key", lambda local_state_path=None: b"ignored")

        accounts = db.read_current_accounts_for_ide("antigravity")

        self.assertEqual(accounts["kilocode.kilo-code"]["accountId"], "acct-kilo")
        self.assertEqual(accounts[db.KILO_NEW_KEY]["accountId"], "acct-kilo-new")
        self.assertEqual(
            accounts[db.KILO_NEW_KEY]["fingerprint"],
            hashlib.sha256(b"refresh-kilo-new").hexdigest(),
        )

    def test_match_saved_to_current_matches_known_extensions_once(self):
        current_accounts = {
            "kilocode.kilo-code": {
                "fingerprint": hashlib.sha256(b"refresh-kilo").hexdigest(),
            },
            "rooveterinaryinc.roo-cline": {
                "fingerprint": hashlib.sha256(b"refresh-roo").hexdigest(),
            },
        }
        saved_entries = [
            {
                "key": oauth_key("kilocode.kilo-code"),
                "value": {"refresh_token": "refresh-kilo"},
            },
            {
                "key": oauth_key("kilocode.kilo-code"),
                "value": {"refresh_token": "refresh-kilo"},
            },
            {
                "key": oauth_key("rooveterinaryinc.roo-cline"),
                "value": {"refresh_token": "refresh-roo"},
            },
            {
                "key": oauth_key("missing.extension"),
                "value": {"refresh_token": "refresh-missing"},
            },
        ]

        matched = db.match_saved_to_current(saved_entries, current_accounts)

        self.assertEqual(matched, ["kilocode", "roo-cline"])

    def test_get_kilo_new_fingerprint_returns_hash_or_none(self):
        kilo_auth = self.root / "kilo" / "auth.json"
        self.write_json(kilo_auth, {"openai": {"refresh": "refresh-kilo", "accountId": "acct-kilo"}})
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))

        self.assertEqual(
            db.get_kilo_new_fingerprint(),
            hashlib.sha256(b"refresh-kilo").hexdigest(),
        )

        self.write_json(kilo_auth, {"openai": {"accountId": "acct-kilo"}})
        self.assertIsNone(db.get_kilo_new_fingerprint())

    def test_encrypt_value_and_decrypt_value_round_trip_with_real_crypto(self):
        aes_key = bytes(range(32))
        plaintext = "real-crypto-payload"

        encrypted = db.encrypt_value(plaintext, aes_key)

        self.assertTrue(encrypted.startswith(b"v10"))
        self.assertNotEqual(encrypted, plaintext.encode("utf-8"))
        self.assertEqual(db.decrypt_value(encrypted, aes_key), plaintext)

    def test_decode_entry_decrypts_buffer_payload_with_real_crypto(self):
        aes_key = bytes(range(32))
        plaintext = json.dumps({"accountId": "acct-crypto", "refresh_token": "refresh-crypto"})
        encrypted = db.encrypt_value(plaintext, aes_key)

        decoded = db._decode_entry(json.dumps({"type": "Buffer", "data": list(encrypted)}), aes_key)

        self.assertEqual(decoded, plaintext)

    def test_restore_writes_secret_entries_as_buffer_and_plain_entries_as_text(self):
        db_path = self.root / "restore.vscdb"
        create_state_db(db_path, [])

        secret_value = {
            "accountId": "acct-restore",
            "refresh_token": "refresh-restore",
            "expires": 444,
        }
        backup_path = self.root / "restore.json"
        self.write_json(
            backup_path,
            {
                "entries": [
                    {"key": oauth_key("kilocode.kilo-code"), "value": secret_value},
                    {"key": "workbench.colorTheme", "value": "Solarized Dark"},
                ]
            },
        )

        encrypted_calls: list[tuple[str, bytes]] = []

        def fake_encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
            encrypted_calls.append((plaintext, aes_key))
            return b"ENC"

        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(db_path),
                    "local_state": str(self.root / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("encrypt_value", fake_encrypt_value)

        db.restore(str(backup_path), create_safety_backup=False)

        rows = read_state_rows(db_path)
        self.assertEqual(
            encrypted_calls,
            [(json.dumps(secret_value, ensure_ascii=False), b"aes-key")],
        )
        self.assertEqual(
            json.loads(rows[oauth_key("kilocode.kilo-code")]),
            {"type": "Buffer", "data": [69, 78, 67]},
        )
        self.assertEqual(rows["workbench.colorTheme"], "Solarized Dark")

    def test_restore_raises_user_error_when_aes_key_is_unavailable(self):
        db_path = self.root / "restore_no_key.vscdb"
        create_state_db(db_path, [])
        backup_path = self.root / "restore_no_key.json"
        self.write_json(
            backup_path,
            {"entries": [{"key": oauth_key("kilocode.kilo-code"), "value": {"refresh_token": "r"}}]},
        )

        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(db_path),
                    "local_state": str(self.root / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: None)

        with self.assertRaisesRegex(db.UserFacingError, "ERROR: Cannot get AES key"):
            db.restore(str(backup_path), create_safety_backup=False)

    def test_restore_checks_aes_key_before_creating_safety_backup(self):
        db_path = self.root / "restore_no_key_default.vscdb"
        create_state_db(db_path, [])
        backup_path = self.root / "restore_no_key_default.json"
        self.write_json(
            backup_path,
            {"entries": [{"key": oauth_key("kilocode.kilo-code"), "value": {"refresh_token": "r"}}]},
        )

        backup_calls: list[dict[str, object]] = []

        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(db_path),
                    "local_state": str(self.root / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: None)
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))

        with self.assertRaisesRegex(db.UserFacingError, "ERROR: Cannot get AES key"):
            db.restore(str(backup_path))

        self.assertEqual(backup_calls, [])

    def test_restore_raises_user_error_when_key_filter_matches_nothing(self):
        db_path = self.root / "restore_filter.vscdb"
        create_state_db(db_path, [])
        backup_path = self.root / "restore_filter.json"
        self.write_json(
            backup_path,
            {"entries": [{"key": "workbench.colorTheme", "value": "Solarized Dark"}]},
        )
        self.patch_db("DB_PATH", str(db_path))

        with self.assertRaises(db.UserFacingError) as exc:
            db.restore(str(backup_path), key_filter="secret://")

        self.assertIn("No keys matching 'secret://' in backup.", str(exc.exception))
        self.assertIn("workbench.colorTheme", str(exc.exception))

    def test_backup_writes_manifest_with_required_and_optional_targets(self):
        vscode_db = self.root / "vscode" / "state.vscdb"
        vscode_local_state = self.root / "vscode" / "Local State"
        kilo_auth = self.root / "shared" / "kilo" / "auth.json"
        codex_auth = self.root / "shared" / "codex" / "auth.json"

        vscode_db.parent.mkdir(parents=True, exist_ok=True)
        vscode_local_state.parent.mkdir(parents=True, exist_ok=True)
        kilo_auth.parent.mkdir(parents=True, exist_ok=True)

        vscode_db.write_text("db", encoding="utf-8")
        vscode_local_state.write_text("{}", encoding="utf-8")
        kilo_auth.write_text("{}", encoding="utf-8")

        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(vscode_db),
                    "local_state": str(vscode_local_state),
                    "process": "Code.exe",
                },
                "antigravity": {
                    "label": "Antigravity",
                    "db": str(self.root / "antigravity" / "state.vscdb"),
                    "local_state": str(self.root / "antigravity" / "Local State"),
                    "process": "Antigravity.exe",
                },
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth))

        out_path = self.root / "full_backup.zip"
        message = db.backup(str(out_path))

        self.assertIn("Full backup saved", message)
        self.assertIn("Skipped 3 optional missing file(s).", message)

        with zipfile.ZipFile(out_path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["kind"], "full")
            self.assertEqual(manifest["current_ide"], "vscode")

            files = {entry["archive_path"]: entry for entry in manifest["files"]}
            self.assertTrue(files["ides/vscode/state.vscdb"]["required"])
            self.assertFalse(files["ides/antigravity/state.vscdb"]["required"])
            self.assertTrue(files["shared/kilo/auth.json"]["exists"])
            self.assertFalse(files["shared/codex/auth.json"]["exists"])

            self.assertEqual(
                set(archive.namelist()),
                {
                    "ides/vscode/state.vscdb",
                    "ides/vscode/Local State",
                    "shared/kilo/auth.json",
                    "manifest.json",
                },
            )

    def test_create_prewrite_backup_skips_optional_missing_files(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("KILO_AUTH_PATH", str(self.root / "missing" / "kilo" / "auth.json"))

        result = db.create_prewrite_backup(include_kilo=True)

        self.assertIsNone(result)
        self.assertFalse((self.root / "backups").exists())

    def test_create_prewrite_backup_raises_when_required_target_is_missing(self):
        existing_db = self.root / "vscode" / "state.vscdb"
        existing_db.parent.mkdir(parents=True, exist_ok=True)
        existing_db.write_text("db", encoding="utf-8")

        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(existing_db),
                    "local_state": str(self.root / "vscode" / "Local State"),
                    "process": "Code.exe",
                }
            },
        )

        with self.assertRaisesRegex(RuntimeError, "required target files are missing"):
            db.create_prewrite_backup(include_db=True)

    def test_save_ide_account_writes_account_file_for_selected_extension(self):
        accounts_dir = self.root / "accounts"
        entries = [
            {
                "key": oauth_key("kilocode.kilo-code"),
                "value": {
                    "accountId": "acct-save-ide",
                    "refresh_token": "refresh-save-ide",
                    "expires": 1700000000000,
                },
            }
        ]

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("_read_current_ide_entries_for_selection", lambda ext_names: entries)

        output = self.capture_output(db.save_ide_account, "alice", "kilocode")

        saved_path = accounts_dir / "alice.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["kind"], "ide")
        self.assertEqual(saved_data["ext"], "kilocode")
        self.assertEqual(saved_data["entries"], entries)
        self.assertIn("Account 'alice' saved [kilocode]", output)
        self.assertIn("acct-save-ide", output)

    def test_save_codex_account_writes_codex_entry_file(self):
        accounts_dir = self.root / "accounts"
        codex_auth_path = self.root / "codex" / "auth.json"
        self.write_json(
            codex_auth_path,
            {
                "tokens": {
                    "access_token": "access-codex",
                    "refresh_token": "refresh-codex",
                    "account_id": "acct-codex",
                    "id_token": "id-codex",
                },
                "expires": 1711111111000,
            },
        )

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth_path))

        output = self.capture_output(db.save_codex_account, "codex_alice")

        saved_path = accounts_dir / "codex_alice.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        entry = saved_data["entries"][0]
        self.assertEqual(saved_data["kind"], "codex")
        self.assertEqual(saved_data["ext"], "codex")
        self.assertEqual(entry["key"], db.CODEX_KEY)
        self.assertEqual(entry["value"]["accountId"], "acct-codex")
        self.assertEqual(entry["value"]["id_token"], "id-codex")
        self.assertIn("Account 'codex_alice' saved [codex]", output)

    def test_save_codex_account_uses_facade_codex_key_contract(self):
        accounts_dir = self.root / "accounts"
        codex_auth_path = self.root / "codex" / "auth.json"
        custom_codex_key = "codex://custom"
        self.write_json(
            codex_auth_path,
            {
                "tokens": {
                    "access_token": "access-codex",
                    "refresh_token": "refresh-codex",
                    "account_id": "acct-codex",
                    "id_token": "id-codex",
                },
                "expires": 1711111111000,
            },
        )

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth_path))
        self.patch_db("CODEX_KEY", custom_codex_key)

        self.capture_output(db.save_codex_account, "codex_alice")

        saved_path = accounts_dir / "codex_alice.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["entries"][0]["key"], custom_codex_key)

    def test_save_codex_account_raises_user_error_without_id_token(self):
        codex_auth_path = self.root / "codex" / "auth.json"
        self.write_json(
            codex_auth_path,
            {
                "tokens": {
                    "access_token": "access-codex",
                    "refresh_token": "refresh-codex",
                    "account_id": "acct-codex",
                },
                "expires": 1711111111000,
            },
        )
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth_path))

        with self.assertRaisesRegex(db.UserFacingError, "ERROR: Codex auth.json requires id_token."):
            db.save_codex_account("codex_alice")

    def test_import_codex_account_writes_saved_account_file(self):
        accounts_dir = self.root / "accounts"
        import_auth_path = self.root / "import" / "auth.json"
        self.write_json(
            import_auth_path,
            {
                "tokens": {
                    "access_token": "access-import",
                    "refresh_token": "refresh-import",
                    "account_id": "acct-import",
                    "id_token": "id-import",
                },
                "expires": 1712222222000,
            },
        )

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        output = self.capture_output(db.import_codex_account, str(import_auth_path), "imported_codex")

        saved_path = accounts_dir / "imported_codex.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        entry = saved_data["entries"][0]
        self.assertEqual(saved_data["kind"], "codex")
        self.assertEqual(entry["key"], db.CODEX_KEY)
        self.assertEqual(entry["value"]["access_token"], "access-import")
        self.assertEqual(entry["value"]["refresh_token"], "refresh-import")
        self.assertEqual(entry["value"]["accountId"], "acct-import")
        self.assertEqual(entry["value"]["id_token"], "id-import")
        self.assertIn("Imported 'imported_codex' [codex]", output)
        self.assertIn("acct-import", output)

    def test_import_codex_account_uses_facade_codex_key_contract(self):
        accounts_dir = self.root / "accounts"
        import_auth_path = self.root / "import" / "auth.json"
        custom_codex_key = "codex://custom"
        self.write_json(
            import_auth_path,
            {
                "tokens": {
                    "access_token": "access-import",
                    "refresh_token": "refresh-import",
                    "account_id": "acct-import",
                    "id_token": "id-import",
                },
                "expires": 1712222222000,
            },
        )

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("CODEX_KEY", custom_codex_key)

        self.capture_output(db.import_codex_account, str(import_auth_path), "imported_codex")

        saved_path = accounts_dir / "imported_codex.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["entries"][0]["key"], custom_codex_key)

    def test_import_codex_account_raises_user_error_when_source_file_is_missing(self):
        missing_path = self.root / "missing" / "auth.json"

        with self.assertRaisesRegex(db.UserFacingError, rf"File not found: .*{missing_path.name}"):
            db.import_codex_account(str(missing_path), "imported_codex")

    def test_import_codex_account_raises_user_error_when_auth_json_is_invalid(self):
        invalid_auth_path = self.root / "import" / "invalid_auth.json"
        invalid_auth_path.parent.mkdir(parents=True, exist_ok=True)
        invalid_auth_path.write_text("{invalid-json", encoding="utf-8")

        with self.assertRaisesRegex(db.UserFacingError, "ERROR: invalid auth.json"):
            db.import_codex_account(str(invalid_auth_path), "imported_codex")

    def test_use_ide_account_remaps_missing_extension_before_direct_db_write(self):
        db_path = self.root / "use_ide_account_remap.vscdb"
        create_state_db(db_path, [])
        source_value = {
            "accountId": "acct-remap",
            "refresh_token": "refresh-remap",
            "expires": 555,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }

        write_call: dict[str, object] = {}
        backup_calls: list[dict[str, object]] = []

        def fake_write_entries(entries: list[dict], *, aes_key=None) -> tuple[int, int]:
            write_call["entries"] = entries
            write_call["aes_key"] = aes_key
            return len(entries), 0

        def fake_create_prewrite_backup(**kwargs):
            backup_calls.append(kwargs)

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("create_prewrite_backup", fake_create_prewrite_backup)
        self.patch_db("_write_entries_to_current_db", fake_write_entries)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("is_ide_running", lambda ide=None: False)

        db.use_ide_account("alice", "roo-cline")

        self.assertEqual(
            backup_calls,
            [{"include_db": True, "include_kilo": False, "note": "before applying IDE account 'alice'"}],
        )
        self.assertEqual(
            write_call["entries"],
            [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["roo-cline"]),
                    "value": source_value,
                }
            ],
        )
        self.assertEqual(write_call["aes_key"], b"aes-key")

    def test_use_ide_account_preserves_restore_style_progress_output(self):
        db_path = self.root / "use_ide_account_output.vscdb"
        create_state_db(db_path, [])
        source_value = {
            "accountId": "acct-remap",
            "refresh_token": "refresh-remap",
            "expires": 555,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("create_prewrite_backup", lambda **kwargs: None)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("_write_entries_to_current_db", lambda entries, *, aes_key=None: (len(entries), 0))

        output = self.capture_output(db.use_ide_account, "alice", "roo-cline")

        self.assertRegex(output, r"Backup: .+\.json")
        self.assertIn("Entries to restore: 1", output)
        self.assertIn("Restored: 1  Skipped: 0", output)
        self.assertIn("Done. Start", output)

    def test_use_ide_account_non_string_target_does_not_succeed_with_zero_entries(self):
        db_path = self.root / "use_ide_account_non_string_target.vscdb"
        create_state_db(db_path, [])
        source_value = {
            "accountId": "acct-remap",
            "refresh_token": "refresh-remap",
            "expires": 555,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }
        write_call: dict[str, object] = {}

        def fake_write_entries(entries: list[dict], *, aes_key=None) -> tuple[int, int]:
            write_call["entries"] = entries
            return len(entries), 0

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db(
            "IDE_EXTENSIONS",
            {
                "both": None,
                "kilocode": "kilocode.kilo-code",
                "roo-cline": 123,
                "kilo-new": db.KILO_NEW_KEY,
            },
        )
        self.patch_db("create_prewrite_backup", lambda **kwargs: None)
        self.patch_db("_write_entries_to_current_db", fake_write_entries)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("is_ide_running", lambda ide=None: False)

        output = self.capture_output(db.use_ide_account, "alice", "roo-cline")

        self.assertEqual(
            write_call["entries"],
            [
                {
                    "key": 'secret://{"extensionId":"123","key":"openai-codex-oauth-credentials"}',
                    "value": source_value,
                }
            ],
        )
        self.assertIn("Entries to restore: 1", output)
        self.assertIn("Restored: 1  Skipped: 0", output)

    def test_use_ide_account_rechecks_current_ide_before_db_write(self):
        db_path = self.root / "use_ide_account_recheck.vscdb"
        create_state_db(db_path, [])
        source_value = {
            "accountId": "acct-remap",
            "refresh_token": "refresh-remap",
            "expires": 555,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }
        guard_calls: list[str] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("create_prewrite_backup", lambda **kwargs: None)
        self.patch_db("guard_vscode_closed", lambda: guard_calls.append("guard"))
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("_write_entries_to_current_db", lambda entries, *, aes_key=None: (len(entries), 0))

        self.capture_output(db.use_ide_account, "alice", "roo-cline")

        self.assertEqual(guard_calls, ["guard", "guard"])

    def test_use_ide_account_does_not_print_restore_progress_before_aes_key_failure(self):
        db_path = self.root / "use_ide_account_no_key.vscdb"
        create_state_db(db_path, [])
        source_value = {
            "accountId": "acct-remap",
            "refresh_token": "refresh-remap",
            "expires": 555,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("create_prewrite_backup", lambda **kwargs: None)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: None)
        self.patch_db("is_ide_running", lambda ide=None: False)

        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(db.UserFacingError, "ERROR: Cannot get AES key"):
                db.use_ide_account("alice", "roo-cline")

        self.assertNotIn("Backup:", output.getvalue())
        self.assertNotIn("Entries to restore:", output.getvalue())
        self.assertNotIn("Target DB:", output.getvalue())

    def test_use_ide_account_rejects_kilo_new_when_any_supported_ide_is_running(self):
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": {"accountId": "acct-kilo-new", "refresh_token": "refresh-kilo-new"},
                }
            ]
        }

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {"label": "VSCode"},
                "antigravity": {"label": "Antigravity"},
            },
        )
        self.patch_db("is_ide_running", lambda ide=None: ide == "vscode")

        with self.assertRaises(db.UserFacingError) as exc:
            db.use_ide_account("alice", "kilo-new")

        self.assertIn("Kilo New may be active in running IDEs", str(exc.exception))
        self.assertIn("VSCode", str(exc.exception))

    def test_load_saved_account_data_raises_account_not_found_error(self):
        self.patch_db("ACCOUNTS_DIR", str(self.root / "accounts"))

        with self.assertRaisesRegex(db.AccountNotFoundError, "Account 'alice' not found."):
            db._load_saved_account_data("alice")

    def test_load_saved_account_data_raises_account_kind_mismatch_error(self):
        accounts_dir = self.root / "accounts"
        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.write_json(
            accounts_dir / "alice.json",
            {
                "name": "alice",
                "kind": "ide",
                "entries": [{"key": db.KILO_NEW_KEY, "value": {"refresh_token": "refresh-1"}}],
            },
        )

        with self.assertRaisesRegex(db.AccountKindMismatchError, "Account 'alice' has kind 'ide', expected 'codex'."):
            db._load_saved_account_data("alice", expected_kind="codex")

    def test_use_ide_account_allows_kilo_new_when_running_with_experimental_flag(self):
        source_value = {
            "accountId": "acct-kilo-new",
            "refresh_token": "refresh-kilo-new",
            "expires": 666,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }

        written_auth: list[dict] = []
        backup_calls: list[dict[str, object]] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {"label": "VSCode"},
                "antigravity": {"label": "Antigravity"},
            },
        )
        self.patch_db("is_ide_running", lambda ide=None: ide == "vscode")
        self.patch_db("_read_kilo_auth", lambda: {"existing": {"keep": True}})
        self.patch_db("_write_kilo_auth", lambda data: written_auth.append(data))

        output = self.capture_output(db.use_ide_account, "alice", "kilo-new", True)

        self.assertIn("experimental", output)
        self.assertIn("VSCode", output)
        self.assertEqual(
            backup_calls,
            [{"include_db": False, "include_kilo": True, "note": "before applying IDE account 'alice'"}],
        )
        self.assertEqual(
            written_auth,
            [
                {
                    "existing": {"keep": True},
                    "openai": {
                        "type": "oauth",
                        "access": "",
                        "refresh": "refresh-kilo-new",
                        "expires": 666,
                        "accountId": "acct-kilo-new",
                    },
                }
            ],
        )

    def test_use_ide_account_writes_kilo_new_auth_from_generic_source(self):
        source_value = {
            "accountId": "acct-kilo-new",
            "refresh_token": "refresh-kilo-new",
            "expires": 666,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": source_value,
                }
            ]
        }

        written_auth: list[dict] = []
        backup_calls: list[dict[str, object]] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("_read_kilo_auth", lambda: {"existing": {"keep": True}})
        self.patch_db("_write_kilo_auth", lambda data: written_auth.append(data))

        db.use_ide_account("alice", "kilo-new")

        self.assertEqual(
            backup_calls,
            [{"include_db": False, "include_kilo": True, "note": "before applying IDE account 'alice'"}],
        )
        self.assertEqual(
            written_auth,
            [
                {
                    "existing": {"keep": True},
                    "openai": {
                        "type": "oauth",
                        "access": "",
                        "refresh": "refresh-kilo-new",
                        "expires": 666,
                        "accountId": "acct-kilo-new",
                    },
                }
            ],
        )

    def test_use_ide_account_prefers_explicit_kilo_new_entry_when_present(self):
        db_path = self.root / "use_ide_account_explicit_kilo.vscdb"
        create_state_db(db_path, [])
        db_source = {
            "accountId": "acct-db",
            "refresh_token": "refresh-db",
            "expires": 111,
        }
        kilo_source = {
            "accountId": "acct-kilo-only",
            "refresh_token": "refresh-kilo-only",
            "expires": 222,
        }
        account_data = {
            "entries": [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["kilocode"]),
                    "value": db_source,
                },
                {
                    "key": db.KILO_NEW_KEY,
                    "value": kilo_source,
                },
            ]
        }

        write_call: dict[str, object] = {}
        written_auth: list[dict] = []
        backup_calls: list[dict[str, object]] = []

        def fake_write_entries(entries: list[dict], *, aes_key=None) -> tuple[int, int]:
            write_call["entries"] = entries
            write_call["aes_key"] = aes_key
            return len(entries), 0

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db("_write_entries_to_current_db", fake_write_entries)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("_read_kilo_auth", lambda: {})
        self.patch_db("_write_kilo_auth", lambda data: written_auth.append(data))

        db.use_ide_account("alice", ["roo-cline", "kilo-new"])

        self.assertEqual(
            backup_calls,
            [{"include_db": True, "include_kilo": True, "note": "before applying IDE account 'alice'"}],
        )
        self.assertEqual(
            write_call["entries"],
            [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["roo-cline"]),
                    "value": db_source,
                }
            ],
        )
        self.assertEqual(write_call["aes_key"], b"aes-key")
        self.assertEqual(
            written_auth,
            [
                {
                    "openai": {
                        "type": "oauth",
                        "access": "",
                        "refresh": "refresh-kilo-only",
                        "expires": 222,
                        "accountId": "acct-kilo-only",
                    }
                }
            ],
        )

    def test_use_codex_account_creates_backup_and_writes_auth(self):
        codex_value = {
            "accountId": "acct-codex",
            "access_token": "access-codex",
            "refresh_token": "refresh-codex",
            "id_token": "id-codex",
            "expires": 999,
        }
        account_data = {
            "entries": [
                {
                    "key": db.CODEX_KEY,
                    "value": codex_value,
                }
            ]
        }

        backup_calls: list[dict[str, object]] = []
        written_auth: list[dict] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("codex.json", account_data, "codex"),
        )
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db("_read_codex_auth", lambda: {"tokens": {"account_id": "acct-codex"}})
        self.patch_db(
            "_to_codex_format",
            lambda value, existing=None: {"tokens": {"account_id": value["accountId"]}, "auth_mode": "chatgpt"},
        )
        self.patch_db("_write_codex_auth", lambda data: written_auth.append(data))

        db.use_codex_account("alice")

        self.assertEqual(
            backup_calls,
            [{"include_codex": True, "note": "before applying Codex account 'alice'"}],
        )
        self.assertEqual(
            written_auth,
            [{"tokens": {"account_id": "acct-codex"}, "auth_mode": "chatgpt"}],
        )

    def test_refresh_saved_account_updates_entries_and_metadata(self):
        account_data = {
            "name": "alice",
            "kind": "ide",
            "saved_at": "2026-05-15T10:00:00",
            "entries": [
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "access_token": "old-access",
                        "refresh_token": "refresh-1",
                        "expires": 111,
                        "accountId": "acct-1",
                    },
                }
            ],
        }
        refreshed_entries = [
            {
                "key": db.KILO_NEW_KEY,
                "value": {
                    "access_token": "new-access",
                    "refresh_token": "refresh-2",
                    "expires": 222,
                    "accountId": "acct-1",
                },
            }
        ]
        write_calls: list[tuple[str, dict]] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("alice.json", account_data, "ide"),
        )
        with patch.object(
            db.oauth_refresh,
            "refresh_saved_entries",
            return_value=db.oauth_refresh.RefreshEntriesResult(
                entries=refreshed_entries,
                refreshed_entries=1,
                refreshed_groups=1,
                refreshed_at="2026-05-15T12:34:56Z",
            ),
        ) as refresh_saved_entries, patch.object(
            db,
            "write_saved_account_batch",
            side_effect=lambda updates: write_calls.extend(list(updates.items())),
        ):
            message = db.refresh_saved_account("alice")

        refresh_saved_entries.assert_called_once_with(account_data["entries"])
        self.assertEqual(message, "Refreshed 'alice' (1 token group, 1 entry)")
        self.assertEqual(
            write_calls,
            [
                (
                    "alice.json",
                    {
                        "name": "alice",
                        "kind": "ide",
                        "saved_at": "2026-05-15T10:00:00",
                        "entries": refreshed_entries,
                        "last_refreshed_at": "2026-05-15T12:34:56Z",
                        "refresh_status": "ok",
                    },
                )
            ],
        )

    def test_refresh_saved_account_persists_error_status_and_raises_user_facing_error(self):
        account_data = {
            "name": "alice",
            "kind": "ide",
            "saved_at": "2026-05-15T10:00:00",
            "entries": [
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "access_token": "old-access",
                        "refresh_token": "refresh-1",
                        "expires": 111,
                        "accountId": "acct-1",
                    },
                }
            ],
            "last_refreshed_at": "2026-05-15T11:11:11Z",
            "refresh_status": "ok",
        }
        write_calls: list[tuple[str, dict]] = []

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("alice.json", account_data, "ide"),
        )
        with patch.object(
            db.oauth_refresh,
            "refresh_saved_entries",
            side_effect=db.oauth_refresh.TokenExchangeError("Token refresh failed: 400 invalid_grant"),
        ) as refresh_saved_entries, patch.object(
            db.oauth_refresh,
            "current_time_iso",
            return_value="2026-05-15T12:00:00Z",
        ), patch.object(
            db,
            "write_saved_account_batch",
            side_effect=lambda updates: write_calls.extend(list(updates.items())),
        ):
            with self.assertRaisesRegex(db.SavedAccountRefreshError, "Refresh failed for 'alice': .*invalid_grant") as exc_info:
                db.refresh_saved_account("alice")

        refresh_saved_entries.assert_called_once_with(account_data["entries"])
        self.assertIsInstance(exc_info.exception.__cause__, db.oauth_refresh.TokenExchangeError)
        self.assertEqual(
            write_calls,
            [
                (
                    "alice.json",
                    {
                        "name": "alice",
                        "kind": "ide",
                        "saved_at": "2026-05-15T10:00:00",
                        "entries": account_data["entries"],
                        "last_refreshed_at": "2026-05-15T11:11:11Z",
                        "refresh_status": "error",
                        "refresh_error": "Token refresh failed: 400 invalid_grant",
                        "refresh_error_at": "2026-05-15T12:00:00Z",
                    },
                )
            ],
        )

    def test_persist_refreshed_saved_account_batch_cleans_up_recovery_snapshot_after_success(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        updates = {
            "alice.json": {
                "entries": [
                    {
                        "key": db.KILO_NEW_KEY,
                        "value": {"refresh_token": "refresh-2", "access_token": "new-access"},
                    }
                ]
            }
        }
        write_calls: list[tuple[str, dict]] = []

        self.patch_db(
            "write_saved_account_batch",
            lambda payload: write_calls.extend(list(payload.items())),
        )

        db.persist_refreshed_saved_account_batch(
            updates,
            subject_label="saved account 'alice'",
            account_names=("alice",),
            providers=(db.oauth_refresh.OPENAI_CODEX_PROVIDER,),
            operation="manual-refresh",
        )

        recovery_dir = self.root / "backups" / "refresh_recovery"
        self.assertEqual(write_calls, [("alice.json", updates["alice.json"])])
        self.assertTrue(recovery_dir.exists())
        self.assertEqual(list(recovery_dir.glob("*.json")), [])

    def test_persist_refreshed_saved_account_batch_keeps_recovery_snapshot_when_primary_write_fails(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        updates = {
            "alice.json": {
                "entries": [
                    {
                        "key": db.KILO_NEW_KEY,
                        "value": {"refresh_token": "refresh-2", "access_token": "new-access"},
                    }
                ]
            }
        }

        self.patch_db(
            "write_saved_account_batch",
            lambda payload: (_ for _ in ()).throw(OSError("disk full")),
        )

        with self.assertRaisesRegex(db.RefreshedCredentialsPersistenceError, "recovery snapshot was saved to") as exc_info:
            db.persist_refreshed_saved_account_batch(
                updates,
                subject_label="saved account 'alice'",
                account_names=("alice",),
                providers=(db.oauth_refresh.OPENAI_CODEX_PROVIDER,),
                operation="manual-refresh",
            )

        recovery_path = Path(exc_info.exception.recovery_path or "")
        self.assertTrue(recovery_path.exists())
        payload = json.loads(recovery_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "saved-account-refresh-recovery")
        self.assertEqual(payload["operation"], "manual-refresh")
        self.assertEqual(payload["account_names"], ["alice"])
        self.assertEqual(payload["providers"], [db.oauth_refresh.OPENAI_CODEX_PROVIDER])
        self.assertEqual(payload["records"], [{"path": "alice.json", "data": updates["alice.json"]}])

    def test_persist_refreshed_saved_account_batch_reports_when_recovery_snapshot_cannot_be_written(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        updates = {
            "alice.json": {
                "entries": [
                    {
                        "key": db.KILO_NEW_KEY,
                        "value": {"refresh_token": "refresh-2", "access_token": "new-access"},
                    }
                ]
            }
        }

        self.patch_db(
            "write_saved_account_batch",
            lambda payload: (_ for _ in ()).throw(OSError("disk full")),
        )

        with patch.object(
            db,
            "_write_refresh_recovery_snapshot",
            side_effect=OSError("backup path unavailable"),
        ):
            with self.assertRaisesRegex(db.RefreshedCredentialsPersistenceError, "No recovery snapshot could be written") as exc_info:
                db.persist_refreshed_saved_account_batch(
                    updates,
                    subject_label="saved account 'alice'",
                    account_names=("alice",),
                    providers=(db.oauth_refresh.OPENAI_CODEX_PROVIDER,),
                    operation="manual-refresh",
                )

        self.assertIsNone(exc_info.exception.recovery_path)
        self.assertIsInstance(exc_info.exception.recovery_error, OSError)
        self.assertIn("backup path unavailable", str(exc_info.exception))

    def test_refresh_saved_account_returns_recovery_message_when_primary_save_fails_after_refresh(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        account_data = {
            "name": "alice",
            "kind": "ide",
            "saved_at": "2026-05-15T10:00:00",
            "entries": [
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "access_token": "old-access",
                        "refresh_token": "refresh-1",
                        "expires": 111,
                        "accountId": "acct-1",
                    },
                }
            ],
        }
        refreshed_entries = [
            {
                "key": db.KILO_NEW_KEY,
                "value": {
                    "access_token": "new-access",
                    "refresh_token": "refresh-2",
                    "expires": 222,
                    "accountId": "acct-1",
                },
            }
        ]

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("alice.json", account_data, "ide"),
        )
        self.patch_db(
            "write_saved_account_batch",
            lambda payload: (_ for _ in ()).throw(OSError("disk full")),
        )

        with patch.object(
            db.oauth_refresh,
            "refresh_saved_entries",
            return_value=db.oauth_refresh.RefreshEntriesResult(
                entries=refreshed_entries,
                refreshed_entries=1,
                refreshed_groups=1,
                refreshed_at="2026-05-15T12:34:56Z",
            ),
        ):
            with self.assertRaisesRegex(db.SavedAccountRefreshError, "recovery snapshot was saved to") as exc_info:
                db.refresh_saved_account("alice")

        message = str(exc_info.exception)
        self.assertIn("Refreshed saved account 'alice', but failed to persist the new credentials locally: disk full.", message)
        self.assertIn("manual sign-in may be required", message)

    def test_read_current_accounts_for_vscode_does_not_merge_kilo_new_auth(self):
        read_calls: list[tuple[str | None, str | None]] = []

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db(
            "read_current_accounts",
            lambda db_path=None, local_state_path=None: read_calls.append((db_path, local_state_path))
            or {"kilocode.kilo-code": {"accountId": "acct-vscode"}},
        )
        self.patch_db(
            "read_current_kilo_new_account",
            lambda: (_ for _ in ()).throw(AssertionError("Kilo New auth should not be read for VSCode")),
        )

        accounts = db.read_current_accounts_for_ide("vscode")

        self.assertEqual(accounts, {"kilocode.kilo-code": {"accountId": "acct-vscode"}})
        self.assertEqual(read_calls, [("db-vscode", "local-vscode")])

    def test_restore_default_path_creates_safety_backup_before_apply(self):
        db_path = self.root / "restore_default.vscdb"
        create_state_db(db_path, [])
        backup_path = self.root / "restore_default.json"
        entries = [{"key": "workbench.colorTheme", "value": "Solarized Dark"}]
        self.write_json(backup_path, {"entries": entries})

        backup_calls: list[dict[str, object]] = []
        apply_calls: list[dict[str, object]] = []
        guard_calls: list[str] = []

        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(db_path),
                    "local_state": str(self.root / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("guard_vscode_closed", lambda: guard_calls.append("guard"))
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db(
            "_apply_entries_to_current_db",
            lambda entries, *, source_label, aes_key=None: apply_calls.append(
                {"entries": list(entries), "source_label": source_label, "aes_key": aes_key}
            ),
        )

        output = self.capture_output(db.restore, str(backup_path))

        self.assertEqual(guard_calls, ["guard"])
        self.assertEqual(
            backup_calls,
            [{"include_db": True, "note": f"before restore from {backup_path.name}"}],
        )
        self.assertEqual(
            apply_calls,
            [{"entries": entries, "source_label": str(backup_path), "aes_key": b"aes-key"}],
        )
        self.assertEqual(output, "\n")

    def test_guard_vscode_closed_raises_user_facing_error_with_selected_ide_label(self):
        self.patch_db("CURRENT_IDE", "antigravity")
        self.patch_db(
            "IDE_PATHS",
            {"antigravity": {"label": "Antigravity", "db": "db", "local_state": "local", "process": "Antigravity.exe"}},
        )
        self.patch_db("is_ide_running", lambda ide=None: True)

        with self.assertRaisesRegex(db.UserFacingError, "Antigravity is running"):
            db.guard_vscode_closed()

    def test_create_prewrite_backup_returns_none_when_no_targets_are_requested(self):
        self.patch_db("_prewrite_backup_targets", lambda **kwargs: [])

        self.assertIsNone(db.create_prewrite_backup())

    def test_write_entries_to_current_db_validates_missing_db_and_aes_key(self):
        self.patch_db("DB_PATH", str(self.root / "missing.vscdb"))
        with self.assertRaisesRegex(db.UserFacingError, "DB not found"):
            db._write_entries_to_current_db([{"key": "k", "value": "v"}])

        db_path = self.root / "write_entries.vscdb"
        create_state_db(db_path, [])
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("get_aes_key", lambda local_state_path=None: None)

        with self.assertRaisesRegex(db.UserFacingError, "Cannot get AES key"):
            db._write_entries_to_current_db([{"key": "k", "value": "v"}])

    def test_apply_entries_to_current_db_prints_progress_and_uses_default_aes_flow(self):
        self.patch_db("DB_PATH", str(self.root / "progress.vscdb"))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db("IDE_PATHS", {"vscode": {"label": "VSCode"}})
        captured: dict[str, object] = {}

        self.patch_db(
            "_write_entries_to_current_db",
            lambda entries, *, aes_key=None: captured.update({"entries": list(entries), "aes_key": aes_key}) or (len(entries), 0),
        )

        output = self.capture_output(
            db._apply_entries_to_current_db,
            [{"key": "workbench.colorTheme", "value": "Solarized Dark"}],
            source_label="backup.json",
        )

        self.assertEqual(captured["aes_key"], None)
        self.assertIn("Backup: backup.json", output)
        self.assertIn("Entries to restore: 1", output)
        self.assertIn("Target DB:", output)
        self.assertIn("Restored: 1  Skipped: 0", output)

    def test_facade_helpers_delegate_to_storage_modules(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db("KILO_AUTH_PATH", "C:/Users/Test/.local/share/kilo/auth.json")
        self.patch_db("CODEX_AUTH_PATH", "C:/Users/Test/.codex/auth.json")

        with patch.object(db.backups, "backups_dir", return_value="backups") as backups_dir, patch.object(
            db.backups, "refresh_recovery_dir", return_value="recovery"
        ) as refresh_recovery_dir, patch.object(
            db.backups, "default_backup_zip_path", return_value="backup.zip"
        ) as default_backup_zip_path, patch.object(
            db.backups, "full_backup_targets", return_value=[{"source": "db"}]
        ) as full_backup_targets, patch.object(
            db.backups, "prewrite_backup_targets", return_value=[{"source": "db"}]
        ) as prewrite_backup_targets:
            self.assertEqual(db._backups_dir(), "backups")
            self.assertEqual(db._refresh_recovery_dir(), "recovery")
            self.assertEqual(db._default_backup_zip_path("prewrite"), "backup.zip")
            self.assertEqual(db._full_backup_targets(), [{"source": "db"}])
            self.assertEqual(
                db._prewrite_backup_targets(include_db=True, include_kilo=False, include_codex=True),
                [{"source": "db"}],
            )

        with patch.object(db.saved_store, "saved_account_kind", return_value="codex") as saved_account_kind, patch.object(
            db.saved_store, "list_saved_accounts", return_value=[{"name": "alice"}]
        ) as list_saved_accounts, patch.object(db.saved_store, "write_saved_account_batch") as write_saved_account_batch:
            self.assertEqual(db.saved_account_kind({"entries": []}), "codex")
            self.assertEqual(db.list_saved_accounts("ide"), [{"name": "alice"}])
            db.write_saved_account_batch({"alice.json": {"entries": []}})
            saved_account_kind.assert_called_once_with({"entries": []}, db.CODEX_KEY)
            list_saved_accounts.assert_called_once_with(db._accounts_dir(), db.CODEX_KEY, "ide")
            write_saved_account_batch.assert_called_once_with({"alice.json": {"entries": []}})

        with patch.object(db.backups, "normalize_saved_account_updates", return_value=[("alice.json", {"entries": []})]) as normalize_updates, patch.object(
            db.backups, "write_refresh_recovery_snapshot", return_value="snapshot.json"
        ) as write_snapshot, patch.object(db.backups, "cleanup_refresh_recovery_snapshot") as cleanup_snapshot, patch.object(
            db.backups, "refreshed_credentials_persistence_message", return_value="persistence message"
        ) as persistence_message:
            self.assertEqual(db._normalize_saved_account_updates({"alice.json": {"entries": []}}), [("alice.json", {"entries": []})])
            self.assertEqual(
                db._write_refresh_recovery_snapshot(
                    {"alice.json": {"entries": []}},
                    subject_label="saved account 'alice'",
                    account_names=("alice",),
                    providers=(db.oauth_refresh.OPENAI_CODEX_PROVIDER,),
                    operation="manual-refresh",
                    created_at="2026-05-17T11:00:00Z",
                ),
                "snapshot.json",
            )
            db._cleanup_refresh_recovery_snapshot("snapshot.json")
            self.assertEqual(
                db._refreshed_credentials_persistence_message(
                    subject_label="saved account 'alice'",
                    save_error=OSError("disk full"),
                    recovery_path="snapshot.json",
                    recovery_error=None,
                ),
                "persistence message",
            )
            normalize_updates.assert_called_once()
            write_snapshot.assert_called_once()
            cleanup_snapshot.assert_called_once_with("snapshot.json")
            persistence_message.assert_called_once()

        with patch.object(db.codex_store, "read_codex_auth", return_value={"tokens": {}}) as read_codex_auth, patch.object(
            db.codex_store, "write_codex_auth"
        ) as write_codex_auth, patch.object(
            db.codex_store, "to_codex_format", return_value={"formatted": True}
        ) as to_codex_format, patch.object(
            db.codex_store, "from_codex_format", return_value={"parsed": True}
        ) as from_codex_format, patch.object(
            db.codex_store, "read_current_codex_account", return_value={db.CODEX_KEY: {"accountId": "acct-codex"}}
        ) as read_current_codex_account, patch.object(
            db.kilo_new_accounts, "read_kilo_auth", return_value={"openai": {}}
        ) as read_kilo_auth, patch.object(
            db.kilo_new_accounts, "write_kilo_auth"
        ) as write_kilo_auth, patch.object(
            db.kilo_new_accounts, "to_kilo_new_format", return_value={"type": "oauth"}
        ) as to_kilo_new_format, patch.object(
            db.kilo_new_accounts, "from_kilo_new_format", return_value={"type": "openai-codex"}
        ) as from_kilo_new_format, patch.object(
            db.kilo_new_accounts, "read_current_kilo_new_account", return_value={db.KILO_NEW_KEY: {"accountId": "acct-kilo"}}
        ) as read_current_kilo_new_account:
            self.assertEqual(db._read_codex_auth(), {"tokens": {}})
            db._write_codex_auth({"tokens": {"account_id": "acct-codex"}})
            self.assertEqual(db._to_codex_format({"accountId": "acct-codex"}), {"formatted": True})
            self.assertEqual(db._from_codex_format({"tokens": {}}), {"parsed": True})
            self.assertEqual(db._read_kilo_auth(), {"openai": {}})
            db._write_kilo_auth({"openai": {"refresh": "refresh-1"}})
            self.assertEqual(db._to_kilo_new_format({"accountId": "acct-kilo"}), {"type": "oauth"})
            self.assertEqual(db._from_kilo_new_format({"openai": {}}), {"type": "openai-codex"})
            self.assertEqual(db.read_current_codex_account(), {db.CODEX_KEY: {"accountId": "acct-codex"}})
            self.assertEqual(db.read_current_kilo_new_account(), {db.KILO_NEW_KEY: {"accountId": "acct-kilo"}})
            read_codex_auth.assert_called_once_with(db.CODEX_AUTH_PATH)
            write_codex_auth.assert_called_once_with(db.CODEX_AUTH_PATH, {"tokens": {"account_id": "acct-codex"}})
            to_codex_format.assert_called_once_with({"accountId": "acct-codex"}, None)
            from_codex_format.assert_called_once_with({"tokens": {}})
            read_kilo_auth.assert_called_once_with(db.KILO_AUTH_PATH)
            write_kilo_auth.assert_called_once_with(db.KILO_AUTH_PATH, {"openai": {"refresh": "refresh-1"}})
            to_kilo_new_format.assert_called_once_with({"accountId": "acct-kilo"})
            from_kilo_new_format.assert_called_once_with({"openai": {}})
            read_current_codex_account.assert_called_once_with(db.CODEX_AUTH_PATH, db.CODEX_KEY, db.account_fingerprint)
            read_current_kilo_new_account.assert_called_once_with(db.KILO_AUTH_PATH, db.KILO_NEW_KEY, db.account_fingerprint)

        self.assertIsNone(db._saved_codex_entry({"entries": [{"key": "other"}]}))

    def test_parse_vscdb_main_raises_cli_removed_message(self):
        with self.assertRaisesRegex(SystemExit, "CLI support removed"):
            db.main()

    def test_restore_validates_missing_paths_and_empty_backups(self):
        db_path = self.root / "restore_checks.vscdb"
        create_state_db(db_path, [])
        self.patch_db("DB_PATH", str(db_path))

        with self.assertRaisesRegex(db.UserFacingError, "Backup file not found"):
            db.restore(str(self.root / "missing.json"), create_safety_backup=False)

        backup_path = self.root / "restore_checks.json"
        self.write_json(backup_path, {"entries": [{"key": "workbench.colorTheme", "value": "Dark"}]})
        self.patch_db("DB_PATH", str(self.root / "missing.vscdb"))
        with self.assertRaisesRegex(db.UserFacingError, "DB not found"):
            db.restore(str(backup_path), create_safety_backup=False)

        self.patch_db("DB_PATH", str(db_path))
        self.write_json(backup_path, {"entries": []})
        with self.assertRaisesRegex(db.UserFacingError, "No entries in backup"):
            db.restore(str(backup_path), create_safety_backup=False)

    def test_backup_message_and_passthrough_helpers_cover_remaining_facade_branches(self):
        with patch.object(db, "_create_backup_archive", return_value={"included": 1, "total": 2, "required_missing": ["missing"], "optional_missing": []}), patch.object(
            db, "_full_backup_targets", return_value=[]
        ):
            message = db.backup()
        self.assertIn("Warning: 1 required file(s) were missing.", message)

        with patch.object(db.ide_context, "dedupe_candidate_paths", return_value=["deduped"]) as dedupe_candidate_paths, patch.object(
            db.state_db, "get_aes_key", return_value=b"aes-key"
        ) as get_aes_key, patch.object(
            db.ide_context, "is_ide_running", return_value=True
        ) as is_ide_running, patch.object(
            db.account_services, "is_kilo_new", return_value=True
        ) as is_kilo_new, patch.object(
            db.account_services, "ide_db_extension_names", return_value=["kilocode", "roo-cline"]
        ) as ide_db_extension_names, patch.object(
            db.account_services, "read_current_ide_entries_for_selection", return_value=[{"key": "entry"}]
        ) as read_current_ide_entries_for_selection, patch.object(
            db, "_ide_context_for", return_value=SimpleNamespace(name="vscode", label="VSCode")
        ):
            self.patch_db("LOCAL_STATE_PATH", "local-state.json")
            self.assertEqual(db._dedupe_candidate_paths(["a", "a"]), ["deduped"])
            self.assertEqual(db.get_aes_key(), b"aes-key")
            self.assertTrue(db.is_ide_running("vscode"))
            self.assertTrue(db._is_kilo_new(db.KILO_NEW_KEY))
            self.assertEqual(db._ide_db_extension_names(), ["kilocode", "roo-cline"])
            self.assertEqual(db._read_current_ide_entries_for_selection(["kilocode", "kilo-new"]), [{"key": "entry"}])

        dedupe_candidate_paths.assert_called_once_with(["a", "a"])
        get_aes_key.assert_called_once_with("local-state.json")
        is_ide_running.assert_called_once()
        is_kilo_new.assert_called_once_with(db.KILO_NEW_KEY, db.KILO_NEW_KEY)
        ide_db_extension_names.assert_called_once_with(db.IDE_EXTENSIONS, db.KILO_NEW_KEY)
        read_current_ide_entries_for_selection.assert_called_once()


class GuiTabsHelperTests(unittest.TestCase):
    def test_formatting_and_selection_helpers_cover_edge_cases(self):
        self.assertIsInstance(gui_tabs.current_time_ms(), int)
        self.assertEqual(gui_tabs.format_saved_at({}), "?")
        self.assertEqual(gui_tabs.format_saved_at({"saved_at": "2026-05-17T11:18:00"}), "2026-05-17 11:18")
        self.assertEqual(gui_tabs.format_expires_ms(0), "")
        self.assertEqual(gui_tabs.format_expires_ms(86_400_000), "1970-01-02")
        with patch("vscode_inject.gui_tabs.datetime.datetime") as fake_datetime:
            fake_datetime.fromtimestamp.side_effect = RuntimeError("bad timestamp")
            self.assertEqual(gui_tabs.format_expires_ms(86_400_000), "")

        self.assertFalse(gui_tabs.is_expired_ms("bad"))
        self.assertFalse(gui_tabs.is_expired_ms(2_000, now_ms=1_000))
        self.assertTrue(gui_tabs.is_expired_ms(1_000, now_ms=2_000))
        self.assertEqual(gui_tabs.format_saved_expires(1_000, now_ms=2_000), "expired")
        self.assertEqual(gui_tabs.format_saved_expires(86_400_000, now_ms=1_000), "1970-01-02")
        self.assertEqual(gui_tabs.expires_row_tags(1_000, now_ms=2_000), (EXPIRED_ROW_TAG,))
        self.assertEqual(gui_tabs.expires_row_tags(86_400_000, now_ms=1_000), ())
        self.assertEqual(gui_tabs.shorten_account_id("abcdefghijklmnop", limit=8), "abcdefgh...")
        self.assertEqual(gui_tabs.shorten_account_id(None), "?")

        entries = [
            {"key": "skip-me", "value": {"accountId": "acct-skip"}},
            {"key": "keep-me", "value": {"accountId": "acct-keep-123456"}},
            {"key": "no-id", "value": {}},
        ]
        self.assertEqual(gui_tabs.summarize_account_ids(entries, skip_keys={"skip-me"}), "acct-kee...")
        self.assertEqual(gui_tabs.summarize_account_ids([{"key": "no-id", "value": {}}]), "?")
        self.assertEqual(gui_tabs.first_expires_ms(entries, skip_keys={"skip-me"}), 0)
        self.assertEqual(
            gui_tabs.first_expires_ms(
                [
                    {"key": "a", "value": {"expires": 5_000}},
                    {"key": "b", "value": {"expires": 2_000}},
                ]
            ),
            2_000,
        )
        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=0):
            self.assertEqual(gui_tabs.first_expires([{"key": "a", "value": {"expires": 1_000}}]), "1970-01-01")

        tree = Mock()
        tree.selection.return_value = ()
        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            self.assertIsNone(gui_tabs.selected_name(tree, "Pick something"))
        showwarning.assert_called_once_with("No selection", "Pick something")

        tree.selection.return_value = ("alice", "bob")
        self.assertEqual(gui_tabs.selected_name(tree, "Pick something"), "alice")

        with patch("vscode_inject.gui_tabs.simpledialog.askstring", return_value=None):
            self.assertIsNone(gui_tabs.ask_account_name(Mock(), "Save", "Name"))
        with patch("vscode_inject.gui_tabs.simpledialog.askstring", return_value="alice account"):
            self.assertEqual(gui_tabs.ask_account_name(Mock(), "Save", "Name"), "alice_account")

        db_module = SimpleNamespace(_accounts_dir=lambda: "C:/accounts")
        with patch("vscode_inject.gui_tabs.os.remove") as remove:
            gui_tabs.delete_saved_account(db_module, "alice")
        remove.assert_called_once_with(os.path.join("C:/accounts", "alice.json"))


class IdeAccountsTabTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def make_db(self, *, running: bool, antigravity_running: bool = False, vscode_running: bool | None = None):
        running_state = {
            "vscode": running if vscode_running is None else vscode_running,
            "antigravity": antigravity_running,
        }
        ide_extensions = {
            "kilocode": "kilocode.kilo-code",
            "roo-cline": "rooveterinaryinc.roo-cline",
            "kilo-new": "kilo-new://openai",
        }
        save_ide_account = Mock(name="save_ide_account")
        use_ide_account = Mock(name="use_ide_account")
        refresh_saved_account = Mock(name="refresh_saved_account")
        backup = Mock(name="backup")

        return SimpleNamespace(
            IDE_EXTENSIONS=ide_extensions,
            IDE_PATHS={
                "vscode": {"label": "VSCode"},
                "antigravity": {"label": "Antigravity"},
            },
            _EXT_DISPLAY={value: key for key, value in ide_extensions.items()},
            KILO_NEW_KEY="kilo-new://openai",
            CODEX_KEY="codex://openai",
            read_current_accounts_for_ide=lambda ide: {},
            get_kilo_new_fingerprint=lambda: None,
            list_saved_accounts=lambda kind: [],
            match_saved_to_current=lambda entries, current_accounts: [],
            account_fingerprint=lambda value: None,
            is_ide_running=lambda ide=None: running_state[ide or "vscode"],
            set_ide=lambda name: None,
            save_ide_account=save_ide_account,
            use_ide_account=use_ide_account,
            refresh_saved_account=refresh_saved_account,
            backup=backup,
            launch_ide=lambda ide=None: f"Started {ide or 'vscode'}",
        )

    def make_services(self, db_module):
        return GuiServices(
            root=self.root,
            db=db_module,
            bg="#1e1e2e",
            fg="#cdd6f4",
            btn_bg="#313244",
            btn_act="#45475a",
            sel_fg="#1e1e2e",
            run_guarded=lambda *args, **kwargs: None,
            set_status=lambda *args, **kwargs: None,
        )

    def test_run_button_is_rendered_in_bottom_button_panel(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        self.assertIs(tab.run_button.master, tab.btn_frame)

    def test_run_button_is_hidden_while_selected_ide_is_running(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=True)))

        tab.refresh()
        self.root.update_idletasks()

        self.assertEqual(tab.run_button.winfo_manager(), "")
        self.assertFalse(tab.run_button_visible)

    def test_run_button_reappears_when_selected_ide_is_closed(self):
        db_module = self.make_db(running=False)
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()
        self.root.update_idletasks()

        self.assertEqual(tab.run_button.winfo_manager(), "pack")
        self.assertTrue(tab.run_button_visible)

    def test_refresh_runtime_state_skips_ui_when_status_unchanged(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        with patch.object(tab.ide_state_label, "config") as label_config, patch.object(tab, "update_run_button_visibility") as update_visibility:
            first = tab.refresh_runtime_state()
            second = tab.refresh_runtime_state()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(label_config.call_count, 1)
        self.assertEqual(update_visibility.call_count, 1)

    def test_refresh_runtime_state_updates_when_selected_ide_changes(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        tab.refresh_runtime_state()
        tab.ide_var.set("antigravity")

        with patch.object(tab.ide_state_label, "config") as label_config, patch.object(tab, "update_run_button_visibility") as update_visibility:
            changed = tab.refresh_runtime_state()

        self.assertTrue(changed)
        self.assertEqual(label_config.call_count, 1)
        self.assertEqual(update_visibility.call_count, 1)

    def test_on_use_allows_experimental_kilo_new_live_write_for_vscode_too(self):
        db_module = self.make_db(running=False, vscode_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", side_effect=[True, True]
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        showerror.assert_not_called()
        self.assertEqual(askyesno.call_count, 2)
        services.run_guarded.assert_called_once_with(
            db_module.use_ide_account,
            "alice",
            ["kilo-new"],
            True,
            success_msg="Switched 'alice' [kilo-new]",
        )

    def test_on_use_requires_both_ides_closed_for_kilo_new_without_experimental_mode(self):
        db_module = self.make_db(running=False, antigravity_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        showerror.assert_not_called()
        askyesno.assert_called_once()
        services.run_guarded.assert_not_called()

    def test_on_use_still_blocks_running_antigravity_when_db_write_is_needed(self):
        db_module = self.make_db(running=False, antigravity_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)
        tab.ide_var.set("antigravity")

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilocode", "kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno"
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        askyesno.assert_not_called()
        showerror.assert_called_once()
        services.run_guarded.assert_not_called()

    def test_on_refresh_selected_runs_saved_account_refresh_for_ide_tab(self):
        db_module = self.make_db(running=False)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"):
            tab.on_refresh_selected()

        services.run_guarded.assert_called_once_with(db_module.refresh_saved_account, "alice", log_prefix="manual-refresh")

    def test_refresh_marks_expired_ide_rows_red_and_labels_them_expired(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "expired_ide",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-15T10:00:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-1",
                                "expires": 1_000,
                            },
                        }
                    ],
                },
            }
        ]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=2_000):
            tab.refresh()

        self.assertEqual(tab.tree.item("expired_ide", "tags"), (EXPIRED_ROW_TAG,))
        self.assertEqual(tab.tree.item("expired_ide", "values")[4], "expired")

    def test_helper_methods_cover_selection_labels_and_current_account_rendering(self):
        db_module = self.make_db(running=False)
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            self.assertEqual(tab.selected_exts(), [])
        showwarning.assert_called_once_with("No extension", "Select at least one IDE extension.")
        self.assertEqual(tab.selected_exts(show_warning=False), [])

        tab.ide_ext_vars["kilocode"].set(True)
        tab.ide_ext_vars["kilo-new"].set(True)
        self.assertEqual(tab.selected_exts(), ["kilocode", "kilo-new"])
        self.assertEqual(tab.format_ext_selection(["kilocode", "kilo-new"]), "kilocode+kilo-new")
        self.assertEqual(tab.db_target_ides_for_exts(["kilocode"]), ["vscode"])
        self.assertEqual(tab.db_target_ides_for_exts(["kilo-new"]), [])
        self.assertEqual(tab.kilo_new_target_ides_for_exts(["kilo-new"]), ["vscode", "antigravity"])
        self.assertTrue(tab.can_hot_swap_kilo_new(["kilo-new"], [], ["antigravity"]))
        self.assertFalse(tab.can_hot_swap_kilo_new(["kilo-new"], ["antigravity"], ["antigravity"]))
        self.assertEqual(tab.required_closed_ides_for_exts(["kilocode", "kilo-new"]), ["vscode", "antigravity"])
        self.assertEqual(
            tab.required_closed_ides_for_exts(["kilocode", "kilo-new"], allow_kilo_new_while_running=True),
            ["vscode"],
        )
        self.assertEqual(tab.format_ide_labels([]), "")
        self.assertEqual(tab.format_ide_labels(["vscode"]), "VSCode")
        self.assertEqual(tab.format_ide_labels(["vscode", "antigravity"]), "VSCode and Antigravity")

        tab.update_current_labels({"kilocode.kilo-code": {"accountId": "acct-kilo-1234567890"}})

        self.assertEqual(tab.current_ide_label.cget("text"), "Current in VSCode:")
        self.assertIn("kilocode:", tab.current_ide_labels["kilocode.kilo-code"].cget("text"))
        self.assertTrue(tab.current_ide_labels["kilocode.kilo-code"].cget("text").endswith("..."))
        self.assertEqual(tab.current_ide_labels["rooveterinaryinc.roo-cline"].cget("text"), "  roo-cline: -")

    def test_refresh_runtime_state_hides_visible_run_button_when_ide_starts_running(self):
        running_state = {"vscode": False, "antigravity": False}
        db_module = self.make_db(running=False)
        db_module.is_ide_running = lambda ide=None: running_state[ide or "vscode"]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()
        self.root.update_idletasks()
        self.assertEqual(tab.run_button.winfo_manager(), "pack")
        self.assertTrue(tab.run_button_visible)

        running_state["vscode"] = True
        changed = tab.refresh_runtime_state(force=True)
        self.root.update_idletasks()

        self.assertTrue(changed)
        self.assertEqual(tab.run_button.winfo_manager(), "")
        self.assertFalse(tab.run_button_visible)

    def test_refresh_survives_backend_errors_and_marks_active_targets(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "alice",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-17T11:18:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-kilo-1234567890",
                                "refresh_token": "refresh-match",
                                "expires": 86_400_000,
                            },
                        }
                    ],
                },
            }
        ]
        db_module.account_fingerprint = lambda value: value.get("refresh_token")
        db_module.match_saved_to_current = lambda entries, current_accounts: ["kilocode"] if current_accounts else []

        failing_db_module = self.make_db(running=False)
        failing_db_module.list_saved_accounts = db_module.list_saved_accounts
        failing_db_module.read_current_accounts_for_ide = lambda ide: (_ for _ in ()).throw(RuntimeError(f"{ide} unavailable"))
        failing_db_module.get_kilo_new_fingerprint = lambda: (_ for _ in ()).throw(RuntimeError("kilo unavailable"))
        notebook = ttk.Notebook(self.root)
        failing_tab = IdeAccountsTab(notebook, self.make_services(failing_db_module))

        failing_tab.refresh()

        self.assertEqual(failing_tab.tree.item("alice", "values")[5], "-")

        db_module.read_current_accounts_for_ide = lambda ide: {
            "kilocode.kilo-code": {"accountId": "acct-live", "fingerprint": "refresh-match"}
        }
        db_module.get_kilo_new_fingerprint = lambda: "refresh-match"
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()

        values = tab.tree.item("alice", "values")
        self.assertEqual(values[5], "VS+AG+KN")
        self.assertEqual(values[2], "acct-kil...")

    def test_on_ide_change_and_save_handlers_delegate_correctly(self):
        db_module = self.make_db(running=False)
        db_module.set_ide = Mock()
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch.object(tab, "refresh") as refresh:
            tab.on_ide_change()
        db_module.set_ide.assert_called_once_with("vscode")
        refresh.assert_called_once_with()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value=None):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=[]):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilocode", "roo-cline"]
        ):
            tab.on_save()

        services.run_guarded.assert_called_once_with(
            db_module.save_ide_account,
            "alice",
            ["kilocode", "roo-cline"],
            success_msg="Saved 'alice' [kilocode+roo-cline]",
        )

    def test_on_use_delete_backup_refresh_and_run_handlers_cover_branches(self):
        db_module = self.make_db(running=False)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        services.set_status = Mock()
        services.refresh_all = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=[]):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilocode"]
        ), patch("vscode_inject.gui_tabs.messagebox.askyesno", return_value=False) as askyesno:
            tab.on_use()
        askyesno.assert_called_once_with("Switch IDE account", "Switch 'alice' [kilocode]?\nVSCode must stay closed until done.")
        services.run_guarded.assert_not_called()

        db_module.is_ide_running = lambda ide=None: True
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilo-new"]
        ), patch.object(tab, "can_hot_swap_kilo_new", return_value=False), patch(
            "vscode_inject.gui_tabs.messagebox.showerror"
        ) as showerror:
            tab.on_use()
        showerror.assert_called_once_with(
            "VSCode and Antigravity running",
            "Close VSCode and Antigravity before switching accounts.",
        )
        services.run_guarded.assert_not_called()
        db_module.is_ide_running = lambda ide=None: False

        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_delete()
        services.set_status.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        askyesno.assert_called_once_with("Delete", "Delete saved account 'alice'?")
        delete_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        delete_saved_account.assert_called_once_with(db_module, "alice")
        services.set_status.assert_called_once_with("Deleted 'alice'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account", side_effect=OSError("delete failed")):
            tab.on_delete()
        services.set_status.assert_called_once_with("delete failed", False)
        services.refresh_all.assert_not_called()

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_refresh_selected()
        services.run_guarded.assert_not_called()

        tab.on_backup()
        services.run_guarded.assert_called_once_with(db_module.backup)

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        tab.on_refresh()
        services.refresh_all.assert_called_once_with()
        services.set_status.assert_called_once_with("Refreshed", True)

        services.set_status.reset_mock()
        with patch.object(tab, "refresh") as refresh:
            tab.on_run()
        services.set_status.assert_called_once_with("Started vscode", True)
        refresh.assert_called_once_with()

        services.set_status.reset_mock()
        db_module.launch_ide = lambda ide=None: (_ for _ in ()).throw(RuntimeError("launch failed"))
        with patch.object(tab, "refresh") as refresh, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_run()
        showerror.assert_called_once_with("Run IDE", "launch failed")
        services.set_status.assert_called_once_with("launch failed", False)
        refresh.assert_not_called()


class CodexTabTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def make_services(self, db_module):
        return GuiServices(
            root=self.root,
            db=db_module,
            bg="#1e1e2e",
            fg="#cdd6f4",
            btn_bg="#313244",
            btn_act="#45475a",
            sel_fg="#1e1e2e",
            run_guarded=Mock(),
            set_status=lambda *args, **kwargs: None,
        )

    def test_on_refresh_selected_runs_saved_account_refresh_for_codex_tab(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"):
            tab.on_refresh_selected()

        services.run_guarded.assert_called_once_with(db_module.refresh_saved_account, "alice", log_prefix="manual-refresh")

    def test_refresh_marks_expired_codex_rows_red_and_labels_them_expired(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [
                {
                    "name": "expired_codex",
                    "data": {
                        "saved_at": "2026-05-15T10:00:00",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex",
                                    "expires": 1_000,
                                },
                            }
                        ],
                    },
                }
            ],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=2_000):
            tab.refresh()

        self.assertEqual(tab.tree.item("expired_codex", "tags"), (EXPIRED_ROW_TAG,))
        self.assertEqual(tab.tree.item("expired_codex", "values")[3], "expired")

    def test_update_current_label_refresh_and_handlers_cover_remaining_codex_branches(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {
                "codex://openai": {"accountId": "acct-codex-1234567890", "fingerprint": "refresh-codex"}
            },
            list_saved_accounts=lambda kind: [
                {
                    "name": "alice",
                    "data": {
                        "saved_at": "2026-05-17T11:18:00",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex-1234567890",
                                    "expires": 86_400_000,
                                    "refresh_token": "refresh-codex",
                                },
                            }
                        ],
                    },
                },
                {
                    "name": "skip",
                    "data": {"entries": [{"key": "other", "value": {"accountId": "acct-skip"}}]},
                },
            ],
            account_fingerprint=lambda value: value.get("refresh_token"),
            save_codex_account=Mock(name="save_codex_account"),
            import_codex_account=Mock(name="import_codex_account"),
            use_codex_account=Mock(name="use_codex_account"),
            refresh_saved_account=Mock(name="refresh_saved_account"),
        )
        services = self.make_services(db_module)
        services.set_status = Mock()
        services.refresh_all = Mock()
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        tab.refresh()

        self.assertTrue(tab.current_value.cget("text").endswith("..."))
        self.assertEqual(tab.tree.item("alice", "values")[4], "active")
        self.assertFalse(tab.tree.exists("skip"))

        tab.update_current_label({})
        self.assertEqual(tab.current_value.cget("text"), "-")

        error_db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: (_ for _ in ()).throw(RuntimeError("codex unavailable")),
            list_saved_accounts=db_module.list_saved_accounts,
            account_fingerprint=db_module.account_fingerprint,
            save_codex_account=db_module.save_codex_account,
            import_codex_account=db_module.import_codex_account,
            use_codex_account=db_module.use_codex_account,
            refresh_saved_account=db_module.refresh_saved_account,
        )
        error_tab = CodexTab(ttk.Notebook(self.root), self.make_services(error_db_module))
        error_tab.refresh()
        self.assertEqual(error_tab.current_value.cget("text"), "-")

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value=None):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"):
            tab.on_save()
        services.run_guarded.assert_called_once_with(
            db_module.save_codex_account,
            "alice",
            success_msg="Saved Codex account 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value=""):
            tab.on_import()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value="C:/tmp/auth.json"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value=None
        ):
            tab.on_import()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value="C:/tmp/auth.json"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice"
        ):
            tab.on_import()
        services.run_guarded.assert_called_once_with(
            db_module.import_codex_account,
            "C:/tmp/auth.json",
            "alice",
            success_msg="Imported Codex account 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ):
            tab.on_use()
        services.run_guarded.assert_called_once_with(
            db_module.use_codex_account,
            "alice",
            success_msg="Switched Codex to 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_delete()
        services.set_status.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        askyesno.assert_called_once_with("Delete", "Delete saved account 'alice'?")
        delete_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        delete_saved_account.assert_called_once_with(db_module, "alice")
        services.set_status.assert_called_once_with("Deleted 'alice'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account", side_effect=OSError("delete failed")):
            tab.on_delete()
        services.set_status.assert_called_once_with("delete failed", False)
        services.refresh_all.assert_not_called()

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_refresh_selected()
        services.run_guarded.assert_not_called()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        tab.on_refresh()
        services.refresh_all.assert_called_once_with()
        services.set_status.assert_called_once_with("Refreshed", True)


class GuiAppPollingTests(unittest.TestCase):
    def test_execute_guarded_call_surfaces_backend_exception_message_and_prints_it(self):
        output = io.StringIO()
        with redirect_stdout(output):
            message, ok = execute_guarded_call(lambda: (_ for _ in ()).throw(db.UserFacingError("Specific backend error")))

        self.assertFalse(ok)
        self.assertEqual(message, "Specific backend error")
        self.assertIn("Specific backend error", output.getvalue())

    def test_execute_guarded_call_prints_traceback_for_unexpected_exception(self):
        with patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            message, ok = execute_guarded_call(lambda: (_ for _ in ()).throw(RuntimeError("Unexpected failure")))

        self.assertFalse(ok)
        self.assertEqual(message, "Unexpected failure")
        print_exc.assert_called_once_with()

    def test_execute_guarded_call_logs_manual_refresh_success_with_prefix(self):
        output = io.StringIO()

        with redirect_stdout(output):
            message, ok = execute_guarded_call(lambda: "Refreshed 'alice' (1 token group, 1 entry)", log_prefix="manual-refresh")

        self.assertTrue(ok)
        self.assertEqual(message, "Refreshed 'alice' (1 token group, 1 entry)")
        self.assertIn("[manual-refresh] INFO: Refreshed 'alice' (1 token group, 1 entry)", output.getvalue())

    def test_execute_guarded_call_logs_manual_refresh_error_with_prefix_without_traceback(self):
        output = io.StringIO()

        with redirect_stdout(output), patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            message, ok = execute_guarded_call(
                lambda: (_ for _ in ()).throw(db.SavedAccountRefreshError("Refresh failed for 'alice': invalid_grant")),
                log_prefix="manual-refresh",
            )

        self.assertFalse(ok)
        self.assertEqual(message, "Refresh failed for 'alice': invalid_grant")
        self.assertIn("[manual-refresh] ERROR: Refresh failed for 'alice': invalid_grant", output.getvalue())
        print_exc.assert_not_called()

    def test_execute_auto_refresh_tick_returns_failed_result_on_unexpected_exception(self):
        scheduler = Mock()
        scheduler.policy = refresh_scheduler.RefreshPolicy(scan_interval_ms=AUTO_REFRESH_START_DELAY_MS * 10)
        scheduler.run_once.side_effect = RuntimeError("scheduler failure")

        with patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            result = execute_auto_refresh_tick(scheduler)

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "scheduler failure")
        self.assertEqual(result.next_delay_ms, AUTO_REFRESH_START_DELAY_MS * 10)
        print_exc.assert_called_once_with()

    def test_start_auto_refresh_worker_skips_when_worker_is_already_running(self):
        scheduler = Mock()
        result_queue: queue.Queue = queue.Queue()
        worker_state = {"running": True}

        started = start_auto_refresh_worker(result_queue, scheduler, worker_state)

        self.assertFalse(started)
        self.assertTrue(result_queue.empty())

    def test_start_auto_refresh_worker_enqueues_scheduler_result(self):
        expected = refresh_scheduler.AutoRefreshResult(next_delay_ms=1234, message="auto", ok=True)
        scheduler = Mock()
        scheduler.run_once.return_value = expected
        result_queue: queue.Queue = queue.Queue()
        worker_state = {"running": False}

        class InlineThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                if self.target:
                    self.target()

        with patch("vscode_inject.gui_app.threading.Thread", InlineThread):
            started = start_auto_refresh_worker(result_queue, scheduler, worker_state)

        self.assertTrue(started)
        self.assertTrue(worker_state["running"])
        self.assertEqual(result_queue.get_nowait(), expected)

    def test_log_auto_refresh_result_prints_prefixed_message(self):
        output = io.StringIO()
        failure = refresh_scheduler.RefreshFailure(
            group=db.oauth_refresh.RefreshGroup(
                key=db.oauth_refresh.RefreshGroupKey(provider="openai-codex", refresh_token="refresh-1"),
                bundle=db.oauth_refresh.TokenBundle(access_token="", refresh_token="refresh-1", expires=0),
                expires=0,
                entries=(
                    db.oauth_refresh.RefreshableRecordEntry(
                        record_name="codex3",
                        record_path="codex3.json",
                        entry_index=0,
                        group_key=db.oauth_refresh.RefreshGroupKey(provider="openai-codex", refresh_token="refresh-1"),
                        bundle=db.oauth_refresh.TokenBundle(access_token="", refresh_token="refresh-1", expires=0),
                    ),
                ),
            ),
            error_message="terminal token error",
            terminal=True,
        )
        result = refresh_scheduler.AutoRefreshResult(
            next_delay_ms=1000,
            ok=False,
            message="terminal token error",
            failures=(failure,),
        )

        with redirect_stdout(output):
            log_auto_refresh_result(result)

        log_output = output.getvalue()
        self.assertIn("[auto-refresh] ERROR: terminal token error", log_output)
        self.assertIn("[auto-refresh] ERROR DETAIL: accounts=[codex3] provider=openai-codex status=terminal: terminal token error", log_output)

    def test_poll_ide_runtime_state_runs_only_for_active_ide_tab(self):
        root = Mock()
        notebook = Mock()
        ide_tab = SimpleNamespace(frame=".ide", refresh_runtime_state=Mock())
        notebook.select.return_value = ".ide"

        poll_ide_runtime_state(root, notebook, ide_tab)

        ide_tab.refresh_runtime_state.assert_called_once_with()
        root.after.assert_called_once_with(POLL_INTERVAL_MS, poll_ide_runtime_state, root, notebook, ide_tab, POLL_INTERVAL_MS)

    def test_poll_ide_runtime_state_skips_inactive_tab(self):
        root = Mock()
        notebook = Mock()
        ide_tab = SimpleNamespace(frame=".ide", refresh_runtime_state=Mock())
        notebook.select.return_value = ".codex"

        poll_ide_runtime_state(root, notebook, ide_tab)

        ide_tab.refresh_runtime_state.assert_not_called()
        root.after.assert_called_once_with(POLL_INTERVAL_MS, poll_ide_runtime_state, root, notebook, ide_tab, POLL_INTERVAL_MS)


if __name__ == "__main__":
    unittest.main()

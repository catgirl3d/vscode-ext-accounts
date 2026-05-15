# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import io
import json
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
from vscode_inject.gui_app import POLL_INTERVAL_MS, poll_ide_runtime_state
from vscode_inject.gui_tabs import GuiServices, IdeAccountsTab


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

    def capture_exit(self, fn, *args, **kwargs) -> tuple[object, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as exc:
                fn(*args, **kwargs)
        return exc.exception.code, output.getvalue()

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

    def test_restore_exits_when_aes_key_is_unavailable(self):
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

        code, output = self.capture_exit(db.restore, str(backup_path), create_safety_backup=False)

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Cannot get AES key", output)

    def test_restore_exits_when_key_filter_matches_nothing(self):
        db_path = self.root / "restore_filter.vscdb"
        create_state_db(db_path, [])
        backup_path = self.root / "restore_filter.json"
        self.write_json(
            backup_path,
            {"entries": [{"key": "workbench.colorTheme", "value": "Solarized Dark"}]},
        )
        self.patch_db("DB_PATH", str(db_path))

        code, output = self.capture_exit(db.restore, str(backup_path), key_filter="secret://")

        self.assertEqual(code, 1)
        self.assertIn("No keys matching 'secret://' in backup.", output)
        self.assertIn("workbench.colorTheme", output)

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

    def test_save_codex_account_exits_without_id_token(self):
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

        code, output = self.capture_exit(db.save_codex_account, "codex_alice")

        self.assertEqual(code, 1)
        self.assertIn("ERROR: Codex auth.json requires id_token.", output)

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

    def test_import_codex_account_exits_when_source_file_is_missing(self):
        missing_path = self.root / "missing" / "auth.json"

        code, output = self.capture_exit(db.import_codex_account, str(missing_path), "imported_codex")

        self.assertEqual(code, 1)
        self.assertIn(f"File not found: {missing_path}", output)

    def test_use_ide_account_remaps_missing_extension_before_restore(self):
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

        restore_call: dict[str, object] = {}
        backup_calls: list[dict[str, object]] = []

        def fake_restore(path: str, create_safety_backup: bool = True):
            with open(path, "r", encoding="utf-8") as fh:
                restore_call["payload"] = json.load(fh)
            restore_call["create_safety_backup"] = create_safety_backup

        def fake_create_prewrite_backup(**kwargs):
            backup_calls.append(kwargs)

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("create_prewrite_backup", fake_create_prewrite_backup)
        self.patch_db("restore", fake_restore)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("is_ide_running", lambda ide=None: False)

        db.use_ide_account("alice", "roo-cline")

        self.assertEqual(
            backup_calls,
            [{"include_db": True, "include_kilo": False, "note": "before applying IDE account 'alice'"}],
        )
        self.assertFalse(restore_call["create_safety_backup"])
        payload = cast(dict[str, object], restore_call["payload"])
        self.assertEqual(
            payload["entries"],
            [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["roo-cline"]),
                    "value": source_value,
                }
            ],
        )

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

        code, output = self.capture_exit(db.use_ide_account, "alice", "kilo-new")

        self.assertEqual(code, 1)
        self.assertIn("Kilo New may be active in running IDEs", output)
        self.assertIn("VSCode", output)

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

        restore_call: dict[str, object] = {}
        written_auth: list[dict] = []
        backup_calls: list[dict[str, object]] = []

        def fake_restore(path: str, create_safety_backup: bool = True):
            with open(path, "r", encoding="utf-8") as fh:
                restore_call["payload"] = json.load(fh)
            restore_call["create_safety_backup"] = create_safety_backup

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: ("account.json", account_data, "ide"),
        )
        self.patch_db("create_prewrite_backup", lambda **kwargs: backup_calls.append(kwargs))
        self.patch_db("restore", fake_restore)
        self.patch_db("guard_vscode_closed", lambda: None)
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("_read_kilo_auth", lambda: {})
        self.patch_db("_write_kilo_auth", lambda data: written_auth.append(data))

        db.use_ide_account("alice", ["roo-cline", "kilo-new"])

        self.assertEqual(
            backup_calls,
            [{"include_db": True, "include_kilo": True, "note": "before applying IDE account 'alice'"}],
        )
        self.assertFalse(restore_call["create_safety_backup"])
        payload = cast(dict[str, object], restore_call["payload"])
        self.assertEqual(
            payload["entries"],
            [
                {
                    "key": db._entry_key_for_ext(db.IDE_EXTENSIONS["roo-cline"]),
                    "value": db_source,
                }
            ],
        )
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
        use_ide_account = Mock(name="use_ide_account")

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
            use_ide_account=use_ide_account,
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


class GuiAppPollingTests(unittest.TestCase):
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

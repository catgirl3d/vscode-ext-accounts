# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

from vscode_inject import parse_vscdb as db


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


class ParseVscdbIntegrationTests(unittest.TestCase):
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
        codex_auth.parent.mkdir(parents=True, exist_ok=True)

        vscode_db.write_bytes(b"vscode-db")
        vscode_local_state.write_bytes(b"vscode-local")
        kilo_auth.write_bytes(b"kilo-auth")
        codex_auth.write_bytes(b"codex-auth")

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
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth))

        backups_dir = self.root / "backups"

        message = db.backup()
        self.assertIn("Full backup saved", message)

        zip_files = list(backups_dir.glob("full_*.zip"))
        self.assertEqual(len(zip_files), 1)

        with zipfile.ZipFile(zip_files[0]) as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("ides/vscode/state.vscdb", names)
            self.assertIn("ides/vscode/Local State", names)
            self.assertIn("shared/kilo/auth.json", names)
            self.assertIn("shared/codex/auth.json", names)

            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["current_ide"], "vscode")
            files_dict = {f["label"]: f for f in manifest["files"]}
            self.assertEqual(
                files_dict["VSCode state.vscdb"]["archive_path"],
                "ides/vscode/state.vscdb",
            )
            self.assertTrue(files_dict["VSCode state.vscdb"]["exists"])
            self.assertTrue(files_dict["VSCode state.vscdb"]["required"])
            self.assertEqual(
                files_dict["VSCode Local State"]["archive_path"],
                "ides/vscode/Local State",
            )
            self.assertTrue(files_dict["VSCode Local State"]["exists"])
            self.assertTrue(files_dict["VSCode Local State"]["required"])
            self.assertEqual(
                files_dict["Kilo New auth.json"]["archive_path"],
                "shared/kilo/auth.json",
            )
            self.assertTrue(files_dict["Kilo New auth.json"]["exists"])
            self.assertFalse(files_dict["Kilo New auth.json"]["required"])

    def test_backup_handles_missing_optional_files_gracefully(self):
        vscode_db = self.root / "vscode" / "state.vscdb"
        vscode_local_state = self.root / "vscode" / "Local State"
        kilo_auth = self.root / "shared" / "kilo" / "auth.json"
        codex_auth = self.root / "shared" / "codex" / "auth.json"

        vscode_db.parent.mkdir(parents=True, exist_ok=True)
        vscode_local_state.parent.mkdir(parents=True, exist_ok=True)

        vscode_db.write_bytes(b"vscode-db")
        vscode_local_state.write_bytes(b"vscode-local")

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
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth))

        backups_dir = self.root / "backups"

        message = db.backup()
        self.assertIn("Full backup saved (2/4 files). Skipped 2 optional missing file(s).", message)

        zip_files = list(backups_dir.glob("full_*.zip"))
        with zipfile.ZipFile(zip_files[0]) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            files_dict = {f["label"]: f for f in manifest["files"]}
            self.assertEqual(
                files_dict["Kilo New auth.json"]["archive_path"],
                "shared/kilo/auth.json",
            )
            self.assertFalse(files_dict["Kilo New auth.json"]["exists"])
            self.assertFalse(files_dict["Kilo New auth.json"]["required"])

    def test_backup_raises_runtime_error_when_required_file_is_missing(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(self.root / "missing" / "state.vscdb"),
                    "local_state": str(self.root / "missing" / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(self.root / "missing-kilo.json"))
        self.patch_db("CODEX_AUTH_PATH", str(self.root / "missing-codex.json"))

        with self.assertRaisesRegex(RuntimeError, "Backup failed: none of the target files exist"):
            db.backup()

    def test_create_prewrite_backup_respects_request_flags_and_paths(self):
        vscode_db = self.root / "vscode" / "state.vscdb"
        vscode_local_state = self.root / "vscode" / "Local State"
        kilo_auth = self.root / "shared" / "kilo" / "auth.json"
        codex_auth = self.root / "shared" / "codex" / "auth.json"

        vscode_db.parent.mkdir(parents=True, exist_ok=True)
        vscode_local_state.parent.mkdir(parents=True, exist_ok=True)
        kilo_auth.parent.mkdir(parents=True, exist_ok=True)
        codex_auth.parent.mkdir(parents=True, exist_ok=True)

        vscode_db.write_bytes(b"db")
        vscode_local_state.write_bytes(b"local")
        kilo_auth.write_bytes(b"kilo")
        codex_auth.write_bytes(b"codex")

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
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth))

        backups_dir = self.root / "backups"

        backup_res = db.create_prewrite_backup(include_db=False, include_kilo=True, include_codex=False, note="test note")
        self.assertIsNotNone(backup_res)
        assert backup_res is not None
        zf_path = Path(backup_res["path"])
        self.assertTrue(zf_path.exists())

        with zipfile.ZipFile(zf_path) as zf:
            names = zf.namelist()
            self.assertIn("prewrite/shared/kilo/auth.json", names)
            self.assertNotIn("prewrite/vscode/state.vscdb", names)

            manifest = json.loads(zf.read("manifest.json").decode("ascii"))
            self.assertEqual(manifest["note"], "test note")

    def test_create_prewrite_backup_returns_none_when_all_targets_are_missing(self):
        self.patch_db("PROJECT_ROOT", str(self.root))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": str(self.root / "missing" / "state.vscdb"),
                    "local_state": str(self.root / "missing" / "Local State"),
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("KILO_AUTH_PATH", str(self.root / "missing" / "kilo.json"))
        self.patch_db("CODEX_AUTH_PATH", str(self.root / "missing" / "codex.json"))

        self.assertIsNone(db.create_prewrite_backup(include_kilo=True))

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

    def test_use_ide_account_explicit_kilo_new_remapping(self):
        db_path = self.root / "use_ide_account_explicit_kilo.vscdb"
        create_state_db(db_path, [])

        accounts_dir = self.root / "accounts"
        kilo_auth = self.root / "missing" / "kilo" / "auth.json"
        account_data = {
            "kind": "ide",
            "ext": "kilo-new",
            "entries": [
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "access_token": "access-explicit",
                        "refresh_token": "refresh-explicit",
                        "accountId": "acct-kilo-only",
                    },
                }
            ],
        }
        self.write_json(accounts_dir / "kilo.json", account_data)

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("DB_PATH", str(db_path))
        self.patch_db("CURRENT_IDE", "vscode")
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("guard_vscode_closed", lambda: None)

        output = self.capture_output(db.use_ide_account, "kilo", ext=["kilo-new"])

        self.assertIn("[kilo-new] Written to", output)
        self.assertIn("accountId: acct-kilo-only", output)
        self.assertTrue(kilo_auth.parent.exists())

        kilo_data = json.loads(kilo_auth.read_text(encoding="utf-8"))
        self.assertEqual(
            kilo_data["openai"],
            {
                "type": "oauth",
                "accountId": "acct-kilo-only",
                "access": "access-explicit",
                "refresh": "refresh-explicit",
                "expires": 0,
            },
        )
        self.assertEqual(read_state_rows(db_path), {})

    def test_use_ide_account_remaps_kilo_new_payload_to_real_db_secret_row_for_clines(self):
        db_path = self.root / "use_ide_account_remap.vscdb"
        create_state_db(db_path, [])

        accounts_dir = self.root / "accounts"
        kilo_auth = self.root / "missing" / "kilo" / "auth.json"
        account_data = {
            "kind": "ide",
            "ext": "kilo-new",
            "entries": [
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "access_token": "access-remap",
                        "refresh_token": "refresh-remap",
                        "accountId": "acct-kilo-new",
                    },
                }
            ],
        }
        self.write_json(accounts_dir / "kilo_remap.json", account_data)

        encrypted_calls: list[tuple[str, bytes]] = []

        def fake_encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
            encrypted_calls.append((plaintext, aes_key))
            return b"ENC"

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
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
        self.write_file(self.root / "Local State")
        self.patch_db("KILO_AUTH_PATH", str(kilo_auth))
        self.patch_db("get_aes_key", lambda local_state_path=None: b"aes-key")
        self.patch_db("encrypt_value", fake_encrypt_value)
        self.patch_db("is_ide_running", lambda ide=None: False)
        self.patch_db("guard_vscode_closed", lambda: None)

        output = self.capture_output(db.use_ide_account, "kilo_remap", ext=["kilocode"])

        self.assertIn("[cross-ext] No 'kilocode.kilo-code' key", output)

        self.assertEqual(
            encrypted_calls,
            [
                (
                    json.dumps({
                        "access_token": "access-remap",
                        "refresh_token": "refresh-remap",
                        "accountId": "acct-kilo-new",
                    }),
                    b"aes-key",
                )
            ],
        )

        rows = read_state_rows(db_path)
        self.assertEqual(
            json.loads(rows[oauth_key("kilocode.kilo-code")]),
            {"type": "Buffer", "data": [69, 78, 67]},
        )
        self.assertFalse(kilo_auth.exists())

    def test_use_codex_account_writes_auth_json_preserving_expires_key_types(self):
        accounts_dir = self.root / "accounts"
        codex_auth = self.root / "missing" / "codex" / "auth.json"
        account_data = {
            "kind": "codex",
            "ext": "codex",
            "entries": [
                {
                    "key": db.CODEX_KEY,
                    "value": {
                        "access_token": "access-codex",
                        "refresh_token": "refresh-codex",
                        "accountId": "acct-codex-use",
                        "id_token": "id-codex-use",
                        "expires": 123456000000,
                    },
                }
            ],
        }
        self.write_json(accounts_dir / "codex.json", account_data)

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))
        self.patch_db("CODEX_AUTH_PATH", str(codex_auth))
        self.write_json(codex_auth, {"expires": 123456000000})

        output = self.capture_output(db.use_codex_account, "codex")

        self.assertIn("[codex] Written to", output)
        self.assertIn("accountId: acct-codex-use", output)

        codex_data = json.loads(codex_auth.read_text(encoding="utf-8"))
        self.assertEqual(codex_data["auth_mode"], "chatgpt")
        self.assertIsNone(codex_data["OPENAI_API_KEY"])
        self.assertEqual(
            codex_data["tokens"],
            {
                "access_token": "access-codex",
                "refresh_token": "refresh-codex",
                "account_id": "acct-codex-use",
                "id_token": "id-codex-use",
            },
        )
        self.assertEqual(codex_data["expires"], 123456000000)

    def test_import_codex_account_imports_file_and_saves_valid_parameters(self):
        auth_file = self.root / "codex_source" / "auth.json"
        self.write_json(
            auth_file,
            {
                "tokens": {
                    "access_token": "access-import",
                    "refresh_token": "refresh-import",
                    "account_id": "acct-import",
                    "id_token": "id-import",
                },
                "expires": 1720000000000,
            },
        )

        accounts_dir = self.root / "accounts"
        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        output = self.capture_output(db.import_codex_account, str(auth_file), "imported")

        self.assertIn("Imported 'imported' [codex]", output)
        self.assertIn("accountId: acct-import", output)

        saved_path = accounts_dir / "imported.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        entry = saved_data["entries"][0]
        self.assertEqual(saved_data["kind"], "codex")
        self.assertEqual(saved_data["ext"], "codex")
        self.assertEqual(entry["key"], db.CODEX_KEY)
        self.assertEqual(entry["value"]["access_token"], "access-import")
        self.assertEqual(entry["value"]["refresh_token"], "refresh-import")
        self.assertEqual(entry["value"]["accountId"], "acct-import")
        self.assertEqual(entry["value"]["id_token"], "id-import")
        self.assertEqual(entry["value"]["expires"], 1720000000000)

    def test_import_ide_account_from_json_string_imports_selected_extensions(self):
        accounts_dir = self.root / "accounts"
        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        output = self.capture_output(
            db.import_ide_account_from_json_string,
            json.dumps(
                [
                    {
                        "access_token": "access-import",
                        "refresh_token": "refresh-import",
                        "account_id": "acct-import-ide",
                        "id_token": "id-import-ide",
                        "expires": 1720000000000,
                    }
                ]
            ),
            "imported_ide",
            ["kilocode", "kilo-new"],
        )

        self.assertIn("Imported IDE account 'imported_ide' [kilocode+kilo-new]", output)
        self.assertIn("expires:", output)

        saved_path = accounts_dir / "imported_ide.json"
        saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["kind"], "ide")
        self.assertEqual(saved_data["ext"], "kilocode+kilo-new")
        self.assertEqual(
            saved_data["entries"],
            [
                {
                    "key": oauth_key("kilocode.kilo-code"),
                    "value": {
                        "type": "openai-codex",
                        "access_token": "access-import",
                        "refresh_token": "refresh-import",
                        "expires": 1720000000000,
                        "accountId": "acct-import-ide",
                        "id_token": "id-import-ide",
                    },
                },
                {
                    "key": db.KILO_NEW_KEY,
                    "value": {
                        "type": "openai-codex",
                        "access_token": "access-import",
                        "refresh_token": "refresh-import",
                        "expires": 1720000000000,
                        "accountId": "acct-import-ide",
                        "id_token": "id-import-ide",
                    },
                },
            ],
        )

    def test_refresh_saved_account_performs_oauth_refresher_flow_and_saves_batch(self):
        accounts_dir = self.root / "accounts"
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
        self.write_json(accounts_dir / "alice.json", account_data)

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

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        with patch.object(
            db.oauth_refresh,
            "refresh_saved_entries",
            return_value=db.oauth_refresh.RefreshEntriesResult(
                entries=refreshed_entries,
                refreshed_entries=1,
                refreshed_groups=1,
                refreshed_at="2026-05-15T12:34:56Z",
            ),
        ) as mock_refresh:
            msg = db.refresh_saved_account("alice")

        mock_refresh.assert_called_once_with(account_data["entries"])
        self.assertIn("Renewed tokens for 'alice'", msg)

        saved_data = json.loads((accounts_dir / "alice.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_data["entries"], refreshed_entries)
        self.assertEqual(saved_data["refresh_status"], db.oauth_refresh.REFRESH_STATUS_OK)
        self.assertEqual(saved_data["last_refreshed_at"], "2026-05-15T12:34:56Z")
        self.assertNotIn("refresh_error", saved_data)

    def test_refresh_saved_account_persists_fatal_errors_into_account_file(self):
        accounts_dir = self.root / "accounts"
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
        self.write_json(accounts_dir / "alice.json", account_data)

        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        with patch.object(
            db.oauth_refresh,
            "refresh_saved_entries",
            side_effect=db.oauth_refresh.TokenExchangeError("Invalid Grant/Token Refused", terminal=True),
        ) as mock_refresh:
            with self.assertRaisesRegex(db.SavedAccountRefreshError, "Invalid Grant/Token Refused") as exc_info:
                db.refresh_saved_account("alice")

        mock_refresh.assert_called_once_with(account_data["entries"])

        saved_data = json.loads((accounts_dir / "alice.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_data["entries"], account_data["entries"])  # untouched entries
        self.assertEqual(saved_data["refresh_status"], db.oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR)
        self.assertEqual(saved_data["refresh_error"], "Invalid Grant/Token Refused")
        self.assertIsNotNone(saved_data.get("refresh_error_at"))

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
        self.assertIn("Renewed tokens for saved account 'alice', but failed to persist the new credentials locally: disk full.", message)
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

    def test_backup_message_warning_branch(self):
        with patch.object(db, "_create_backup_archive", return_value={"included": 1, "total": 2, "required_missing": ["missing"], "optional_missing": []}), patch.object(
            db, "_full_backup_targets", return_value=[]
        ):
            message = db.backup()
        self.assertIn("Warning: 1 required file(s) were missing.", message)


if __name__ == "__main__":
    unittest.main()

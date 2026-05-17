from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import ide_context
from vscode_inject import account_services
from vscode_inject import parse_vscdb as db
from vscode_inject import state_db


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


class RefactorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def patch_db(self, name: str, value):
        patcher = patch.object(db, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_explicit_ide_context_matches_facade_selection_contract(self):
        self.patch_db("CURRENT_IDE", db.CURRENT_IDE)
        self.patch_db("DB_PATH", db.DB_PATH)
        self.patch_db("LOCAL_STATE_PATH", db.LOCAL_STATE_PATH)
        custom_paths = {
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
        }
        self.patch_db("IDE_PATHS", custom_paths)

        explicit = ide_context.resolve_context("antigravity", custom_paths)
        db.set_ide("antigravity")
        selected = db._ide_context_for()

        self.assertEqual(db.CURRENT_IDE, explicit.name)
        self.assertEqual(db.DB_PATH, explicit.db_path)
        self.assertEqual(db.LOCAL_STATE_PATH, explicit.local_state_path)
        self.assertEqual(selected.name, explicit.name)
        self.assertEqual(selected.db_path, explicit.db_path)
        self.assertEqual(selected.local_state_path, explicit.local_state_path)

    def test_windows_app_path_candidates_returns_empty_when_winreg_is_unavailable(self):
        with patch.object(ide_context, "winreg", None):
            self.assertEqual(ide_context.windows_app_path_candidates("Code.exe"), [])

    def test_state_db_write_entries_to_db_preserves_secret_and_plain_contract(self):
        db_path = self.root / "state.vscdb"
        create_state_db(db_path, [])
        encrypted_calls: list[tuple[str, bytes]] = []

        def fake_encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
            encrypted_calls.append((plaintext, aes_key))
            return b"ENC"

        restored, skipped = state_db.write_entries_to_db(
            str(db_path),
            [
                {
                    "key": 'secret://{"extensionId":"kilocode.kilo-code","key":"openai-codex-oauth-credentials"}',
                    "value": {
                        "accountId": "acct-direct-write",
                        "refresh_token": "refresh-direct-write",
                        "expires": 123,
                    },
                },
                {
                    "key": "workbench.colorTheme",
                    "value": "Solarized Dark",
                },
            ],
            b"aes-key",
            encrypt_value_fn=fake_encrypt_value,
            print_fn=None,
        )

        rows = read_state_rows(db_path)
        self.assertEqual((restored, skipped), (2, 0))
        self.assertEqual(
            encrypted_calls,
            [
                (
                    json.dumps(
                        {
                            "accountId": "acct-direct-write",
                            "refresh_token": "refresh-direct-write",
                            "expires": 123,
                        },
                        ensure_ascii=False,
                    ),
                    b"aes-key",
                )
            ],
        )
        self.assertEqual(
            json.loads(rows['secret://{"extensionId":"kilocode.kilo-code","key":"openai-codex-oauth-credentials"}']),
            {"type": "Buffer", "data": [69, 78, 67]},
        )
        self.assertEqual(rows["workbench.colorTheme"], "Solarized Dark")

    def test_account_services_entry_key_parser_tolerates_non_secret_keys(self):
        self.assertEqual(account_services._extension_id_from_entry_key("window.zoomLevel"), "")
        self.assertEqual(state_db._extension_id_from_secret_key("window.zoomLevel"), "")
        self.assertEqual(
            account_services._extension_id_from_entry_key(
                'secret://{"extensionId":"kilocode.kilo-code","key":"openai-codex-oauth-credentials"}'
            ),
            "kilocode.kilo-code",
        )

    def test_entry_key_for_ext_escapes_json_special_characters(self):
        entry_key = account_services.entry_key_for_ext('roo"\\cline', 'oauth"\\key', 'kilo-new://openai')

        self.assertTrue(entry_key.startswith("secret://"))
        self.assertEqual(account_services._extension_id_from_entry_key(entry_key), 'roo"\\cline')
        self.assertEqual(
            json.loads(entry_key[len("secret://"):]),
            {"extensionId": 'roo"\\cline', "key": 'oauth"\\key'},
        )

    def test_state_db_decode_entry_uses_late_bound_decrypt_value_by_default(self):
        encrypted_value = json.dumps({"type": "Buffer", "data": [1, 2, 3]})

        with patch.object(state_db, "decrypt_value", lambda raw, aes_key: "patched"):
            decoded = state_db.decode_entry(encrypted_value, b"aes-key")

        self.assertEqual(decoded, "patched")

    def test_state_db_read_entries_for_extension_ids_reads_exact_target_keys(self):
        db_path = self.root / "selection.vscdb"
        oauth_key = "openai-codex-oauth-credentials"
        target_key = state_db._secret_storage_key("kilocode.kilo-code", oauth_key)
        other_ext_key = state_db._secret_storage_key("kilocode.kilo-code-extra", oauth_key)
        other_oauth_key = state_db._secret_storage_key("kilocode.kilo-code", "different-oauth-key")
        create_state_db(
            db_path,
            [
                (target_key, json.dumps({"accountId": "acct-target"})),
                (other_ext_key, json.dumps({"accountId": "acct-other-ext"})),
                (other_oauth_key, json.dumps({"accountId": "acct-other-oauth"})),
            ],
        )

        entries = state_db.read_entries_for_extension_ids(
            str(db_path),
            str(self.root / "Local State"),
            oauth_key,
            ["kilocode.kilo-code"],
            get_aes_key_fn=lambda _path: b"aes-key",
            decode_entry_fn=lambda value, _aes_key: value,
        )

        self.assertEqual(entries, [{"key": target_key, "value": {"accountId": "acct-target"}}])

    def test_state_db_read_current_accounts_skips_corrupt_and_non_mapping_payloads(self):
        db_path = self.root / "skip_invalid_accounts.vscdb"
        oauth_key = "openai-codex-oauth-credentials"
        valid_key = state_db._secret_storage_key("kilocode.kilo-code", oauth_key)
        invalid_json_key = state_db._secret_storage_key("broken-json", oauth_key)
        list_payload_key = state_db._secret_storage_key("list-payload", oauth_key)
        string_payload_key = state_db._secret_storage_key("string-payload", oauth_key)
        valid_payload = {
            "accountId": "acct-valid",
            "refresh_token": "refresh-valid",
            "expires": 123,
        }
        create_state_db(
            db_path,
            [
                (valid_key, json.dumps(valid_payload)),
                (invalid_json_key, "{broken-json"),
                (list_payload_key, json.dumps(["unexpected"])),
                (string_payload_key, json.dumps("unexpected")),
            ],
        )

        fingerprint_calls: list[dict] = []

        def fake_account_fingerprint(value: dict) -> str:
            fingerprint_calls.append(value)
            return f"fp:{value['accountId']}"

        accounts = state_db.read_current_accounts(
            str(db_path),
            str(self.root / "Local State"),
            oauth_key,
            get_aes_key_fn=lambda _path: b"aes-key",
            decode_entry_fn=lambda value, _aes_key: value,
            account_fingerprint=fake_account_fingerprint,
        )

        self.assertEqual(
            accounts,
            {
                "kilocode.kilo-code": {
                    "accountId": "acct-valid",
                    "fingerprint": "fp:acct-valid",
                    "expires": 123,
                }
            },
        )
        self.assertEqual(fingerprint_calls, [valid_payload])

    def test_state_db_read_current_accounts_escapes_like_wildcards_in_oauth_key(self):
        db_path = self.root / "wildcard_oauth_key.vscdb"
        oauth_key = "openai_codex%oauth"
        exact_key = state_db._secret_storage_key("kilocode.kilo-code", oauth_key)
        wildcard_match_key = state_db._secret_storage_key("roo-cline", "openaiXcodexSURPRISEoauth")
        create_state_db(
            db_path,
            [
                (exact_key, json.dumps({"accountId": "acct-exact", "refresh_token": "refresh-exact"})),
                (wildcard_match_key, json.dumps({"accountId": "acct-wild", "refresh_token": "refresh-wild"})),
            ],
        )

        accounts = state_db.read_current_accounts(
            str(db_path),
            str(self.root / "Local State"),
            oauth_key,
            get_aes_key_fn=lambda _path: b"aes-key",
            decode_entry_fn=lambda value, _aes_key: value,
            account_fingerprint=lambda value: value.get("refresh_token"),
        )

        self.assertEqual(
            accounts,
            {
                "kilocode.kilo-code": {
                    "accountId": "acct-exact",
                    "fingerprint": "refresh-exact",
                    "expires": None,
                }
            },
        )

    def test_state_db_read_entries_for_extension_ids_preserves_malformed_and_non_mapping_values(self):
        db_path = self.root / "selection_invalid_values.vscdb"
        oauth_key = "openai-codex-oauth-credentials"
        invalid_json_key = state_db._secret_storage_key("broken-json", oauth_key)
        list_payload_key = state_db._secret_storage_key("list-payload", oauth_key)
        string_payload_key = state_db._secret_storage_key("string-payload", oauth_key)
        create_state_db(
            db_path,
            [
                (invalid_json_key, "{broken-json"),
                (list_payload_key, json.dumps(["unexpected"])),
                (string_payload_key, json.dumps("unexpected")),
            ],
        )

        entries = state_db.read_entries_for_extension_ids(
            str(db_path),
            str(self.root / "Local State"),
            oauth_key,
            ["broken-json", "list-payload", "string-payload"],
            get_aes_key_fn=lambda _path: b"aes-key",
            decode_entry_fn=lambda value, _aes_key: value,
        )

        self.assertEqual(len(entries), 3)
        self.assertEqual(
            {entry["key"]: entry["value"] for entry in entries},
            {
                invalid_json_key: "{broken-json",
                list_payload_key: ["unexpected"],
                string_payload_key: "unexpected",
            },
        )


if __name__ == "__main__":
    unittest.main()

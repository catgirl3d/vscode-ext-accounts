from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import base64
import ctypes
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class StateDbModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_get_aes_key_success_and_failure_paths(self):
        local_state_path = self.root / "Local State"
        local_state_path.parent.mkdir(parents=True, exist_ok=True)
        local_state_path.write_text(json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPIencrypted").decode("ascii")}}), encoding="utf-8")
        
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

        fail_messages: list[str] = []
        failing_windll = SimpleNamespace(
            crypt32=SimpleNamespace(CryptUnprotectData=lambda *_args: 0),
            kernel32=SimpleNamespace(LocalFree=lambda _ptr: None),
        )
        with patch.object(state_db.ctypes, "windll", failing_windll):
            self.assertIsNone(state_db.get_aes_key(str(local_state_path), print_fn=fail_messages.append))
        self.assertIn("CryptUnprotectData failed", fail_messages[0])

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
        decrypted_val = state_db.decrypt_value(encrypted[:-1] + b"x", aes_key)
        self.assertTrue(
            decrypted_val.startswith("<decrypt failed:"),
            msg=f"Expected startswith '<decrypt failed:', got {repr(decrypted_val)}"
        )

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

    def test_state_db_write_entries_to_db_preserves_secret_and_plain_contract(self):
        db_path = self.root / "state2.vscdb"
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

from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vscode_inject import kilo_key_accounts


def create_kilo_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE credential (
                id text PRIMARY KEY,
                integration_id text,
                label text NOT NULL,
                value text NOT NULL,
                connector_id text,
                method_id text,
                active integer,
                time_created integer NOT NULL,
                time_updated integer NOT NULL
            );
            """
        )
        con.commit()
    finally:
        con.close()


def insert_credential(
    path: Path,
    *,
    credential_id: str,
    provider: str,
    label: str = "default",
    value: dict,
    active: int | None = None,
    time_created: int = 1,
    time_updated: int = 1,
) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO credential(id, integration_id, label, value, active, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                credential_id,
                provider,
                label,
                json.dumps(value, separators=(",", ":"), ensure_ascii=False),
                active,
                time_created,
                time_updated,
            ),
        )
        con.commit()
    finally:
        con.close()


def read_credentials(path: Path) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return list(
            con.execute(
                "SELECT id, integration_id, label, value, active, time_created, time_updated FROM credential ORDER BY id ASC"
            )
        )
    finally:
        con.close()


class KiloKeyAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.kilo_dir = self.root / "kilo"
        self.db_path = self.kilo_dir / "kilo.db"
        self.auth_path = self.kilo_dir / "auth.json"
        create_kilo_db(self.db_path)

    def write_auth(self, data: dict) -> None:
        self.auth_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_read_current_key_prefers_active_opencode_go_credential(self):
        insert_credential(
            self.db_path,
            credential_id="cred-old",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "old-key"},
            time_created=2,
        )
        insert_credential(
            self.db_path,
            credential_id="cred-active",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "active-key"},
            active=1,
            time_created=1,
        )
        insert_credential(
            self.db_path,
            credential_id="cred-other",
            provider="groq",
            value={"type": "key", "key": "other-key"},
            active=1,
            time_created=3,
        )

        self.assertEqual(kilo_key_accounts.read_current_key(str(self.db_path)), "active-key")

    def test_read_current_key_returns_none_for_missing_db_or_provider(self):
        missing_db = self.root / "missing" / "kilo.db"
        self.assertIsNone(kilo_key_accounts.read_current_key(str(missing_db)))

        insert_credential(
            self.db_path,
            credential_id="cred-other",
            provider="groq",
            value={"type": "key", "key": "other-key"},
        )
        self.assertIsNone(kilo_key_accounts.read_current_key(str(self.db_path)))

    def test_read_current_entry_uses_saved_account_entry_shape(self):
        insert_credential(
            self.db_path,
            credential_id="cred-1",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "oc_sk_current"},
        )

        self.assertEqual(
            kilo_key_accounts.read_current_entry(
                str(self.db_path),
                kilo_key_accounts.KILO_OPENCODE_GO_KEY,
            ),
            {
                "key": kilo_key_accounts.KILO_OPENCODE_GO_KEY,
                "value": {"key": "oc_sk_current"},
            },
        )

    def test_replace_key_updates_existing_credential_and_auth_mirror(self):
        insert_credential(
            self.db_path,
            credential_id="cred-opencode",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            label="Imported",
            value={"type": "key", "key": "old-key"},
            time_created=100,
            time_updated=100,
        )
        insert_credential(
            self.db_path,
            credential_id="cred-groq",
            provider="groq",
            value={"type": "key", "key": "groq-key"},
            time_created=50,
            time_updated=50,
        )
        self.write_auth(
            {
                "openai": {"type": "oauth", "access": "a", "refresh": "r", "expires": 1},
                "opencode-go": {"type": "api", "key": "old-key"},
            }
        )

        kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        rows = read_credentials(self.db_path)
        self.assertEqual(len(rows), 2)
        opencode_row = next(row for row in rows if row[1] == "opencode-go")
        self.assertEqual(opencode_row[0], "cred-opencode")
        self.assertEqual(opencode_row[2], "Imported")
        self.assertEqual(json.loads(opencode_row[3]), {"type": "key", "key": "new-key"})
        self.assertEqual(opencode_row[5], 100)
        self.assertGreater(opencode_row[6], 100)
        groq_row = next(row for row in rows if row[1] == "groq")
        self.assertEqual(json.loads(groq_row[3]), {"type": "key", "key": "groq-key"})

        auth = json.loads(self.auth_path.read_text(encoding="utf-8"))
        self.assertEqual(auth["opencode-go"], {"type": "api", "key": "new-key"})
        self.assertEqual(auth["openai"], {"type": "oauth", "access": "a", "refresh": "r", "expires": 1})

    def test_replace_key_inserts_credential_when_provider_row_is_missing(self):
        self.write_auth({"groq": {"type": "api", "key": "groq-key"}})

        kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        rows = read_credentials(self.db_path)
        self.assertEqual(len(rows), 1)
        credential_id, provider, label, value, _active, time_created, time_updated = rows[0]
        self.assertTrue(credential_id.startswith("cred_"))
        self.assertEqual(provider, kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER)
        self.assertEqual(label, "Imported")
        self.assertEqual(json.loads(value), {"type": "key", "key": "new-key"})
        self.assertEqual(time_created, time_updated)
        self.assertGreater(time_created, 0)
        self.assertEqual(json.loads(self.auth_path.read_text(encoding="utf-8"))["opencode-go"], {"type": "api", "key": "new-key"})

    def test_replace_key_restores_auth_mirror_when_db_write_fails(self):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TRIGGER reject_update BEFORE UPDATE ON credential BEGIN SELECT RAISE(ABORT, 'write blocked'); END"
            )
            con.commit()
        finally:
            con.close()
        insert_credential(
            self.db_path,
            credential_id="cred-opencode",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "old-key"},
        )
        self.write_auth({"opencode-go": {"type": "api", "key": "old-key"}})
        original_auth = self.auth_path.read_bytes()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "write blocked"):
            kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        self.assertEqual(self.auth_path.read_bytes(), original_auth)
        rows = read_credentials(self.db_path)
        self.assertEqual(json.loads(rows[0][3]), {"type": "key", "key": "old-key"})

    def test_replace_key_removes_new_auth_file_when_db_write_fails(self):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TRIGGER reject_update BEFORE UPDATE ON credential BEGIN SELECT RAISE(ABORT, 'write blocked'); END"
            )
            con.commit()
        finally:
            con.close()
        insert_credential(
            self.db_path,
            credential_id="cred-opencode",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "old-key"},
        )
        self.assertFalse(self.auth_path.exists())

        with self.assertRaisesRegex(sqlite3.IntegrityError, "write blocked"):
            kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        self.assertFalse(self.auth_path.exists())
        rows = read_credentials(self.db_path)
        self.assertEqual(json.loads(rows[0][3]), {"type": "key", "key": "old-key"})

    def test_replace_key_keeps_auth_and_db_unchanged_when_auth_write_fails(self):
        insert_credential(
            self.db_path,
            credential_id="cred-opencode",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "old-key"},
        )
        self.write_auth({"openai": {"type": "oauth", "access": "a"}, "opencode-go": {"type": "api", "key": "old-key"}})
        original_auth = self.auth_path.read_bytes()

        with patch.object(
            kilo_key_accounts.os,
            "replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        self.assertEqual(self.auth_path.read_bytes(), original_auth)
        rows = read_credentials(self.db_path)
        self.assertEqual(json.loads(rows[0][3]), {"type": "key", "key": "old-key"})

    def test_replace_key_restores_auth_when_update_affects_no_rows(self):
        insert_credential(
            self.db_path,
            credential_id="cred-opencode",
            provider=kilo_key_accounts.KILO_OPENCODE_GO_PROVIDER,
            value={"type": "key", "key": "old-key"},
        )
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TRIGGER skip_update BEFORE UPDATE ON credential BEGIN SELECT RAISE(IGNORE); END"
            )
            con.commit()
        finally:
            con.close()
        self.write_auth({"opencode-go": {"type": "api", "key": "old-key"}})
        original_auth = self.auth_path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "credential row"):
            kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "new-key")

        self.assertEqual(self.auth_path.read_bytes(), original_auth)
        rows = read_credentials(self.db_path)
        self.assertEqual(json.loads(rows[0][3]), {"type": "key", "key": "old-key"})

    def test_replace_key_rejects_empty_key_without_touching_auth(self):
        self.write_auth({"opencode-go": {"type": "api", "key": "old-key"}})
        original_auth = self.auth_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "key"):
            kilo_key_accounts.replace_key(str(self.db_path), str(self.auth_path), "   ")

        self.assertEqual(self.auth_path.read_bytes(), original_auth)

    def test_from_import_format_accepts_key_payloads(self):
        self.assertEqual(kilo_key_accounts.from_import_format({"key": "oc_sk_1"}), {"key": "oc_sk_1"})
        self.assertEqual(kilo_key_accounts.from_import_format({"type": "key", "key": "oc_sk_2"}), {"key": "oc_sk_2"})
        self.assertEqual(kilo_key_accounts.from_import_format({"type": "api", "key": "oc_sk_3"}), {"key": "oc_sk_3"})
        self.assertEqual(kilo_key_accounts.from_import_format("oc_sk_4"), {"key": "oc_sk_4"})

        with self.assertRaisesRegex(ValueError, "key"):
            kilo_key_accounts.from_import_format({"type": "api"})
        with self.assertRaisesRegex(ValueError, "key"):
            kilo_key_accounts.from_import_format("")


if __name__ == "__main__":
    unittest.main()

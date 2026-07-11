from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import json
import sqlite3
import tempfile
import unittest
import base64
from pathlib import Path

from vscode_inject import omp_openai_accounts
from vscode_inject import openai_identity


def create_agent_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE auth_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                credential_type TEXT NOT NULL,
                data TEXT NOT NULL,
                disabled_cause TEXT DEFAULT NULL,
                identity_key TEXT DEFAULT NULL,
                created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            """
        )
        con.commit()
    finally:
        con.close()


def read_rows(path: Path) -> list[tuple[int, str, str, str | None, str | None, str]]:
    con = sqlite3.connect(path)
    try:
        return list(
            con.execute(
                "SELECT id, provider, credential_type, disabled_cause, identity_key, data FROM auth_credentials ORDER BY id ASC"
            )
        )
    finally:
        con.close()


def jwt_with_exp(exp_seconds: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_seconds}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{payload}.signature"


class OmpOpenAIAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "omp" / "agent.db"
        create_agent_db(self.db_path)

    def write_row(
        self,
        *,
        provider: str,
        credential_type: str,
        data: dict,
        disabled_cause: str | None = None,
        identity_key: str | None = None,
    ) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO auth_credentials(provider, credential_type, data, disabled_cause, identity_key) VALUES (?, ?, ?, ?, ?)",
                (provider, credential_type, json.dumps(data, separators=(",", ":"), ensure_ascii=False), disabled_cause, identity_key),
            )
            con.commit()
        finally:
            con.close()

    def test_read_current_entries_and_accounts_normalize_omp_rows(self):
        self.write_row(
            provider=omp_openai_accounts.OPENAI_CODEX_PROVIDER,
            credential_type=omp_openai_accounts.OAUTH_CREDENTIAL_TYPE,
            data={
                "access": "access-1",
                "refresh": "refresh-1",
                "expires": 123,
                "accountId": "acct-1",
                "email": "User@Example.com",
            },
            identity_key="email:user@example.com",
        )
        self.write_row(
            provider=omp_openai_accounts.OPENAI_CODEX_PROVIDER,
            credential_type=omp_openai_accounts.OAUTH_CREDENTIAL_TYPE,
            data={
                "access": "access-disabled",
                "refresh": "refresh-disabled",
                "expires": 456,
                "accountId": "acct-disabled",
            },
            disabled_cause="disabled",
        )
        self.write_row(
            provider="other-provider",
            credential_type=omp_openai_accounts.OAUTH_CREDENTIAL_TYPE,
            data={"access": "other", "refresh": "other", "expires": 789, "accountId": "acct-other"},
        )

        entries = omp_openai_accounts.read_current_openai_entries(str(self.db_path), "omp://openai")
        self.assertEqual(
            entries,
            [
                {
                    "key": "omp://openai",
                    "identity_key": "email:user@example.com",
                    "value": {
                        "type": "openai-codex",
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires": 123,
                        "accountId": "acct-1",
                        "email": "user@example.com",
                        "identity_key": "email:user@example.com",
                    },
                }
            ],
        )

        current_accounts = omp_openai_accounts.read_current_openai_accounts(
            str(self.db_path),
            "omp://openai",
            lambda value: f"fp:{value['refresh_token']}",
        )
        self.assertEqual(
            current_accounts,
            [
                {
                    "key": "omp://openai",
                    "accountId": "acct-1",
                    "fingerprint": "fp:refresh-1",
                    "expires": 123,
                    "email": "user@example.com",
                }
            ],
        )

    def test_replace_openai_credentials_soft_disables_previous_provider_rows(self):
        self.write_row(
            provider=omp_openai_accounts.OPENAI_CODEX_PROVIDER,
            credential_type=omp_openai_accounts.OAUTH_CREDENTIAL_TYPE,
            data={"access": "old-access", "refresh": "old-refresh", "expires": 1, "accountId": "acct-old"},
            identity_key="account:acct-old",
        )
        self.write_row(
            provider=omp_openai_accounts.OPENAI_CODEX_PROVIDER,
            credential_type="api_key",
            data={"key": "sk-old"},
        )

        omp_openai_accounts.replace_openai_credentials(
            str(self.db_path),
            [
                {
                    "key": "omp://openai",
                    "value": {
                        "type": "openai-codex",
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires": 999,
                        "accountId": "acct-new",
                        "email": "New@Example.com",
                    },
                }
            ],
        )

        rows = read_rows(self.db_path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][3], omp_openai_accounts.REPLACED_DISABLED_CAUSE)
        self.assertEqual(rows[1][3], omp_openai_accounts.REPLACED_DISABLED_CAUSE)
        self.assertIsNone(rows[2][3])
        self.assertEqual(rows[2][1], omp_openai_accounts.OPENAI_CODEX_PROVIDER)
        self.assertEqual(rows[2][2], omp_openai_accounts.OAUTH_CREDENTIAL_TYPE)
        self.assertEqual(rows[2][4], "account:acct-new")
        self.assertEqual(
            json.loads(rows[2][5]),
            {
                "access": "new-access",
                "refresh": "new-refresh",
                "expires": 999,
                "accountId": "acct-new",
                "email": "new@example.com",
            },
        )

    def test_replace_openai_credentials_validates_missing_tokens(self):
        with self.assertRaisesRegex(ValueError, "missing access_token or refresh_token"):
            omp_openai_accounts.replace_openai_credentials(
                str(self.db_path),
                [{"key": "omp://openai", "value": {"access_token": "access-only"}}],
            )

    def test_from_omp_import_format_normalizes_payload_and_derives_identity_and_expiry(self):
        token = jwt_with_exp(1_767_225_600)
        normalized = omp_openai_accounts.from_omp_import_format(
            {
                "access_token": token,
                "refresh_token": "refresh-1",
                "account_id": "acct-1",
                "email": "User@Example.com",
            }
        )

        self.assertEqual(
            normalized,
            {
                "type": omp_openai_accounts.OPENAI_CODEX_PROVIDER,
                "access_token": token,
                "refresh_token": "refresh-1",
                "expires": 1_767_225_600_000,
                "accountId": "acct-1",
                "email": "user@example.com",
                "identity_key": "account:acct-1",
            },
        )

        explicit = omp_openai_accounts.from_omp_import_format(
            {
                "access": "access-2",
                "refresh": "refresh-2",
                "accountId": "acct-2",
                "expires": 123,
                "identity_key": "custom:acct-2",
                "id_token": "id-2",
            }
        )
        self.assertEqual(explicit["expires"], 123)
        self.assertEqual(explicit["identity_key"], "custom:acct-2")
        self.assertEqual(explicit["id_token"], "id-2")
        self.assertEqual(openai_identity.identity_key_for_value({"email": " User@Example.com "}), "email:user@example.com")
        self.assertEqual(openai_identity.identity_key_for_value({"account_id": "acct-3"}), "account:acct-3")
        self.assertEqual(
            openai_identity.identity_key_for_value({"email": "user@example.com", "accountId": "acct-4"}),
            "account:acct-4",
        )
        self.assertEqual(
            openai_identity.identity_key_for_entry(
                {
                    "identity_key": "custom:entry",
                    "value": {"accountId": "acct-5", "email": "user@example.com"},
                }
            ),
            "custom:entry",
        )
        self.assertEqual(
            openai_identity.identity_keys_for_entry(
                {
                    "value": {"accountId": "acct-5", "email": "user@example.com"},
                }
            ),
            ("account:acct-5", "email:user@example.com"),
        )

        from_bool_expiry = omp_openai_accounts.from_omp_import_format(
            {
                "access_token": token,
                "refresh_token": "refresh-3",
                "account_id": "acct-3",
                "expires": True,
            }
        )
        self.assertEqual(from_bool_expiry["expires"], 1_767_225_600_000)


if __name__ == "__main__":
    unittest.main()

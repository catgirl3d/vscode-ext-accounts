from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import base64
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vscode_inject import codex_accounts


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


if __name__ == "__main__":
    unittest.main()

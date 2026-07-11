from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import hashlib
import base64
import json
import tempfile
import unittest
from pathlib import Path

from vscode_inject import kilo_new_accounts


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def make_jwt(exp: int, **claims) -> str:
    payload = base64.b64encode(json.dumps({"exp": exp, **claims}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


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
                    "email": "saved@example.com",
                }
            ),
            {
                "type": "oauth",
                "access": "access-2",
                "refresh": "refresh-2",
                "expires": 456,
                "accountId": "acct-2",
                "email": "saved@example.com",
            },
        )
        self.assertEqual(
            kilo_new_accounts.from_kilo_new_format(
                {
                    "access": make_jwt(789, email="kilo@example.com"),
                    "refresh": "refresh-3",
                    "expires": 789,
                    "accountId": "acct-3",
                }
            ),
            {
                "type": "openai-codex",
                "access_token": make_jwt(789, email="kilo@example.com"),
                "refresh_token": "refresh-3",
                "expires": 789,
                "accountId": "acct-3",
                "email": "kilo@example.com",
            },
        )

    def test_kilo_new_fingerprint_and_current_account_contract(self):
        auth_path = self.root / "kilo" / "auth.json"
        auth = {
            "openai": {
                "access": "access-1",
                "refresh": "refresh-1",
                "expires": 123,
                "accountId": "acct-1",
                "email": "current@example.com",
            }
        }
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
                    "email": "current@example.com",
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


if __name__ == "__main__":
    unittest.main()

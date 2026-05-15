from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import oauth_refresh as refresh


def encode_jwt(payload: dict) -> str:
    def encode_part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")

    header = encode_part(b'{"alg":"none","typ":"JWT"}')
    body = encode_part(__import__("json").dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{header}.{body}.signature"


def secret_key(extension_id: str) -> str:
    return (
        'secret://{"extensionId":"'
        + extension_id
        + '","key":"'
        + refresh.OPENAI_CODEX_STORAGE_KEY
        + '"}'
    )


class OAuthRefreshTests(unittest.TestCase):
    maxDiff = None

    def test_refresh_saved_entries_groups_by_refresh_token_and_updates_all_supported_entries(self):
        entries = [
            {
                "key": secret_key("kilocode.kilo-code"),
                "value": {
                    "access_token": "old-access-a",
                    "refresh_token": "refresh-shared",
                    "expires": 1,
                    "accountId": "acct-shared",
                },
            },
            {
                "key": secret_key("rooveterinaryinc.roo-cline"),
                "value": {
                    "access_token": "old-access-b",
                    "refresh_token": "refresh-shared",
                    "expires": 2,
                },
            },
            {
                "key": refresh.KILO_NEW_KEY,
                "value": {
                    "access_token": "old-access-c",
                    "refresh_token": "refresh-kilo",
                    "expires": 3,
                    "accountId": "acct-kilo",
                },
            },
            {
                "key": "unrelated",
                "value": {
                    "refresh_token": "refresh-unrelated",
                    "access_token": "leave-me-alone",
                    "expires": 4,
                },
            },
        ]
        seen_bundles: list[refresh.TokenBundle] = []

        def fake_refresher(bundle: refresh.TokenBundle) -> refresh.TokenBundle:
            seen_bundles.append(bundle)
            if bundle.refresh_token == "refresh-shared":
                return refresh.TokenBundle(
                    access_token="new-access-shared",
                    refresh_token="refresh-shared-rotated",
                    expires=111,
                    account_id="acct-shared-new",
                    id_token="id-shared",
                )
            return refresh.TokenBundle(
                access_token="new-access-kilo",
                refresh_token="refresh-kilo",
                expires=222,
                account_id="acct-kilo",
                id_token=None,
            )

        result = refresh.refresh_saved_entries(
            entries,
            refreshers={refresh.OPENAI_CODEX_PROVIDER: fake_refresher},
        )

        self.assertEqual(result.refreshed_entries, 3)
        self.assertEqual(result.refreshed_groups, 2)
        self.assertEqual(
            [bundle.refresh_token for bundle in seen_bundles],
            ["refresh-shared", "refresh-kilo"],
        )
        self.assertEqual(
            result.entries,
            [
                {
                    "key": secret_key("kilocode.kilo-code"),
                    "value": {
                        "access_token": "new-access-shared",
                        "refresh_token": "refresh-shared-rotated",
                        "expires": 111,
                        "accountId": "acct-shared-new",
                        "id_token": "id-shared",
                    },
                },
                {
                    "key": secret_key("rooveterinaryinc.roo-cline"),
                    "value": {
                        "access_token": "new-access-shared",
                        "refresh_token": "refresh-shared-rotated",
                        "expires": 111,
                        "accountId": "acct-shared-new",
                        "id_token": "id-shared",
                    },
                },
                {
                    "key": refresh.KILO_NEW_KEY,
                    "value": {
                        "access_token": "new-access-kilo",
                        "refresh_token": "refresh-kilo",
                        "expires": 222,
                        "accountId": "acct-kilo",
                    },
                },
                {
                    "key": "unrelated",
                    "value": {
                        "refresh_token": "refresh-unrelated",
                        "access_token": "leave-me-alone",
                        "expires": 4,
                    },
                },
            ],
        )

    def test_refresh_saved_entries_rejects_accounts_without_supported_tokens(self):
        with self.assertRaisesRegex(refresh.UnsupportedSavedAccountError, "does not contain any supported"):
            refresh.refresh_saved_entries(
                [
                    {
                        "key": "unrelated",
                        "value": {"access_token": "x", "expires": 1},
                    }
                ]
            )

    def test_refresh_openai_codex_bundle_preserves_existing_refresh_and_id_token_when_not_returned(self):
        bundle = refresh.TokenBundle(
            access_token="old-access",
            refresh_token="keep-refresh",
            expires=1,
            account_id="acct-existing",
            id_token="existing-id-token",
        )

        def fake_post(url: str, data: dict[str, str]) -> dict:
            self.assertEqual(url, refresh.OPENAI_CODEX_TOKEN_ENDPOINT)
            self.assertEqual(
                data,
                {
                    "grant_type": "refresh_token",
                    "client_id": refresh.OPENAI_CODEX_CLIENT_ID,
                    "refresh_token": "keep-refresh",
                },
            )
            return {
                "access_token": "new-access",
                "expires_in": 120,
            }

        refreshed = refresh.refresh_openai_codex_bundle(
            bundle,
            post_form=fake_post,
            now_ms=lambda: 5_000,
        )

        self.assertEqual(
            refreshed,
            refresh.TokenBundle(
                access_token="new-access",
                refresh_token="keep-refresh",
                expires=125_000,
                account_id="acct-existing",
                id_token="existing-id-token",
            ),
        )

    def test_refresh_openai_codex_bundle_extracts_account_id_from_id_token(self):
        id_token = encode_jwt({"chatgpt_account_id": "acct-from-id-token"})
        bundle = refresh.TokenBundle(
            access_token="old-access",
            refresh_token="refresh-1",
            expires=1,
            account_id="acct-existing",
            id_token=None,
        )

        refreshed = refresh.refresh_openai_codex_bundle(
            bundle,
            post_form=lambda url, data: {
                "access_token": "new-access",
                "refresh_token": "refresh-2",
                "id_token": id_token,
                "expires_in": 60,
            },
            now_ms=lambda: 10_000,
        )

        self.assertEqual(
            refreshed,
            refresh.TokenBundle(
                access_token="new-access",
                refresh_token="refresh-2",
                expires=70_000,
                account_id="acct-from-id-token",
                id_token=id_token,
            ),
        )


if __name__ == "__main__":
    unittest.main()

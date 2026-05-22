from __future__ import annotations

import base64
import io
import json
import sys
import unittest
from pathlib import Path
from urllib import error as urlerror


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

    def test_decode_extract_and_terminal_helpers_cover_edge_cases(self):
        self.assertIsNone(refresh.decode_jwt_claims(None))
        self.assertIsNone(refresh.decode_jwt_claims("not-a-jwt"))
        self.assertEqual(
            refresh.decode_jwt_claims(encode_jwt({"chatgpt_account_id": "acct-1"})),
            {"chatgpt_account_id": "acct-1"},
        )

        self.assertEqual(refresh.extract_account_id_from_claims({"chatgpt_account_id": "acct-direct"}), "acct-direct")
        self.assertEqual(
            refresh.extract_account_id_from_claims(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-nested"}}
            ),
            "acct-nested",
        )
        self.assertEqual(
            refresh.extract_account_id_from_claims({"organizations": [{"id": "org-1"}]}),
            "org-1",
        )
        self.assertIsNone(refresh.extract_account_id_from_claims({"organizations": ["bad"]}))
        self.assertEqual(
            refresh.extract_account_id(
                encode_jwt({"organizations": [{"id": "org-from-access"}]}),
                encode_jwt({"chatgpt_account_id": "acct-from-id"}),
            ),
            "acct-from-id",
        )
        self.assertEqual(
            refresh.extract_account_id(encode_jwt({"chatgpt_account_id": "acct-from-access"})),
            "acct-from-access",
        )

        self.assertTrue(
            refresh.is_terminal_token_exchange_failure(
                status_code=400,
                error_code="invalid_grant",
                error_message="token expired",
            )
        )
        self.assertTrue(
            refresh.is_terminal_token_exchange_failure(
                status_code=401,
                error_code=None,
                error_message="Your refresh token has already been used.",
            )
        )
        self.assertFalse(
            refresh.is_terminal_token_exchange_failure(
                status_code=503,
                error_code=None,
                error_message="gateway timeout",
            )
        )

    def test_oauth_error_parsing_and_http_post_failures(self):
        self.assertEqual(refresh._oauth_error_details("not-json"), (None, None))
        self.assertEqual(
            refresh._oauth_error_details(
                json.dumps({"error": {"type": "invalid_request_error", "message": "refresh token expired"}})
            ),
            ("invalid_request_error", "refresh token expired"),
        )
        self.assertEqual(
            refresh._oauth_error_details(json.dumps({"error": "invalid_grant", "message": "sign in again"})),
            ("invalid_grant", "sign in again"),
        )

        class FakeResponse:
            def __init__(self, payload: str):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return self.payload.encode("utf-8")

        parsed = refresh.post_form_urlencoded(
            "https://example.test/token",
            {"grant_type": "refresh_token"},
            urlopen=lambda req, timeout=30.0: FakeResponse('{"access_token":"abc"}'),
        )
        self.assertEqual(parsed, {"access_token": "abc"})

        with self.assertRaisesRegex(refresh.TokenExchangeError, "invalid JSON"):
            refresh.post_form_urlencoded(
                "https://example.test/token",
                {"grant_type": "refresh_token"},
                urlopen=lambda req, timeout=30.0: FakeResponse("{"),
            )

        with self.assertRaisesRegex(refresh.TokenExchangeError, "invalid payload"):
            refresh.post_form_urlencoded(
                "https://example.test/token",
                {"grant_type": "refresh_token"},
                urlopen=lambda req, timeout=30.0: FakeResponse('["bad"]'),
            )

        def raise_http_error(req, timeout=30.0):
            raise urlerror.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"invalid_grant","error_description":"refresh token has already been used"}'),
            )

        with self.assertRaises(refresh.TokenExchangeError) as http_error:
            refresh.post_form_urlencoded(
                "https://example.test/token",
                {"grant_type": "refresh_token"},
                urlopen=raise_http_error,
            )

        self.assertEqual(http_error.exception.status_code, 400)
        self.assertEqual(http_error.exception.error_code, "invalid_grant")
        self.assertTrue(http_error.exception.terminal)
        self.assertIn("invalid_grant", str(http_error.exception))

        with self.assertRaises(refresh.TokenExchangeError) as url_error:
            refresh.post_form_urlencoded(
                "https://example.test/token",
                {"grant_type": "refresh_token"},
                urlopen=lambda req, timeout=30.0: (_ for _ in ()).throw(urlerror.URLError("offline")),
            )

        self.assertEqual(url_error.exception.error_description, "offline")

    def test_token_bundle_collection_and_group_helpers_cover_edge_cases(self):
        bundle = refresh.token_bundle_from_value(
            {
                "tokens": {
                    "access_token": 123,
                    "refresh_token": 456,
                    "account_id": 789,
                    "id_token": 999,
                },
                "expires": "bad",
            }
        )
        self.assertEqual(
            bundle,
            refresh.TokenBundle(
                access_token="123",
                refresh_token="456",
                expires=0,
                account_id="789",
                id_token=None,
            ),
        )

        self.assertTrue(refresh._entry_uses_openai_codex_key(refresh.KILO_NEW_KEY))
        self.assertTrue(refresh._entry_uses_openai_codex_key(refresh.CODEX_KEY))
        self.assertFalse(refresh._entry_uses_openai_codex_key("window.zoomLevel"))
        self.assertFalse(refresh._entry_uses_openai_codex_key("secret://{bad-json"))
        self.assertTrue(refresh._entry_uses_openai_codex_key(secret_key("kilocode.kilo-code")))

        self.assertIsNone(refresh._provider_for_entry(secret_key("kilocode.kilo-code"), {"access_token": "a"}))
        self.assertEqual(
            refresh._provider_for_entry(secret_key("kilocode.kilo-code"), {"refresh_token": "r"}),
            refresh.OPENAI_CODEX_PROVIDER,
        )
        self.assertEqual(
            refresh._provider_for_entry("custom", {"type": refresh.OPENAI_CODEX_PROVIDER, "refresh_token": "r"}),
            refresh.OPENAI_CODEX_PROVIDER,
        )

        refreshable = refresh.collect_refreshable_entries(
            [
                {"key": secret_key("kilocode.kilo-code"), "value": {"refresh_token": "refresh-1", "access_token": "access-1"}},
                {"key": "broken", "value": "bad"},
                {"key": 5, "value": {}},
            ]
        )
        self.assertEqual(len(refreshable), 1)
        self.assertEqual(refreshable[0].provider, refresh.OPENAI_CODEX_PROVIDER)
        self.assertEqual(refreshable[0].bundle.refresh_token, "refresh-1")

        records = refresh.saved_account_records(
            [
                {"name": "alice", "path": "alice.json", "data": {"entries": []}, "kind": "ide"},
                {"path": "fallback.json", "data": {"entries": []}, "kind": 123},
                {"path": "skip.json", "data": {"entries": []}, "readable": False},
                {"path": "skip2.json", "data": "bad"},
            ]
        )
        self.assertEqual(
            records,
            [
                refresh.SavedAccountRecord(name="alice", path="alice.json", data={"entries": []}, kind="ide"),
                refresh.SavedAccountRecord(name="fallback.json", path="fallback.json", data={"entries": []}, kind=None),
            ],
        )

        grouped = refresh.collect_refresh_groups(
            [
                refresh.SavedAccountRecord(
                    name="alice",
                    path="alice.json",
                    data={
                        "entries": [
                            {"key": secret_key("kilocode.kilo-code"), "value": {"refresh_token": "shared", "expires": 1200}},
                            "bad-entry",
                        ]
                    },
                ),
                refresh.SavedAccountRecord(
                    name="bob",
                    path="bob.json",
                    data={
                        "entries": [
                            {"key": refresh.KILO_NEW_KEY, "value": {"type": refresh.OPENAI_CODEX_PROVIDER, "refresh_token": "shared", "expires": 0}},
                        ]
                    },
                ),
            ]
        )

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].account_names(), ("alice", "bob"))
        self.assertEqual(grouped[0].record_paths(), ("alice.json", "bob.json"))
        self.assertEqual(grouped[0].expires, 0)
        self.assertEqual(refresh.refresh_due_at_ms(grouped[0], 600), 0)

    def test_apply_refresh_helpers_validate_record_layout_and_missing_refreshers(self):
        bundle = refresh.TokenBundle(
            access_token="new-access",
            refresh_token="new-refresh",
            expires=5_000,
            account_id="acct-1",
            id_token="id-1",
        )
        group_key = refresh.RefreshGroupKey(provider=refresh.OPENAI_CODEX_PROVIDER, refresh_token="refresh-1")
        entry = refresh.RefreshableRecordEntry(
            record_name="alice",
            record_path="alice.json",
            entry_index=0,
            group_key=group_key,
            bundle=bundle,
        )
        group = refresh.RefreshGroup(key=group_key, bundle=bundle, expires=1_000, entries=(entry,))
        record = refresh.SavedAccountRecord(
            name="alice",
            path="alice.json",
            data={
                "entries": [{"key": secret_key("kilocode.kilo-code"), "value": {"refresh_token": "old-refresh"}}],
                "refresh_error": "old error",
                "refresh_error_at": "yesterday",
            },
        )

        updated = refresh.apply_refreshed_group(
            {"alice.json": record},
            group,
            bundle,
            refreshed_at="2024-01-02T03:04:05Z",
        )
        self.assertEqual(updated["alice.json"]["refresh_status"], "ok")
        self.assertEqual(updated["alice.json"]["last_refreshed_at"], "2024-01-02T03:04:05Z")
        self.assertNotIn("refresh_error", updated["alice.json"])
        self.assertEqual(updated["alice.json"]["entries"][0]["value"]["access_token"], "new-access")

        errored = refresh.apply_refresh_error(
            {"alice.json": record},
            group,
            status="terminal_error",
            error_message="invalid_grant",
            error_at="2024-01-02T03:04:05Z",
        )
        self.assertEqual(errored["alice.json"]["refresh_status"], "terminal_error")
        self.assertEqual(errored["alice.json"]["refresh_error"], "invalid_grant")
        self.assertEqual(errored["alice.json"]["refresh_error_at"], "2024-01-02T03:04:05Z")

        with self.assertRaisesRegex(refresh.OAuthRefreshError, "Missing saved account record"):
            refresh.apply_refreshed_group({}, group, bundle)

        broken_entries_record = refresh.SavedAccountRecord(name="alice", path="alice.json", data={"entries": "bad"})
        with self.assertRaisesRegex(refresh.OAuthRefreshError, "invalid entry layout"):
            refresh.apply_refreshed_group({"alice.json": broken_entries_record}, group, bundle)

        broken_object_record = refresh.SavedAccountRecord(name="alice", path="alice.json", data={"entries": ["bad"]})
        with self.assertRaisesRegex(refresh.OAuthRefreshError, "non-object entry"):
            refresh.apply_refreshed_group({"alice.json": broken_object_record}, group, bundle)

        broken_value_record = refresh.SavedAccountRecord(
            name="alice",
            path="alice.json",
            data={"entries": [{"key": secret_key("kilocode.kilo-code"), "value": "bad"}]},
        )
        with self.assertRaisesRegex(refresh.OAuthRefreshError, "invalid OAuth value payload"):
            refresh.apply_refreshed_group({"alice.json": broken_value_record}, group, bundle)

        with self.assertRaisesRegex(refresh.OAuthRefreshError, "Missing saved account record"):
            refresh.apply_refresh_error({}, group, status="error", error_message="boom")

        with self.assertRaisesRegex(refresh.UnsupportedSavedAccountError, "No refresher is registered"):
            refresh.refresh_saved_entries(
                [{"key": secret_key("kilocode.kilo-code"), "value": {"refresh_token": "refresh-1", "access_token": "access-1"}}],
                refreshers={"other-provider": lambda bundle: bundle},
            )

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

    def test_is_terminal_refresh_error_detects_terminal_token_exchange_failures(self):
        exc = refresh.TokenExchangeError(
            "Token refresh failed: 401 invalid_request_error: Your refresh token has already been used to generate a new access token.",
            status_code=401,
            error_code="invalid_request_error",
            error_description="Your refresh token has already been used to generate a new access token.",
        )

        self.assertTrue(refresh.is_terminal_refresh_error(exc))

    def test_is_terminal_refresh_error_keeps_transient_errors_retryable(self):
        exc = refresh.TokenExchangeError(
            "Token refresh failed: 503 upstream timeout",
            status_code=503,
            error_description="upstream timeout",
        )

        self.assertFalse(refresh.is_terminal_refresh_error(exc))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import json
import unittest

from vscode_inject import usage_limits


class _FakeResponse:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class UsageLimitsTests(unittest.TestCase):
    def test_parse_openai_usage_limits_extracts_only_usable_windows(self):
        limits = usage_limits.parse_openai_usage_limits(
            {
                "rate_limits": {
                    "primary": {
                        "used_percent": 58,
                        "limit_window_seconds": 18_000,
                        "resets_at": "2026-07-12T10:30:00Z",
                    },
                    "secondary": {
                        "used_percent": 12,
                        "limit_window_seconds": 604_800,
                        "resets_at": "2026-07-12T12:45:00Z",
                    },
                },
                "rate_limit_reset_credits": {
                    "available_count": 4,
                },
                "credits": {
                    "balance": 19,
                },
            }
        )

        self.assertEqual(
            limits,
            [
                {
                    "remaining": 42,
                    "windowSeconds": 18_000,
                    "resetAt": "2026-07-12T10:30:00Z",
                    "resetAtMs": 1_783_852_200_000,
                },
                {
                    "remaining": 88,
                    "windowSeconds": 604_800,
                    "resetAt": "2026-07-12T12:45:00Z",
                    "resetAtMs": 1_783_860_300_000,
                },
            ],
        )

    def test_parse_openai_usage_limits_supports_single_monthly_window(self):
        limits = usage_limits.parse_openai_usage_limits(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": 2_592_000,
                        "reset_at": 1_786_364_155,
                    },
                    "secondary_window": {
                        "limit": 100,
                    },
                }
            }
        )

        self.assertEqual(
            limits,
            [
                {
                    "remaining": 95,
                    "windowSeconds": 2_592_000,
                    "resetAt": 1_786_364_155,
                    "resetAtMs": 1_786_364_155_000,
                }
            ],
        )

    def test_fetch_openai_usage_snapshot_returns_compact_limits_and_sends_account_id(self):
        seen_headers = []

        def fake_urlopen(req, timeout=0):
            seen_headers.append(dict(req.header_items()))
            self.assertTrue(req.full_url.endswith("/usage"))
            return _FakeResponse(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 25,
                            "limit_window_seconds": 18_000,
                            "reset_at": "2026-07-12T09:00:00Z",
                        },
                        "secondary_window": {
                            "used_percent": 40,
                            "limit_window_seconds": 604_800,
                            "reset_at": "2026-07-12T11:00:00Z",
                        },
                    }
                }
            )

        snapshot = usage_limits.fetch_openai_usage_snapshot(
            "access-token",
            account_id="acct-1",
            urlopen=fake_urlopen,
        )

        self.assertEqual(
            snapshot["limits"],
            [
                {
                    "remaining": 75,
                    "windowSeconds": 18_000,
                    "resetAt": "2026-07-12T09:00:00Z",
                    "resetAtMs": 1_783_846_800_000,
                },
                {
                    "remaining": 60,
                    "windowSeconds": 604_800,
                    "resetAt": "2026-07-12T11:00:00Z",
                    "resetAtMs": 1_783_854_000_000,
                },
            ],
        )
        self.assertTrue(
            any(dict((key.lower(), value) for key, value in headers.items()).get("chatgpt-account-id") == "acct-1" for headers in seen_headers)
        )


if __name__ == "__main__":
    unittest.main()

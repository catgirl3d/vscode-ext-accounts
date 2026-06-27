from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import oauth_refresh
from vscode_inject import parse_vscdb as db
from vscode_inject import refresh_scheduler


def secret_key(extension_id: str) -> str:
    return (
        'secret://{"extensionId":"'
        + extension_id
        + '","key":"openai-codex-oauth-credentials"}'
    )


class RefreshSchedulerTests(unittest.TestCase):
    def test_run_once_refreshes_due_group_once_across_multiple_saved_accounts(self):
        now_ms = 1_778_857_200_000
        raw_records = [
            {
                "name": "alice",
                "path": "alice.json",
                "kind": "ide",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": secret_key("kilocode.kilo-code"),
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access-a",
                                "refresh_token": "shared-refresh",
                                "expires": now_ms + 1_000,
                                "accountId": "acct-old",
                            },
                        }
                    ]
                },
            },
            {
                "name": "bob",
                "path": "bob.json",
                "kind": "codex",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.CODEX_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access-b",
                                "refresh_token": "shared-refresh",
                                "expires": now_ms + 2_000,
                                "accountId": "acct-old",
                                "id_token": "old-id",
                            },
                        }
                    ]
                },
            },
        ]
        write_calls: list[dict[str, dict]] = []

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: write_calls.append(dict(updates)),
            persist_refreshed_group=lambda updates, _group: write_calls.append(dict(updates)),
            refreshers={
                oauth_refresh.OPENAI_CODEX_PROVIDER: lambda bundle: oauth_refresh.TokenBundle(
                    access_token="new-access",
                    refresh_token="rotated-refresh",
                    expires=now_ms + 7 * 24 * 60 * 60 * 1000,
                    account_id="acct-new",
                    id_token="new-id",
                )
            },
            now_ms=lambda: now_ms,
            now_iso=lambda: "2026-05-15T15:00:00Z",
        )

        result = scheduler.run_once()

        self.assertTrue(result.ok)
        self.assertTrue(result.refresh_ui)
        self.assertEqual(result.due_groups, 1)
        self.assertEqual(result.refreshed_groups, 1)
        self.assertEqual(result.refreshed_accounts, 2)
        self.assertEqual(result.refreshed_entries, 2)
        self.assertEqual(result.failed_groups, 0)
        self.assertEqual(result.failures, ())
        self.assertEqual(result.next_delay_ms, scheduler.policy.scan_interval_ms)
        self.assertEqual(
            result.message,
            "Auto-refreshed 1 token group in 2 accounts",
        )
        self.assertEqual(len(write_calls), 1)
        self.assertEqual(
            write_calls[0],
            {
                "alice.json": {
                    "entries": [
                        {
                            "key": secret_key("kilocode.kilo-code"),
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "new-access",
                                "refresh_token": "rotated-refresh",
                                "expires": now_ms + 7 * 24 * 60 * 60 * 1000,
                                "accountId": "acct-new",
                                "id_token": "new-id",
                            },
                        }
                    ],
                    "last_refreshed_at": "2026-05-15T15:00:00Z",
                    "refresh_status": oauth_refresh.REFRESH_STATUS_OK,
                },
                "bob.json": {
                    "entries": [
                        {
                            "key": oauth_refresh.CODEX_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "new-access",
                                "refresh_token": "rotated-refresh",
                                "expires": now_ms + 7 * 24 * 60 * 60 * 1000,
                                "accountId": "acct-new",
                                "id_token": "new-id",
                            },
                        }
                    ],
                    "last_refreshed_at": "2026-05-15T15:00:00Z",
                    "refresh_status": oauth_refresh.REFRESH_STATUS_OK,
                },
            },
        )

    def test_run_once_uses_retry_backoff_after_failed_refresh(self):
        current_time = {"value": 1_000}
        raw_records = [
            {
                "name": "alice",
                "path": "alice.json",
                "kind": "ide",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.KILO_NEW_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access",
                                "refresh_token": "refresh-1",
                                "expires": 61_000,
                                "accountId": "acct-1",
                            },
                        }
                    ]
                },
            }
        ]
        attempts = {"count": 0}
        write_calls: list[dict[str, dict]] = []

        def flaky_refresher(bundle: oauth_refresh.TokenBundle) -> oauth_refresh.TokenBundle:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise oauth_refresh.TokenExchangeError("temporary upstream failure", status_code=503)
            return oauth_refresh.TokenBundle(
                access_token="new-access",
                refresh_token="refresh-2",
                expires=current_time["value"] + 120_000,
                account_id="acct-1",
                id_token=None,
            )

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: write_calls.append(dict(updates)),
            persist_refreshed_group=lambda updates, _group: write_calls.append(dict(updates)),
            refreshers={oauth_refresh.OPENAI_CODEX_PROVIDER: flaky_refresher},
            now_ms=lambda: current_time["value"],
            now_iso=lambda: "2026-05-15T15:00:00Z",
            policy=refresh_scheduler.RefreshPolicy(
                refresh_before_ms=10 * 60 * 1000,
                scan_interval_ms=60_000,
                min_delay_ms=5_000,
                initial_retry_ms=30_000,
                max_retry_ms=120_000,
            ),
        )

        first = scheduler.run_once()
        self.assertFalse(first.ok)
        self.assertFalse(first.refresh_ui)
        self.assertEqual(first.failed_groups, 1)
        self.assertEqual(first.terminal_failed_groups, 0)
        self.assertEqual(len(first.failures), 1)
        self.assertEqual(first.failures[0].group.account_names(), ("alice",))
        self.assertFalse(first.failures[0].terminal)
        self.assertEqual(first.refreshed_groups, 0)
        self.assertEqual(first.next_delay_ms, 30_000)
        self.assertIn("temporary upstream failure", first.message or "")
        self.assertIn("retry in 30s", first.message or "")
        self.assertEqual(attempts["count"], 1)

        current_time["value"] += 10_000
        second = scheduler.run_once()
        self.assertTrue(second.ok)
        self.assertEqual(second.due_groups, 0)
        self.assertEqual(second.next_delay_ms, 20_000)
        self.assertIsNone(second.message)
        self.assertEqual(attempts["count"], 1)

        current_time["value"] += 20_000
        third = scheduler.run_once()
        self.assertTrue(third.ok)
        self.assertEqual(third.refreshed_groups, 1)
        self.assertEqual(third.failed_groups, 0)
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(len(write_calls), 1)

    def test_run_once_disables_terminal_failures_for_current_session(self):
        current_time = {"value": 1_000}
        raw_records = [
            {
                "name": "codex3",
                "path": "codex3.json",
                "kind": "codex",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.CODEX_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access",
                                "refresh_token": "dead-refresh",
                                "expires": 1_500,
                                "accountId": "acct-dead",
                            },
                        }
                    ]
                },
            }
        ]
        attempts = {"count": 0}
        write_calls: list[dict[str, dict]] = []

        def terminal_refresher(bundle: oauth_refresh.TokenBundle) -> oauth_refresh.TokenBundle:
            attempts["count"] += 1
            raise oauth_refresh.TokenExchangeError(
                "Token refresh failed: 401 invalid_request_error: Your refresh token has already been used to generate a new access token.",
                status_code=401,
                error_code="invalid_request_error",
                error_description="Your refresh token has already been used to generate a new access token.",
                terminal=True,
            )

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: write_calls.append(dict(updates)),
            persist_refreshed_group=lambda updates, _group: write_calls.append(dict(updates)),
            refreshers={oauth_refresh.OPENAI_CODEX_PROVIDER: terminal_refresher},
            now_ms=lambda: current_time["value"],
            now_iso=lambda: "2026-05-15T18:00:00Z",
        )

        first = scheduler.run_once()
        self.assertFalse(first.ok)
        self.assertEqual(first.failed_groups, 1)
        self.assertEqual(first.terminal_failed_groups, 1)
        self.assertEqual(len(first.failures), 1)
        self.assertEqual(first.failures[0].group.account_names(), ("codex3",))
        self.assertTrue(first.failures[0].terminal)
        self.assertEqual(first.refreshed_groups, 0)
        self.assertEqual(first.next_delay_ms, scheduler.policy.scan_interval_ms)
        self.assertIn("Auto-refresh disabled for codex3", first.message or "")
        self.assertEqual(attempts["count"], 1)
        self.assertEqual(
            write_calls,
            [
                {
                    "codex3.json": {
                        "entries": [
                            {
                                "key": oauth_refresh.CODEX_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "access_token": "old-access",
                                    "refresh_token": "dead-refresh",
                                    "expires": 1_500,
                                    "accountId": "acct-dead",
                                },
                            }
                        ],
                        "refresh_status": oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR,
                        "refresh_error": "Token refresh failed: 401 invalid_request_error: Your refresh token has already been used to generate a new access token.",
                        "refresh_error_at": "2026-05-15T18:00:00Z",
                    }
                }
            ],
        )

        current_time["value"] += 60_000
        second = scheduler.run_once()
        self.assertTrue(second.ok)
        self.assertEqual(second.due_groups, 0)
        self.assertEqual(second.failed_groups, 0)
        self.assertEqual(second.next_delay_ms, scheduler.policy.scan_interval_ms)
        self.assertEqual(attempts["count"], 1)

    def test_run_once_disables_group_when_local_write_fails_after_successful_refresh(self):
        current_time = {"value": 1_000}
        raw_records = [
            {
                "name": "alice",
                "path": "alice.json",
                "kind": "ide",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.KILO_NEW_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access",
                                "refresh_token": "refresh-1",
                                "expires": 1_500,
                                "accountId": "acct-1",
                            },
                        }
                    ]
                },
            }
        ]
        attempts = {"count": 0}

        def refresher(bundle: oauth_refresh.TokenBundle) -> oauth_refresh.TokenBundle:
            attempts["count"] += 1
            return oauth_refresh.TokenBundle(
                access_token="new-access",
                refresh_token="refresh-2",
                expires=current_time["value"] + 120_000,
                account_id="acct-1",
                id_token=None,
            )

        with tempfile.TemporaryDirectory() as tempdir, patch.object(db, "PROJECT_ROOT", tempdir), patch.object(
            db,
            "write_saved_account_batch",
            lambda updates: (_ for _ in ()).throw(OSError("disk full")),
        ):
            scheduler = refresh_scheduler.AutoRefreshScheduler(
                list_saved_accounts=lambda: raw_records,
                write_saved_account_batch=db.write_saved_account_batch,
                persist_refreshed_group=db.persist_auto_refresh_group,
                refreshers={oauth_refresh.OPENAI_CODEX_PROVIDER: refresher},
                now_ms=lambda: current_time["value"],
            )

            first = scheduler.run_once()
            self.assertFalse(first.ok)
            self.assertEqual(first.failed_groups, 1)
            self.assertEqual(first.terminal_failed_groups, 1)
            self.assertEqual(len(first.failures), 1)
            self.assertEqual(first.failures[0].group.account_names(), ("alice",))
            self.assertIn("recovery snapshot was saved to", first.message or "")
            self.assertIn("manual sign-in may be required", first.message or "")
            self.assertEqual(attempts["count"], 1)

            recovery_dir = Path(tempdir) / "backups" / "refresh_recovery"
            recovery_files = list(recovery_dir.glob("*.json"))
            self.assertEqual(len(recovery_files), 1)
            payload = json.loads(recovery_files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["kind"], "saved-account-refresh-recovery")
            self.assertEqual(payload["operation"], "auto-refresh")
            self.assertEqual(payload["account_names"], ["alice"])

            current_time["value"] += 60_000
            second = scheduler.run_once()
            self.assertTrue(second.ok)
            self.assertEqual(second.due_groups, 0)
            self.assertEqual(attempts["count"], 1)

    def test_run_once_uses_scan_interval_when_nothing_is_due(self):
        now_ms = 10_000
        raw_records = [
            {
                "name": "future",
                "path": "future.json",
                "kind": "ide",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.KILO_NEW_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "access",
                                "refresh_token": "refresh-future",
                                "expires": now_ms + 3 * 24 * 60 * 60 * 1000,
                                "accountId": "acct-future",
                            },
                        }
                    ]
                },
            }
        ]

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: None,
            persist_refreshed_group=lambda updates, _group: None,
            now_ms=lambda: now_ms,
        )

        result = scheduler.run_once()

        self.assertTrue(result.ok)
        self.assertEqual(result.due_groups, 0)
        self.assertEqual(result.refreshed_groups, 0)
        self.assertEqual(result.next_delay_ms, scheduler.policy.scan_interval_ms)
        self.assertIsNone(result.message)

    def test_run_once_skips_expired_groups_but_still_refreshes_current_due_tokens(self):
        now_ms = 10_000
        raw_records = [
            {
                "name": "expired_snapshot",
                "path": "expired_snapshot.json",
                "kind": "codex",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.CODEX_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-expired-access",
                                "refresh_token": "refresh-expired",
                                "expires": now_ms - 1,
                                "accountId": "acct-expired",
                            },
                        }
                    ]
                },
            },
            {
                "name": "current_due",
                "path": "current_due.json",
                "kind": "codex",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.CODEX_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-current-access",
                                "refresh_token": "refresh-current",
                                "expires": now_ms + 1_000,
                                "accountId": "acct-current",
                            },
                        }
                    ]
                },
            },
        ]
        seen_refresh_tokens: list[str] = []
        write_calls: list[dict[str, dict]] = []

        def refresher(bundle: oauth_refresh.TokenBundle) -> oauth_refresh.TokenBundle:
            seen_refresh_tokens.append(bundle.refresh_token)
            return oauth_refresh.TokenBundle(
                access_token="new-current-access",
                refresh_token="refresh-current-rotated",
                expires=now_ms + 7 * 24 * 60 * 60 * 1000,
                account_id="acct-current",
                id_token=None,
            )

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: write_calls.append(dict(updates)),
            persist_refreshed_group=lambda updates, _group: write_calls.append(dict(updates)),
            refreshers={oauth_refresh.OPENAI_CODEX_PROVIDER: refresher},
            now_ms=lambda: now_ms,
            now_iso=lambda: "2026-05-15T19:00:00Z",
        )

        result = scheduler.run_once()

        self.assertTrue(result.ok)
        self.assertEqual(result.due_groups, 1)
        self.assertEqual(result.refreshed_groups, 1)
        self.assertEqual(seen_refresh_tokens, ["refresh-current"])
        self.assertEqual(
            write_calls,
            [
                {
                    "current_due.json": {
                        "entries": [
                            {
                                "key": oauth_refresh.CODEX_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "access_token": "new-current-access",
                                    "refresh_token": "refresh-current-rotated",
                                    "expires": now_ms + 7 * 24 * 60 * 60 * 1000,
                                    "accountId": "acct-current",
                                },
                            }
                        ],
                        "last_refreshed_at": "2026-05-15T19:00:00Z",
                        "refresh_status": oauth_refresh.REFRESH_STATUS_OK,
                    }
                }
            ],
        )

    def test_run_once_retries_when_provider_has_no_registered_refresher_and_prunes_stale_state(self):
        now_ms = 1_000
        raw_records = [
            {
                "name": "alice",
                "path": "alice.json",
                "kind": "ide",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": oauth_refresh.KILO_NEW_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "access_token": "old-access",
                                "refresh_token": "refresh-1",
                                "expires": now_ms + 1_000,
                                "accountId": "acct-1",
                            },
                        }
                    ]
                },
            }
        ]

        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: raw_records,
            write_saved_account_batch=lambda updates: None,
            persist_refreshed_group=lambda updates, _group: None,
            refreshers={"other-provider": lambda bundle: bundle},
            now_ms=lambda: now_ms,
            policy=refresh_scheduler.RefreshPolicy(initial_retry_ms=30_000, max_retry_ms=120_000),
        )
        stale_key = oauth_refresh.RefreshGroupKey(provider=oauth_refresh.OPENAI_CODEX_PROVIDER, refresh_token="stale-refresh")
        scheduler._group_states[stale_key] = refresh_scheduler.GroupRuntimeState(failure_count=2, next_retry_at=99_999)

        result = scheduler.run_once()

        self.assertFalse(result.ok)
        self.assertEqual(result.failed_groups, 1)
        self.assertEqual(result.terminal_failed_groups, 0)
        self.assertIn("No refresher is registered", result.message or "")
        self.assertIn("retry in 30s", result.message or "")
        self.assertNotIn(stale_key, scheduler._group_states)

    def test_build_message_handles_multiple_failures_and_empty_retry_hint(self):
        scheduler = refresh_scheduler.AutoRefreshScheduler(
            list_saved_accounts=lambda: [],
            write_saved_account_batch=lambda updates: None,
            persist_refreshed_group=lambda updates, _group: None,
            now_ms=lambda: 1_000,
        )

        def make_group(name: str, refresh_token: str) -> oauth_refresh.RefreshGroup:
            bundle = oauth_refresh.TokenBundle(
                access_token=f"access-{name}",
                refresh_token=refresh_token,
                expires=2_000,
                account_id=f"acct-{name}",
                id_token=None,
            )
            key = oauth_refresh.RefreshGroupKey(provider=oauth_refresh.OPENAI_CODEX_PROVIDER, refresh_token=refresh_token)
            entry = oauth_refresh.RefreshableRecordEntry(
                record_name=name,
                record_path=f"{name}.json",
                entry_index=0,
                group_key=key,
                bundle=bundle,
            )
            return oauth_refresh.RefreshGroup(key=key, bundle=bundle, expires=2_000, entries=(entry,))

        failures = [
            refresh_scheduler.RefreshFailure(
                group=make_group("alice", "refresh-1"),
                error_message="temporary failure",
                terminal=False,
                next_retry_at=31_000,
            ),
            refresh_scheduler.RefreshFailure(
                group=make_group("bob", "refresh-2"),
                error_message="revoked",
                terminal=True,
                next_retry_at=None,
            ),
        ]

        self.assertEqual(
            scheduler._build_message(0, 0, failures),
            "2 token groups failed, 1 terminal, first was alice: temporary failure (retry in 30s)",
        )
        self.assertIsNone(scheduler._build_message(0, 0, []))
        self.assertEqual(scheduler._retry_message(None), "")


if __name__ == "__main__":
    unittest.main()

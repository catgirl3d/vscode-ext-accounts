from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from vscode_inject import account_services
from vscode_inject import oauth_refresh


class UserFacingError(RuntimeError):
    pass


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

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class AccountServicesModuleTests(TempDirTestCase):
    kilo_new_key = "kilo-new://openai"

    def test_selection_and_entry_key_helpers_validate_edge_cases(self):
        self.assertEqual(
            account_services.entry_key_for_ext(self.kilo_new_key, "oauth-key", self.kilo_new_key),
            self.kilo_new_key,
        )
        self.assertEqual(account_services._extension_id_from_entry_key("secret://{bad-json"), "")

        with self.assertRaisesRegex(ValueError, "Select at least one extension"):
            account_services.normalize_ide_ext_selection(
                [],
                {"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key, "both": None},
                self.kilo_new_key,
            )

    def test_read_current_ide_entries_for_selection_includes_kilo_new_account(self):
        entries = account_services.read_current_ide_entries_for_selection(
            ["kilocode", "kilo-new"],
            ide_extensions={"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key},
            kilo_new_key=self.kilo_new_key,
            read_db_entries=lambda ext_ids: [{"key": ext_ids[0], "value": {"accountId": "acct-db"}}],
            read_kilo_auth=lambda: {"openai": {"accountId": "acct-kilo", "refresh": "refresh-kilo"}},
            from_kilo_new_format=lambda value: {"converted": value["accountId"]},
        )

        self.assertEqual(
            entries,
            [
                {"key": "kilocode.kilo-code", "value": {"accountId": "acct-db"}},
                {"key": self.kilo_new_key, "value": {"converted": "acct-kilo"}},
            ],
        )

    def test_first_expires_ms_uses_shared_filtering_rules(self):
        entries = [
            {"key": "skip-me", "value": {"expires": 1_000}},
            {"key": "keep-a", "value": {"expires": 5_000}},
            {"key": "keep-b", "value": {"expires": 2_000}},
            {"key": "bad", "value": {"expires": "later"}},
        ]

        self.assertEqual(account_services.first_expires_ms(entries), 1_000)
        self.assertEqual(account_services.first_expires_ms(entries, skip_keys=("skip-me",)), 2_000)
        self.assertEqual(account_services.first_expires_ms([{"key": "bad", "value": {}}]), 0)

    def test_save_ide_and_codex_account_raise_user_facing_errors_for_missing_data(self):
        with self.assertRaisesRegex(UserFacingError, "No matching account entries found"):
            account_services.save_ide_account(
                "alice",
                "kilocode",
                normalize_selection=lambda ext: (["kilocode"], "kilocode"),
                read_current_ide_entries_for_selection=lambda ext_names: [],
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "invalid characters"):
            account_services.save_ide_account(
                "alice",
                "kilocode",
                normalize_selection=lambda ext: (["kilocode"], "kilocode"),
                read_current_ide_entries_for_selection=lambda ext_names: [{"key": "secret://ide", "value": {}}],
                write_account_file=lambda *args: (_ for _ in ()).throw(ValueError("Account name contains invalid characters: /")),
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "missing access_token or refresh_token"):
            account_services.save_codex_account(
                "alice",
                codex_key="codex://auth",
                read_codex_auth=lambda: {},
                from_codex_format=lambda _value: {},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.save_codex_account(
                "alice",
                codex_key="codex://auth",
                read_codex_auth=lambda: {},
                from_codex_format=lambda _value: {"access_token": "access-1", "refresh_token": "refresh-1"},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

    def test_save_omp_openai_account_requires_live_credentials_and_preserves_entries(self):
        with self.assertRaisesRegex(UserFacingError, "No active OMP OpenAI credentials"):
            account_services.save_omp_openai_account(
                "alice",
                omp_key="omp://openai",
                read_omp_openai_entries=lambda: [],
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        result = account_services.save_omp_openai_account(
            "alice",
            omp_key="omp://openai",
            read_omp_openai_entries=lambda: [
                {
                    "key": "omp://openai",
                    "identity_key": "email:alice@example.com",
                    "value": {
                        "type": "openai-codex",
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires": 123,
                        "accountId": "acct-1",
                        "email": "alice@example.com",
                    },
                }
            ],
            write_account_file=lambda name, kind, ext_label, entries: "alice.json",
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.path, "alice.json")
        self.assertEqual(result.ext_label, "omp-openai")
        self.assertEqual(
            result.entries,
            [
                {
                    "key": "omp://openai",
                    "identity_key": "email:alice@example.com",
                    "value": {
                        "type": "openai-codex",
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires": 123,
                        "accountId": "acct-1",
                        "email": "alice@example.com",
                    },
                }
            ],
        )

    def test_import_omp_openai_account_data_accepts_object_or_array_and_dedupes_latest_identity(self):
        write_calls: list[tuple[str, str, str, list[dict]]] = []

        result = account_services.import_omp_openai_account_data(
            [
                {
                    "access_token": "access-old",
                    "refresh_token": "refresh-shared",
                    "account_id": "acct-1",
                    "email": "old@example.com",
                    "expires": 100,
                },
                {
                    "access_token": "access-new",
                    "refresh_token": "refresh-shared",
                    "account_id": "acct-1",
                    "email": "new@example.com",
                    "expires": 200,
                },
            ],
            "omp-set",
            omp_key="omp://openai",
            from_omp_import_format=lambda value: {
                "type": "openai-codex",
                "access_token": value["access_token"],
                "refresh_token": value["refresh_token"],
                "accountId": value["account_id"],
                "email": value.get("email"),
                "expires": value["expires"],
                "identity_key": f"account:{value['account_id']}",
            },
            write_account_file=lambda name, kind, ext_label, entries: write_calls.append((name, kind, ext_label, entries)) or "omp-set.json",
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.path, "omp-set.json")
        self.assertEqual(result.ext_label, "omp-openai")
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0]["value"]["access_token"], "access-new")
        self.assertEqual(write_calls[0][1:3], ("omp", "omp-openai"))

        object_result = account_services.import_omp_openai_account_data(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "account_id": "acct-2",
                "expires": 300,
            },
            "omp-single",
            omp_key="omp://openai",
            from_omp_import_format=lambda value: {
                "type": "openai-codex",
                "access_token": value["access_token"],
                "refresh_token": value["refresh_token"],
                "accountId": value["account_id"],
                "expires": value["expires"],
            },
            write_account_file=lambda *args: "omp-single.json",
            user_facing_error_cls=UserFacingError,
        )
        self.assertEqual(object_result.entries[0]["value"]["accountId"], "acct-2")

    def test_import_omp_openai_account_data_validates_payload_and_required_fields(self):
        with self.assertRaisesRegex(UserFacingError, "Empty JSON array"):
            account_services.import_omp_openai_account_data(
                [],
                "omp-empty",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "JSON must be an object or an array of objects"):
            account_services.import_omp_openai_account_data(
                "bad-payload",
                "omp-bad",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "access_token or refresh_token missing"):
            account_services.import_omp_openai_account_data(
                {"refresh_token": "refresh-only", "expires": 100},
                "omp-missing",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "could not decode access token expiry"):
            account_services.import_omp_openai_account_data(
                {"access_token": "access-1", "refresh_token": "refresh-1"},
                "omp-exp",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "could not decode access token expiry"):
            account_services.import_omp_openai_account_data(
                {"access_token": "access-1", "refresh_token": "refresh-1", "expires": True},
                "omp-bool-exp",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

    def test_append_omp_openai_account_data_merges_into_existing_saved_set(self):
        persisted: list[tuple[str, dict]] = []
        lock_events: list[str] = []

        class RecorderLock:
            def __enter__(self):
                lock_events.append("enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                lock_events.append("exit")
                return False

        result = account_services.append_omp_openai_account_data(
            [
                {
                    "access_token": "access-replaced",
                    "refresh_token": "refresh-1-new",
                    "account_id": "acct-1",
                    "expires": 222,
                },
                {
                    "access_token": "access-3",
                    "refresh_token": "refresh-3",
                    "account_id": "acct-3",
                    "expires": 333,
                },
            ],
            "team-set",
            omp_key="omp://openai",
            from_omp_import_format=lambda value: {
                "type": "openai-codex",
                "access_token": value["access_token"],
                "refresh_token": value["refresh_token"],
                "accountId": value["account_id"],
                "expires": value["expires"],
                "identity_key": f"account:{value['account_id']}",
            },
            load_saved_account_data=lambda name, expected_kind=None: (
                "team-set.json",
                {
                    "name": "team-set",
                    "kind": "omp",
                    "ext": "omp-openai",
                    "refresh_status": "error",
                    "refresh_error": "boom",
                    "refresh_error_at": "2026-05-15T12:00:00Z",
                    "auto_refresh_disabled_groups": [
                        {"provider": "openai-codex", "refresh_token": "refresh-2"}
                    ],
                    "entries": [
                        {
                            "key": "omp://openai",
                            "value": {
                                "type": "openai-codex",
                                "access_token": "access-1-old",
                                "refresh_token": "refresh-1-old",
                                "accountId": "acct-1",
                                "expires": 111,
                                "identity_key": "account:acct-1",
                            },
                        },
                        {
                            "key": "omp://openai",
                            "value": {
                                "type": "openai-codex",
                                "access_token": "access-2",
                                "refresh_token": "refresh-2",
                                "accountId": "acct-2",
                                "expires": 222,
                                "identity_key": "account:acct-2",
                            },
                        },
                    ],
                },
                "omp",
            ),
            write_saved_account_data=lambda path, data: lock_events.append("write") or persisted.append((path, data)),
            operation_lock=RecorderLock(),
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.path, "team-set.json")
        self.assertEqual(result.ext_label, "omp-openai")
        self.assertEqual(len(result.entries), 3)
        self.assertEqual([entry["value"]["accountId"] for entry in result.entries], ["acct-2", "acct-1", "acct-3"])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0][0], "team-set.json")
        self.assertEqual(persisted[0][1]["name"], "team-set")
        self.assertEqual(persisted[0][1]["refresh_status"], "error")
        self.assertEqual(persisted[0][1]["refresh_error"], "boom")
        self.assertEqual(persisted[0][1]["refresh_error_at"], "2026-05-15T12:00:00Z")
        self.assertEqual(
            persisted[0][1]["auto_refresh_disabled_groups"],
            [{"provider": "openai-codex", "refresh_token": "refresh-2"}],
        )
        self.assertEqual(lock_events, ["enter", "write", "exit"])

        with self.assertRaisesRegex(UserFacingError, "invalid entries payload"):
            account_services.append_omp_openai_account_data(
                {"access_token": "access-1", "refresh_token": "refresh-1", "account_id": "acct-1", "expires": 111},
                "broken",
                omp_key="omp://openai",
                from_omp_import_format=lambda value: {
                    "type": "openai-codex",
                    "access_token": value["access_token"],
                    "refresh_token": value["refresh_token"],
                    "accountId": value["account_id"],
                    "expires": value["expires"],
                },
                load_saved_account_data=lambda name, expected_kind=None: ("broken.json", {"entries": "bad"}, "omp"),
                write_saved_account_data=lambda path, data: None,
                user_facing_error_cls=UserFacingError,
            )

    def test_refresh_saved_account_rejects_invalid_entries_payload(self):
        with self.assertRaisesRegex(ValueError, "invalid entries payload"):
            account_services.refresh_saved_account(
                "alice",
                operation_lock=contextlib.nullcontext(),
                load_saved_account_data=lambda name: ("account.json", {"entries": "invalid"}, "ide"),
                oauth_refresh_module=SimpleNamespace(),
                is_terminal_refresh_error=lambda exc: False,
                write_saved_account_batch=lambda updates: None,
                persist_refreshed_saved_account_batch=lambda *args, **kwargs: None,
                saved_account_refresh_error_cls=UserFacingError,
                persistence_error_cls=RuntimeError,
            )

    def test_refresh_saved_account_uses_explicit_terminal_classifier(self):
        class DummyOAuthRefreshError(RuntimeError):
            pass

        write_calls: list[tuple[str, dict]] = []
        terminal_exc = DummyOAuthRefreshError("refresh failed")
        terminal_exc.refresh_group_key = oauth_refresh.RefreshGroupKey(
            provider=oauth_refresh.OPENAI_CODEX_PROVIDER,
            refresh_token="refresh-1",
        )
        refresh_module = SimpleNamespace(
            OAuthRefreshError=DummyOAuthRefreshError,
            collect_refreshable_entries=lambda entries: [
                SimpleNamespace(
                    provider=oauth_refresh.OPENAI_CODEX_PROVIDER,
                    bundle=oauth_refresh.TokenBundle(
                        access_token="old-access",
                        refresh_token="refresh-1",
                        expires=1_000,
                    ),
                )
            ],
            refresh_saved_entries=lambda entries: (_ for _ in ()).throw(terminal_exc),
            current_time_iso=lambda: "2026-05-15T12:00:00Z",
            saved_account_records=oauth_refresh.saved_account_records,
            auto_refresh_disabled_group_keys=oauth_refresh.auto_refresh_disabled_group_keys,
            set_auto_refresh_disabled_group_keys=oauth_refresh.set_auto_refresh_disabled_group_keys,
            apply_auto_refresh_disabled_group_updates=oauth_refresh.apply_auto_refresh_disabled_group_updates,
        )

        with self.assertRaisesRegex(UserFacingError, "Token renewal failed for 'alice': refresh failed"):
            account_services.refresh_saved_account(
                "alice",
                operation_lock=contextlib.nullcontext(),
                load_saved_account_data=lambda name: (
                    "account.json",
                    {
                        "entries": [
                            {
                                "key": oauth_refresh.KILO_NEW_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "access_token": "old-access",
                                    "refresh_token": "refresh-1",
                                    "expires": 1_000,
                                },
                            }
                        ]
                    },
                    "ide",
                ),
                list_saved_accounts=lambda: [
                    {
                        "name": "alice",
                        "path": "account.json",
                        "kind": "ide",
                        "data": {
                            "entries": [
                                {
                                    "key": oauth_refresh.KILO_NEW_KEY,
                                    "value": {
                                        "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                        "access_token": "old-access",
                                        "refresh_token": "refresh-1",
                                        "expires": 1_000,
                                    },
                                }
                            ]
                        },
                    },
                    {
                        "name": "bob",
                        "path": "bob.json",
                        "kind": "codex",
                        "data": {
                            "entries": [
                                {
                                    "key": oauth_refresh.CODEX_KEY,
                                    "value": {
                                        "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                        "access_token": "old-access-b",
                                        "refresh_token": "refresh-1",
                                        "expires": 2_000,
                                    },
                                }
                            ]
                        },
                    },
                ],
                oauth_refresh_module=refresh_module,
                is_terminal_refresh_error=lambda exc: True,
                write_saved_account_batch=lambda updates: write_calls.extend(list(updates.items())),
                persist_refreshed_saved_account_batch=lambda *args, **kwargs: None,
                saved_account_refresh_error_cls=UserFacingError,
                persistence_error_cls=RuntimeError,
            )

        self.assertEqual(
            write_calls,
            [
                (
                    "account.json",
                    {
                        "entries": [
                            {
                                "key": oauth_refresh.KILO_NEW_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "access_token": "old-access",
                                    "refresh_token": "refresh-1",
                                    "expires": 1_000,
                                },
                            }
                        ],
                        "auto_refresh_disabled_groups": [
                            {
                                "provider": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "refresh_token": "refresh-1",
                            }
                        ],
                        "refresh_status": oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR,
                        "refresh_error": "refresh failed",
                        "refresh_error_at": "2026-05-15T12:00:00Z",
                    },
                ),
                (
                    "bob.json",
                    {
                        "entries": [
                            {
                                "key": oauth_refresh.CODEX_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "access_token": "old-access-b",
                                    "refresh_token": "refresh-1",
                                    "expires": 2_000,
                                },
                            }
                        ],
                        "auto_refresh_disabled_groups": [
                            {
                                "provider": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "refresh_token": "refresh-1",
                            }
                        ],
                    },
                )
            ],
        )

    def test_refresh_saved_account_success_clears_auto_refresh_disabled_groups(self):
        persisted_calls: list[dict[str, dict]] = []
        refresh_module = SimpleNamespace(
            OAuthRefreshError=RuntimeError,
            AUTO_REFRESH_DISABLED_GROUPS_KEY="auto_refresh_disabled_groups",
            collect_refreshable_entries=lambda entries: [SimpleNamespace(provider=oauth_refresh.OPENAI_CODEX_PROVIDER)],
            refresh_saved_entries=lambda entries: SimpleNamespace(
                entries=[
                    {
                        "key": oauth_refresh.KILO_NEW_KEY,
                        "value": {
                            "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                            "refresh_token": "refresh-1",
                            "access_token": "new-access",
                        },
                    }
                ],
                refreshed_at="2026-05-15T12:00:00Z",
                refreshed_groups=1,
                refreshed_entries=1,
            ),
            current_time_iso=lambda: "2026-05-15T12:00:00Z",
            group_keys_from_entries=oauth_refresh.group_keys_from_entries,
            saved_account_records=oauth_refresh.saved_account_records,
            apply_auto_refresh_disabled_group_updates=oauth_refresh.apply_auto_refresh_disabled_group_updates,
        )

        message = account_services.refresh_saved_account(
            "alice",
            operation_lock=contextlib.nullcontext(),
            load_saved_account_data=lambda name: (
                "alice.json",
                {
                    "entries": [
                        {
                            "key": oauth_refresh.KILO_NEW_KEY,
                            "value": {
                                "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                "refresh_token": "refresh-1",
                                "access_token": "old-access",
                            },
                        }
                    ],
                    "auto_refresh_disabled_groups": [
                        {"provider": oauth_refresh.OPENAI_CODEX_PROVIDER, "refresh_token": "refresh-1"}
                    ],
                },
                "ide",
            ),
            list_saved_accounts=lambda: [
                {
                    "name": "alice",
                    "path": "alice.json",
                    "kind": "ide",
                    "data": {
                        "entries": [
                            {
                                "key": oauth_refresh.KILO_NEW_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "refresh_token": "refresh-1",
                                    "access_token": "old-access",
                                },
                            }
                        ],
                        "auto_refresh_disabled_groups": [
                            {"provider": oauth_refresh.OPENAI_CODEX_PROVIDER, "refresh_token": "refresh-1"}
                        ],
                    },
                },
                {
                    "name": "bob",
                    "path": "bob.json",
                    "kind": "codex",
                    "data": {
                        "entries": [
                            {
                                "key": oauth_refresh.CODEX_KEY,
                                "value": {
                                    "type": oauth_refresh.OPENAI_CODEX_PROVIDER,
                                    "refresh_token": "refresh-1",
                                    "access_token": "other-access",
                                    "expires": 2_000,
                                },
                            }
                        ],
                        "auto_refresh_disabled_groups": [
                            {"provider": oauth_refresh.OPENAI_CODEX_PROVIDER, "refresh_token": "refresh-1"}
                        ],
                        "refresh_status": oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR,
                    },
                },
            ],
            oauth_refresh_module=refresh_module,
            is_terminal_refresh_error=lambda exc: False,
            write_saved_account_batch=lambda updates: None,
            persist_refreshed_saved_account_batch=lambda updates, **kwargs: persisted_calls.append(dict(updates)),
            saved_account_refresh_error_cls=UserFacingError,
            persistence_error_cls=RuntimeError,
        )

        self.assertEqual(message, "Renewed tokens for 'alice' (1 token group, 1 entry)")
        self.assertEqual(len(persisted_calls), 1)
        self.assertNotIn("auto_refresh_disabled_groups", persisted_calls[0]["alice.json"])
        self.assertNotIn("auto_refresh_disabled_groups", persisted_calls[0]["bob.json"])

    def test_use_ide_account_validates_kind_and_handles_running_kilo_new(self):
        base_kwargs = {
            "normalize_selection": lambda ext: (["kilocode"], "kilocode"),
            "ide_extensions": {"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key},
            "code_key": "code-key",
            "kilo_new_key": self.kilo_new_key,
            "ide_paths": {"vscode": {"label": "VSCode"}, "antigravity": {"label": "Antigravity"}},
            "guard_current_ide_closed": lambda: None,
            "is_ide_running": lambda ide: False,
            "create_prewrite_backup": lambda **kwargs: None,
            "apply_db_entries": lambda entries: None,
            "entry_key_for_ext_fn": lambda ext_id: f"secret://{ext_id}",
            "to_kilo_new_format": lambda value: {"accountId": value.get("accountId", "?")},
            "read_kilo_auth": lambda: {},
            "write_kilo_auth": lambda auth: None,
            "kilo_auth_path": "kilo/auth.json",
            "user_facing_error_cls": UserFacingError,
            "print_fn": lambda msg: None,
        }

        with self.assertRaisesRegex(UserFacingError, "Codex-only"):
            account_services.use_ide_account(
                "alice",
                load_saved_account_data=lambda name: ("account.json", {}, "codex"),
                **base_kwargs,
            )

        with self.assertRaisesRegex(UserFacingError, "No IDE entries"):
            account_services.use_ide_account(
                "alice",
                load_saved_account_data=lambda name: ("account.json", {"entries": [{"key": "code-key", "value": {}}]}, "ide"),
                **base_kwargs,
            )

        messages: list[str] = []
        backups = []
        written = []
        account_services.use_ide_account(
            "alice",
            ext=["kilo-new"],
            allow_kilo_new_while_running=True,
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": self.kilo_new_key,
                            "value": {
                                "accountId": "acct-kilo",
                                "access_token": "access-kilo",
                                "refresh_token": "refresh-kilo",
                            },
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["kilo-new"], "kilo-new"),
            ide_extensions=base_kwargs["ide_extensions"],
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths=base_kwargs["ide_paths"],
            guard_current_ide_closed=base_kwargs["guard_current_ide_closed"],
            is_ide_running=lambda ide: ide == "vscode",
            create_prewrite_backup=lambda **kwargs: backups.append(kwargs),
            apply_db_entries=base_kwargs["apply_db_entries"],
            entry_key_for_ext_fn=base_kwargs["entry_key_for_ext_fn"],
            to_kilo_new_format=lambda value: {
                "accountId": value["accountId"],
                "access": value["access_token"],
                "refresh": value["refresh_token"],
                "expires": value.get("expires", 0),
            },
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: written.append(auth),
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(backups, [{"include_db": False, "include_kilo": True, "note": "before applying IDE account 'alice'"}])
        self.assertIn("WARNING: Writing shared Kilo New auth while IDEs are running (VSCode)", messages[0])
        self.assertEqual(messages[-2:], ["[kilo-new] Written to kilo/auth.json", "  accountId: acct-kilo"])
        self.assertEqual(
            written,
            [{"openai": {"accountId": "acct-kilo", "access": "access-kilo", "refresh": "refresh-kilo", "expires": 0}}],
        )

    def test_use_ide_account_reuses_exact_db_entry_without_cross_extension_remap(self):
        applied_entries: list[list[dict]] = []
        messages: list[str] = []
        entry_key = 'secret://{"extensionId":"rooveterinaryinc.roo-cline","key":"oauth"}'

        account_services.use_ide_account(
            "alice",
            ext=["roo-cline"],
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": entry_key,
                            "value": {"accountId": "acct-roo", "refresh_token": "refresh-roo"},
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["roo-cline"], "roo-cline"),
            ide_extensions={"roo-cline": "rooveterinaryinc.roo-cline", "kilo-new": self.kilo_new_key},
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths={"vscode": {"label": "VSCode"}},
            guard_current_ide_closed=lambda: None,
            is_ide_running=lambda ide: False,
            create_prewrite_backup=lambda **kwargs: None,
            apply_db_entries=lambda entries: applied_entries.append(entries),
            entry_key_for_ext_fn=lambda ext_id: f'secret://{{"extensionId":"{ext_id}","key":"oauth"}}',
            to_kilo_new_format=lambda value: value,
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: None,
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(
            applied_entries,
            [[{"key": entry_key, "value": {"accountId": "acct-roo", "refresh_token": "refresh-roo"}}]],
        )
        self.assertNotIn("[cross-ext]", "\n".join(messages))

    def test_use_ide_account_can_remap_db_target_from_kilo_new_source(self):
        applied_entries: list[list[dict]] = []
        messages: list[str] = []

        account_services.use_ide_account(
            "alice",
            ext=["roo-cline"],
            load_saved_account_data=lambda name: (
                "account.json",
                {
                    "entries": [
                        {
                            "key": self.kilo_new_key,
                            "value": {"accountId": "acct-kilo", "refresh_token": "refresh-kilo"},
                        }
                    ]
                },
                "ide",
            ),
            normalize_selection=lambda ext: (["roo-cline"], "roo-cline"),
            ide_extensions={"roo-cline": "rooveterinaryinc.roo-cline", "kilo-new": self.kilo_new_key},
            code_key="code-key",
            kilo_new_key=self.kilo_new_key,
            ide_paths={"vscode": {"label": "VSCode"}},
            guard_current_ide_closed=lambda: None,
            is_ide_running=lambda ide: False,
            create_prewrite_backup=lambda **kwargs: None,
            apply_db_entries=lambda entries: applied_entries.append(entries),
            entry_key_for_ext_fn=lambda ext_id: f'secret://{{"extensionId":"{ext_id}","key":"oauth"}}',
            to_kilo_new_format=lambda value: value,
            read_kilo_auth=lambda: {},
            write_kilo_auth=lambda auth: None,
            kilo_auth_path="kilo/auth.json",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(
            applied_entries,
            [[
                {
                    "key": 'secret://{"extensionId":"rooveterinaryinc.roo-cline","key":"oauth"}',
                    "value": {"accountId": "acct-kilo", "refresh_token": "refresh-kilo"},
                }
            ]],
        )
        self.assertIn("[cross-ext] No 'rooveterinaryinc.roo-cline' key", messages[1])

    def test_use_codex_account_and_import_codex_account_validate_inputs(self):
        with self.assertRaisesRegex(UserFacingError, "does not contain a Codex entry"):
            account_services.use_codex_account(
                "alice",
                load_saved_account_data=lambda name, expected_kind=None: ("account.json", {}, "codex"),
                saved_codex_entry=lambda data: None,
                create_prewrite_backup=lambda **kwargs: None,
                to_codex_format=lambda value, existing: existing,
                read_codex_auth=lambda: {},
                write_codex_auth=lambda auth: None,
                codex_auth_path="codex/auth.json",
                user_facing_error_cls=UserFacingError,
                print_fn=lambda msg: None,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.use_codex_account(
                "alice",
                load_saved_account_data=lambda name, expected_kind=None: ("account.json", {}, "codex"),
                saved_codex_entry=lambda data: {"value": {"accountId": "acct-1"}},
                create_prewrite_backup=lambda **kwargs: None,
                to_codex_format=lambda value, existing: (_ for _ in ()).throw(ValueError("requires id_token")),
                read_codex_auth=lambda: {},
                write_codex_auth=lambda auth: None,
                codex_auth_path="codex/auth.json",
                user_facing_error_cls=UserFacingError,
                print_fn=lambda msg: None,
            )

        auth_path = self.root / "codex" / "import.json"
        self.write_json(auth_path, {"source": "auth"})

        with self.assertRaisesRegex(UserFacingError, "access_token or refresh_token missing"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {"accountId": "acct-1", "expires": 123, "id_token": "id-1"},
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "could not decode access token expiry"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "accountId": "acct-1",
                    "expires": 0,
                    "id_token": "id-1",
                },
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.import_codex_account(
                str(auth_path),
                "alice",
                codex_key="codex://auth",
                from_codex_format=lambda data: {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "accountId": "acct-1",
                    "expires": 123,
                },
                write_account_file=lambda *args: "unused.json",
                user_facing_error_cls=UserFacingError,
            )

    def test_use_omp_openai_account_applies_full_saved_set(self):
        backups: list[dict[str, object]] = []
        replaced_entries: list[list[dict]] = []
        messages: list[str] = []

        account_services.use_omp_openai_account(
            "alice",
            load_saved_account_data=lambda name, expected_kind=None: (
                "omp.json",
                {
                    "entries": [
                        {
                            "key": "omp://openai",
                            "identity_key": "email:alice@example.com",
                            "value": {
                                "type": "openai-codex",
                                "access_token": "access-1",
                                "refresh_token": "refresh-1",
                                "expires": 123,
                                "accountId": "acct-1",
                            },
                        },
                        {
                            "key": "omp://openai",
                            "value": {
                                "type": "openai-codex",
                                "access_token": "access-2",
                                "refresh_token": "refresh-2",
                                "expires": 456,
                                "accountId": "acct-2",
                            },
                        },
                    ]
                },
                "omp",
            ),
            omp_key="omp://openai",
            create_prewrite_backup=lambda **kwargs: backups.append(kwargs),
            replace_omp_openai_credentials=lambda entries: replaced_entries.append(list(entries)),
            omp_agent_db_path="C:/Users/Test/.omp/agent/agent.db",
            user_facing_error_cls=UserFacingError,
            print_fn=messages.append,
        )

        self.assertEqual(
            backups,
            [{"include_omp": True, "note": "before applying OMP OpenAI account 'alice'"}],
        )
        self.assertEqual(len(replaced_entries), 1)
        self.assertEqual(len(replaced_entries[0]), 2)
        self.assertEqual(messages[-3:], [
            "[omp-openai] Written to C:/Users/Test/.omp/agent/agent.db",
            "  credentials: 2",
            "  accountIds: acct-1, acct-2",
        ])

        with self.assertRaisesRegex(UserFacingError, "does not contain any OMP OpenAI entries"):
            account_services.use_omp_openai_account(
                "alice",
                load_saved_account_data=lambda name, expected_kind=None: ("omp.json", {"entries": []}, "omp"),
                omp_key="omp://openai",
                create_prewrite_backup=lambda **kwargs: None,
                replace_omp_openai_credentials=lambda entries: None,
                omp_agent_db_path="C:/Users/Test/.omp/agent/agent.db",
                user_facing_error_cls=UserFacingError,
                print_fn=lambda msg: None,
            )

    def test_import_ide_account_data_writes_selected_ide_entries(self):
        write_calls: list[tuple[str, str, str, list[dict]]] = []

        result = account_services.import_ide_account_data(
            [
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "account_id": "acct-1",
                    "id_token": "id-1",
                    "expires": 123,
                }
            ],
            "alice",
            ["kilocode", "kilo-new"],
            ide_extensions={"kilocode": "kilocode.kilo-code", "kilo-new": self.kilo_new_key},
            kilo_new_key=self.kilo_new_key,
            from_codex_format=lambda value: {
                "access_token": value["access_token"],
                "refresh_token": value["refresh_token"],
                "accountId": value["account_id"],
                "id_token": value["id_token"],
                "expires": value["expires"],
            },
            write_account_file=lambda name, kind, ext_label, entries: write_calls.append((name, kind, ext_label, entries)) or "alice.json",
            entry_key_for_ext_fn=lambda ext_id: f"secret://{ext_id}",
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.path, "alice.json")
        self.assertEqual(result.ext_label, "kilocode+kilo-new")
        self.assertEqual(
            result.entries,
            [
                {
                    "key": "secret://kilocode.kilo-code",
                    "value": {
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "accountId": "acct-1",
                        "id_token": "id-1",
                        "expires": 123,
                    },
                },
                {
                    "key": self.kilo_new_key,
                    "value": {
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "accountId": "acct-1",
                        "id_token": "id-1",
                        "expires": 123,
                    },
                },
            ],
        )
        self.assertEqual(write_calls, [("alice", "ide", "kilocode+kilo-new", result.entries)])

    def test_import_ide_account_data_validates_array_and_id_token_requirements(self):
        with self.assertRaisesRegex(UserFacingError, "Empty JSON array provided"):
            account_services.import_ide_account_data(
                [],
                "alice",
                ["kilocode"],
                ide_extensions={"kilocode": "kilocode.kilo-code"},
                kilo_new_key=self.kilo_new_key,
                from_codex_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                entry_key_for_ext_fn=lambda ext_id: ext_id,
                user_facing_error_cls=UserFacingError,
            )

        with self.assertRaisesRegex(UserFacingError, "requires id_token"):
            account_services.import_ide_account_data(
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "accountId": "acct-1",
                    "expires": 123,
                },
                "alice",
                ["kilocode"],
                ide_extensions={"kilocode": "kilocode.kilo-code"},
                kilo_new_key=self.kilo_new_key,
                from_codex_format=lambda value: value,
                write_account_file=lambda *args: "unused.json",
                entry_key_for_ext_fn=lambda ext_id: ext_id,
                user_facing_error_cls=UserFacingError,
            )

    def test_print_saved_entries_includes_expiration_only_when_present(self):
        messages: list[str] = []
        account_services.print_saved_entries(
            [
                {"value": {"accountId": "acct-1"}},
                {"value": {"accountId": "acct-2", "expires": 1700000000000}},
            ],
            print_fn=messages.append,
        )

        self.assertEqual(messages[0], "  accountId: acct-1")
        self.assertEqual(messages[1], "  accountId: acct-2")
        self.assertTrue(messages[2].startswith("  expires:   "))

    def test_fetch_saved_account_usage_deduplicates_entries_and_writes_snapshots(self):
        written_records: list[tuple[str, dict]] = []
        fetch_usage_snapshot = Mock(
            return_value={
                "fetched_at": "2026-07-11T10:00:00Z",
                "limits": [
                    {
                        "remaining": 42,
                        "windowSeconds": 18_000,
                    }
                ],
            }
        )

        result = account_services.fetch_saved_account_usage(
            "alice",
            expected_kind="ide",
            operation_lock=contextlib.nullcontext(),
            load_saved_account_data=lambda name, expected_kind=None: (
                "alice.json",
                {
                    "name": "alice",
                    "usage_snapshots": {
                        "stale-key": {
                            "fetched_at": "2026-07-10T10:00:00Z",
                            "limits": [{"remaining": 1, "windowSeconds": 18_000}],
                        }
                    },
                    "entries": [
                        {
                            "key": "secret://kilocode",
                            "value": {
                                "access_token": "access-1",
                                "refresh_token": "refresh-1",
                                "accountId": "acct-1",
                                "email": "Alice@example.com",
                            },
                        },
                        {
                            "key": self.kilo_new_key,
                            "value": {
                                "access_token": "access-1",
                                "refresh_token": "refresh-1",
                                "accountId": "acct-1",
                                "email": "alice@example.com",
                            },
                        },
                    ],
                },
                "ide",
            ),
            write_saved_account_data=lambda path, data: written_records.append((path, data)),
            fetch_usage_snapshot=fetch_usage_snapshot,
            user_facing_error_cls=UserFacingError,
        )

        fetch_usage_snapshot.assert_called_once_with("access-1", account_id="acct-1")
        self.assertEqual(result.path, "alice.json")
        self.assertEqual(result.requested_snapshots, 1)
        self.assertEqual(result.updated_snapshots, 1)
        self.assertEqual(result.failed_snapshots, 0)
        self.assertEqual(result.error_messages, ())
        self.assertEqual(len(written_records), 1)
        self.assertEqual(written_records[0][0], "alice.json")
        self.assertIn("usage_last_fetched_at", written_records[0][1])
        self.assertEqual(
            written_records[0][1]["usage_snapshots"],
            {
                "identity:account:acct-1": {
                    "fetched_at": "2026-07-11T10:00:00Z",
                    "limits": [
                        {
                            "remaining": 42,
                            "windowSeconds": 18_000,
                        }
                    ],
                }
            },
        )

    def test_fetch_saved_account_usage_fetches_same_email_accounts_separately(self):
        written_records: list[tuple[str, dict]] = []
        fetch_calls: list[tuple[str, str | None]] = []

        def fetch_usage_snapshot(access_token, account_id=None):
            fetch_calls.append((access_token, account_id))
            if account_id == "acct-a":
                return {
                    "fetched_at": "2026-07-11T10:00:00Z",
                    "limits": [{"remaining": 10, "windowSeconds": 18_000}],
                }
            return {
                "fetched_at": "2026-07-11T10:00:01Z",
                "limits": [{"remaining": 20, "windowSeconds": 604_800}],
            }

        result = account_services.fetch_saved_account_usage(
            "team",
            expected_kind="omp",
            operation_lock=contextlib.nullcontext(),
            load_saved_account_data=lambda name, expected_kind=None: (
                "team.json",
                {
                    "name": "team",
                    "entries": [
                        {
                            "key": "omp://openai-a",
                            "value": {
                                "access_token": "access-a",
                                "refresh_token": "refresh-a",
                                "accountId": "acct-a",
                                "email": "shared@example.com",
                            },
                        },
                        {
                            "key": "omp://openai-b",
                            "value": {
                                "access_token": "access-b",
                                "refresh_token": "refresh-b",
                                "accountId": "acct-b",
                                "email": "shared@example.com",
                            },
                        },
                    ],
                },
                "omp",
            ),
            write_saved_account_data=lambda path, data: written_records.append((path, data)),
            fetch_usage_snapshot=fetch_usage_snapshot,
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.requested_snapshots, 2)
        self.assertEqual(result.updated_snapshots, 2)
        self.assertEqual(result.failed_snapshots, 0)
        self.assertEqual(fetch_calls, [("access-a", "acct-a"), ("access-b", "acct-b")])
        self.assertEqual(
            written_records[0][1]["usage_snapshots"],
            {
                "identity:account:acct-a": {
                    "fetched_at": "2026-07-11T10:00:00Z",
                    "limits": [{"remaining": 10, "windowSeconds": 18_000}],
                },
                "identity:account:acct-b": {
                    "fetched_at": "2026-07-11T10:00:01Z",
                    "limits": [{"remaining": 20, "windowSeconds": 604_800}],
                },
            },
        )

    def test_fetch_saved_account_usage_uses_explicit_identity_key_when_present(self):
        written_records: list[tuple[str, dict]] = []

        result = account_services.fetch_saved_account_usage(
            "team",
            expected_kind="omp",
            operation_lock=contextlib.nullcontext(),
            load_saved_account_data=lambda name, expected_kind=None: (
                "team.json",
                {
                    "name": "team",
                    "entries": [
                        {
                            "key": "omp://openai-a",
                            "identity_key": "custom:shared",
                            "value": {
                                "access_token": "access-a",
                                "refresh_token": "refresh-a",
                                "accountId": "acct-a",
                                "email": "shared@example.com",
                            },
                        }
                    ],
                },
                "omp",
            ),
            write_saved_account_data=lambda path, data: written_records.append((path, data)),
            fetch_usage_snapshot=lambda access_token, account_id=None: {
                "fetched_at": "2026-07-11T10:00:00Z",
                "limits": [{"remaining": 10, "windowSeconds": 18_000}],
            },
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.requested_snapshots, 1)
        self.assertEqual(
            written_records[0][1]["usage_snapshots"],
            {
                "identity:custom:shared": {
                    "fetched_at": "2026-07-11T10:00:00Z",
                    "limits": [{"remaining": 10, "windowSeconds": 18_000}],
                }
            },
        )

    def test_fetch_saved_account_usage_preserves_failed_current_snapshot(self):
        written_records: list[tuple[str, dict]] = []

        def fetch_usage_snapshot(access_token, account_id=None):
            if account_id == "acct-a":
                return {
                    "fetched_at": "2026-07-11T12:00:00Z",
                    "limits": [{"remaining": 15, "windowSeconds": 18_000}],
                }
            raise RuntimeError("temporary outage")

        result = account_services.fetch_saved_account_usage(
            "team",
            expected_kind="omp",
            operation_lock=contextlib.nullcontext(),
            load_saved_account_data=lambda name, expected_kind=None: (
                "team.json",
                {
                    "name": "team",
                    "usage_snapshots": {
                        "identity:account:acct-a": {
                            "fetched_at": "2026-07-10T10:00:00Z",
                            "limits": [{"remaining": 70, "windowSeconds": 18_000}],
                        },
                        "identity:account:acct-b": {
                            "fetched_at": "2026-07-10T10:00:00Z",
                            "limits": [{"remaining": 80, "windowSeconds": 604_800}],
                        },
                        "stale-key": {
                            "fetched_at": "2026-07-10T10:00:00Z",
                            "limits": [{"remaining": 1, "windowSeconds": 2_592_000}],
                        },
                    },
                    "entries": [
                        {
                            "key": "omp://openai-a",
                            "value": {
                                "access_token": "access-a",
                                "refresh_token": "refresh-a",
                                "accountId": "acct-a",
                                "email": "shared@example.com",
                            },
                        },
                        {
                            "key": "omp://openai-b",
                            "value": {
                                "access_token": "access-b",
                                "refresh_token": "refresh-b",
                                "accountId": "acct-b",
                                "email": "shared@example.com",
                            },
                        },
                    ],
                },
                "omp",
            ),
            write_saved_account_data=lambda path, data: written_records.append((path, data)),
            fetch_usage_snapshot=fetch_usage_snapshot,
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.requested_snapshots, 2)
        self.assertEqual(result.updated_snapshots, 1)
        self.assertEqual(result.failed_snapshots, 1)
        self.assertEqual(result.error_messages, ("temporary outage",))
        self.assertEqual(
            written_records[0][1]["usage_snapshots"],
            {
                "identity:account:acct-a": {
                    "fetched_at": "2026-07-11T12:00:00Z",
                    "limits": [{"remaining": 15, "windowSeconds": 18_000}],
                },
                "identity:account:acct-b": {
                    "fetched_at": "2026-07-10T10:00:00Z",
                    "limits": [{"remaining": 80, "windowSeconds": 604_800}],
                },
            },
        )

    def test_fetch_saved_account_usage_raises_when_all_snapshots_fail(self):
        writes: list[tuple[str, dict]] = []

        with self.assertRaisesRegex(UserFacingError, "Usage fetch failed for 'alice': boom"):
            account_services.fetch_saved_account_usage(
                "alice",
                expected_kind="codex",
                operation_lock=contextlib.nullcontext(),
                load_saved_account_data=lambda name, expected_kind=None: (
                    "alice.json",
                    {
                        "name": "alice",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "access_token": "access-1",
                                    "refresh_token": "refresh-1",
                                    "accountId": "acct-1",
                                },
                            }
                        ],
                    },
                    "codex",
                ),
                write_saved_account_data=lambda path, data: writes.append((path, data)),
                fetch_usage_snapshot=lambda access_token, account_id=None: (_ for _ in ()).throw(RuntimeError("boom")),
                user_facing_error_cls=UserFacingError,
            )

        self.assertEqual(writes, [])

    def test_fetch_saved_accounts_usage_aggregates_partial_failures(self):
        writes: list[tuple[str, dict]] = []
        snapshots_by_account = {
            "alice": {
                "fetched_at": "2026-07-11T10:00:00Z",
                "limits": [{"remaining": 42, "windowSeconds": 18_000}],
            },
            "bob": RuntimeError("network down"),
        }

        account_payloads = {
            "alice": {
                "name": "alice",
                "entries": [
                    {
                        "key": "codex://openai",
                        "value": {
                            "access_token": "access-a",
                            "refresh_token": "refresh-a",
                            "accountId": "acct-a",
                        },
                    }
                ],
            },
            "bob": {
                "name": "bob",
                "entries": [
                    {
                        "key": "codex://openai",
                        "value": {
                            "access_token": "access-b",
                            "refresh_token": "refresh-b",
                            "accountId": "acct-b",
                        },
                    }
                ],
            },
        }

        def fetch_usage_snapshot(access_token, account_id=None):
            if access_token == "access-a":
                return snapshots_by_account["alice"]
            raise snapshots_by_account["bob"]

        result = account_services.fetch_saved_accounts_usage(
            expected_kind="codex",
            operation_lock=contextlib.nullcontext(),
            list_saved_accounts=lambda kind: [
                {"name": "alice"},
                {"name": "bob"},
            ] if kind == "codex" else [],
            load_saved_account_data=lambda name, expected_kind=None: (f"{name}.json", account_payloads[name], "codex"),
            write_saved_account_data=lambda path, data: writes.append((path, data)),
            fetch_usage_snapshot=fetch_usage_snapshot,
            user_facing_error_cls=UserFacingError,
        )

        self.assertEqual(result.requested_accounts, 2)
        self.assertEqual(result.updated_accounts, 1)
        self.assertEqual(result.failed_accounts, 1)
        self.assertEqual(result.requested_snapshots, 2)
        self.assertEqual(result.updated_snapshots, 1)
        self.assertEqual(result.failed_snapshots, 1)
        self.assertEqual(result.error_messages, ("bob: network down",))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], "alice.json")

    def test_fetch_saved_accounts_usage_raises_when_every_account_fails(self):
        with self.assertRaisesRegex(UserFacingError, "Mass usage fetch failed for codex: alice: boom"):
            account_services.fetch_saved_accounts_usage(
                expected_kind="codex",
                operation_lock=contextlib.nullcontext(),
                list_saved_accounts=lambda kind: [{"name": "alice"}] if kind == "codex" else [],
                load_saved_account_data=lambda name, expected_kind=None: (
                    "alice.json",
                    {
                        "name": "alice",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "access_token": "access-a",
                                    "refresh_token": "refresh-a",
                                    "accountId": "acct-a",
                                },
                            }
                        ],
                    },
                    "codex",
                ),
                write_saved_account_data=lambda path, data: None,
                fetch_usage_snapshot=lambda access_token, account_id=None: (_ for _ in ()).throw(RuntimeError("boom")),
                user_facing_error_cls=UserFacingError,
            )


class RefactorAccountServicesTests(unittest.TestCase):
    def test_account_services_entry_key_parser_tolerates_non_secret_keys(self):
        self.assertEqual(account_services._extension_id_from_entry_key("window.zoomLevel"), "")
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


if __name__ == "__main__":
    unittest.main()

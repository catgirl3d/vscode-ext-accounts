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
        refresh_module = SimpleNamespace(
            OAuthRefreshError=DummyOAuthRefreshError,
            collect_refreshable_entries=lambda entries: [],
            refresh_saved_entries=lambda entries: (_ for _ in ()).throw(DummyOAuthRefreshError("refresh failed")),
            current_time_iso=lambda: "2026-05-15T12:00:00Z",
        )

        with self.assertRaisesRegex(UserFacingError, "Refresh failed for 'alice': refresh failed"):
            account_services.refresh_saved_account(
                "alice",
                operation_lock=contextlib.nullcontext(),
                load_saved_account_data=lambda name: ("account.json", {"entries": []}, "ide"),
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
                        "entries": [],
                        "refresh_status": oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR,
                        "refresh_error": "refresh failed",
                        "refresh_error_at": "2026-05-15T12:00:00Z",
                    },
                )
            ],
        )

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

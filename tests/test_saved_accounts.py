from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vscode_inject import saved_accounts


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2024, 1, 2, 3, 4, 5, tzinfo=tz)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SavedAccountsTests(TempDirTestCase):
    def test_write_saved_account_batch_updates_multiple_files(self):
        first = self.root / "accounts" / "first.json"
        second = self.root / "accounts" / "second.json"
        self.write_json(first, {"name": "first", "value": 1})
        self.write_json(second, {"name": "second", "value": 2})

        saved_accounts.write_saved_account_batch(
            {
                str(first): {"name": "first", "value": 10},
                str(second): {"name": "second", "value": 20},
            }
        )

        self.assertEqual(self.read_json(first), {"name": "first", "value": 10})
        self.assertEqual(self.read_json(second), {"name": "second", "value": 20})

    def test_write_saved_account_batch_rolls_back_replaced_files_on_failure(self):
        first = self.root / "accounts" / "first.json"
        second = self.root / "accounts" / "second.json"
        self.write_json(first, {"name": "first", "value": 1})
        self.write_json(second, {"name": "second", "value": 2})
        real_replace = os.replace
        replace_calls = {"count": 0}

        def flaky_replace(src: str, dst: str):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_batch(
                    {
                        str(first): {"name": "first", "value": 10},
                        str(second): {"name": "second", "value": 20},
                    }
                )

        self.assertEqual(self.read_json(first), {"name": "first", "value": 1})
        self.assertEqual(self.read_json(second), {"name": "second", "value": 2})

    def test_rename_saved_account_moves_file_and_updates_embedded_name(self):
        accounts_dir = self.root / "accounts"
        source = accounts_dir / "alice.json"
        self.write_json(source, {"name": "alice", "kind": "ide", "entries": []})

        path, data, kind = saved_accounts.rename_saved_account(
            str(accounts_dir),
            "codex://openai",
            "alice",
            "alice_renamed",
            expected_kind="ide",
        )

        self.assertEqual(kind, "ide")
        self.assertEqual(data["name"], "alice_renamed")
        self.assertEqual(path, str(accounts_dir / "alice_renamed.json"))
        self.assertFalse(source.exists())
        self.assertEqual(
            self.read_json(accounts_dir / "alice_renamed.json"),
            {"name": "alice_renamed", "kind": "ide", "entries": []},
        )

    def test_rename_saved_account_rejects_existing_target_name(self):
        accounts_dir = self.root / "accounts"
        self.write_json(accounts_dir / "alice.json", {"name": "alice", "kind": "ide", "entries": []})
        self.write_json(accounts_dir / "bob.json", {"name": "bob", "kind": "codex", "entries": []})

        with self.assertRaisesRegex(ValueError, "Account 'bob' already exists as codex"):
            saved_accounts.rename_saved_account(
                str(accounts_dir),
                "codex://openai",
                "alice",
                "bob",
                expected_kind="ide",
            )

        self.assertTrue((accounts_dir / "alice.json").exists())
        self.assertTrue((accounts_dir / "bob.json").exists())

    def test_saved_account_name_validation_rejects_path_unsafe_names(self):
        invalid_names = ["", "..", "team/name", r"team\name", "con", "name:", "name."]

        for name in invalid_names:
            with self.assertRaises(ValueError, msg=name):
                saved_accounts.normalize_account_name(name)

    def test_write_and_delete_saved_account_use_normalized_validated_name(self):
        accounts_dir = self.root / "accounts"

        path = saved_accounts.write_account_file(
            str(accounts_dir),
            "codex://openai",
            "  alice account  ",
            "ide",
            "kilocode",
            [],
        )

        written_path = Path(path)
        self.assertEqual(written_path.name, "alice account.json")
        self.assertEqual(self.read_json(written_path)["name"], "alice account")

        saved_accounts.delete_saved_account(
            str(accounts_dir),
            " alice account ",
            "codex://openai",
            expected_kind="ide",
        )
        self.assertFalse(written_path.exists())

    def test_delete_saved_account_rejects_wrong_expected_kind_without_removing_file(self):
        accounts_dir = self.root / "accounts"
        path = accounts_dir / "alice.json"
        self.write_json(path, {"name": "alice", "kind": "codex", "entries": []})

        with self.assertRaises(saved_accounts.SavedAccountKindMismatchError) as ctx:
            saved_accounts.delete_saved_account(
                str(accounts_dir),
                "alice",
                "codex://openai",
                expected_kind="ide",
            )

        self.assertEqual(ctx.exception.actual_kind, "codex")
        self.assertTrue(path.exists())

    def test_rename_saved_account_rolls_back_when_rewrite_fails(self):
        accounts_dir = self.root / "accounts"
        source = accounts_dir / "alice.json"
        self.write_json(source, {"name": "alice", "kind": "ide", "entries": []})

        with patch("vscode_inject.saved_accounts.write_saved_account_data", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                saved_accounts.rename_saved_account(
                    str(accounts_dir),
                    "codex://openai",
                    "alice",
                    "alice_renamed",
                    expected_kind="ide",
                )

        self.assertTrue(source.exists())
        self.assertFalse((accounts_dir / "alice_renamed.json").exists())
        self.assertEqual(self.read_json(source), {"name": "alice", "kind": "ide", "entries": []})

    def test_cleanup_paths_ignore_missing_temp_files(self):
        target = self.root / "accounts" / "broken.json"

        with patch("vscode_inject.saved_accounts.json.dump", side_effect=RuntimeError("dump failed")):
            with patch("vscode_inject.saved_accounts.os.unlink", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "dump failed"):
                    saved_accounts._stage_saved_account_data(str(target), {"name": "broken"})

        existing = self.root / "accounts" / "existing.json"
        self.write_json(existing, {"name": "existing", "value": 1})
        with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
            with patch("vscode_inject.saved_accounts.os.unlink", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    saved_accounts._restore_account_file_bytes(str(existing), b'{"name":"restored","value":2}')

        staged_path = self.root / "accounts" / "missing-stage.json"
        target_path = self.root / "accounts" / "target.json"
        with patch("vscode_inject.saved_accounts._stage_saved_account_data", return_value=str(staged_path)):
            with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
                with patch("vscode_inject.saved_accounts.os.unlink", side_effect=FileNotFoundError):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        saved_accounts.write_saved_account_batch([(str(target_path), {"name": "target"})])


class SavedAccountsModuleTests(TempDirTestCase):
    codex_key = "codex://auth"

    def test_saved_account_kind_listing_and_filters(self):
        accounts_dir = self.root / "accounts"
        self.write_json(accounts_dir / "ide.json", {"kind": "ide", "entries": [{"key": "secret://ide", "value": {}}]})
        self.write_json(accounts_dir / "codex.json", {"entries": [{"key": self.codex_key, "value": {}}]})
        self.write_text(accounts_dir / "broken.json", "{")

        self.assertEqual(saved_accounts.saved_account_kind({"kind": "codex"}, self.codex_key), "codex")
        self.assertEqual(saved_accounts.saved_account_kind({"entries": [{"key": self.codex_key}]}, self.codex_key), "codex")
        self.assertEqual(saved_accounts.saved_account_kind({"entries": [{"key": "other"}]}, self.codex_key), "ide")

        records = saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key)
        by_name = {record["name"]: record for record in records}

        self.assertTrue(by_name["ide"]["readable"])
        self.assertEqual(by_name["ide"]["kind"], "ide")
        self.assertTrue(by_name["codex"]["readable"])
        self.assertEqual(by_name["codex"]["kind"], "codex")
        self.assertFalse(by_name["broken"]["readable"])
        self.assertIsNone(by_name["broken"]["kind"])
        self.assertIsNone(by_name["broken"]["data"])

        self.assertEqual(
            [record["name"] for record in saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key, kind="ide")],
            ["ide"],
        )
        self.assertEqual(
            [record["name"] for record in saved_accounts.list_saved_accounts(str(accounts_dir), self.codex_key, kind="codex")],
            ["codex"],
        )

    def test_load_saved_account_and_write_account_file_validate_kinds(self):
        accounts_dir = self.root / "accounts"
        self.write_json(accounts_dir / "codex.json", {"entries": [{"key": self.codex_key, "value": {}}]})
        self.write_text(accounts_dir / "broken.json", "{")
        self.write_json(accounts_dir / "ide.json", {"kind": "ide", "entries": []})

        with self.assertRaises(FileNotFoundError):
            saved_accounts.load_saved_account(str(accounts_dir), "missing", self.codex_key)

        path, data, kind = saved_accounts.load_saved_account(str(accounts_dir), "codex", self.codex_key, expected_kind="codex")
        self.assertEqual(Path(path), accounts_dir / "codex.json")
        self.assertEqual(kind, "codex")
        self.assertEqual(data["entries"][0]["key"], self.codex_key)

        with self.assertRaises(saved_accounts.SavedAccountKindMismatchError) as ctx:
            saved_accounts.load_saved_account(str(accounts_dir), "codex", self.codex_key, expected_kind="ide")
        self.assertEqual(ctx.exception.actual_kind, "codex")

        with self.assertRaisesRegex(ValueError, "Cannot overwrite unreadable account file"):
            saved_accounts.write_account_file(str(accounts_dir), self.codex_key, "broken", "ide", "kilocode", [])

        with self.assertRaisesRegex(ValueError, "already exists as ide"):
            saved_accounts.write_account_file(str(accounts_dir), self.codex_key, "ide", "codex", "codex", [])

        with patch("vscode_inject.saved_accounts.datetime.datetime", FixedDateTime):
            out = saved_accounts.write_account_file(
                str(accounts_dir),
                self.codex_key,
                "fresh",
                "ide",
                "kilocode",
                [{"key": "secret://ide", "value": {"accountId": "acct-1"}}],
            )

        written = self.read_json(Path(out))
        self.assertEqual(written["name"], "fresh")
        self.assertEqual(written["kind"], "ide")
        self.assertEqual(written["ext"], "kilocode")
        self.assertEqual(written["saved_at"], "2024-01-02T03:04:05")

    def test_write_saved_account_data_removes_staged_file_on_replace_failure(self):
        target = self.root / "accounts" / "new.json"

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_data(str(target), {"name": "new"})

        self.assertTrue(target.parent.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_saved_account_helpers_clean_up_temp_files_on_failure_paths(self):
        target_dir = self.root / "accounts"
        target = target_dir / "target.json"

        with patch("vscode_inject.saved_accounts.json.dump", side_effect=RuntimeError("dump failed")):
            with self.assertRaisesRegex(RuntimeError, "dump failed"):
                saved_accounts._stage_saved_account_data(str(target), {"name": "broken"})

        self.assertEqual(list(target_dir.iterdir()), [])

        missing = target_dir / "missing.json"
        saved_accounts._restore_account_file_bytes(str(missing), None)
        self.assertFalse(missing.exists())

        existing = target_dir / "existing.json"
        self.write_json(existing, {"name": "existing", "value": 1})
        with patch("vscode_inject.saved_accounts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts._restore_account_file_bytes(str(existing), b'{"name":"restored","value":2}')

        self.assertEqual(self.read_json(existing), {"name": "existing", "value": 1})
        self.assertEqual(sorted(path.name for path in target_dir.iterdir()), ["existing.json"])

    def test_write_saved_account_data_ignores_missing_staged_file_and_empty_batch(self):
        target = self.root / "accounts" / "late-failure.json"

        def delete_and_fail(src: str, dst: str):
            os.unlink(src)
            raise OSError("replace failed")

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=delete_and_fail):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_data(str(target), {"name": "late-failure"})

        self.assertEqual(list(target.parent.iterdir()), [])
        saved_accounts.write_saved_account_batch({})

    def test_write_saved_account_batch_rolls_back_new_files_when_replace_fails(self):
        created = self.root / "accounts" / "created.json"
        existing = self.root / "accounts" / "existing.json"
        self.write_json(existing, {"name": "existing", "value": 1})
        real_replace = os.replace
        replace_calls = {"count": 0}

        def flaky_replace(src: str, dst: str):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(OSError, "replace failed"):
                saved_accounts.write_saved_account_batch(
                    [
                        (str(created), {"name": "created", "value": 10}),
                        (str(existing), {"name": "existing", "value": 20}),
                    ]
                )

        self.assertFalse(created.exists())
        self.assertEqual(self.read_json(existing), {"name": "existing", "value": 1})

    def test_write_saved_account_batch_surfaces_rollback_errors(self):
        first = self.root / "accounts" / "first.json"
        second = self.root / "accounts" / "second.json"
        self.write_json(first, {"name": "first", "value": 1})
        self.write_json(second, {"name": "second", "value": 2})
        real_replace = os.replace
        replace_calls = {"count": 0}

        def flaky_replace(src: str, dst: str):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        with patch("vscode_inject.saved_accounts.os.replace", side_effect=flaky_replace):
            with patch("vscode_inject.saved_accounts._restore_account_file_bytes", side_effect=OSError("rollback failed")):
                with self.assertRaisesRegex(RuntimeError, "rollback failed"):
                    saved_accounts.write_saved_account_batch(
                        [
                            (str(first), {"name": "first", "value": 10}),
                            (str(second), {"name": "second", "value": 20}),
                        ]
                    )


if __name__ == "__main__":
    unittest.main()

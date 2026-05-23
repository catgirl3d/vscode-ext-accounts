from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vscode_inject import saved_accounts


class SavedAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()

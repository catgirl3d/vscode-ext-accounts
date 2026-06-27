# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

from vscode_inject import parse_vscdb as db


def create_state_db(path: Path, rows: list[tuple[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        if rows:
            con.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", rows)
        con.commit()
    finally:
        con.close()


def read_state_rows(path: Path) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT key, value FROM ItemTable ORDER BY key"))
    finally:
        con.close()


def oauth_key(ext_id: str) -> str:
    return f'secret://{{"extensionId":"{ext_id}","key":"{db.OAUTH_KEY}"}}'


class ParseVscdbTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def patch_db(self, name: str, value):
        patcher = patch.object(db, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_file(self, path: Path, content: str = "stub") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def capture_output(self, fn, *args, **kwargs) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            fn(*args, **kwargs)
        return output.getvalue()

    def test_account_fingerprint_prefers_refresh_then_account_id(self):
        self.assertEqual(
            db.account_fingerprint({"refresh_token": "refresh-1", "accountId": "acct-1"}),
            hashlib.sha256(b"refresh-1").hexdigest(),
        )
        self.assertEqual(
            db.account_fingerprint({"accountId": "acct-2"}),
            hashlib.sha256(b"acct-2").hexdigest(),
        )
        self.assertIsNone(db.account_fingerprint({"expires": 123}))
        self.assertIsNone(db.account_fingerprint("not-a-dict"))

    def test_account_fingerprint_supports_nested_tokens_and_non_string_values(self):
        self.assertEqual(
            db.account_fingerprint({"tokens": {"refresh_token": "nested-refresh"}}),
            hashlib.sha256(b"nested-refresh").hexdigest(),
        )
        self.assertEqual(
            db.account_fingerprint({"tokens": {"account_id": 12345}}),
            hashlib.sha256(b"12345").hexdigest(),
        )

    def test_set_ide_updates_selected_paths_and_rejects_unknown_name(self):
        self.patch_db("CURRENT_IDE", db.CURRENT_IDE)
        self.patch_db("DB_PATH", db.DB_PATH)
        self.patch_db("LOCAL_STATE_PATH", db.LOCAL_STATE_PATH)
        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                },
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                },
            },
        )

        db.set_ide("antigravity")

        self.assertEqual(db.CURRENT_IDE, "antigravity")
        self.assertEqual(db.DB_PATH, "db-antigravity")
        self.assertEqual(db.LOCAL_STATE_PATH, "local-antigravity")

        with self.assertRaisesRegex(ValueError, "Unknown IDE"):
            db.set_ide("cursor")

    def test_resolve_ide_executable_path_prefers_env_override(self):
        env_exe = self.root / "env" / "Code.exe"
        fallback_exe = self.root / "fallback" / "Code.exe"
        self.write_file(env_exe)
        self.write_file(fallback_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_env": "VSCODE_INJECT_VSCODE_EXE",
                    "launch_paths": [str(fallback_exe)],
                }
            },
        )

        with patch.dict("os.environ", {"VSCODE_INJECT_VSCODE_EXE": str(env_exe)}, clear=False):
            self.assertEqual(db.resolve_ide_executable_path("vscode"), str(env_exe))

    def test_resolve_ide_executable_path_falls_back_to_windows_app_paths(self):
        registry_exe = self.root / "registry" / "Antigravity.exe"
        self.write_file(registry_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                    "launch_paths": [str(self.root / "missing" / "Antigravity.exe")],
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [str(registry_exe)] if exe_name == "Antigravity.exe" else [])

        self.assertEqual(db.resolve_ide_executable_path("antigravity"), str(registry_exe))

    def test_resolve_ide_executable_path_falls_back_to_path_commands(self):
        path_exe = self.root / "path" / "code.cmd"
        self.write_file(path_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_commands": ["code"],
                    "launch_paths": [str(self.root / "missing" / "Code.exe")],
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [])
        self.patch_db("_path_command_candidates", lambda command_names: [str(path_exe)] if "code" in command_names else [])

        self.assertEqual(db.resolve_ide_executable_path("vscode"), str(path_exe))

    def test_resolve_ide_executable_path_accepts_iterable_launch_config_values(self):
        path_exe = self.root / "iterable" / "Code.exe"
        self.write_file(path_exe)

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                    "launch_commands": {"code"},
                    "launch_paths": {str(path_exe)},
                }
            },
        )
        self.patch_db("_windows_app_path_candidates", lambda exe_name: [])
        self.patch_db("_path_command_candidates", lambda command_names: [])

        self.assertEqual(db.resolve_ide_executable_path("vscode"), str(path_exe))

    def test_launch_ide_starts_resolved_executable(self):
        exe_path = str(self.root / "launch" / "Code.exe")

        self.patch_db(
            "IDE_PATHS",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "db-vscode",
                    "local_state": "local-vscode",
                    "process": "Code.exe",
                }
            },
        )
        self.patch_db("resolve_ide_executable_path", lambda ide=None: exe_path)

        with patch("subprocess.Popen") as popen:
            message = db.launch_ide("vscode")

        popen.assert_called_once_with([exe_path], close_fds=True)
        self.assertEqual(message, "Started VSCode")

    def test_launch_ide_reports_checked_paths_when_executable_missing(self):
        checked_paths = [r"%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe"]

        self.patch_db(
            "IDE_PATHS",
            {
                "antigravity": {
                    "label": "Antigravity",
                    "db": "db-antigravity",
                    "local_state": "local-antigravity",
                    "process": "Antigravity.exe",
                    "launch_env": "VSCODE_INJECT_ANTIGRAVITY_EXE",
                    "launch_paths": checked_paths,
                }
            },
        )
        self.patch_db("resolve_ide_executable_path", lambda ide=None: None)
        self.patch_db("ide_executable_candidates", lambda ide=None: list(checked_paths))

        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Antigravity executable not found") as exc:
                db.launch_ide("antigravity")

        self.assertIn("%LOCALAPPDATA%\\Programs\\Antigravity\\Antigravity.exe", str(exc.exception))
        self.assertIn("VSCODE_INJECT_ANTIGRAVITY_EXE", str(exc.exception))

    def test_normalize_ide_ext_selection_preserves_current_contract(self):
        names, label = db._normalize_ide_ext_selection(None)
        self.assertEqual(names, ["kilocode", "roo-cline"])
        self.assertEqual(label, "both")

        names, label = db._normalize_ide_ext_selection(["kilo-new", "both"])
        self.assertEqual(names, ["kilo-new", "kilocode", "roo-cline"])
        self.assertEqual(label, "kilo-new+kilocode+roo-cline")

        with self.assertRaisesRegex(ValueError, "Unknown extension"):
            db._normalize_ide_ext_selection("unknown-ext")

    def test_import_ide_account_from_json_string_rejects_invalid_json(self):
        with self.assertRaisesRegex(db.UserFacingError, "invalid JSON"):
            db.import_ide_account_from_json_string("{not-json", "alice", ["kilocode"])


    def test_delete_saved_account_maps_kind_mismatch_error(self):
        accounts_dir = self.root / "accounts"
        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        with patch.object(
            db.saved_store,
            "delete_saved_account",
            side_effect=db.saved_store.SavedAccountKindMismatchError("codex"),
        ) as delete_saved:
            with self.assertRaisesRegex(db.AccountKindMismatchError, "expected 'ide'"):
                db.delete_saved_account("alice", expected_kind="ide")

        delete_saved.assert_called_once_with(str(accounts_dir), "alice", db.CODEX_KEY, "ide")

    def test_rename_saved_account_maps_value_errors_to_user_facing_error(self):
        accounts_dir = self.root / "accounts"
        self.patch_db("ACCOUNTS_DIR", str(accounts_dir))

        with patch.object(
            db.saved_store,
            "rename_saved_account",
            side_effect=ValueError("Account name contains invalid characters: /"),
        ) as rename_saved:
            with self.assertRaisesRegex(db.UserFacingError, "invalid characters"):
                db.rename_saved_account("alice", "bad/name", expected_kind="ide")

        rename_saved.assert_called_once_with(str(accounts_dir), db.CODEX_KEY, "alice", "bad/name", "ide")

    def test_refresh_saved_account_passes_expected_kind_to_loader(self):
        captured: dict[str, str] = {}

        self.patch_db(
            "_load_saved_account_data",
            lambda name, expected_kind=None: captured.update({"name": name, "expected_kind": expected_kind})
            or ("account.json", {"entries": []}, "ide"),
        )

        def fake_refresh(name, **kwargs):
            self.assertEqual(name, "alice")
            kwargs["load_saved_account_data"]("alice")
            return "ok"

        with patch.object(db.account_services, "refresh_saved_account", side_effect=fake_refresh) as refresh_saved:
            result = db.refresh_saved_account("alice", expected_kind="ide")

        self.assertEqual(result, "ok")
        self.assertEqual(captured, {"name": "alice", "expected_kind": "ide"})
        refresh_saved.assert_called_once()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vscode_inject import ide_context
from vscode_inject import parse_vscdb as db


class IdeContextModuleTests(unittest.TestCase):
    def test_tupled_context_and_candidate_helpers_cover_edge_cases(self):
        self.assertEqual(ide_context._tupled("code"), ("code",))
        self.assertEqual(ide_context._tupled(None), ())
        self.assertEqual(ide_context._tupled(5), ())
        self.assertEqual(ide_context._tupled(["code", 7]), ("code", "7"))

        with self.assertRaisesRegex(ValueError, "Unknown IDE"):
            ide_context.resolve_context("cursor", {})

        context = ide_context.resolve_context(
            "vscode",
            {
                "vscode": {
                    "label": "VSCode",
                    "db": "state.vscdb",
                    "local_state": "Local State",
                    "process": "Code.exe",
                    "launch_env": "CODE_EXE",
                    "launch_commands": ["code"],
                    "launch_paths": ["C:/Code.exe"],
                }
            },
        )
        self.assertEqual(context.launch_commands, ("code",))
        self.assertEqual(context.launch_paths, ("C:/Code.exe",))

        overridden = ide_context.override_context(context, db_path="alt.vscdb")
        self.assertEqual(overridden.db_path, "alt.vscdb")
        self.assertEqual(overridden.local_state_path, "Local State")

        deduped = ide_context.dedupe_candidate_paths(["", ".\\Code.exe", ".\\Code.exe", "./Code.exe"])
        self.assertEqual(len(deduped), 1)

    def test_windows_path_command_launch_and_running_helpers(self):
        class FakeKey:
            def __init__(self, value: str):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        registry_values = {
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe"): "C:/VSCode/Code.exe",
            ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Code.exe"): "C:/VSCode/Code.exe",
        }

        def open_key(hive, subkey):
            value = registry_values.get((hive, subkey))
            if value is None:
                raise OSError("missing")
            return FakeKey(value)

        fake_winreg = SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            HKEY_LOCAL_MACHINE="HKLM",
            OpenKey=open_key,
            QueryValueEx=lambda key, _name: (key.value, 1),
        )

        with patch.object(ide_context, "winreg", fake_winreg):
            self.assertEqual(ide_context.windows_app_path_candidates("Code.exe"), [os.path.normpath("C:/VSCode/Code.exe")])

        with patch("vscode_inject.ide_context.shutil.which", side_effect=lambda name: "C:/bin/code.cmd" if name == "code" else None):
            self.assertEqual(ide_context.path_command_candidates(["", "code", "missing"]), [os.path.normpath("C:/bin/code.cmd")])

        context = ide_context.IDEContext(
            name="vscode",
            label="VSCode",
            process_name="Code.exe",
            launch_env="CODE_EXE",
            launch_commands=("code",),
            launch_paths=("C:/Configured/Code.exe",),
        )
        candidates = ide_context.ide_executable_candidates(
            context,
            environ={"CODE_EXE": "C:/Env/Code.exe"},
            windows_app_path_candidates_fn=lambda exe: ["C:/Registry/Code.exe"] if exe == "Code.exe" else [],
            path_command_candidates_fn=lambda commands: ["C:/Path/code.cmd"] if "code" in commands else [],
        )
        self.assertEqual(
            candidates,
            [
                os.path.normpath("C:/Env/Code.exe"),
                os.path.normpath("C:/Configured/Code.exe"),
                os.path.normpath("C:/Registry/Code.exe"),
                os.path.normpath("C:/Path/code.cmd"),
            ],
        )

        self.assertIsNone(
            ide_context.resolve_ide_executable_path(
                context,
                executable_candidates=lambda ctx: ["C:/missing.exe"],
                isfile=lambda path: False,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "no candidate paths configured"):
            ide_context.launch_ide(
                ide_context.IDEContext(name="plain", label="Plain IDE"),
                resolve_executable_path=lambda ctx: None,
                executable_candidates=lambda ctx: [],
            )

        self.assertFalse(ide_context.is_ide_running(ide_context.IDEContext(name="plain", label="Plain IDE")))
        self.assertTrue(
            ide_context.is_ide_running(
                ide_context.IDEContext(name="vscode", label="VSCode", process_name="Code.exe"),
                run=lambda *args, **kwargs: SimpleNamespace(stdout='"Code.exe","123","Console","1","42 K"'),
            )
        )


class IdeContextImportFallbackTests(unittest.TestCase):
    def test_module_import_sets_winreg_to_none_when_unavailable(self):
        spec = importlib.util.find_spec("vscode_inject.ide_context")
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        real_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "winreg":
                raise ImportError("winreg unavailable")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            spec.loader.exec_module(module)

        self.assertIsNone(module.winreg)


class RefactorIdeContextTests(unittest.TestCase):
    def test_explicit_ide_context_matches_facade_selection_contract(self):
        patcher_id = patch.object(db, "CURRENT_IDE", db.CURRENT_IDE)
        patcher_db = patch.object(db, "DB_PATH", db.DB_PATH)
        patcher_ls = patch.object(db, "LOCAL_STATE_PATH", db.LOCAL_STATE_PATH)
        patcher_id.start()
        patcher_db.start()
        patcher_ls.start()
        self.addCleanup(patcher_id.stop)
        self.addCleanup(patcher_db.stop)
        self.addCleanup(patcher_ls.stop)

        custom_paths = {
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
        }

        with patch.object(db, "IDE_PATHS", custom_paths):
            explicit = ide_context.resolve_context("antigravity", custom_paths)
            db.set_ide("antigravity")
            selected = db._ide_context_for()

            self.assertEqual(db.CURRENT_IDE, explicit.name)
            self.assertEqual(db.DB_PATH, explicit.db_path)
            self.assertEqual(db.LOCAL_STATE_PATH, explicit.local_state_path)
            self.assertEqual(selected.name, explicit.name)
            self.assertEqual(selected.db_path, explicit.db_path)
            self.assertEqual(selected.local_state_path, explicit.local_state_path)

    def test_windows_app_path_candidates_returns_empty_when_winreg_is_unavailable(self):
        with patch.object(ide_context, "winreg", None):
            self.assertEqual(ide_context.windows_app_path_candidates("Code.exe"), [])


if __name__ == "__main__":
    unittest.main()

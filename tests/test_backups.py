from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vscode_inject import backups


class BackupsModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_prewrite_backup_targets_include_codex_when_requested(self):
        targets = backups.prewrite_backup_targets(
            {"vscode": {"label": "VSCode", "db": "db.vscdb", "local_state": "Local State"}},
            "vscode",
            "kilo/auth.json",
            "codex/auth.json",
            include_db=False,
            include_kilo=False,
            include_codex=True,
        )

        self.assertEqual(
            targets,
            [
                {
                    "source": "codex/auth.json",
                    "archive_path": "prewrite/shared/codex/auth.json",
                    "label": "Codex auth.json",
                    "required": False,
                }
            ],
        )

    def test_prewrite_backup_targets_include_omp_db_and_wal_sidecars(self):
        targets = backups.prewrite_backup_targets(
            {"vscode": {"label": "VSCode", "db": "db.vscdb", "local_state": "Local State"}},
            "vscode",
            "kilo/auth.json",
            "codex/auth.json",
            "omp/agent.db",
            include_db=False,
            include_kilo=False,
            include_codex=False,
            include_omp=True,
        )

        self.assertEqual(
            targets,
            [
                {
                    "source": "omp/agent.db",
                    "archive_path": "prewrite/shared/omp/agent.db",
                    "label": "OMP agent.db",
                    "required": False,
                },
                {
                    "source": "omp/agent.db-wal",
                    "archive_path": "prewrite/shared/omp/agent.db-wal",
                    "label": "OMP agent.db-wal",
                    "required": False,
                },
                {
                    "source": "omp/agent.db-shm",
                    "archive_path": "prewrite/shared/omp/agent.db-shm",
                    "label": "OMP agent.db-shm",
                    "required": False,
                },
            ],
        )

    def test_create_backup_archive_appends_zip_extension_and_rejects_empty_targets(self):
        source = self.root / "source.txt"
        self.write_text(source, "backup me")
        messages: list[str] = []

        result = backups.create_backup_archive(
            str(self.root),
            "vscode",
            [{"source": str(source), "archive_path": "state/source.txt", "label": "Source", "required": True}],
            out_path=str(self.root / "archive-no-ext"),
            backup_kind="manual",
            print_fn=messages.append,
        )

        self.assertTrue(result["path"].endswith(".zip"))
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(messages[-2:], [f"Backup saved: {result['path']}", "Files included: 1/1"])

        with self.assertRaisesRegex(RuntimeError, "none of the target files exist"):
            backups.create_backup_archive(
                str(self.root),
                "vscode",
                [{"source": str(self.root / "missing.txt"), "archive_path": "missing.txt", "label": "Missing", "required": False}],
                backup_kind="manual",
                print_fn=lambda _message: None,
            )

    def test_refresh_recovery_snapshot_cleanup_handles_dump_failures_and_unlink_errors(self):
        with patch("vscode_inject.backups.json.dump", side_effect=RuntimeError("dump failed")):
            with self.assertRaisesRegex(RuntimeError, "dump failed"):
                backups.write_refresh_recovery_snapshot(
                    str(self.root),
                    {"account.json": {"entries": []}},
                    subject_label="saved account 'alice'",
                    account_names=("alice",),
                    providers=("openai",),
                    operation="manual-refresh",
                    created_at="2024-01-02T03:04:05",
                )

        recovery_dir = self.root / "backups" / "refresh_recovery"
        self.assertEqual(list(recovery_dir.iterdir()), [])

        messages: list[str] = []
        backups.cleanup_refresh_recovery_snapshot(str(recovery_dir / "missing.json"), print_fn=messages.append)

        snapshot = recovery_dir / "snapshot.json"
        self.write_text(snapshot, "{}")
        with patch("vscode_inject.backups.os.unlink", side_effect=OSError("busy")):
            backups.cleanup_refresh_recovery_snapshot(str(snapshot), print_fn=messages.append)

        self.assertEqual(
            messages,
            [f"WARNING: Failed to remove refresh recovery snapshot {snapshot}: busy"],
        )

    def test_refresh_recovery_snapshot_ignores_missing_temp_file_during_failed_write(self):
        with patch("vscode_inject.backups.json.dump", side_effect=RuntimeError("dump failed")):
            with patch("vscode_inject.backups.os.unlink", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "dump failed"):
                    backups.write_refresh_recovery_snapshot(
                        str(self.root),
                        {"account.json": {"entries": []}},
                        subject_label="saved account 'alice'",
                        account_names=("alice",),
                        providers=("openai",),
                        operation="manual-refresh",
                        created_at="2024-01-02T03:04:05",
                    )


if __name__ == "__main__":
    unittest.main()

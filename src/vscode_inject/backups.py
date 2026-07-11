from __future__ import annotations

import datetime
import json
import os
import tempfile
import zipfile
from typing import Callable, Mapping, Sequence


def backups_dir(project_root: str) -> str:
    path = os.path.join(project_root, "backups")
    os.makedirs(path, exist_ok=True)
    return path


def refresh_recovery_dir(project_root: str) -> str:
    path = os.path.join(backups_dir(project_root), "refresh_recovery")
    os.makedirs(path, exist_ok=True)
    return path


def default_backup_zip_path(project_root: str, prefix: str, current_ide: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(backups_dir(project_root), f"{prefix}_{current_ide.replace(' ', '_')}_{ts}.zip")


def full_backup_targets(
    ide_paths: Mapping[str, Mapping[str, object]],
    current_ide: str,
    kilo_auth_path: str,
    codex_auth_path: str,
    omp_agent_db_path: str | None = None,
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for ide_name, cfg in ide_paths.items():
        label = str(cfg.get("label", ide_name))
        is_current = ide_name == current_ide
        targets.append(
            {
                "source": str(cfg.get("db", "")),
                "archive_path": f"ides/{ide_name}/state.vscdb",
                "label": f"{label} state.vscdb",
                "required": is_current,
            }
        )
        targets.append(
            {
                "source": str(cfg.get("local_state", "")),
                "archive_path": f"ides/{ide_name}/Local State",
                "label": f"{label} Local State",
                "required": is_current,
            }
        )

    targets.append(
        {
            "source": kilo_auth_path,
            "archive_path": "shared/kilo/auth.json",
            "label": "Kilo New auth.json",
            "required": False,
        }
    )
    targets.append(
        {
            "source": codex_auth_path,
            "archive_path": "shared/codex/auth.json",
            "label": "Codex auth.json",
            "required": False,
        }
    )
    if omp_agent_db_path:
        targets.extend(
            [
                {
                    "source": omp_agent_db_path,
                    "archive_path": "shared/omp/agent.db",
                    "label": "OMP agent.db",
                    "required": False,
                },
                {
                    "source": omp_agent_db_path + "-wal",
                    "archive_path": "shared/omp/agent.db-wal",
                    "label": "OMP agent.db-wal",
                    "required": False,
                },
                {
                    "source": omp_agent_db_path + "-shm",
                    "archive_path": "shared/omp/agent.db-shm",
                    "label": "OMP agent.db-shm",
                    "required": False,
                },
            ]
        )
    return targets


def prewrite_backup_targets(
    ide_paths: Mapping[str, Mapping[str, object]],
    current_ide: str,
    kilo_auth_path: str,
    codex_auth_path: str,
    omp_agent_db_path: str | None = None,
    *,
    include_db: bool,
    include_kilo: bool,
    include_codex: bool,
    include_omp: bool = False,
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    if include_db:
        cfg = ide_paths[current_ide]
        label = str(cfg.get("label", current_ide))
        targets.append(
            {
                "source": str(cfg.get("db", "")),
                "archive_path": f"prewrite/{current_ide}/state.vscdb",
                "label": f"{label} state.vscdb",
                "required": True,
            }
        )
        targets.append(
            {
                "source": str(cfg.get("local_state", "")),
                "archive_path": f"prewrite/{current_ide}/Local State",
                "label": f"{label} Local State",
                "required": True,
            }
        )
    if include_kilo:
        targets.append(
            {
                "source": kilo_auth_path,
                "archive_path": "prewrite/shared/kilo/auth.json",
                "label": "Kilo New auth.json",
                "required": False,
            }
        )
    if include_codex:
        targets.append(
            {
                "source": codex_auth_path,
                "archive_path": "prewrite/shared/codex/auth.json",
                "label": "Codex auth.json",
                "required": False,
            }
        )
    if include_omp and omp_agent_db_path:
        targets.extend(
            [
                {
                    "source": omp_agent_db_path,
                    "archive_path": "prewrite/shared/omp/agent.db",
                    "label": "OMP agent.db",
                    "required": False,
                },
                {
                    "source": omp_agent_db_path + "-wal",
                    "archive_path": "prewrite/shared/omp/agent.db-wal",
                    "label": "OMP agent.db-wal",
                    "required": False,
                },
                {
                    "source": omp_agent_db_path + "-shm",
                    "archive_path": "prewrite/shared/omp/agent.db-shm",
                    "label": "OMP agent.db-shm",
                    "required": False,
                },
            ]
        )
    return targets


def create_backup_archive(
    project_root: str,
    current_ide: str,
    targets: Sequence[Mapping[str, object]],
    out_path: str | None = None,
    *,
    backup_kind: str,
    note: str | None = None,
    fail_on_required_missing: bool = False,
    print_fn: Callable[[str], None] = print,
) -> dict:
    if out_path is None:
        out_path = default_backup_zip_path(project_root, backup_kind, current_ide)
    elif not out_path.lower().endswith(".zip"):
        out_path = out_path + ".zip"

    manifest = {
        "version": 2,
        "kind": backup_kind,
        "created_at": datetime.datetime.now().isoformat(),
        "current_ide": current_ide,
        "note": note,
        "files": [],
        "warnings": [],
    }

    for target in targets:
        source = str(target["source"])
        archive_path = str(target["archive_path"])
        label = str(target["label"])
        exists = os.path.exists(source)
        entry = {
            "label": label,
            "source": source,
            "archive_path": archive_path,
            "exists": exists,
            "required": bool(target.get("required", False)),
        }
        if exists:
            stat = os.stat(source)
            entry["size"] = stat.st_size
            entry["modified_at"] = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
        manifest["files"].append(entry)

    missing_entries = [entry for entry in manifest["files"] if not entry["exists"]]
    required_missing_entries = [entry for entry in missing_entries if entry["required"]]
    optional_missing_entries = [entry for entry in missing_entries if not entry["required"]]
    included = sum(1 for entry in manifest["files"] if entry["exists"])
    total = len(manifest["files"])

    if required_missing_entries:
        manifest["warnings"].append(f"Missing {len(required_missing_entries)} required target file(s)")
    if optional_missing_entries:
        manifest["warnings"].append(f"Skipped {len(optional_missing_entries)} optional missing file(s)")

    if required_missing_entries:
        warning = manifest["warnings"][0]
        print_fn(f"WARNING: {warning}")
        for entry in required_missing_entries:
            print_fn(f"  - {entry['label']}: {entry['source']}")

    if optional_missing_entries:
        print_fn(f"INFO: Skipped {len(optional_missing_entries)} optional missing file(s)")
        for entry in optional_missing_entries:
            print_fn(f"  - {entry['label']}: {entry['source']}")

    if included == 0:
        raise RuntimeError("Backup failed: none of the target files exist.")
    if fail_on_required_missing and required_missing_entries:
        raise RuntimeError("Backup failed: required target files are missing.")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in manifest["files"]:
            if entry["exists"]:
                zf.write(entry["source"], entry["archive_path"])
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    print_fn(f"Backup saved: {out_path}")
    print_fn(f"Files included: {included}/{total}")
    return {
        "path": out_path,
        "included": included,
        "total": total,
        "missing": missing_entries,
        "required_missing": required_missing_entries,
        "optional_missing": optional_missing_entries,
    }


def normalize_saved_account_updates(
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
) -> list[tuple[str, dict]]:
    return list(updates.items()) if isinstance(updates, Mapping) else list(updates)


def write_refresh_recovery_snapshot(
    project_root: str,
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
    *,
    subject_label: str,
    account_names: Sequence[str],
    providers: Sequence[str],
    operation: str,
    created_at: str,
) -> str:
    items = normalize_saved_account_updates(updates)
    payload = {
        "version": 1,
        "kind": "saved-account-refresh-recovery",
        "created_at": created_at,
        "operation": operation,
        "subject": subject_label,
        "account_names": list(account_names),
        "providers": list(providers),
        "records": [{"path": path, "data": data} for path, data in items],
    }

    fd, recovery_path = tempfile.mkstemp(
        prefix=f"{operation}-",
        suffix=".json",
        dir=refresh_recovery_dir(project_root),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(recovery_path)
        except FileNotFoundError:
            pass
        raise
    return recovery_path


def cleanup_refresh_recovery_snapshot(path: str, *, print_fn: Callable[[str], None] = print) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print_fn(f"WARNING: Failed to remove refresh recovery snapshot {path}: {exc}")


def refreshed_credentials_persistence_message(
    *,
    subject_label: str,
    save_error: Exception,
    recovery_path: str | None,
    recovery_error: Exception | None,
) -> str:
    base = f"Renewed tokens for {subject_label}, but failed to persist the new credentials locally: {save_error}."
    if recovery_path:
        return (
            f"{base} A recovery snapshot was saved to {recovery_path}. "
            "The previous saved snapshot may already be invalid, so manual sign-in may be required if the recovery snapshot cannot be restored."
        )
    recovery_suffix = f" ({recovery_error})" if recovery_error is not None else ""
    return (
        f"{base} No recovery snapshot could be written{recovery_suffix}. "
        "The previous saved snapshot may already be invalid, and manual sign-in may be required."
    )

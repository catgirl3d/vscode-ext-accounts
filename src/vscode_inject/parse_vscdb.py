"""
Parse VSCode state.vscdb and find Kilocode / ChatGPT related entries.
On Windows, encrypted values (v10 prefix) are decrypted via DPAPI + AES-256-GCM.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from typing import Callable, Mapping, Sequence, TypeVar

from . import account_services
from . import backups
from . import codex_accounts as codex_store
from . import ide_context
from . import kilo_new_accounts
from . import omp_openai_accounts as omp_store
from . import oauth_refresh
from . import saved_accounts as saved_store
from . import state_db

IDE_PATHS = ide_context.default_ide_paths()
CURRENT_IDE = "vscode"
_INITIAL_CONTEXT = ide_context.resolve_context(CURRENT_IDE, IDE_PATHS)
DB_PATH = _INITIAL_CONTEXT.db_path
LOCAL_STATE_PATH = _INITIAL_CONTEXT.local_state_path


class UserFacingError(RuntimeError):
    """Expected backend error that can be shown directly in the GUI."""


class SavedAccountError(UserFacingError):
    """Base class for saved-account selection and validation errors."""


class AccountNotFoundError(SavedAccountError):
    """Raised when a saved account name does not exist."""


class AccountKindMismatchError(SavedAccountError):
    """Raised when a saved account exists, but not for the requested target kind."""


SavedAccountStoreResult = TypeVar("SavedAccountStoreResult")


class SavedAccountRefreshError(UserFacingError):
    """Raised when a manual saved-account refresh fails in an expected way."""


class RenewedCredentialsPersistenceError(UserFacingError):
    """Raised when renewed credentials were obtained but could not be saved locally."""

    def __init__(
        self,
        message: str,
        *,
        recovery_path: str | None = None,
        recovery_error: Exception | None = None,
    ):
        super().__init__(message)
        self.recovery_path = recovery_path
        self.recovery_error = recovery_error


SAVED_ACCOUNT_REFRESH_LOCK = threading.RLock()


def _ide_context_for(name: str | None = None) -> ide_context.IDEContext:
    ide_name = name or CURRENT_IDE
    context = ide_context.resolve_context(ide_name, IDE_PATHS)
    if ide_name == CURRENT_IDE:
        context = ide_context.override_context(context, db_path=DB_PATH, local_state_path=LOCAL_STATE_PATH)
    return context


def set_ide(name: str):
    global DB_PATH, LOCAL_STATE_PATH, CURRENT_IDE
    context = ide_context.resolve_context(name, IDE_PATHS)
    CURRENT_IDE = context.name
    DB_PATH = context.db_path
    LOCAL_STATE_PATH = context.local_state_path


def _dedupe_candidate_paths(paths: list[str]) -> list[str]:
    return ide_context.dedupe_candidate_paths(paths)


def _windows_app_path_candidates(exe_name: str) -> list[str]:
    return ide_context.windows_app_path_candidates(exe_name)


def _path_command_candidates(command_names: list[str]) -> list[str]:
    return ide_context.path_command_candidates(command_names)


def ide_executable_candidates(ide: str | None = None) -> list[str]:
    return ide_context.ide_executable_candidates(
        _ide_context_for(ide),
        windows_app_path_candidates_fn=_windows_app_path_candidates,
        path_command_candidates_fn=_path_command_candidates,
    )


def resolve_ide_executable_path(ide: str | None = None) -> str | None:
    return ide_context.resolve_ide_executable_path(
        _ide_context_for(ide),
        executable_candidates=lambda context: ide_executable_candidates(context.name),
    )


def launch_ide(ide: str | None = None) -> str:
    return ide_context.launch_ide(
        _ide_context_for(ide),
        resolve_executable_path=lambda context: resolve_ide_executable_path(context.name),
        executable_candidates=lambda context: ide_executable_candidates(context.name),
    )


def get_aes_key(local_state_path: str | None = None):
    return state_db.get_aes_key(local_state_path or LOCAL_STATE_PATH)


def decrypt_value(raw: bytes, aes_key: bytes | None) -> str:
    return state_db.decrypt_value(raw, aes_key)


def is_ide_running(ide: str | None = None) -> bool:
    return ide_context.is_ide_running(_ide_context_for(ide))


def guard_vscode_closed():
    if is_ide_running():
        raise UserFacingError(
            f"ERROR: {_ide_context_for().label} is running. Close it before making changes."
        )


def encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
    return state_db.encrypt_value(plaintext, aes_key)


def _decode_entry(value, aes_key):
    return state_db.decode_entry(value, aes_key, decrypt_value_fn=decrypt_value)


PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.dirname(PACKAGE_ROOT)
PROJECT_ROOT = os.path.dirname(SRC_ROOT)


def _backups_dir() -> str:
    return backups.backups_dir(PROJECT_ROOT)


def _refresh_recovery_dir() -> str:
    return backups.refresh_recovery_dir(PROJECT_ROOT)


def _default_backup_zip_path(prefix: str) -> str:
    return backups.default_backup_zip_path(PROJECT_ROOT, prefix, CURRENT_IDE)


def _full_backup_targets() -> list[dict[str, object]]:
    return backups.full_backup_targets(IDE_PATHS, CURRENT_IDE, KILO_AUTH_PATH, CODEX_AUTH_PATH, OMP_AGENT_DB_PATH)


def _prewrite_backup_targets(*, include_db: bool, include_kilo: bool, include_codex: bool, include_omp: bool = False) -> list[dict[str, object]]:
    return backups.prewrite_backup_targets(
        IDE_PATHS,
        CURRENT_IDE,
        KILO_AUTH_PATH,
        CODEX_AUTH_PATH,
        OMP_AGENT_DB_PATH,
        include_db=include_db,
        include_kilo=include_kilo,
        include_codex=include_codex,
        include_omp=include_omp,
    )


def _create_backup_archive(
    targets: list[dict[str, object]],
    out_path: str | None = None,
    *,
    backup_kind: str,
    note: str | None = None,
    fail_on_required_missing: bool = False,
) -> dict:
    return backups.create_backup_archive(
        PROJECT_ROOT,
        CURRENT_IDE,
        targets,
        out_path,
        backup_kind=backup_kind,
        note=note,
        fail_on_required_missing=fail_on_required_missing,
    )


def create_prewrite_backup(
    *,
    include_db: bool = False,
    include_kilo: bool = False,
    include_codex: bool = False,
    include_omp: bool = False,
    note: str | None = None,
) -> dict | None:
    targets = _prewrite_backup_targets(
        include_db=include_db,
        include_kilo=include_kilo,
        include_codex=include_codex,
        include_omp=include_omp,
    )
    if not targets:
        return None

    existing_targets = [target for target in targets if os.path.exists(str(target["source"]))]
    has_required_targets = any(bool(target.get("required", False)) for target in targets)
    if not existing_targets and not has_required_targets:
        print("INFO: Skipped pre-write backup because the target file does not exist yet")
        for target in targets:
            print(f"  - {target['label']}: {target['source']}")
        return None

    return _create_backup_archive(
        targets,
        backup_kind="prewrite",
        note=note,
        fail_on_required_missing=True,
    )


def _write_entries_to_current_db(entries: Sequence[dict], *, aes_key: bytes | None = None) -> tuple[int, int]:
    if not os.path.exists(DB_PATH):
        raise UserFacingError(f"DB not found: {DB_PATH}")

    resolved_aes_key = aes_key if aes_key is not None else get_aes_key()
    if resolved_aes_key is None:
        raise UserFacingError("ERROR: Cannot get AES key — cannot encrypt values.")

    return state_db.write_entries_to_db(
        DB_PATH,
        entries,
        resolved_aes_key,
        encrypt_value_fn=encrypt_value,
    )


def _apply_entries_to_current_db(
    entries: Sequence[dict],
    *,
    source_label: str,
    aes_key: bytes | None = None,
) -> tuple[int, int]:
    print(f"Backup: {source_label}")
    print(f"Entries to restore: {len(entries)}")
    print(f"Target DB: {DB_PATH}")
    print()

    if aes_key is None:
        restored, skipped = _write_entries_to_current_db(entries)
    else:
        restored, skipped = _write_entries_to_current_db(entries, aes_key=aes_key)

    print()
    print(f"Restored: {restored}  Skipped: {skipped}")
    print(f"Done. Start {IDE_PATHS[CURRENT_IDE]['label']} now.")
    return restored, skipped


def restore(backup_path: str, key_filter: str | None = None, *, create_safety_backup: bool = True):
    """Restore entry-based JSON data into state.vscdb.

    This is an internal helper used for remapped IDE slot writes.
    The currently selected IDE must be closed before running this.
    """
    if not os.path.exists(backup_path):
        raise UserFacingError(f"Backup file not found: {backup_path}")
    if not os.path.exists(DB_PATH):
        raise UserFacingError(f"DB not found: {DB_PATH}")

    with open(backup_path, "r", encoding="utf-8") as fh:
        backup_data = json.load(fh)

    all_entries = backup_data.get("entries", [])
    if not all_entries:
        raise UserFacingError("No entries in backup.")

    entries = [entry for entry in all_entries if key_filter is None or key_filter.lower() in entry["key"].lower()]
    if not entries:
        available_keys = "\n".join(f"  {entry['key']}" for entry in all_entries)
        message = f"No keys matching '{key_filter}' in backup."
        if available_keys:
            message += f"\nAvailable keys:\n{available_keys}"
        raise UserFacingError(message)

    guard_vscode_closed()
    aes_key = get_aes_key()
    if aes_key is None:
        raise UserFacingError("ERROR: Cannot get AES key — cannot encrypt values.")

    if create_safety_backup:
        create_prewrite_backup(include_db=True, note=f"before restore from {os.path.basename(backup_path)}")
        print()

    _apply_entries_to_current_db(entries, source_label=backup_path, aes_key=aes_key)


def backup(out_path: str | None = None):
    """Create a real full-file backup archive of all storages used by the app."""
    result = _create_backup_archive(_full_backup_targets(), out_path, backup_kind="full")
    message = f"Full backup saved ({result['included']}/{result['total']} files)."
    if result["required_missing"]:
        message += f" Warning: {len(result['required_missing'])} required file(s) were missing."
    if result["optional_missing"]:
        message += f" Skipped {len(result['optional_missing'])} optional missing file(s)."
    return message


ACCOUNTS_DIR = os.path.join(PROJECT_ROOT, "accounts")
OAUTH_KEY = oauth_refresh.OPENAI_CODEX_STORAGE_KEY
KILO_AUTH_PATH = os.path.join(os.path.expanduser("~"), ".local", "share", "kilo", "auth.json")
KILO_NEW_KEY = "kilo-new://openai"
CODEX_AUTH_PATH = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
CODEX_KEY = "codex://openai"
OMP_AGENT_DB_PATH = os.path.join(os.path.expanduser("~"), ".omp", "agent", "agent.db")
OMP_OPENAI_KEY = oauth_refresh.OMP_OPENAI_KEY
SPECIAL_KIND_KEYS = {"codex": CODEX_KEY, "omp": OMP_OPENAI_KEY}

IDE_EXTENSIONS = {
    "both": None,
    "kilocode": "kilocode.kilo-code",
    "roo-cline": "rooveterinaryinc.roo-cline",
    "kilo-new": KILO_NEW_KEY,
}

CODEX_TARGETS = {
    "codex": CODEX_KEY,
}

EXTENSIONS = {**IDE_EXTENSIONS, **CODEX_TARGETS}
_EXT_DISPLAY = {v: k for k, v in EXTENSIONS.items() if v is not None}


def _accounts_dir() -> str:
    return saved_store.ensure_accounts_dir(ACCOUNTS_DIR)


def _is_kilo_new(ext_sub: str | None) -> bool:
    return account_services.is_kilo_new(ext_sub, KILO_NEW_KEY)


def _entry_key_for_ext(ext_id: str) -> str:
    return account_services.entry_key_for_ext(ext_id, OAUTH_KEY, KILO_NEW_KEY)


def _ide_db_extension_names() -> list[str]:
    return account_services.ide_db_extension_names(IDE_EXTENSIONS, KILO_NEW_KEY)


def _normalize_ide_ext_selection(ext: str | list[str] | tuple[str, ...] | None) -> tuple[list[str], str]:
    return account_services.normalize_ide_ext_selection(ext, IDE_EXTENSIONS, KILO_NEW_KEY)


def saved_account_kind(data: dict) -> str:
    return saved_store.saved_account_kind(data, SPECIAL_KIND_KEYS)


def list_saved_accounts(kind: str | None = None) -> list[dict]:
    return saved_store.list_saved_accounts(_accounts_dir(), SPECIAL_KIND_KEYS, kind)


def write_saved_account_batch(updates: dict[str, dict] | list[tuple[str, dict]]) -> None:
    saved_store.write_saved_account_batch(updates)


def _normalize_saved_account_updates(
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
) -> list[tuple[str, dict]]:
    return backups.normalize_saved_account_updates(updates)


def _write_refresh_recovery_snapshot(
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
    *,
    subject_label: str,
    account_names: Sequence[str],
    providers: Sequence[str],
    operation: str,
    created_at: str | None = None,
) -> str:
    return backups.write_refresh_recovery_snapshot(
        PROJECT_ROOT,
        updates,
        subject_label=subject_label,
        account_names=account_names,
        providers=providers,
        operation=operation,
        created_at=created_at or oauth_refresh.current_time_iso(),
    )


def _cleanup_refresh_recovery_snapshot(path: str) -> None:
    backups.cleanup_refresh_recovery_snapshot(path)


def _refreshed_credentials_persistence_message(
    *,
    subject_label: str,
    save_error: Exception,
    recovery_path: str | None,
    recovery_error: Exception | None,
) -> str:
    return backups.refreshed_credentials_persistence_message(
        subject_label=subject_label,
        save_error=save_error,
        recovery_path=recovery_path,
        recovery_error=recovery_error,
    )


def persist_refreshed_saved_account_batch(
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
    *,
    subject_label: str,
    account_names: Sequence[str],
    providers: Sequence[str],
    operation: str,
) -> None:
    items = _normalize_saved_account_updates(updates)
    write_updates = dict(items)
    recovery_path: str | None = None
    recovery_error: Exception | None = None

    try:
        recovery_path = _write_refresh_recovery_snapshot(
            items,
            subject_label=subject_label,
            account_names=account_names,
            providers=providers,
            operation=operation,
        )
    except Exception as exc:
        recovery_error = exc

    try:
        write_saved_account_batch(write_updates)
    except Exception as exc:
        raise RenewedCredentialsPersistenceError(
            _refreshed_credentials_persistence_message(
                subject_label=subject_label,
                save_error=exc,
                recovery_path=recovery_path,
                recovery_error=recovery_error,
            ),
            recovery_path=recovery_path,
            recovery_error=recovery_error,
        ) from exc

    if recovery_path:
        _cleanup_refresh_recovery_snapshot(recovery_path)


def persist_auto_refresh_group(
    updates: Mapping[str, dict] | Sequence[tuple[str, dict]],
    group: oauth_refresh.RefreshGroup,
) -> None:
    account_names = group.account_names()
    names = ", ".join(account_names) or "saved account"
    persist_refreshed_saved_account_batch(
        updates,
        subject_label=f"token group for {names}",
        account_names=account_names,
        providers=(group.key.provider,),
        operation="auto-refresh",
    )


def _read_codex_auth() -> dict:
    return codex_store.read_codex_auth(CODEX_AUTH_PATH)


def _write_codex_auth(data: dict):
    codex_store.write_codex_auth(CODEX_AUTH_PATH, data)


def _to_codex_format(value: dict, existing: dict | None = None) -> dict:
    return codex_store.to_codex_format(value, existing)


def _from_codex_format(value: dict) -> dict:
    return codex_store.from_codex_format(value)


def _read_kilo_auth() -> dict:
    return kilo_new_accounts.read_kilo_auth(KILO_AUTH_PATH)


def _write_kilo_auth(data: dict):
    kilo_new_accounts.write_kilo_auth(KILO_AUTH_PATH, data)


def _to_kilo_new_format(value: dict) -> dict:
    return kilo_new_accounts.to_kilo_new_format(value)


def _from_kilo_new_format(value: dict) -> dict:
    return kilo_new_accounts.from_kilo_new_format(value)


def _read_current_omp_openai_entries() -> list[dict]:
    return omp_store.read_current_openai_entries(OMP_AGENT_DB_PATH, OMP_OPENAI_KEY)


def read_current_omp_openai_accounts() -> list[dict]:
    return omp_store.read_current_openai_accounts(
        OMP_AGENT_DB_PATH,
        OMP_OPENAI_KEY,
        account_fingerprint,
    )


def _replace_omp_openai_credentials(entries: Sequence[Mapping[str, object]]) -> None:
    omp_store.replace_openai_credentials(OMP_AGENT_DB_PATH, entries)


def get_kilo_new_fingerprint() -> str | None:
    return kilo_new_accounts.get_kilo_new_fingerprint(KILO_AUTH_PATH)


def read_current_kilo_new_account() -> dict[str, dict]:
    return kilo_new_accounts.read_current_kilo_new_account(
        KILO_AUTH_PATH,
        KILO_NEW_KEY,
        account_fingerprint,
    )


def read_current_codex_account() -> dict[str, dict]:
    return codex_store.read_current_codex_account(CODEX_AUTH_PATH, CODEX_KEY, account_fingerprint)


def account_fingerprint(value) -> str | None:
    return account_services.account_fingerprint(value)


def read_current_accounts(db_path: str | None = None, local_state_path: str | None = None) -> dict[str, dict]:
    return state_db.read_current_accounts(
        db_path or DB_PATH,
        local_state_path or LOCAL_STATE_PATH,
        OAUTH_KEY,
        get_aes_key_fn=get_aes_key,
        decode_entry_fn=_decode_entry,
        account_fingerprint=account_fingerprint,
    )


def match_saved_to_current(
    saved_entries: list[dict],
    current_accounts: dict[str, dict],
) -> list[str]:
    return account_services.match_saved_to_current(saved_entries, current_accounts, _EXT_DISPLAY)


def read_current_accounts_for_ide(ide: str) -> dict[str, dict]:
    return account_services.read_current_accounts_for_ide(
        ide,
        ide_paths=IDE_PATHS,
        read_current_accounts=read_current_accounts,
        read_current_kilo_new_account=read_current_kilo_new_account,
    )


def _run_saved_account_store_call(
    name: str,
    store_call: Callable[[], SavedAccountStoreResult],
    *,
    expected_kind: str | None = None,
    use_lock: bool = False,
    map_value_error: bool = False,
) -> SavedAccountStoreResult:
    try:
        if use_lock:
            with SAVED_ACCOUNT_REFRESH_LOCK:
                return store_call()
        return store_call()
    except FileNotFoundError as exc:
        raise AccountNotFoundError(f"Account '{name}' not found.") from exc
    except saved_store.SavedAccountKindMismatchError as exc:
        raise AccountKindMismatchError(
            f"Account '{name}' has kind '{exc.actual_kind}', expected '{expected_kind}'."
        ) from exc
    except ValueError as exc:
        if map_value_error:
            raise UserFacingError(str(exc)) from exc
        raise


def _load_saved_account_data(name: str, expected_kind: str | None = None) -> tuple[str, dict, str]:
    return _run_saved_account_store_call(
        name,
        lambda: saved_store.load_saved_account(_accounts_dir(), name, SPECIAL_KIND_KEYS, expected_kind),
        expected_kind=expected_kind,
    )


def rename_saved_account(name: str, new_name: str, expected_kind: str | None = None) -> str:
    _path, data, _kind = _run_saved_account_store_call(
        name,
        lambda: saved_store.rename_saved_account(_accounts_dir(), SPECIAL_KIND_KEYS, name, new_name, expected_kind),
        expected_kind=expected_kind,
        use_lock=True,
        map_value_error=True,
    )
    return data["name"]


def delete_saved_account(name: str, expected_kind: str | None = None) -> None:
    _run_saved_account_store_call(
        name,
        lambda: saved_store.delete_saved_account(_accounts_dir(), name, SPECIAL_KIND_KEYS, expected_kind),
        expected_kind=expected_kind,
        use_lock=True,
        map_value_error=True,
    )


def _saved_codex_entry(data: dict) -> dict | None:
    entries = data.get("entries", []) if isinstance(data, dict) else []
    for entry in entries:
        if entry.get("key") == CODEX_KEY:
            return entry
    return None


def _read_current_ide_entries_for_selection(ext_names: list[str]) -> list[dict]:
    return account_services.read_current_ide_entries_for_selection(
        ext_names,
        ide_extensions=IDE_EXTENSIONS,
        kilo_new_key=KILO_NEW_KEY,
        read_db_entries=lambda db_target_ids: state_db.read_entries_for_extension_ids(
            DB_PATH,
            LOCAL_STATE_PATH,
            OAUTH_KEY,
            db_target_ids,
            get_aes_key_fn=get_aes_key,
            decode_entry_fn=_decode_entry,
        ),
        read_kilo_auth=_read_kilo_auth,
        from_kilo_new_format=_from_kilo_new_format,
    )


def _write_account_file(name: str, kind: str, ext_label: str, entries: list[dict]) -> str:
    return saved_store.write_account_file(_accounts_dir(), SPECIAL_KIND_KEYS, name, kind, ext_label, entries)


def _print_saved_entries(entries: list[dict]):
    account_services.print_saved_entries(entries)


def save_ide_account(name: str, ext: str | list[str] | tuple[str, ...] | None = None):
    result = account_services.save_ide_account(
        name,
        ext,
        normalize_selection=_normalize_ide_ext_selection,
        read_current_ide_entries_for_selection=_read_current_ide_entries_for_selection,
        write_account_file=_write_account_file,
        user_facing_error_cls=UserFacingError,
    )
    print(f"Account '{name}' saved [{result.ext_label}] ->{result.path}")
    _print_saved_entries(result.entries)


def save_codex_account(name: str):
    result = account_services.save_codex_account(
        name,
        codex_key=CODEX_KEY,
        read_codex_auth=_read_codex_auth,
        from_codex_format=_from_codex_format,
        write_account_file=_write_account_file,
        user_facing_error_cls=UserFacingError,
    )
    print(f"Account '{name}' saved [{result.ext_label}] ->{result.path}")
    _print_saved_entries(result.entries)


def save_omp_openai_account(name: str):
    result = account_services.save_omp_openai_account(
        name,
        omp_key=OMP_OPENAI_KEY,
        read_omp_openai_entries=_read_current_omp_openai_entries,
        write_account_file=_write_account_file,
        user_facing_error_cls=UserFacingError,
    )
    print(f"Account '{name}' saved [{result.ext_label}] ->{result.path}")
    _print_saved_entries(result.entries)


def refresh_saved_account(name: str, expected_kind: str | None = None) -> str:
    return account_services.refresh_saved_account(
        name,
        operation_lock=SAVED_ACCOUNT_REFRESH_LOCK,
        load_saved_account_data=lambda account_name: _load_saved_account_data(account_name, expected_kind=expected_kind),
        list_saved_accounts=list_saved_accounts,
        oauth_refresh_module=oauth_refresh,
        is_terminal_refresh_error=oauth_refresh.is_terminal_refresh_error,
        write_saved_account_batch=write_saved_account_batch,
        persist_refreshed_saved_account_batch=persist_refreshed_saved_account_batch,
        saved_account_refresh_error_cls=SavedAccountRefreshError,
        persistence_error_cls=RenewedCredentialsPersistenceError,
    )


def use_ide_account(
    name: str,
    ext: str | list[str] | tuple[str, ...] | None = None,
    allow_kilo_new_while_running: bool = False,
):
    def apply_db_entries(entries: Sequence[dict]) -> tuple[int, int]:
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
        tmp_path = handle.name
        try:
            with handle:
                json.dump({"entries": list(entries)}, handle)
            restore(tmp_path, create_safety_backup=False)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return len(entries), 0

    account_services.use_ide_account(
        name,
        ext,
        allow_kilo_new_while_running,
        load_saved_account_data=_load_saved_account_data,
        normalize_selection=_normalize_ide_ext_selection,
        ide_extensions=IDE_EXTENSIONS,
        code_key=CODEX_KEY,
        kilo_new_key=KILO_NEW_KEY,
        ide_paths=IDE_PATHS,
        guard_current_ide_closed=guard_vscode_closed,
        is_ide_running=is_ide_running,
        create_prewrite_backup=create_prewrite_backup,
        apply_db_entries=apply_db_entries,
        entry_key_for_ext_fn=_entry_key_for_ext,
        to_kilo_new_format=_to_kilo_new_format,
        read_kilo_auth=_read_kilo_auth,
        write_kilo_auth=_write_kilo_auth,
        kilo_auth_path=KILO_AUTH_PATH,
        user_facing_error_cls=UserFacingError,
    )


def use_codex_account(name: str):
    account_services.use_codex_account(
        name,
        load_saved_account_data=_load_saved_account_data,
        saved_codex_entry=_saved_codex_entry,
        create_prewrite_backup=create_prewrite_backup,
        to_codex_format=_to_codex_format,
        read_codex_auth=_read_codex_auth,
        write_codex_auth=_write_codex_auth,
        codex_auth_path=CODEX_AUTH_PATH,
        user_facing_error_cls=UserFacingError,
    )


def use_omp_openai_account(name: str):
    account_services.use_omp_openai_account(
        name,
        load_saved_account_data=_load_saved_account_data,
        omp_key=OMP_OPENAI_KEY,
        create_prewrite_backup=create_prewrite_backup,
        replace_omp_openai_credentials=_replace_omp_openai_credentials,
        omp_agent_db_path=OMP_AGENT_DB_PATH,
        user_facing_error_cls=UserFacingError,
    )


def import_codex_account(auth_path: str, name: str):
    result = account_services.import_codex_account(
        auth_path,
        name,
        codex_key=CODEX_KEY,
        from_codex_format=_from_codex_format,
        write_account_file=_write_account_file,
        user_facing_error_cls=UserFacingError,
    )
    exp_dt = datetime.datetime.fromtimestamp(result.expires_ms / 1000)
    print(f"Imported '{name}' [codex] ->{result.path}")
    print(f"  accountId: {result.account_id}")
    print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def import_ide_account_from_json_string(json_str: str, name: str, exts: list[str]):
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise UserFacingError(f"ERROR: invalid JSON: {exc}") from exc

    result = account_services.import_ide_account_data(
        data,
        name,
        exts,
        ide_extensions=IDE_EXTENSIONS,
        kilo_new_key=KILO_NEW_KEY,
        from_codex_format=_from_codex_format,
        write_account_file=_write_account_file,
        entry_key_for_ext_fn=_entry_key_for_ext,
        user_facing_error_cls=UserFacingError,
    )
    exp_dt = datetime.datetime.fromtimestamp(account_services.first_expires_ms(result.entries) / 1000)
    print(f"Imported IDE account '{name}' [{result.ext_label}] -> {result.path}")
    print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def main():
    raise SystemExit("CLI support removed. Use `python main.py`.")


if __name__ == "__main__":
    main()

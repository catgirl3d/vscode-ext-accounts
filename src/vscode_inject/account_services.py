from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import oauth_refresh
from . import saved_account_status


@dataclass(frozen=True)
class SavedAccountWriteResult:
    path: str
    ext_label: str
    entries: list[dict]


@dataclass(frozen=True)
class ImportedCodexAccountResult:
    path: str
    account_id: str
    expires_ms: int


def _write_saved_account_file(
    name: str,
    kind: str,
    ext_label: str,
    entries: list[dict],
    *,
    write_account_file,
    user_facing_error_cls,
) -> str:
    try:
        return write_account_file(name, kind, ext_label, entries)
    except ValueError as exc:
        raise user_facing_error_cls(str(exc)) from exc


def _build_saved_account_write_result(
    name: str,
    kind: str,
    ext_label: str,
    entries: list[dict],
    *,
    write_account_file,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    path = _write_saved_account_file(
        name,
        kind,
        ext_label,
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )
    return SavedAccountWriteResult(path=path, ext_label=ext_label, entries=entries)


def _validate_saved_account_value(
    value: Mapping[str, Any],
    *,
    user_facing_error_cls,
    missing_tokens_message: str,
    missing_id_token_message: str,
    missing_expires_message: str | None = None,
):
    if not value.get("access_token") or not value.get("refresh_token"):
        raise user_facing_error_cls(missing_tokens_message)

    expires_ms = value.get("expires", 0)
    if missing_expires_message and not expires_ms:
        raise user_facing_error_cls(missing_expires_message)
    if not value.get("id_token"):
        raise user_facing_error_cls(missing_id_token_message)
    return expires_ms


def is_kilo_new(ext_sub: str | None, kilo_new_key: str) -> bool:
    return ext_sub == kilo_new_key


def entry_key_for_ext(ext_id: str, oauth_key: str, kilo_new_key: str) -> str:
    if is_kilo_new(ext_id, kilo_new_key):
        return ext_id
    payload = {"extensionId": str(ext_id), "key": str(oauth_key)}
    return f"secret://{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"


def _extension_id_from_entry_key(key: str) -> str:
    if not key.startswith("secret://"):
        return ""
    try:
        payload = json.loads(key[len("secret://"):])
    except Exception:
        return ""
    ext_id = payload.get("extensionId", "")
    return ext_id if isinstance(ext_id, str) else ""


def ide_db_extension_names(ide_extensions: Mapping[str, str | None], kilo_new_key: str) -> list[str]:
    return [
        name
        for name, ext_id in ide_extensions.items()
        if ext_id and not is_kilo_new(ext_id, kilo_new_key)
    ]


def normalize_ide_ext_selection(
    ext: str | list[str] | tuple[str, ...] | None,
    ide_extensions: Mapping[str, str | None],
    kilo_new_key: str,
) -> tuple[list[str], str]:
    if ext is None or ext == "both":
        names = ide_db_extension_names(ide_extensions, kilo_new_key)
        return names, "both"

    items = [ext] if isinstance(ext, str) else list(ext)
    normalized: list[str] = []
    valid = {name for name in ide_extensions if name != "both"}
    db_names = ide_db_extension_names(ide_extensions, kilo_new_key)

    for name in items:
        if name == "both":
            for db_name in db_names:
                if db_name not in normalized:
                    normalized.append(db_name)
            continue
        if name not in valid:
            valid_str = ", ".join(sorted(valid))
            raise ValueError(f"Unknown extension '{name}'. Expected one of: {valid_str}")
        if name not in normalized:
            normalized.append(name)

    if not normalized:
        raise ValueError("Select at least one extension.")
    return normalized, "+".join(normalized)


def account_fingerprint(value: object) -> str | None:
    if not isinstance(value, dict):
        return None

    raw_tokens = value.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    refresh_token = value.get("refresh_token") or value.get("refresh") or tokens.get("refresh_token")
    if refresh_token:
        refresh_str = refresh_token if isinstance(refresh_token, str) else str(refresh_token)
        return hashlib.sha256(refresh_str.encode("utf-8")).hexdigest()

    account_id = value.get("accountId") or value.get("account_id") or tokens.get("account_id")
    if account_id:
        account_id_str = account_id if isinstance(account_id, str) else str(account_id)
        return hashlib.sha256(account_id_str.encode("utf-8")).hexdigest()
    return None


def account_email(value: object) -> str | None:
    if not isinstance(value, dict):
        return None

    email = value.get("email")
    if isinstance(email, str):
        normalized = email.strip()
        if normalized:
            return normalized

    raw_tokens = value.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}

    nested_email = tokens.get("email")
    if isinstance(nested_email, str):
        normalized_nested = nested_email.strip()
        if normalized_nested:
            return normalized_nested

    access_token = value.get("access_token") or value.get("access") or tokens.get("access_token") or ""
    id_token = value.get("id_token") or tokens.get("id_token")
    access_token_str = access_token if isinstance(access_token, str) else str(access_token or "")
    id_token_str = id_token if isinstance(id_token, str) and id_token else None
    return oauth_refresh.extract_email(access_token_str, id_token_str)


def match_saved_to_current(
    saved_entries: Sequence[dict],
    current_accounts: Mapping[str, dict],
    ext_display: Mapping[str, str],
) -> list[str]:
    matched: list[str] = []
    for entry in saved_entries:
        ext_id = _extension_id_from_entry_key(entry.get("key", ""))
        current = current_accounts.get(ext_id)
        if not current:
            continue
        saved_fp = account_fingerprint(entry.get("value", {}))
        cur_fp = current.get("fingerprint")
        if saved_fp and cur_fp and saved_fp == cur_fp:
            short = ext_display.get(ext_id, ext_id)
            if short not in matched:
                matched.append(short)
    return matched


def read_current_accounts_for_ide(
    ide: str,
    *,
    ide_paths: Mapping[str, Mapping[str, object]],
    read_current_accounts: Callable[[str, str], dict[str, dict]],
    read_current_kilo_new_account: Callable[[], dict[str, dict]],
) -> dict[str, dict]:
    cfg = ide_paths[ide]
    accounts = read_current_accounts(str(cfg.get("db", "")), str(cfg.get("local_state", "")))
    if ide == "antigravity":
        accounts.update(read_current_kilo_new_account())
    return accounts


def read_current_ide_entries_for_selection(
    ext_names: Sequence[str],
    *,
    ide_extensions: Mapping[str, str | None],
    kilo_new_key: str,
    read_db_entries: Callable[[Sequence[str]], list[dict]],
    read_kilo_auth: Callable[[], dict],
    from_kilo_new_format: Callable[[dict], dict],
) -> list[dict]:
    db_target_ids = [ide_extensions[name] for name in ext_names if name != "kilo-new"]
    entries = list(read_db_entries(db_target_ids)) if db_target_ids else []
    if "kilo-new" in ext_names:
        openai_entry = read_kilo_auth().get("openai")
        if openai_entry:
            entries.append({"key": kilo_new_key, "value": from_kilo_new_format(openai_entry)})
    return entries


def save_ide_account(
    name: str,
    ext: str | list[str] | tuple[str, ...] | None,
    *,
    normalize_selection,
    read_current_ide_entries_for_selection,
    write_account_file,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    ext_names, ext_label = normalize_selection(ext)
    entries = read_current_ide_entries_for_selection(ext_names)
    if not entries:
        raise user_facing_error_cls(f"No matching account entries found for {ext_label}.")
    return _build_saved_account_write_result(
        name,
        "ide",
        ext_label,
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )


def save_codex_account(
    name: str,
    *,
    codex_key: str,
    read_codex_auth,
    from_codex_format,
    write_account_file,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    value = from_codex_format(read_codex_auth())
    _validate_saved_account_value(
        value,
        user_facing_error_cls=user_facing_error_cls,
        missing_tokens_message="ERROR: Codex auth.json is missing access_token or refresh_token.",
        missing_id_token_message="ERROR: Codex auth.json requires id_token.",
    )

    entries = [{"key": codex_key, "value": value}]
    return _build_saved_account_write_result(
        name,
        "codex",
        "codex",
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )


def save_omp_openai_account(
    name: str,
    *,
    omp_key: str,
    read_omp_openai_entries,
    write_account_file,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    try:
        entries = list(read_omp_openai_entries())
    except ValueError as exc:
        raise user_facing_error_cls(f"ERROR: {exc}") from exc
    if not entries:
        raise user_facing_error_cls("No active OMP OpenAI credentials found in agent.db.")

    return _build_saved_account_write_result(
        name,
        "omp",
        "omp-openai",
        [{"key": omp_key, **{k: v for k, v in entry.items() if k != "key"}} for entry in entries],
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )


def _normalize_omp_import_items(data: Any, *, user_facing_error_cls) -> list[dict[str, Any]]:
    if isinstance(data, list):
        if not data:
            raise user_facing_error_cls("ERROR: Empty JSON array provided")
        items = data
    else:
        items = [data]

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise user_facing_error_cls("ERROR: JSON must be an object or an array of objects")
        normalized_items.append(item)
    return normalized_items


def _omp_import_entry_merge_key(entry: Mapping[str, Any]) -> str | None:
    value = entry.get("value")
    if not isinstance(value, Mapping):
        return None

    explicit_identity_key = entry.get("identity_key")
    if isinstance(explicit_identity_key, str) and explicit_identity_key:
        return f"identity:{explicit_identity_key}"

    embedded_identity_key = value.get("identity_key")
    if isinstance(embedded_identity_key, str) and embedded_identity_key:
        return f"identity:{embedded_identity_key}"

    email = value.get("email")
    if isinstance(email, str):
        normalized_email = email.strip().lower()
        if normalized_email:
            return f"identity:email:{normalized_email}"

    account_id = value.get("accountId") or value.get("account_id")
    if isinstance(account_id, str) and account_id:
        return f"identity:account:{account_id}"

    fingerprint = account_fingerprint(dict(value))
    if fingerprint:
        return f"fingerprint:{fingerprint}"
    return None


def _dedupe_omp_import_entries(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped_reversed: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for entry in reversed(list(entries)):
        merge_key = _omp_import_entry_merge_key(entry)
        if merge_key and merge_key in seen_keys:
            continue
        if merge_key:
            seen_keys.add(merge_key)
        deduped_reversed.append(entry)
    deduped_reversed.reverse()
    return deduped_reversed


def _build_omp_import_entries(
    data: Any,
    *,
    omp_key: str,
    from_omp_import_format,
    user_facing_error_cls,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _normalize_omp_import_items(data, user_facing_error_cls=user_facing_error_cls):
        try:
            value = from_omp_import_format(item)
        except ValueError as exc:
            raise user_facing_error_cls(f"ERROR: {exc}") from exc

        if not value.get("access_token") or not value.get("refresh_token"):
            raise user_facing_error_cls("ERROR: access_token or refresh_token missing in data")
        expires_ms = value.get("expires")
        if not isinstance(expires_ms, int) or expires_ms <= 0:
            raise user_facing_error_cls("ERROR: could not decode access token expiry from data")

        entry: dict[str, Any] = {"key": omp_key, "value": value}
        identity_key = item.get("identity_key")
        if not isinstance(identity_key, str) or not identity_key:
            identity_key = value.get("identity_key")
        if isinstance(identity_key, str) and identity_key:
            entry["identity_key"] = identity_key
        entries.append(entry)

    return _dedupe_omp_import_entries(entries)


def import_omp_openai_account_data(
    data: Any,
    name: str,
    *,
    omp_key: str,
    from_omp_import_format,
    write_account_file,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    entries = _build_omp_import_entries(
        data,
        omp_key=omp_key,
        from_omp_import_format=from_omp_import_format,
        user_facing_error_cls=user_facing_error_cls,
    )
    return _build_saved_account_write_result(
        name,
        "omp",
        "omp-openai",
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )


def append_omp_openai_account_data(
    data: Any,
    target_name: str,
    *,
    omp_key: str,
    from_omp_import_format,
    load_saved_account_data,
    write_saved_account_data,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    path, account_data, _kind = load_saved_account_data(target_name, expected_kind="omp")
    existing_entries = account_data.get("entries", []) if isinstance(account_data, dict) else []
    if not isinstance(existing_entries, list):
        raise user_facing_error_cls(f"Account '{target_name}' has an invalid entries payload.")

    imported_entries = _build_omp_import_entries(
        data,
        omp_key=omp_key,
        from_omp_import_format=from_omp_import_format,
        user_facing_error_cls=user_facing_error_cls,
    )
    imported_merge_keys = {
        merge_key
        for merge_key in (_omp_import_entry_merge_key(entry) for entry in imported_entries)
        if isinstance(merge_key, str) and merge_key
    }

    preserved_entries: list[dict[str, Any]] = []
    for entry in existing_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("key") != omp_key:
            preserved_entries.append(entry)
            continue
        merge_key = _omp_import_entry_merge_key(entry)
        if merge_key and merge_key in imported_merge_keys:
            continue
        preserved_entries.append(entry)

    merged_entries = preserved_entries + imported_entries
    saved_name = account_data.get("name") if isinstance(account_data, dict) and isinstance(account_data.get("name"), str) else target_name
    updated_data = {
        "name": saved_name,
        "kind": "omp",
        "ext": "omp-openai",
        "saved_at": datetime.datetime.now().isoformat(),
        "entries": merged_entries,
    }
    write_saved_account_data(path, updated_data)
    return SavedAccountWriteResult(path=path, ext_label="omp-openai", entries=merged_entries)


def refresh_saved_account(
    name: str,
    *,
    operation_lock,
    load_saved_account_data,
    list_saved_accounts=None,
    oauth_refresh_module,
    is_terminal_refresh_error: Callable[[Exception], bool],
    write_saved_account_batch,
    persist_refreshed_saved_account_batch,
    saved_account_refresh_error_cls,
    persistence_error_cls,
) -> str:
    with operation_lock:
        path, account_data, _kind = load_saved_account_data(name)
        saved_records = oauth_refresh_module.saved_account_records(list_saved_accounts()) if list_saved_accounts else []
        records_by_path = {record.path: record for record in saved_records}
        entries = account_data.get("entries", []) if isinstance(account_data, dict) else []
        if not isinstance(entries, list):
            raise ValueError(f"Account '{name}' has an invalid entries payload.")

        refreshable_entries = oauth_refresh_module.collect_refreshable_entries(entries)
        providers: list[str] = []
        for refreshable in refreshable_entries:
            if refreshable.provider not in providers:
                providers.append(refreshable.provider)

        updated_data = dict(account_data)
        try:
            refreshed = oauth_refresh_module.refresh_saved_entries(entries)
        except oauth_refresh_module.OAuthRefreshError as exc:
            terminal_error = is_terminal_refresh_error(exc)
            updated_data["refresh_status"] = (
                saved_account_status.REFRESH_STATUS_TERMINAL_ERROR
                if terminal_error
                else saved_account_status.REFRESH_STATUS_ERROR
            )
            updated_data["refresh_error"] = str(exc)
            updated_data["refresh_error_at"] = oauth_refresh_module.current_time_iso()
            updates = {path: updated_data}
            failed_group_key = getattr(exc, "refresh_group_key", None)
            if terminal_error and failed_group_key is not None:
                updates.update(
                    oauth_refresh_module.apply_auto_refresh_disabled_group_updates(
                        records_by_path,
                        (failed_group_key,),
                        disabled=True,
                    )
                )
                disabled_group_keys = oauth_refresh_module.auto_refresh_disabled_group_keys(updated_data)
                disabled_group_keys.add(failed_group_key)
                oauth_refresh_module.set_auto_refresh_disabled_group_keys(updated_data, disabled_group_keys)
                updates[path] = updated_data

            write_saved_account_batch(updates)
            raise saved_account_refresh_error_cls(f"Token renewal failed for '{name}': {exc}") from exc

        updated_data["entries"] = refreshed.entries
        updated_data["last_refreshed_at"] = refreshed.refreshed_at
        updated_data["refresh_status"] = saved_account_status.REFRESH_STATUS_OK
        updated_data.pop("refresh_error", None)
        updated_data.pop("refresh_error_at", None)
        updated_data.pop(getattr(oauth_refresh_module, "AUTO_REFRESH_DISABLED_GROUPS_KEY", "auto_refresh_disabled_groups"), None)
        refreshed_group_keys = oauth_refresh_module.group_keys_from_entries(refreshed.entries)
        updates = oauth_refresh_module.apply_auto_refresh_disabled_group_updates(
            records_by_path,
            refreshed_group_keys,
            disabled=False,
        )
        updates[path] = updated_data
        account_names: list[str] = []
        for updated_path in updates:
            record = records_by_path.get(updated_path)
            record_name = record.name if record is not None else (name if updated_path == path else updated_path)
            if record_name not in account_names:
                account_names.append(record_name)
        try:
            persist_refreshed_saved_account_batch(
                updates,
                subject_label=f"saved account '{name}'",
                account_names=tuple(account_names or [name]),
                providers=providers,
                operation="manual-refresh",
            )
        except persistence_error_cls as exc:
            raise saved_account_refresh_error_cls(str(exc)) from exc

        group_label = "group" if refreshed.refreshed_groups == 1 else "groups"
        entry_label = "entry" if refreshed.refreshed_entries == 1 else "entries"
        return (
            f"Renewed tokens for '{name}' "
            f"({refreshed.refreshed_groups} token {group_label}, {refreshed.refreshed_entries} {entry_label})"
        )


def use_ide_account(
    name: str,
    ext: str | list[str] | tuple[str, ...] | None = None,
    allow_kilo_new_while_running: bool = False,
    *,
    load_saved_account_data,
    normalize_selection,
    ide_extensions: Mapping[str, str | None],
    code_key: str,
    kilo_new_key: str,
    ide_paths: Mapping[str, Mapping[str, object]],
    guard_current_ide_closed,
    is_ide_running,
    create_prewrite_backup,
    apply_db_entries,
    entry_key_for_ext_fn,
    to_kilo_new_format,
    read_kilo_auth,
    write_kilo_auth,
    kilo_auth_path: str,
    user_facing_error_cls,
    print_fn: Callable[[str], None] = print,
) -> None:
    _path, account_data, account_kind = load_saved_account_data(name)
    if account_kind == "codex":
        raise user_facing_error_cls(f"Account '{name}' is Codex-only and cannot be applied to IDE targets.")

    ext_names, _ = normalize_selection(ext)
    entries = account_data.get("entries", [])
    ide_entries = [entry for entry in entries if entry.get("key") != code_key]
    source = next(iter(ide_entries), None)
    if not source:
        raise user_facing_error_cls(f"No IDE entries in account '{name}'.")

    db_target_ids = [ide_extensions[target_name] for target_name in ext_names if target_name != "kilo-new"]
    source_db = next((entry for entry in ide_entries if entry.get("key") != kilo_new_key), None)
    source_kilo_new = next((entry for entry in ide_entries if entry.get("key") == kilo_new_key), None)
    generic_source = source_db or source_kilo_new or source

    needs_db_write = bool(db_target_ids)
    needs_kilo_write = "kilo-new" in ext_names

    if needs_db_write:
        guard_current_ide_closed()

    running_kilo_new_ides = [ide for ide in ide_paths if needs_kilo_write and is_ide_running(ide)]
    if running_kilo_new_ides and not allow_kilo_new_while_running:
        labels = ", ".join(str(ide_paths[ide].get("label", ide)) for ide in running_kilo_new_ides)
        raise user_facing_error_cls(
            f"ERROR: Kilo New may be active in running IDEs: {labels}. Close them before switching accounts."
        )
    if running_kilo_new_ides and allow_kilo_new_while_running:
        labels = ", ".join(str(ide_paths[ide].get("label", ide)) for ide in running_kilo_new_ides)
        print_fn(f"WARNING: Writing shared Kilo New auth while IDEs are running ({labels}) (experimental).")

    if needs_db_write or needs_kilo_write:
        create_prewrite_backup(
            include_db=needs_db_write,
            include_kilo=needs_kilo_write,
            note=f"before applying IDE account '{name}'",
        )
        print_fn("")

    if db_target_ids:
        remapped_entries = []
        for ext_id in db_target_ids:
            target_key = entry_key_for_ext_fn(ext_id)
            existing = next((entry for entry in ide_entries if entry.get("key") == target_key), None)
            if existing:
                remapped_entries.append(existing)
                continue
            print_fn(f"[cross-ext] No '{ext_id}' key — remapping from: {generic_source['key']}")
            remapped_entries.append({"key": target_key, "value": generic_source["value"]})
        apply_db_entries(remapped_entries)

    if "kilo-new" not in ext_names:
        return

    source_entry = source_kilo_new or generic_source
    new_entry = to_kilo_new_format(source_entry["value"])
    kilo_auth = read_kilo_auth()
    kilo_auth["openai"] = new_entry
    write_kilo_auth(kilo_auth)
    print_fn(f"[kilo-new] Written to {kilo_auth_path}")
    print_fn(f"  accountId: {new_entry.get('accountId', '?')}")


def use_codex_account(
    name: str,
    *,
    load_saved_account_data,
    saved_codex_entry,
    create_prewrite_backup,
    to_codex_format,
    read_codex_auth,
    write_codex_auth,
    codex_auth_path: str,
    user_facing_error_cls,
    print_fn: Callable[[str], None] = print,
) -> None:
    _path, account_data, _kind = load_saved_account_data(name, expected_kind="codex")
    source_entry = saved_codex_entry(account_data)
    if not source_entry:
        raise user_facing_error_cls(f"Account '{name}' does not contain a Codex entry.")

    create_prewrite_backup(include_codex=True, note=f"before applying Codex account '{name}'")
    print_fn("")

    try:
        codex_auth = to_codex_format(source_entry["value"], read_codex_auth())
    except ValueError as exc:
        raise user_facing_error_cls(f"ERROR: {exc}") from exc

    write_codex_auth(codex_auth)
    print_fn(f"[codex] Written to {codex_auth_path}")
    print_fn(f"  accountId: {codex_auth.get('tokens', {}).get('account_id', '?')}")


def use_omp_openai_account(
    name: str,
    *,
    load_saved_account_data,
    omp_key: str,
    create_prewrite_backup,
    replace_omp_openai_credentials,
    omp_agent_db_path: str,
    user_facing_error_cls,
    print_fn: Callable[[str], None] = print,
) -> None:
    _path, account_data, _kind = load_saved_account_data(name, expected_kind="omp")
    raw_entries = account_data.get("entries", []) if isinstance(account_data, dict) else []
    entries = [entry for entry in raw_entries if isinstance(entry, dict) and entry.get("key") == omp_key]
    if not entries:
        raise user_facing_error_cls(f"Account '{name}' does not contain any OMP OpenAI entries.")

    create_prewrite_backup(include_omp=True, note=f"before applying OMP OpenAI account '{name}'")
    print_fn("")

    try:
        replace_omp_openai_credentials(entries)
    except ValueError as exc:
        raise user_facing_error_cls(f"ERROR: {exc}") from exc
    account_ids = [
        str(entry.get("value", {}).get("accountId", "?"))
        for entry in entries
        if isinstance(entry.get("value"), Mapping)
    ]
    print_fn(f"[omp-openai] Written to {omp_agent_db_path}")
    print_fn(f"  credentials: {len(entries)}")
    if account_ids:
        print_fn(f"  accountIds: {', '.join(account_ids)}")


def import_codex_account(
    auth_path: str,
    name: str,
    *,
    codex_key: str,
    from_codex_format,
    write_account_file,
    user_facing_error_cls,
) -> ImportedCodexAccountResult:
    if not os.path.exists(auth_path):
        raise user_facing_error_cls(f"File not found: {auth_path}")

    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise user_facing_error_cls(f"ERROR: invalid auth.json: {exc}") from exc

    value = from_codex_format(data)
    account_id = value.get("accountId") or ""
    expires_ms = _validate_saved_account_value(
        value,
        user_facing_error_cls=user_facing_error_cls,
        missing_tokens_message="ERROR: access_token or refresh_token missing in auth.json",
        missing_id_token_message="ERROR: Codex import requires id_token in auth.json.",
        missing_expires_message="ERROR: could not decode access token expiry from auth.json",
    )

    entries = [{"key": codex_key, "value": value}]
    out = _write_saved_account_file(
        name,
        "codex",
        "codex",
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )
    return ImportedCodexAccountResult(path=out, account_id=account_id, expires_ms=expires_ms)


def print_saved_entries(entries: Sequence[dict], *, print_fn: Callable[[str], None] = print) -> None:
    for entry in entries:
        value = entry.get("value", {})
        if isinstance(value, dict):
            print_fn(f"  accountId: {value.get('accountId', '?')}")
            exp = value.get("expires")
            if exp:
                exp_dt = datetime.datetime.fromtimestamp(exp / 1000)
                print_fn(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def first_expires_ms(entries: Sequence[dict], *, skip_keys: Sequence[str] | None = None) -> int:
    skip_key_set = set(skip_keys or [])
    expires_values = []
    for entry in entries:
        if entry.get("key") in skip_key_set:
            continue
        value = entry.get("value", {})
        if isinstance(value, dict):
            expires_ms = value.get("expires")
            if isinstance(expires_ms, int) and expires_ms > 0:
                expires_values.append(expires_ms)
    return min(expires_values, default=0)


def import_ide_account_data(
    data: Any,
    name: str,
    exts: list[str],
    *,
    ide_extensions: Mapping[str, str | None],
    kilo_new_key: str,
    from_codex_format,
    write_account_file,
    entry_key_for_ext_fn,
    user_facing_error_cls,
) -> SavedAccountWriteResult:
    if isinstance(data, list):
        if not data:
            raise user_facing_error_cls("ERROR: Empty JSON array provided")
        data = data[0]

    if not isinstance(data, dict):
        raise user_facing_error_cls("ERROR: JSON must be an object or an array of objects")

    value = from_codex_format(data)
    _validate_saved_account_value(
        value,
        user_facing_error_cls=user_facing_error_cls,
        missing_tokens_message="ERROR: access_token or refresh_token missing in data",
        missing_id_token_message="ERROR: Codex data requires id_token.",
        missing_expires_message="ERROR: could not decode access token expiry from data",
    )

    entries = []
    for ext_name in exts:
        ext_id = ide_extensions.get(ext_name)
        if ext_id is None:
            continue
        if ext_id == kilo_new_key:
            entries.append({"key": kilo_new_key, "value": value})
        else:
            key = entry_key_for_ext_fn(ext_id)
            entries.append({"key": key, "value": value})

    if not entries:
        raise user_facing_error_cls("ERROR: No extensions selected to import the account for.")

    ext_label = "+".join(exts)
    return _build_saved_account_write_result(
        name,
        "ide",
        ext_label,
        entries,
        write_account_file=write_account_file,
        user_facing_error_cls=user_facing_error_cls,
    )

from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


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
    out = write_account_file(name, "ide", ext_label, entries)
    return SavedAccountWriteResult(path=out, ext_label=ext_label, entries=entries)


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
    if not value.get("access_token") or not value.get("refresh_token"):
        raise user_facing_error_cls("ERROR: Codex auth.json is missing access_token or refresh_token.")
    if not value.get("id_token"):
        raise user_facing_error_cls("ERROR: Codex auth.json requires id_token.")

    entry = {"key": codex_key, "value": value}
    out = write_account_file(name, "codex", "codex", [entry])
    return SavedAccountWriteResult(path=out, ext_label="codex", entries=[entry])


def refresh_saved_account(
    name: str,
    *,
    operation_lock,
    load_saved_account_data,
    oauth_refresh_module,
    write_saved_account_batch,
    persist_refreshed_saved_account_batch,
    saved_account_refresh_error_cls,
    persistence_error_cls,
) -> str:
    with operation_lock:
        path, account_data, _kind = load_saved_account_data(name)
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
            updated_data["refresh_status"] = "error"
            updated_data["refresh_error"] = str(exc)
            updated_data["refresh_error_at"] = oauth_refresh_module.current_time_iso()
            write_saved_account_batch({path: updated_data})
            raise saved_account_refresh_error_cls(f"Refresh failed for '{name}': {exc}") from exc

        updated_data["entries"] = refreshed.entries
        updated_data["last_refreshed_at"] = refreshed.refreshed_at
        updated_data["refresh_status"] = "ok"
        updated_data.pop("refresh_error", None)
        updated_data.pop("refresh_error_at", None)
        try:
            persist_refreshed_saved_account_batch(
                {path: updated_data},
                subject_label=f"saved account '{name}'",
                account_names=(name,),
                providers=providers,
                operation="manual-refresh",
            )
        except persistence_error_cls as exc:
            raise saved_account_refresh_error_cls(str(exc)) from exc

        group_label = "group" if refreshed.refreshed_groups == 1 else "groups"
        entry_label = "entry" if refreshed.refreshed_entries == 1 else "entries"
        return (
            f"Refreshed '{name}' "
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
            if not generic_source:
                raise user_facing_error_cls(f"No source entry available for '{ext_id}'.")
            print_fn(f"[cross-ext] No '{ext_id}' key — remapping from: {generic_source['key']}")
            remapped_entries.append({"key": target_key, "value": generic_source["value"]})
        apply_db_entries(remapped_entries)

    if "kilo-new" not in ext_names:
        return

    source_entry = source_kilo_new or generic_source
    if not source_entry:
        raise user_facing_error_cls("No source entry available for 'kilo-new'.")

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
    access_token = value.get("access_token")
    refresh_token = value.get("refresh_token")
    account_id = value.get("accountId") or ""
    expires_ms = value.get("expires", 0)

    if not access_token or not refresh_token:
        raise user_facing_error_cls("ERROR: access_token or refresh_token missing in auth.json")
    if not expires_ms:
        raise user_facing_error_cls("ERROR: could not decode access token expiry from auth.json")
    if not value.get("id_token"):
        raise user_facing_error_cls("ERROR: Codex import requires id_token in auth.json.")

    entry = {"key": codex_key, "value": value}
    out = write_account_file(name, "codex", "codex", [entry])
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

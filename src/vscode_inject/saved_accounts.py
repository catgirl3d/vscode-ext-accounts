import datetime
import json
import os
import tempfile
from typing import Mapping, Sequence


INVALID_ACCOUNT_NAME_CHARS = '<>:"/\\|?*'
WINDOWS_RESERVED_ACCOUNT_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class SavedAccountKindMismatchError(ValueError):
    def __init__(self, actual_kind: str):
        super().__init__(actual_kind)
        self.actual_kind = actual_kind


def ensure_accounts_dir(accounts_dir: str) -> str:
    os.makedirs(accounts_dir, exist_ok=True)
    return accounts_dir


def normalize_account_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Account name must be a string.")

    normalized = name.strip()
    if not normalized:
        raise ValueError("Account name cannot be empty.")
    if normalized in {".", ".."}:
        raise ValueError("Account name cannot be '.' or '..'.")
    if normalized.endswith((" ", ".")):
        raise ValueError("Account name cannot end with a space or dot.")

    invalid_chars = sorted({char for char in normalized if char in INVALID_ACCOUNT_NAME_CHARS or ord(char) < 32})
    if invalid_chars:
        chars = "".join(repr(char)[1:-1] for char in invalid_chars)
        raise ValueError(f"Account name contains invalid characters: {chars}")

    reserved_candidate = normalized.split(".", 1)[0].upper()
    if reserved_candidate in WINDOWS_RESERVED_ACCOUNT_NAMES:
        raise ValueError(f"Account name '{normalized}' is reserved on Windows.")

    return normalized


def saved_account_path(accounts_dir: str, name: str) -> tuple[str, str]:
    normalized_name = normalize_account_name(name)
    return os.path.join(ensure_accounts_dir(accounts_dir), f"{normalized_name}.json"), normalized_name


def _stage_saved_account_data(path: str, data: dict) -> str:
    normalized_path = os.path.abspath(path)
    directory = os.path.dirname(normalized_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".account-", suffix=".json", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return tmp_path


def _restore_account_file_bytes(path: str, original_bytes: bytes | None) -> None:
    if original_bytes is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return

    normalized_path = os.path.abspath(path)
    directory = os.path.dirname(normalized_path)
    fd, tmp_path = tempfile.mkstemp(prefix=".account-rollback-", suffix=".json", dir=directory or None)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(original_bytes)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def write_saved_account_data(path: str, data: dict) -> None:
    tmp_path = _stage_saved_account_data(path, data)
    try:
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def write_saved_account_batch(updates: Mapping[str, dict] | Sequence[tuple[str, dict]]) -> None:
    items = list(updates.items()) if isinstance(updates, Mapping) else list(updates)
    if not items:
        return

    originals: dict[str, bytes | None] = {}
    staged_paths: dict[str, str] = {}
    replaced_paths: list[str] = []
    try:
        for path, data in items:
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    originals[path] = fh.read()
            else:
                originals[path] = None
            staged_paths[path] = _stage_saved_account_data(path, data)

        for path, _data in items:
            os.replace(staged_paths[path], path)
            replaced_paths.append(path)
    except Exception:
        rollback_errors: list[Exception] = []
        for path in reversed(replaced_paths):
            try:
                _restore_account_file_bytes(path, originals.get(path))
            except Exception as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise RuntimeError(f"Failed to write saved account batch and rollback cleanly: {rollback_errors[0]}") from rollback_errors[0]
        raise
    finally:
        for path, staged_path in staged_paths.items():
            if path in replaced_paths:
                continue
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass


def _normalize_kind_keys(kind_keys: str | Mapping[str, str] | None) -> dict[str, str]:
    if isinstance(kind_keys, str):
        return {"codex": kind_keys}
    if not isinstance(kind_keys, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for kind, key in kind_keys.items():
        if isinstance(kind, str) and kind and isinstance(key, str) and key:
            normalized[kind] = key
    return normalized


def saved_account_kind(data: dict, kind_keys: str | Mapping[str, str] | None) -> str:
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind in {"ide", "codex", "omp"}:
        return kind

    entries = data.get("entries", []) if isinstance(data, dict) else []
    keys = [entry.get("key") for entry in entries if isinstance(entry, dict)]
    for candidate_kind, candidate_key in _normalize_kind_keys(kind_keys).items():
        if keys and all(key == candidate_key for key in keys):
            return candidate_kind
    return "ide"


def list_saved_accounts(accounts_dir: str, kind_keys: str | Mapping[str, str] | None, kind: str | None = None) -> list[dict]:
    records = []
    base_dir = ensure_accounts_dir(accounts_dir)
    for filename in sorted(f for f in os.listdir(base_dir) if f.endswith(".json")):
        path = os.path.join(base_dir, filename)
        name = filename[:-5]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            account_kind = saved_account_kind(data, kind_keys)
            if kind and account_kind != kind:
                continue
            records.append({
                "name": name,
                "path": path,
                "data": data,
                "kind": account_kind,
                "readable": True,
            })
        except Exception:
            if kind is None:
                records.append({
                    "name": name,
                    "path": path,
                    "data": None,
                    "kind": None,
                    "readable": False,
                })
    return records


def resolve_existing_account_path(accounts_dir: str, name: str) -> str:
    for char in name:
        if char in '/\\':
            raise ValueError("Account name contains invalid characters: '/' or '\\'")
    
    base_dir = ensure_accounts_dir(accounts_dir)
    exact_path = os.path.join(base_dir, f"{name}.json")
    if os.path.isfile(exact_path):
        return exact_path

    try:
        norm = normalize_account_name(name)
        norm_path = os.path.join(base_dir, f"{norm}.json")
        if os.path.isfile(norm_path):
            return norm_path
    except Exception:
        pass

    return exact_path


def load_saved_account(
    accounts_dir: str,
    name: str,
    kind_keys: str | Mapping[str, str] | None,
    expected_kind: str | None = None,
) -> tuple[str, dict, str]:
    path = resolve_existing_account_path(accounts_dir, name)
    if not os.path.exists(path):
        raise FileNotFoundError(name)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    kind = saved_account_kind(data, kind_keys)
    if expected_kind and kind != expected_kind:
        raise SavedAccountKindMismatchError(kind)
    return path, data, kind


def rename_saved_account(
    accounts_dir: str,
    kind_keys: str | Mapping[str, str] | None,
    name: str,
    new_name: str,
    expected_kind: str | None = None,
) -> tuple[str, dict, str]:
    normalized_new_name = normalize_account_name(new_name)

    path = resolve_existing_account_path(accounts_dir, name)
    if not os.path.exists(path):
        raise FileNotFoundError(name)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    kind = saved_account_kind(data, kind_keys)
    if expected_kind and kind != expected_kind:
        raise SavedAccountKindMismatchError(kind)

    new_path = os.path.join(ensure_accounts_dir(accounts_dir), f"{normalized_new_name}.json")
    if os.path.exists(new_path) and os.path.abspath(path).lower() != os.path.abspath(new_path).lower():
        try:
            with open(new_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            existing_kind = saved_account_kind(existing, kind_keys)
        except Exception as exc:
            raise ValueError(f"Cannot overwrite unreadable account file '{normalized_new_name}.json': {exc}") from exc
        raise ValueError(
            f"Account '{normalized_new_name}' already exists as {existing_kind}. Use a different name."
        )

    if path == new_path:
        return path, data, kind

    renamed_data = dict(data)
    renamed_data["name"] = normalized_new_name
    os.replace(path, new_path)
    try:
        write_saved_account_data(new_path, renamed_data)
    except Exception:
        try:
            os.replace(new_path, path)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"Failed to rename saved account '{name}' to '{normalized_new_name}' and rollback cleanly: {rollback_exc}"
            ) from rollback_exc
        raise

    return new_path, renamed_data, kind


def delete_saved_account(
    accounts_dir: str,
    name: str,
    kind_keys: str | Mapping[str, str] | None = None,
    expected_kind: str | None = None,
) -> None:
    path = resolve_existing_account_path(accounts_dir, name)
    if not os.path.exists(path):
        raise FileNotFoundError(name)

    if expected_kind:
        if not kind_keys:
            raise ValueError("kind_keys is required when expected_kind is provided.")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        kind = saved_account_kind(data, kind_keys)
        if kind != expected_kind:
            raise SavedAccountKindMismatchError(kind)

    os.remove(path)


def write_account_file(
    accounts_dir: str,
    kind_keys: str | Mapping[str, str] | None,
    name: str,
    kind: str,
    ext_label: str,
    entries: list[dict],
) -> str:
    out, normalized_name = saved_account_path(accounts_dir, name)
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                existing = json.load(f)
            existing_kind = saved_account_kind(existing, kind_keys)
        except Exception as exc:
            raise ValueError(f"Cannot overwrite unreadable account file '{name}.json': {exc}") from exc
        if existing_kind != kind:
            raise ValueError(
                f"Account '{name}' already exists as {existing_kind}. Use a different name for the {kind} account."
            )

    write_saved_account_data(
        out,
        {
            "name": normalized_name,
            "kind": kind,
            "ext": ext_label,
            "saved_at": datetime.datetime.now().isoformat(),
            "entries": entries,
        },
    )
    return out

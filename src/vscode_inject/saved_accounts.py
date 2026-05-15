import datetime
import json
import os
import tempfile


class SavedAccountKindMismatchError(ValueError):
    def __init__(self, actual_kind: str):
        super().__init__(actual_kind)
        self.actual_kind = actual_kind


def ensure_accounts_dir(accounts_dir: str) -> str:
    os.makedirs(accounts_dir, exist_ok=True)
    return accounts_dir


def write_saved_account_data(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".account-", suffix=".json", dir=directory or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def saved_account_kind(data: dict, codex_key: str) -> str:
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind in {"ide", "codex"}:
        return kind

    entries = data.get("entries", []) if isinstance(data, dict) else []
    keys = [entry.get("key") for entry in entries if isinstance(entry, dict)]
    if keys and all(key == codex_key for key in keys):
        return "codex"
    return "ide"


def list_saved_accounts(accounts_dir: str, codex_key: str, kind: str | None = None) -> list[dict]:
    records = []
    base_dir = ensure_accounts_dir(accounts_dir)
    for filename in sorted(f for f in os.listdir(base_dir) if f.endswith(".json")):
        path = os.path.join(base_dir, filename)
        name = filename[:-5]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            account_kind = saved_account_kind(data, codex_key)
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


def load_saved_account(accounts_dir: str, name: str, codex_key: str, expected_kind: str | None = None) -> tuple[str, dict, str]:
    path = os.path.join(ensure_accounts_dir(accounts_dir), f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(name)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    kind = saved_account_kind(data, codex_key)
    if expected_kind and kind != expected_kind:
        raise SavedAccountKindMismatchError(kind)
    return path, data, kind


def write_account_file(
    accounts_dir: str,
    codex_key: str,
    name: str,
    kind: str,
    ext_label: str,
    entries: list[dict],
) -> str:
    base_dir = ensure_accounts_dir(accounts_dir)
    out = os.path.join(base_dir, f"{name}.json")
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                existing = json.load(f)
            existing_kind = saved_account_kind(existing, codex_key)
        except Exception as exc:
            raise ValueError(f"Cannot overwrite unreadable account file '{name}.json': {exc}") from exc
        if existing_kind != kind:
            raise ValueError(
                f"Account '{name}' already exists as {existing_kind}. Use a different name for the {kind} account."
            )

    write_saved_account_data(
        out,
        {
            "name": name,
            "kind": kind,
            "ext": ext_label,
            "saved_at": datetime.datetime.now().isoformat(),
            "entries": entries,
        },
    )
    return out

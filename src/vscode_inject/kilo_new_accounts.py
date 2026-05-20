from __future__ import annotations

import hashlib
import json
import os


def read_kilo_auth(auth_path: str) -> dict:
    if not os.path.exists(auth_path):
        return {}
    with open(auth_path, encoding="utf-8") as fh:
        return json.load(fh)


def write_kilo_auth(auth_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(auth_path), exist_ok=True)
    with open(auth_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def to_kilo_new_format(value: dict) -> dict:
    return {
        "type": "oauth",
        "access": value.get("access_token") or value.get("access", ""),
        "refresh": value.get("refresh_token") or value.get("refresh", ""),
        "expires": value.get("expires", 0),
        "accountId": value.get("accountId", ""),
    }


def from_kilo_new_format(value: dict) -> dict:
    return {
        "type": "openai-codex",
        "access_token": value.get("access", ""),
        "refresh_token": value.get("refresh", ""),
        "expires": value.get("expires", 0),
        "accountId": value.get("accountId", ""),
    }


def get_kilo_new_fingerprint(auth_path: str) -> str | None:
    auth = read_kilo_auth(auth_path)
    openai_entry = auth.get("openai")
    if not isinstance(openai_entry, dict):
        return None

    refresh = openai_entry.get("refresh")
    if not refresh:
        return None
    return hashlib.sha256(str(refresh).encode("utf-8")).hexdigest()


def read_current_kilo_new_account(
    auth_path: str,
    kilo_new_key: str,
    account_fingerprint,
) -> dict[str, dict]:
    auth = read_kilo_auth(auth_path)
    openai_entry = auth.get("openai")
    if not isinstance(openai_entry, dict):
        return {}

    info = {
        "accountId": openai_entry.get("accountId", "?"),
        "fingerprint": account_fingerprint(openai_entry),
        "expires": openai_entry.get("expires"),
    }
    return {kilo_new_key: info}

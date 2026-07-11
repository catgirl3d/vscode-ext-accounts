from __future__ import annotations

from typing import Any, Mapping


def normalized_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def identity_key_for_value(value: Mapping[str, Any]) -> str | None:
    keys = identity_keys_for_value(value)
    return keys[0] if keys else None


def identity_keys_for_value(value: Mapping[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []

    account_id = value.get("accountId") or value.get("account_id")
    if isinstance(account_id, str) and account_id:
        keys.append(f"account:{account_id}")

    email = normalized_email(value.get("email"))
    if email:
        email_key = f"email:{email}"
        if email_key not in keys:
            keys.append(email_key)

    return tuple(keys)


def identity_key_for_entry(entry: Mapping[str, Any]) -> str | None:
    keys = identity_keys_for_entry(entry)
    return keys[0] if keys else None


def identity_keys_for_entry(entry: Mapping[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []

    explicit_identity_key = entry.get("identity_key")
    if isinstance(explicit_identity_key, str) and explicit_identity_key:
        keys.append(explicit_identity_key)

    value = entry.get("value")
    if not isinstance(value, Mapping):
        return tuple(keys)

    embedded_identity_key = value.get("identity_key")
    if isinstance(embedded_identity_key, str) and embedded_identity_key:
        if embedded_identity_key not in keys:
            keys.append(embedded_identity_key)

    for identity_key in identity_keys_for_value(value):
        if identity_key not in keys:
            keys.append(identity_key)

    return tuple(keys)

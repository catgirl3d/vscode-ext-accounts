from __future__ import annotations

from typing import Any, Mapping


def normalized_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def identity_key_for_value(value: Mapping[str, Any]) -> str | None:
    email = normalized_email(value.get("email"))
    if email:
        return f"email:{email}"

    account_id = value.get("accountId") or value.get("account_id")
    if isinstance(account_id, str) and account_id:
        return f"account:{account_id}"
    return None

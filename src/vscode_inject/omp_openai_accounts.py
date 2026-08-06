from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Mapping, Sequence

from .jwt_utils import decode_jwt_exp_ms
from .openai_codex import OPENAI_CODEX_PROVIDER
from .openai_identity import identity_key_for_entry, identity_key_for_value, normalized_email as _normalized_email


OAUTH_CREDENTIAL_TYPE = "oauth"
REPLACED_DISABLED_CAUSE = "replaced by vscode-ext-accounts"


def _as_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def from_omp_auth_format(value: Mapping[str, Any], *, identity_key: str | None = None) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "type": OPENAI_CODEX_PROVIDER,
        "access_token": _as_string(value.get("access")),
        "refresh_token": _as_string(value.get("refresh")),
        "expires": value.get("expires") if isinstance(value.get("expires"), int) else 0,
        "accountId": _as_string(value.get("accountId")),
    }

    email = _normalized_email(value.get("email"))
    if email:
        normalized["email"] = email

    id_token = value.get("id_token")
    if isinstance(id_token, str) and id_token:
        normalized["id_token"] = id_token

    if isinstance(identity_key, str) and identity_key:
        normalized["identity_key"] = identity_key

    return normalized


def from_omp_import_format(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_tokens = value.get("tokens")
    tokens: Mapping[str, Any] = raw_tokens if isinstance(raw_tokens, Mapping) else {}

    raw_inner = value.get("value")
    inner: Mapping[str, Any] = raw_inner if isinstance(raw_inner, Mapping) else {}

    access_token = (
        tokens.get("access_token")
        or tokens.get("access")
        or tokens.get("accessToken")
        or value.get("access_token")
        or value.get("access")
        or value.get("accessToken")
        or inner.get("access_token")
        or inner.get("access")
        or inner.get("accessToken")
    )
    access_token_str = access_token if isinstance(access_token, str) else ""

    refresh_token = (
        tokens.get("refresh_token")
        or tokens.get("refresh")
        or tokens.get("refreshToken")
        or value.get("refresh_token")
        or value.get("refresh")
        or value.get("refreshToken")
        or inner.get("refresh_token")
        or inner.get("refresh")
        or inner.get("refreshToken")
    )
    refresh_token_str = refresh_token if isinstance(refresh_token, str) else ""

    account_id = (
        tokens.get("account_id")
        or tokens.get("accountId")
        or value.get("account_id")
        or value.get("accountId")
        or inner.get("account_id")
        or inner.get("accountId")
    )
    account_id_str = account_id if isinstance(account_id, str) else ""

    expires = value.get("expires") or tokens.get("expires") or inner.get("expires")
    expires_ms = expires if type(expires) is int else 0
    if not expires_ms:
        expires_ms = decode_jwt_exp_ms(access_token_str)

    normalized: dict[str, Any] = {
        "type": OPENAI_CODEX_PROVIDER,
        "access_token": access_token_str,
        "refresh_token": refresh_token_str,
        "expires": expires_ms,
        "accountId": account_id_str,
    }

    email = (
        _normalized_email(value.get("email"))
        or _normalized_email(tokens.get("email"))
        or _normalized_email(inner.get("email"))
    )
    if email:
        normalized["email"] = email

    id_token = (
        tokens.get("id_token")
        or tokens.get("idToken")
        or value.get("id_token")
        or value.get("idToken")
        or inner.get("id_token")
        or inner.get("idToken")
    )
    if isinstance(id_token, str) and id_token:
        normalized["id_token"] = id_token

    explicit_identity_key = (
        value.get("identity_key")
        or tokens.get("identity_key")
        or inner.get("identity_key")
    )
    if isinstance(explicit_identity_key, str) and explicit_identity_key:
        normalized["identity_key"] = explicit_identity_key
    else:
        derived_identity_key = identity_key_for_value(normalized)
        if derived_identity_key:
            normalized["identity_key"] = derived_identity_key

    return normalized


def to_omp_auth_format(value: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    existing_value = dict(existing) if isinstance(existing, Mapping) else {}

    account_id = value.get("accountId") or value.get("account_id") or existing_value.get("accountId")
    normalized_account_id = _as_string(account_id)

    email = _normalized_email(value.get("email"))
    if email is None and normalized_account_id and existing_value.get("accountId") == normalized_account_id:
        email = _normalized_email(existing_value.get("email"))

    out = dict(existing_value)
    out.pop("type", None)
    out.pop("identity_key", None)
    out["access"] = _as_string(value.get("access_token") or value.get("access"))
    out["refresh"] = _as_string(value.get("refresh_token") or value.get("refresh"))
    out["expires"] = value.get("expires") if isinstance(value.get("expires"), int) else 0
    out["accountId"] = normalized_account_id
    if email:
        out["email"] = email
    else:
        out.pop("email", None)

    id_token = value.get("id_token")
    if isinstance(id_token, str) and id_token:
        out["id_token"] = id_token
    else:
        out.pop("id_token", None)
    return out


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=5.0)


def _ensure_auth_credentials_table(con: sqlite3.Connection, db_path: str) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'auth_credentials'"
    ).fetchone()
    if row is None:
        raise ValueError(f"OMP DB has no auth_credentials table: {db_path}")


def list_openai_credentials(db_path: str, provider: str = OPENAI_CODEX_PROVIDER) -> list[dict[str, Any]]:
    if not os.path.exists(db_path):
        return []

    con = _connect(db_path)
    try:
        _ensure_auth_credentials_table(con, db_path)
        rows = con.execute(
            """
            SELECT id, data, identity_key
            FROM auth_credentials
            WHERE provider = ? AND credential_type = ? AND disabled_cause IS NULL
            ORDER BY id ASC
            """,
            (provider, OAUTH_CREDENTIAL_TYPE),
        ).fetchall()
    finally:
        con.close()

    credentials: list[dict[str, Any]] = []
    for row_id, raw_data, identity_key in rows:
        try:
            parsed = json.loads(raw_data)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        credentials.append(
            {
                "id": row_id,
                "data": parsed,
                "identity_key": identity_key if isinstance(identity_key, str) and identity_key else None,
            }
        )
    return credentials


def read_current_openai_entries(db_path: str, omp_key: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for credential in list_openai_credentials(db_path):
        entry: dict[str, Any] = {
            "key": omp_key,
            "value": from_omp_auth_format(
                credential["data"],
                identity_key=credential.get("identity_key"),
            ),
        }
        identity_key = credential.get("identity_key")
        if isinstance(identity_key, str) and identity_key:
            entry["identity_key"] = identity_key
        entries.append(entry)
    return entries


def read_current_openai_accounts(db_path: str, omp_key: str, fingerprint_func) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for entry in read_current_openai_entries(db_path, omp_key):
        value = entry.get("value", {})
        if not isinstance(value, dict):
            continue
        accounts.append(
            {
                "key": omp_key,
                "accountId": value.get("accountId", "?"),
                "fingerprint": fingerprint_func(value),
                "expires": value.get("expires"),
                "email": value.get("email"),
            }
        )
    return accounts


def replace_openai_credentials(
    db_path: str,
    entries: Sequence[Mapping[str, Any]],
    *,
    provider: str = OPENAI_CODEX_PROVIDER,
    disabled_cause: str = REPLACED_DISABLED_CAUSE,
) -> None:
    if not os.path.exists(db_path):
        raise ValueError(f"OMP DB not found: {db_path}")
    if not entries:
        raise ValueError("Selected account does not contain any OMP OpenAI entries.")

    payloads: list[tuple[str, str | None]] = []
    for entry in entries:
        value = entry.get("value")
        if not isinstance(value, Mapping):
            raise ValueError("OMP entry payload must contain an object value.")

        raw_value = to_omp_auth_format(value)
        if not raw_value.get("access") or not raw_value.get("refresh"):
            raise ValueError("OMP entry is missing access_token or refresh_token.")

        identity_key = identity_key_for_entry(entry) or identity_key_for_value(raw_value)

        payloads.append((json.dumps(raw_value, separators=(",", ":"), ensure_ascii=False), identity_key))

    con = _connect(db_path)
    try:
        _ensure_auth_credentials_table(con, db_path)
        with con:
            # OMP can keep multiple active credentials for one provider, so replace the full active set atomically.
            con.execute(
                """
                UPDATE auth_credentials
                SET disabled_cause = ?, updated_at = CAST(strftime('%s','now') AS INTEGER)
                WHERE provider = ? AND disabled_cause IS NULL
                """,
                (disabled_cause, provider),
            )
            con.executemany(
                """
                INSERT INTO auth_credentials (provider, credential_type, data, identity_key)
                VALUES (?, ?, ?, ?)
                """,
                [(provider, OAUTH_CREDENTIAL_TYPE, data, identity_key) for data, identity_key in payloads],
            )
    finally:
        con.close()

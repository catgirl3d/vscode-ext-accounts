from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Mapping


KILO_OPENCODE_GO_PROVIDER = "opencode-go"
KILO_OPENCODE_GO_KEY = "kilo://opencode-go"
KILO_CREDENTIAL_TYPE = "key"
KILO_AUTH_TYPE = "api"
KILO_IMPORTED_LABEL = "Imported"
_AUTH_WRITE_LOCK = threading.Lock()


def _as_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=5.0)


def _ensure_credential_table(con: sqlite3.Connection, db_path: str) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'credential'"
    ).fetchone()
    if row is None:
        raise ValueError(f"Kilo DB has no credential table: {db_path}")


def _select_credential_row(
    con: sqlite3.Connection,
    provider: str,
    columns: str,
) -> tuple | None:
    return con.execute(
        f"""
        SELECT {columns}
        FROM credential
        WHERE integration_id = ?
        ORDER BY CASE WHEN active = 1 THEN 0 ELSE 1 END, time_created ASC
        LIMIT 1
        """,
        (provider,),
    ).fetchone()


def _read_credential_row(
    db_path: str,
    provider: str,
) -> tuple[str, str, str] | None:
    if not os.path.exists(db_path):
        return None

    con = _connect(db_path)
    try:
        _ensure_credential_table(con, db_path)
        row = _select_credential_row(con, provider, "id, label, value")
    finally:
        con.close()
    if row is None:
        return None
    return str(row[0]), str(row[1]), _as_string(row[2])


def _key_from_credential_value(raw_value: str) -> str | None:
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    key = _as_string(parsed.get("key")).strip()
    return key or None


def read_current_key(db_path: str, provider: str = KILO_OPENCODE_GO_PROVIDER) -> str | None:
    row = _read_credential_row(db_path, provider)
    if row is None:
        return None
    return _key_from_credential_value(row[2])


def read_current_entry(
    db_path: str,
    entry_key: str,
    provider: str = KILO_OPENCODE_GO_PROVIDER,
) -> dict[str, Any] | None:
    key = read_current_key(db_path, provider)
    if not key:
        return None
    return {"key": entry_key, "value": {"key": key}}


def from_import_format(data: Any) -> dict[str, Any]:
    if isinstance(data, str):
        key = data.strip()
    elif isinstance(data, Mapping):
        key = _as_string(data.get("key")).strip()
    else:
        key = ""

    if not key:
        raise ValueError("API key is missing. Expected a JSON string or an object with a 'key' field.")
    return {"key": key}


def _current_time_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _write_auth_mirror(auth_path: str, provider: str, key: str) -> bytes | None:
    original_bytes: bytes | None = None
    if os.path.exists(auth_path):
        with open(auth_path, "rb") as fh:
            original_bytes = fh.read()
        try:
            auth = json.loads(original_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Cannot parse Kilo auth.json: {exc}") from exc
        if not isinstance(auth, dict):
            raise ValueError("Kilo auth.json must contain a JSON object.")
    else:
        auth = {}

    auth[provider] = {"type": KILO_AUTH_TYPE, "key": key}
    payload = json.dumps(auth, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(auth_path, payload)
    return original_bytes


def _restore_auth_mirror(auth_path: str, original_bytes: bytes | None) -> None:
    if original_bytes is None:
        try:
            os.unlink(auth_path)
        except FileNotFoundError:
            pass
        return
    _atomic_write_bytes(auth_path, original_bytes)


def _replace_credential(db_path: str, provider: str, key: str) -> None:
    con = _connect(db_path)
    try:
        _ensure_credential_table(con, db_path)
        now_ms = _current_time_ms()
        con.execute("BEGIN IMMEDIATE")
        try:
            row = _select_credential_row(con, provider, "id")
            value = json.dumps({"type": KILO_CREDENTIAL_TYPE, "key": key}, separators=(",", ":"), ensure_ascii=False)
            if row is None:
                con.execute(
                    """
                    INSERT INTO credential(id, integration_id, label, value, time_created, time_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (f"cred_{uuid.uuid4().hex}", provider, KILO_IMPORTED_LABEL, value, now_ms, now_ms),
                )
            else:
                cursor = con.execute(
                    "UPDATE credential SET value = ?, time_updated = ? WHERE id = ?",
                    (value, now_ms, row[0]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Kilo credential row disappeared during the update.")
            con.commit()
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()


def replace_key(
    db_path: str,
    auth_path: str,
    key: str,
    provider: str = KILO_OPENCODE_GO_PROVIDER,
) -> None:
    normalized_key = _as_string(key).strip()
    if not normalized_key:
        raise ValueError("API key cannot be empty.")
    if not os.path.exists(db_path):
        raise ValueError(f"Kilo DB not found: {db_path}")

    with _AUTH_WRITE_LOCK:
        original_auth = _write_auth_mirror(auth_path, provider, normalized_key)
        try:
            _replace_credential(db_path, provider, normalized_key)
        except Exception as db_exc:
            try:
                _restore_auth_mirror(auth_path, original_auth)
            except Exception as restore_exc:
                raise RuntimeError(
                    f"Kilo DB write failed ({db_exc}) and auth.json rollback failed ({restore_exc})."
                ) from db_exc
            raise

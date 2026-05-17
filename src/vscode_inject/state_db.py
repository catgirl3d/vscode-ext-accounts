from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import tempfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from typing import Callable, Sequence


def get_aes_key(local_state_path: str, *, print_fn: Callable[[str], None] = print) -> bytes | None:
    try:
        with open(local_state_path, "r", encoding="utf-8") as fh:
            local_state = json.load(fh)
        enc_key_b64 = local_state["os_crypt"]["encrypted_key"]
        enc_key = base64.b64decode(enc_key_b64)
        assert enc_key[:5] == b"DPAPI", "Expected DPAPI prefix"
        dpapi_blob = enc_key[5:]

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        p_in = DATA_BLOB(len(dpapi_blob), ctypes.cast(ctypes.c_char_p(dpapi_blob), ctypes.POINTER(ctypes.c_char)))
        p_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(p_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(p_out),
        )
        if not ok:
            raise RuntimeError("CryptUnprotectData failed")
        key = ctypes.string_at(p_out.pbData, p_out.cbData)
        ctypes.windll.kernel32.LocalFree(p_out.pbData)
        return key
    except Exception as exc:
        print_fn(f"[warn] Could not get AES key: {exc}")
        return None


def decrypt_value(raw: bytes, aes_key: bytes | None) -> str:
    if not raw:
        return ""
    if not raw.startswith(b"v10"):
        try:
            return raw.decode("utf-8")
        except Exception:
            return repr(raw[:200])

    if aes_key is None:
        return f"<encrypted v10, {len(raw)} bytes — DPAPI key unavailable>"

    try:
        nonce = raw[3:15]
        ct_and_tag = raw[15:]
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ct_and_tag, None)
        return plaintext.decode("utf-8")
    except Exception as exc:
        return f"<decrypt failed: {exc}>"


def encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
    aesgcm = AESGCM(aes_key)
    nonce = os.urandom(12)
    ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return b"v10" + nonce + ct_and_tag


def decode_entry(
    value,
    aes_key,
    *,
    decrypt_value_fn: Callable[[bytes, bytes | None], str] | None = None,
) -> str:
    decryptor = decrypt_value_fn or decrypt_value
    if isinstance(value, bytes):
        return decryptor(value, aes_key)
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict) and obj.get("type") == "Buffer" and "data" in obj:
                return decryptor(bytes(obj["data"]), aes_key)
        except Exception:
            pass
        return value
    return str(value) if value is not None else ""


def _copied_db_path(db_path: str) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    shutil.copy2(db_path, handle.name)
    return handle.name


def _extension_id_from_secret_key(key: str) -> str:
    if not key.startswith("secret://"):
        return ""
    try:
        payload = json.loads(key[len("secret://"):])
    except Exception:
        return ""
    ext_id = payload.get("extensionId", "")
    return ext_id if isinstance(ext_id, str) else ""


def _secret_storage_key(extension_id: object, oauth_key: object) -> str:
    payload = {"extensionId": str(extension_id), "key": str(oauth_key)}
    return f"secret://{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"


def _escape_like_fragment(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def read_current_accounts(
    db_path: str,
    local_state_path: str,
    oauth_key: str,
    *,
    get_aes_key_fn: Callable[[str], bytes | None],
    decode_entry_fn,
    account_fingerprint,
) -> dict[str, dict]:
    if not os.path.exists(db_path):
        return {}

    aes_key = get_aes_key_fn(local_state_path)
    tmp_path = _copied_db_path(db_path)
    con = sqlite3.connect(tmp_path)
    result: dict[str, dict] = {}
    oauth_key_pattern = (
        'secret://{"extensionId":"%","key":"' + _escape_like_fragment(str(oauth_key)) + '"}'
    )
    try:
        for key, value in con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (oauth_key_pattern,),
        ):
            ext_id = _extension_id_from_secret_key(key)
            decoded = decode_entry_fn(value, aes_key)
            try:
                decoded_parsed = json.loads(decoded)
            except Exception:
                decoded_parsed = decoded
            if isinstance(decoded_parsed, dict) and ext_id:
                result[ext_id] = {
                    "accountId": decoded_parsed.get("accountId", "?"),
                    "fingerprint": account_fingerprint(decoded_parsed),
                    "expires": decoded_parsed.get("expires"),
                }
    finally:
        con.close()
        os.unlink(tmp_path)
    return result


def read_entries_for_extension_ids(
    db_path: str,
    local_state_path: str,
    oauth_key: str,
    extension_ids: Sequence[str],
    *,
    get_aes_key_fn: Callable[[str], bytes | None],
    decode_entry_fn,
) -> list[dict]:
    if not extension_ids or not os.path.exists(db_path):
        return []

    aes_key = get_aes_key_fn(local_state_path)
    tmp_path = _copied_db_path(db_path)
    con = sqlite3.connect(tmp_path)
    entries: list[dict] = []
    target_keys = [_secret_storage_key(ext_id, oauth_key) for ext_id in extension_ids]
    placeholders = ", ".join("?" for _ in target_keys)
    try:
        query = f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders}) ORDER BY key"
        for key, value in con.execute(query, target_keys):
            decoded = decode_entry_fn(value, aes_key)
            try:
                decoded_parsed = json.loads(decoded)
            except Exception:
                decoded_parsed = decoded
            entries.append({"key": key, "value": decoded_parsed})
    finally:
        con.close()
        os.unlink(tmp_path)
    return entries


def serialize_entry_value(
    key: str,
    value,
    aes_key: bytes,
    *,
    encrypt_value_fn: Callable[[str, bytes], bytes] = encrypt_value,
) -> str:
    is_secret = key.startswith("secret://")
    if is_secret:
        plaintext = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if not plaintext:
            return json.dumps({"type": "Buffer", "data": []})
        encrypted = encrypt_value_fn(plaintext, aes_key)
        return json.dumps({"type": "Buffer", "data": list(encrypted)})

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value) if value is not None else ""


def write_entries_to_db(
    db_path: str,
    entries: Sequence[dict],
    aes_key: bytes,
    *,
    encrypt_value_fn: Callable[[str, bytes], bytes] = encrypt_value,
    print_fn: Callable[[str], None] | None = print,
) -> tuple[int, int]:
    con = sqlite3.connect(db_path)
    restored = 0
    skipped = 0
    try:
        for entry in entries:
            key = entry["key"]
            value = entry["value"]
            db_value = serialize_entry_value(key, value, aes_key, encrypt_value_fn=encrypt_value_fn)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                    (key, db_value),
                )
                restored += 1
                if print_fn:
                    print_fn(f"  [OK] {key}")
            except Exception as exc:
                skipped += 1
                if print_fn:
                    print_fn(f"  [FAIL] {key}: {exc}")
        con.commit()
    finally:
        con.close()
    return restored, skipped

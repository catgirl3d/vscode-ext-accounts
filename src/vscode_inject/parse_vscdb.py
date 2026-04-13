"""
Parse VSCode state.vscdb and find Kilocode / ChatGPT related entries.
On Windows, encrypted values (v10 prefix) are decrypted via DPAPI + AES-256-GCM.
"""

import sqlite3
import json
import os
import sys
import base64
import datetime
import zipfile

from . import codex_accounts as codex_store
from . import saved_accounts as saved_store

IDE_PATHS = {
    "vscode": {
        "label": "VSCode",
        "db": os.path.expandvars(r"%APPDATA%\Code\User\globalStorage\state.vscdb"),
        "local_state": os.path.expandvars(r"%APPDATA%\Code\Local State"),
        "process": "Code.exe",
    },
    "antigravity": {
        "label": "Antigravity",
        "db": os.path.expandvars(r"%APPDATA%\Antigravity\User\globalStorage\state.vscdb"),
        "local_state": os.path.expandvars(r"%APPDATA%\Antigravity\Local State"),
        "process": "Antigravity.exe",
    },
}
CURRENT_IDE = "vscode"
DB_PATH = IDE_PATHS["vscode"]["db"]
LOCAL_STATE_PATH = IDE_PATHS["vscode"]["local_state"]


def set_ide(name: str):
    global DB_PATH, LOCAL_STATE_PATH, CURRENT_IDE
    if name not in IDE_PATHS:
        valid = ", ".join(IDE_PATHS)
        raise ValueError(f"Unknown IDE '{name}'. Expected one of: {valid}")
    cfg = IDE_PATHS[name]
    DB_PATH = cfg["db"]
    LOCAL_STATE_PATH = cfg["local_state"]
    CURRENT_IDE = name


# ── Decryption ────────────────────────────────────────────────────────────────

def get_aes_key(local_state_path: str | None = None):
    """Read encrypted_key from Local State and decrypt with DPAPI."""
    try:
        with open(local_state_path or LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        enc_key_b64 = local_state["os_crypt"]["encrypted_key"]
        enc_key = base64.b64decode(enc_key_b64)
        assert enc_key[:5] == b"DPAPI", "Expected DPAPI prefix"
        dpapi_blob = enc_key[5:]

        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        p_in = DATA_BLOB(len(dpapi_blob), ctypes.cast(ctypes.c_char_p(dpapi_blob), ctypes.POINTER(ctypes.c_char)))
        p_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(p_in), None, None, None, None, 0, ctypes.byref(p_out)
        )
        if not ok:
            raise RuntimeError("CryptUnprotectData failed")
        key = ctypes.string_at(p_out.pbData, p_out.cbData)
        ctypes.windll.kernel32.LocalFree(p_out.pbData)
        return key
    except Exception as e:
        print(f"[warn] Could not get AES key: {e}")
        return None


def decrypt_value(raw: bytes, aes_key: bytes | None) -> str:
    """Try to decrypt a v10-encrypted value, fall back to raw."""
    if not raw:
        return ""
    # Plain text (not encrypted)
    if not raw.startswith(b"v10"):
        try:
            return raw.decode("utf-8")
        except Exception:
            return repr(raw[:200])

    if aes_key is None:
        return f"<encrypted v10, {len(raw)} bytes — DPAPI key unavailable>"

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        # Layout: b"v10" + 12-byte nonce + ciphertext + 16-byte tag
        nonce = raw[3:15]
        ct_and_tag = raw[15:]
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ct_and_tag, None)
        return plaintext.decode("utf-8")
    except Exception as e:
        return f"<decrypt failed: {e}>"


# ── Main ──────────────────────────────────────────────────────────────────────

def is_ide_running(ide: str | None = None) -> bool:
    """Check if the given IDE process is currently running."""
    import subprocess
    process = IDE_PATHS[ide or CURRENT_IDE]["process"]
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {process}", "/NH", "/FO", "CSV"],
        capture_output=True, text=True
    )
    return process in result.stdout


def guard_vscode_closed():
    """Exit with error if the current IDE is running."""
    if is_ide_running():
        label = IDE_PATHS[CURRENT_IDE]["label"]
        print(f"ERROR: {label} is running. Close it before making changes.")
        sys.exit(1)


def encrypt_value(plaintext: str, aes_key: bytes) -> bytes:
    """Encrypt a string back to v10 + AES-256-GCM format."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os as _os
    aesgcm = AESGCM(aes_key)
    nonce = _os.urandom(12)
    ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return b"v10" + nonce + ct_and_tag


def _decode_entry(value, aes_key):
    """Shared decode logic for backup/get."""
    if isinstance(value, bytes):
        return decrypt_value(value, aes_key)
    elif isinstance(value, str):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict) and obj.get("type") == "Buffer" and "data" in obj:
                return decrypt_value(bytes(obj["data"]), aes_key)
        except Exception:
            pass
        return value
    return str(value) if value is not None else ""


PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.dirname(PACKAGE_ROOT)
PROJECT_ROOT = os.path.dirname(SRC_ROOT)


def _backups_dir() -> str:
    path = os.path.join(PROJECT_ROOT, "backups")
    os.makedirs(path, exist_ok=True)
    return path


def _default_backup_zip_path(prefix: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ide = CURRENT_IDE.replace(" ", "_")
    return os.path.join(_backups_dir(), f"{prefix}_{ide}_{ts}.zip")


def _full_backup_targets() -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for ide_name, cfg in IDE_PATHS.items():
        label = cfg["label"]
        is_current = ide_name == CURRENT_IDE
        targets.append({
            "source": cfg["db"],
            "archive_path": f"ides/{ide_name}/state.vscdb",
            "label": f"{label} state.vscdb",
            "required": is_current,
        })
        targets.append({
            "source": cfg["local_state"],
            "archive_path": f"ides/{ide_name}/Local State",
            "label": f"{label} Local State",
            "required": is_current,
        })

    targets.append({
        "source": KILO_AUTH_PATH,
        "archive_path": "shared/kilo/auth.json",
        "label": "Kilo New auth.json",
        "required": False,
    })
    targets.append({
        "source": CODEX_AUTH_PATH,
        "archive_path": "shared/codex/auth.json",
        "label": "Codex auth.json",
        "required": False,
    })
    return targets


def _prewrite_backup_targets(*, include_db: bool, include_kilo: bool, include_codex: bool) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    if include_db:
        cfg = IDE_PATHS[CURRENT_IDE]
        ide_name = CURRENT_IDE
        label = cfg["label"]
        targets.append({
            "source": cfg["db"],
            "archive_path": f"prewrite/{ide_name}/state.vscdb",
            "label": f"{label} state.vscdb",
            "required": True,
        })
        targets.append({
            "source": cfg["local_state"],
            "archive_path": f"prewrite/{ide_name}/Local State",
            "label": f"{label} Local State",
            "required": True,
        })
    if include_kilo:
        targets.append({
            "source": KILO_AUTH_PATH,
            "archive_path": "prewrite/shared/kilo/auth.json",
            "label": "Kilo New auth.json",
            "required": False,
        })
    if include_codex:
        targets.append({
            "source": CODEX_AUTH_PATH,
            "archive_path": "prewrite/shared/codex/auth.json",
            "label": "Codex auth.json",
            "required": False,
        })
    return targets


def _create_backup_archive(targets: list[dict[str, object]], out_path: str | None = None, *, backup_kind: str, note: str | None = None, fail_on_required_missing: bool = False) -> dict:
    if out_path is None:
        out_path = _default_backup_zip_path(backup_kind)
    elif not out_path.lower().endswith(".zip"):
        out_path = out_path + ".zip"

    manifest = {
        "version": 2,
        "kind": backup_kind,
        "created_at": datetime.datetime.now().isoformat(),
        "current_ide": CURRENT_IDE,
        "note": note,
        "files": [],
        "warnings": [],
    }

    for target in targets:
        source = str(target["source"])
        archive_path = str(target["archive_path"])
        label = str(target["label"])
        exists = os.path.exists(source)
        entry = {
            "label": label,
            "source": source,
            "archive_path": archive_path,
            "exists": exists,
            "required": bool(target.get("required", False)),
        }
        if exists:
            stat = os.stat(source)
            entry["size"] = stat.st_size
            entry["modified_at"] = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
        manifest["files"].append(entry)

    missing_entries = [entry for entry in manifest["files"] if not entry["exists"]]
    required_missing_entries = [entry for entry in missing_entries if entry["required"]]
    optional_missing_entries = [entry for entry in missing_entries if not entry["required"]]
    included = sum(1 for entry in manifest["files"] if entry["exists"])
    total = len(manifest["files"])
    if required_missing_entries:
        warning = f"Missing {len(required_missing_entries)} required target file(s)"
        manifest["warnings"].append(warning)
    if optional_missing_entries:
        manifest["warnings"].append(f"Skipped {len(optional_missing_entries)} optional missing file(s)")

    if required_missing_entries:
        warning = manifest["warnings"][0]
        print(f"WARNING: {warning}")
        for entry in required_missing_entries:
            print(f"  - {entry['label']}: {entry['source']}")

    if optional_missing_entries:
        print(f"INFO: Skipped {len(optional_missing_entries)} optional missing file(s)")
        for entry in optional_missing_entries:
            print(f"  - {entry['label']}: {entry['source']}")

    if included == 0:
        raise RuntimeError("Backup failed: none of the target files exist.")

    if fail_on_required_missing and required_missing_entries:
        raise RuntimeError("Backup failed: required target files are missing.")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in manifest["files"]:
            if entry["exists"]:
                zf.write(entry["source"], entry["archive_path"])
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    print(f"Backup saved: {out_path}")
    print(f"Files included: {included}/{total}")
    return {
        "path": out_path,
        "included": included,
        "total": total,
        "missing": missing_entries,
        "required_missing": required_missing_entries,
        "optional_missing": optional_missing_entries,
    }


def create_prewrite_backup(*, include_db: bool = False, include_kilo: bool = False, include_codex: bool = False, note: str | None = None) -> dict | None:
    targets = _prewrite_backup_targets(include_db=include_db, include_kilo=include_kilo, include_codex=include_codex)
    if not targets:
        return None

    existing_targets = [target for target in targets if os.path.exists(str(target["source"]))]
    has_required_targets = any(bool(target.get("required", False)) for target in targets)
    if not existing_targets and not has_required_targets:
        print("INFO: Skipped pre-write backup because the target file does not exist yet")
        for target in targets:
            print(f"  - {target['label']}: {target['source']}")
        return None

    return _create_backup_archive(targets, backup_kind="prewrite", note=note, fail_on_required_missing=True)


def restore(backup_path: str, key_filter: str | None = None, *, create_safety_backup: bool = True):
    """Restore entry-based JSON data into state.vscdb.

    This is an internal helper used for remapped IDE slot writes.
    The currently selected IDE must be closed before running this.
    """
    if not os.path.exists(backup_path):
        print(f"Backup file not found: {backup_path}")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    with open(backup_path, "r", encoding="utf-8") as f:
        backup_data = json.load(f)

    all_entries = backup_data.get("entries", [])
    if not all_entries:
        print("No entries in backup.")
        sys.exit(1)

    entries = [e for e in all_entries if key_filter is None or key_filter.lower() in e["key"].lower()]
    if not entries:
        print(f"No keys matching '{key_filter}' in backup.")
        print("Available keys:")
        for e in all_entries:
            print(f"  {e['key']}")
        sys.exit(1)

    guard_vscode_closed()

    aes_key = get_aes_key()
    if aes_key is None:
        print("ERROR: Cannot get AES key — cannot encrypt values.")
        sys.exit(1)

    if create_safety_backup:
        create_prewrite_backup(include_db=True, note=f"before restore from {os.path.basename(backup_path)}")
        print()

    print(f"Backup: {backup_path}")
    print(f"Entries to restore: {len(entries)}")
    print(f"Target DB: {DB_PATH}")
    print()

    con = sqlite3.connect(DB_PATH)
    restored = 0
    skipped = 0

    for entry in entries:
        key = entry["key"]
        value = entry["value"]

        # Determine how the value was originally stored:
        # secret:// keys ->Buffer-wrapped encrypted bytes
        # plain keys ->JSON string or plain string
        is_secret = key.startswith("secret://")

        if is_secret:
            # Serialize value back to JSON string if it's a dict/list
            if isinstance(value, (dict, list)):
                plaintext = json.dumps(value, ensure_ascii=False)
            else:
                plaintext = str(value)

            if not plaintext:
                # Store as empty Buffer
                db_value = json.dumps({"type": "Buffer", "data": []})
            else:
                encrypted = encrypt_value(plaintext, aes_key)
                db_value = json.dumps({"type": "Buffer", "data": list(encrypted)})
        else:
            # Non-secret: store as plain JSON string
            if isinstance(value, (dict, list)):
                db_value = json.dumps(value, ensure_ascii=False)
            else:
                db_value = str(value) if value is not None else ""

        try:
            con.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (key, db_value)
            )
            print(f"  [OK] {key}")
            restored += 1
        except Exception as e:
            print(f"  [FAIL] {key}: {e}")
            skipped += 1

    con.commit()
    con.close()

    print()
    print(f"Restored: {restored}  Skipped: {skipped}")
    print(f"Done. Start {IDE_PATHS[CURRENT_IDE]['label']} now.")


def backup(out_path: str | None = None):
    """Create a real full-file backup archive of all storages used by the app."""
    result = _create_backup_archive(_full_backup_targets(), out_path, backup_kind="full")
    message = f"Full backup saved ({result['included']}/{result['total']} files)."
    if result["required_missing"]:
        message += f" Warning: {len(result['required_missing'])} required file(s) were missing."
    if result["optional_missing"]:
        message += f" Skipped {len(result['optional_missing'])} optional missing file(s)."
    return message


ACCOUNTS_DIR = os.path.join(PROJECT_ROOT, "accounts")
OAUTH_KEY = "openai-codex-oauth-credentials"
KILO_AUTH_PATH = os.path.join(os.path.expanduser("~"), ".local", "share", "kilo", "auth.json")
KILO_NEW_KEY = "kilo-new://openai"
CODEX_AUTH_PATH = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
CODEX_KEY = "codex://openai"

# Known IDE targets ("kilo-new" uses file-based auth, not state.vscdb)
IDE_EXTENSIONS = {
    "both":      None,
    "kilocode":  "kilocode.kilo-code",
    "roo-cline": "rooveterinaryinc.roo-cline",
    "kilo-new":  KILO_NEW_KEY,
}

CODEX_TARGETS = {
    "codex":     CODEX_KEY,
}

EXTENSIONS = {**IDE_EXTENSIONS, **CODEX_TARGETS}

# Friendly names for display (reverse lookup by extensionId)
_EXT_DISPLAY = {v: k for k, v in EXTENSIONS.items() if v is not None}


def _accounts_dir() -> str:
    return saved_store.ensure_accounts_dir(ACCOUNTS_DIR)


def _is_kilo_new(ext_sub: str | None) -> bool:
    return ext_sub == KILO_NEW_KEY


def _entry_key_for_ext(ext_id: str) -> str:
    if _is_kilo_new(ext_id):
        return ext_id
    return f'secret://{{"extensionId":"{ext_id}","key":"{OAUTH_KEY}"}}'


def _ide_db_extension_names() -> list[str]:
    return [
        name
        for name, ext_id in IDE_EXTENSIONS.items()
        if ext_id and not _is_kilo_new(ext_id)
    ]


def _normalize_ide_ext_selection(ext: str | list[str] | tuple[str, ...] | None) -> tuple[list[str], str]:
    if ext is None or ext == "both":
        names = _ide_db_extension_names()
        return names, "both"

    if isinstance(ext, str):
        items = [ext]
    else:
        items = list(ext)

    normalized: list[str] = []
    valid = {name for name in IDE_EXTENSIONS if name != "both"}
    for name in items:
        if name == "both":
            for db_name in _ide_db_extension_names():
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


def saved_account_kind(data: dict) -> str:
    return saved_store.saved_account_kind(data, CODEX_KEY)


def list_saved_accounts(kind: str | None = None) -> list[dict]:
    return saved_store.list_saved_accounts(_accounts_dir(), CODEX_KEY, kind)


def _read_codex_auth() -> dict:
    return codex_store.read_codex_auth(CODEX_AUTH_PATH)


def _write_codex_auth(data: dict):
    codex_store.write_codex_auth(CODEX_AUTH_PATH, data)


def _to_codex_format(value: dict, existing: dict | None = None) -> dict:
    return codex_store.to_codex_format(value, existing)


def _from_codex_format(value: dict) -> dict:
    return codex_store.from_codex_format(value)


# ── Kilo New (auth.json) helpers ──────────────────────────────────────────────

def _read_kilo_auth() -> dict:
    if not os.path.exists(KILO_AUTH_PATH):
        return {}
    with open(KILO_AUTH_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write_kilo_auth(data: dict):
    os.makedirs(os.path.dirname(KILO_AUTH_PATH), exist_ok=True)
    with open(KILO_AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _to_kilo_new_format(value: dict) -> dict:
    """Convert internal (old-style) token format to kilo auth.json format."""
    return {
        "type": "oauth",
        "access":    value.get("access_token") or value.get("access", ""),
        "refresh":   value.get("refresh_token") or value.get("refresh", ""),
        "expires":   value.get("expires", 0),
        "accountId": value.get("accountId", ""),
    }


def _from_kilo_new_format(value: dict) -> dict:
    """Convert kilo auth.json format to internal (old-style) token format."""
    return {
        "type":          "openai-codex",
        "access_token":  value.get("access", ""),
        "refresh_token": value.get("refresh", ""),
        "expires":       value.get("expires", 0),
        "accountId":     value.get("accountId", ""),
    }


def get_kilo_new_fingerprint() -> str | None:
    """SHA-256 of the refresh token currently in kilo auth.json."""
    import hashlib
    auth = _read_kilo_auth()
    refresh = auth.get("openai", {}).get("refresh")
    if not refresh:
        return None
    return hashlib.sha256(refresh.encode()).hexdigest()


def read_current_kilo_new_account() -> dict[str, dict]:
    """Read the currently active Kilo New account from auth.json."""
    auth = _read_kilo_auth()
    openai_entry = auth.get("openai")
    if not isinstance(openai_entry, dict):
        return {}

    info = {
        "accountId": openai_entry.get("accountId", "?"),
        "fingerprint": account_fingerprint(openai_entry),
        "expires": openai_entry.get("expires"),
    }
    return {KILO_NEW_KEY: info}


def read_current_codex_account() -> dict[str, dict]:
    """Read the currently active Codex account from auth.json."""
    return codex_store.read_current_codex_account(CODEX_AUTH_PATH, CODEX_KEY, account_fingerprint)


# ── Current account detection ─────────────────────────────────────────────────

def account_fingerprint(value) -> str | None:
    """Compute a stable fingerprint for an OAuth entry.

    Handles old format (refresh_token), kilo-new format (refresh),
    and Codex auth.json payloads.
    Falls back to accountId, returns None when neither is present.
    """
    if not isinstance(value, dict):
        return None
    import hashlib
    raw_tokens = value.get("tokens")
    tokens: dict[str, object]
    if isinstance(raw_tokens, dict):
        tokens = raw_tokens
    else:
        tokens = {}
    rt = value.get("refresh_token") or value.get("refresh") or tokens.get("refresh_token")
    if rt:
        rt_str = rt if isinstance(rt, str) else str(rt)
        return hashlib.sha256(rt_str.encode("utf-8")).hexdigest()
    aid = value.get("accountId") or value.get("account_id") or tokens.get("account_id")
    if aid:
        aid_str = aid if isinstance(aid, str) else str(aid)
        return hashlib.sha256(aid_str.encode("utf-8")).hexdigest()
    return None


def read_current_accounts(db_path: str | None = None, local_state_path: str | None = None) -> dict[str, dict]:
    """Read currently active OAuth accounts from ``state.vscdb``.

    Returns a dict mapping ``extensionId`` ->``{"accountId": ..., "fingerprint": ..., "expires": ...}``.
    Safe to call while VSCode is running (reads a temp copy).
    """
    import shutil, tempfile

    _db = db_path or DB_PATH
    if not os.path.exists(_db):
        return {}

    aes_key = get_aes_key(local_state_path)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(_db, tmp.name)

    con = sqlite3.connect(tmp.name)
    result: dict[str, dict] = {}

    for key, value in con.execute("SELECT key, value FROM ItemTable ORDER BY key"):
        if OAUTH_KEY not in key:
            continue

        # Extract extensionId from key like: secret://{"extensionId":"...","key":"..."}
        try:
            payload_str = key[len("secret://"):]
            payload = json.loads(payload_str)
            ext_id = payload.get("extensionId", "")
        except Exception:
            ext_id = ""

        decoded = _decode_entry(value, aes_key)
        try:
            decoded_parsed = json.loads(decoded)
        except Exception:
            decoded_parsed = decoded

        if isinstance(decoded_parsed, dict):
            info: dict = {
                "accountId": decoded_parsed.get("accountId", "?"),
                "fingerprint": account_fingerprint(decoded_parsed),
                "expires": decoded_parsed.get("expires"),
            }
            if ext_id:
                result[ext_id] = info

    con.close()
    os.unlink(tmp.name)
    return result


def match_saved_to_current(
    saved_entries: list[dict],
    current_accounts: dict[str, dict],
) -> list[str]:
    """Return list of extension shortnames where *saved_entries* match current DB state.

    Compares by fingerprint (SHA-256 of refresh_token) with accountId fallback.
    """
    matched: list[str] = []

    for entry in saved_entries:
        key = entry.get("key", "")
        value = entry.get("value", {})

        # Determine which extension slot this entry targets
        try:
            payload_str = key[len("secret://"):]
            payload = json.loads(payload_str)
            ext_id = payload.get("extensionId", "")
        except Exception:
            ext_id = ""

        current = current_accounts.get(ext_id)
        if not current:
            continue

        saved_fp = account_fingerprint(value)
        cur_fp = current.get("fingerprint")

        if saved_fp and cur_fp and saved_fp == cur_fp:
            short = _EXT_DISPLAY.get(ext_id, ext_id)
            if short not in matched:
                matched.append(short)

    return matched


def read_current_accounts_for_ide(ide: str) -> dict[str, dict]:
    """Read active OAuth accounts for a specific IDE.

    Antigravity also includes the file-based Kilo New auth state.
    """
    cfg = IDE_PATHS[ide]
    accounts = read_current_accounts(cfg["db"], cfg["local_state"])
    if ide == "antigravity":
        accounts.update(read_current_kilo_new_account())
    return accounts


def _load_saved_account_data(name: str, expected_kind: str | None = None) -> tuple[str, dict, str]:
    try:
        return saved_store.load_saved_account(_accounts_dir(), name, CODEX_KEY, expected_kind)
    except FileNotFoundError:
        print(f"Account '{name}' not found.")
        sys.exit(1)
    except saved_store.SavedAccountKindMismatchError as exc:
        actual_kind = exc.actual_kind
        print(f"Account '{name}' is a {actual_kind} account, expected {expected_kind}.")
        sys.exit(1)


def _saved_codex_entry(data: dict) -> dict | None:
    entries = data.get("entries", []) if isinstance(data, dict) else []
    for entry in entries:
        if entry.get("key") == CODEX_KEY:
            return entry
    return None


def _read_current_ide_entries_for_selection(ext_names: list[str]) -> list[dict]:
    import shutil
    import tempfile

    entries = []
    db_target_ids = [IDE_EXTENSIONS[name] for name in ext_names if name != "kilo-new"]

    if db_target_ids:
        aes_key = get_aes_key()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(DB_PATH, tmp.name)
        con = sqlite3.connect(tmp.name)

        try:
            for key, value in con.execute("SELECT key, value FROM ItemTable ORDER BY key"):
                if OAUTH_KEY not in key:
                    continue
                if not any(ext_id in key for ext_id in db_target_ids):
                    continue
                decoded = _decode_entry(value, aes_key)
                try:
                    decoded_parsed = json.loads(decoded)
                except Exception:
                    decoded_parsed = decoded
                entries.append({"key": key, "value": decoded_parsed})
        finally:
            con.close()
            os.unlink(tmp.name)

    if "kilo-new" in ext_names:
        kilo_auth = _read_kilo_auth()
        openai_entry = kilo_auth.get("openai")
        if openai_entry:
            entries.append({"key": KILO_NEW_KEY, "value": _from_kilo_new_format(openai_entry)})

    return entries


def _write_account_file(name: str, kind: str, ext_label: str, entries: list[dict]) -> str:
    return saved_store.write_account_file(_accounts_dir(), CODEX_KEY, name, kind, ext_label, entries)


def _print_saved_entries(entries: list[dict]):
    for entry in entries:
        value = entry.get("value", {})
        if isinstance(value, dict):
            print(f"  accountId: {value.get('accountId','?')}")
            exp = value.get("expires")
            if exp:
                exp_dt = datetime.datetime.fromtimestamp(exp / 1000)
                print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def save_ide_account(name: str, ext: str | list[str] | tuple[str, ...] | None = None):
    """Save current IDE-backed account state as a named account."""
    ext_names, ext_label = _normalize_ide_ext_selection(ext)
    entries = _read_current_ide_entries_for_selection(ext_names)
    if not entries:
        print(f"No matching account entries found for {ext_label}.")
        sys.exit(1)

    out = _write_account_file(name, "ide", ext_label, entries)

    print(f"Account '{name}' saved [{ext_label}] ->{out}")
    _print_saved_entries(entries)


def save_codex_account(name: str):
    """Save current Codex auth.json as a named account."""
    value = _from_codex_format(_read_codex_auth())
    if not value.get("access_token") or not value.get("refresh_token"):
        print("ERROR: Codex auth.json is missing access_token or refresh_token.")
        sys.exit(1)
    if not value.get("id_token"):
        print("ERROR: Codex auth.json requires id_token.")
        sys.exit(1)

    entry = {"key": CODEX_KEY, "value": value}
    out = _write_account_file(name, "codex", "codex", [entry])
    print(f"Account '{name}' saved [codex] ->{out}")
    _print_saved_entries([entry])


def use_ide_account(name: str, ext: str | list[str] | tuple[str, ...] | None = None):
    """Apply a saved IDE-family account to DB slots and/or Kilo New."""
    _path, account_data, account_kind = _load_saved_account_data(name)
    if account_kind == "codex":
        print(f"Account '{name}' is Codex-only and cannot be applied to IDE targets.")
        sys.exit(1)

    ext_names, _ = _normalize_ide_ext_selection(ext)

    entries = account_data.get("entries", [])
    ide_entries = [entry for entry in entries if entry.get("key") != CODEX_KEY]
    source = next(iter(ide_entries), None)
    if not source:
        print(f"No IDE entries in account '{name}'.")
        sys.exit(1)

    db_target_ids = [IDE_EXTENSIONS[target_name] for target_name in ext_names if target_name != "kilo-new"]
    source_db = next((entry for entry in ide_entries if entry.get("key") != KILO_NEW_KEY), None)
    source_kilo_new = next((entry for entry in ide_entries if entry.get("key") == KILO_NEW_KEY), None)
    generic_source = source_db or source_kilo_new or source

    needs_db_write = bool(db_target_ids)
    needs_kilo_write = "kilo-new" in ext_names

    if needs_db_write:
        guard_vscode_closed()

    if needs_kilo_write and is_ide_running("antigravity"):
        print("ERROR: Antigravity is running. Close it before switching accounts.")
        sys.exit(1)

    if needs_db_write or needs_kilo_write:
        create_prewrite_backup(
            include_db=needs_db_write,
            include_kilo=needs_kilo_write,
            note=f"before applying IDE account '{name}'",
        )
        print()

    if db_target_ids:
        remapped_entries = []
        for ext_id in db_target_ids:
            entry_key = _entry_key_for_ext(ext_id)
            existing = next((entry for entry in ide_entries if entry.get("key") == entry_key), None)
            if existing:
                remapped_entries.append(existing)
                continue

            if not generic_source:
                print(f"No source entry available for '{ext_id}'.")
                sys.exit(1)

            print(f"[cross-ext] No '{ext_id}' key — remapping from: {generic_source['key']}")
            remapped_entries.append({"key": entry_key, "value": generic_source["value"]})

        import tempfile

        remapped = {**account_data, "entries": remapped_entries}
        tmp_path = tempfile.mktemp(suffix=".json")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(remapped, f)
            restore(tmp_path, create_safety_backup=False)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    if "kilo-new" not in ext_names:
        return

    source_entry = source_kilo_new or generic_source
    if not source_entry:
        print("No source entry available for 'kilo-new'.")
        sys.exit(1)

    new_entry = _to_kilo_new_format(source_entry["value"])
    kilo_auth = _read_kilo_auth()
    kilo_auth["openai"] = new_entry
    _write_kilo_auth(kilo_auth)
    print(f"[kilo-new] Written to {KILO_AUTH_PATH}")
    print(f"  accountId: {new_entry.get('accountId', '?')}")


def use_codex_account(name: str):
    """Apply a saved Codex account to ~/.codex/auth.json."""
    _path, account_data, _kind = _load_saved_account_data(name, expected_kind="codex")
    source_entry = _saved_codex_entry(account_data)
    if not source_entry:
        print(f"Account '{name}' does not contain a Codex entry.")
        sys.exit(1)

    create_prewrite_backup(include_codex=True, note=f"before applying Codex account '{name}'")
    print()

    try:
        codex_auth = _to_codex_format(source_entry["value"], _read_codex_auth())
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    _write_codex_auth(codex_auth)
    print(f"[codex] Written to {CODEX_AUTH_PATH}")
    print(f"  accountId: {codex_auth.get('tokens', {}).get('account_id', '?')}")


def import_codex_account(auth_path: str, name: str):
    """Import tokens from a Codex auth.json and save as a Codex account."""
    if not os.path.exists(auth_path):
        print(f"File not found: {auth_path}")
        sys.exit(1)

    with open(auth_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    value = _from_codex_format(data)
    access_token = value.get("access_token")
    refresh_token = value.get("refresh_token")
    account_id = value.get("accountId")
    expires_ms = value.get("expires", 0)

    if not access_token or not refresh_token:
        print("ERROR: access_token or refresh_token missing in auth.json")
        sys.exit(1)

    if not expires_ms:
        print("ERROR: could not decode access token expiry from auth.json")
        sys.exit(1)

    if not value.get("id_token"):
        print("ERROR: Codex import requires id_token in auth.json.")
        sys.exit(1)

    entry = {"key": CODEX_KEY, "value": value}
    out = _write_account_file(name, "codex", "codex", [entry])

    exp_dt = datetime.datetime.fromtimestamp(expires_ms / 1000)
    print(f"Imported '{name}' [codex] ->{out}")
    print(f"  accountId: {account_id}")
    print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def main():
    raise SystemExit("CLI support removed. Use `python main.py`.")


if __name__ == "__main__":
    main()

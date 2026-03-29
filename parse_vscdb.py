"""
Parse VSCode state.vscdb and find Kilocode / ChatGPT related entries.
On Windows, encrypted values (v10 prefix) are decrypted via DPAPI + AES-256-GCM.
"""

import sqlite3
import json
import os
import sys
import base64
import struct
import datetime

DB_PATH = os.path.expandvars(r"%APPDATA%\Code\User\globalStorage\state.vscdb")
LOCAL_STATE_PATH = os.path.expandvars(r"%APPDATA%\Code\Local State")

SEARCH_KEYS = [
    "kilocode", "kilo-code", "kilo_code",
    "chatgpt", "openai",
    "github.copilot",
    "secret", "token", "cookie", "auth", "session",
]

# ── Decryption ────────────────────────────────────────────────────────────────

def get_aes_key():
    """Read encrypted_key from Local State and decrypt with DPAPI."""
    try:
        with open(LOCAL_STATE_PATH, "r", encoding="utf-8") as f:
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

def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    print(f"DB: {DB_PATH}\n")

    aes_key = get_aes_key()
    if aes_key:
        print(f"AES key obtained ({len(aes_key)} bytes)\n")
    else:
        print("AES key unavailable — encrypted values will not be decrypted\n")

    # Copy DB to temp to avoid lock issues
    import shutil, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(DB_PATH, tmp.name)

    con = sqlite3.connect(tmp.name)
    cur = con.execute("SELECT key, value FROM ItemTable ORDER BY key")

    results = []
    for key, value in cur:
        k_lower = key.lower()
        if any(s in k_lower for s in SEARCH_KEYS):
            if isinstance(value, bytes):
                decoded = decrypt_value(value, aes_key)
            elif isinstance(value, str):
                # May be JSON with {"type":"Buffer","data":[...]} from VSCode secret storage
                try:
                    obj = json.loads(value)
                    if isinstance(obj, dict) and obj.get("type") == "Buffer" and "data" in obj:
                        raw = bytes(obj["data"])
                        decoded = decrypt_value(raw, aes_key)
                    else:
                        decoded = value
                except Exception:
                    decoded = value
            else:
                decoded = str(value) if value is not None else ""
            results.append((key, decoded))

    con.close()
    os.unlink(tmp.name)

    # Always show all keys for reference
    tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp2.close()
    shutil.copy2(DB_PATH, tmp2.name)
    con2 = sqlite3.connect(tmp2.name)
    all_keys = [r[0] for r in con2.execute("SELECT key FROM ItemTable ORDER BY key")]
    con2.close()
    os.unlink(tmp2.name)

    if not results:
        print("No matching keys found.")
        print(f"\nSearched for: {SEARCH_KEYS}")
        print("\nAll keys in DB:")
        for k in all_keys:
            print(" ", k)
        return

    print(f"All keys in DB ({len(all_keys)} total):")
    for k in all_keys:
        marker = " <--" if any(s in k.lower() for s in SEARCH_KEYS) else ""
        print(f"  {k}{marker}")
    print()

    for key, value in results:
        print(f"{'='*60}")
        print(f"KEY: {key}")
        print(f"VALUE:")
        # Try pretty-print JSON
        try:
            parsed = json.loads(value)
            print(json.dumps(parsed, indent=2, ensure_ascii=False))
        except Exception:
            print(value)
        print()


def is_vscode_running() -> bool:
    """Check if any VSCode process is currently running."""
    import subprocess
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Code.exe", "/NH", "/FO", "CSV"],
        capture_output=True, text=True
    )
    return "Code.exe" in result.stdout


def guard_vscode_closed():
    """Exit with error if VSCode is running."""
    if is_vscode_running():
        print("ERROR: VSCode is running. Close it before making changes to state.vscdb.")
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


def get_key(pattern: str, out_path: str | None = None):
    """Extract keys matching pattern from DB into a profile JSON file."""
    import shutil, tempfile
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    aes_key = get_aes_key()

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(DB_PATH, tmp.name)
    con = sqlite3.connect(tmp.name)

    matched = []
    for key, value in con.execute("SELECT key, value FROM ItemTable ORDER BY key"):
        if pattern.lower() in key.lower():
            decoded = _decode_entry(value, aes_key)
            try:
                decoded_parsed = json.loads(decoded)
            except Exception:
                decoded_parsed = decoded
            matched.append({"key": key, "value": decoded_parsed})

    con.close()
    os.unlink(tmp.name)

    if not matched:
        print(f"No keys matching '{pattern}'")
        return

    profile = {
        "created_at": datetime.datetime.now().isoformat(),
        "source": DB_PATH,
        "filter": pattern,
        "entries": matched,
    }

    if out_path is None:
        safe = pattern.replace("/", "_").replace(":", "_").replace('"', "").replace(" ", "_")
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"profile_{safe}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(matched)} key(s) → {out_path}")
    for e in matched:
        print(f"  {e['key']}")


def restore(backup_path: str, key_filter: str | None = None):
    """Restore secrets from a backup JSON into state.vscdb.
    VSCode must be closed before running this.
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

    # Auto-backup the DB file before any writes
    import shutil
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    db_backup_path = DB_PATH + f".bak_{ts}"
    shutil.copy2(DB_PATH, db_backup_path)
    print(f"DB backed up to: {db_backup_path}")
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
        # secret:// keys → Buffer-wrapped encrypted bytes
        # plain keys → JSON string or plain string
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
    print("Done. Start VSCode now.")


def backup(out_path: str | None = None):
    """Save all matched secrets to a JSON file (read-only from DB)."""
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    aes_key = get_aes_key()

    import shutil, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(DB_PATH, tmp.name)

    con = sqlite3.connect(tmp.name)
    cur = con.execute("SELECT key, value FROM ItemTable ORDER BY key")

    backup_data = {
        "created_at": datetime.datetime.now().isoformat(),
        "source": DB_PATH,
        "entries": []
    }

    for key, value in cur:
        k_lower = key.lower()
        if not any(s in k_lower for s in SEARCH_KEYS):
            continue

        if isinstance(value, bytes):
            decoded = decrypt_value(value, aes_key)
        elif isinstance(value, str):
            try:
                obj = json.loads(value)
                if isinstance(obj, dict) and obj.get("type") == "Buffer" and "data" in obj:
                    raw = bytes(obj["data"])
                    decoded = decrypt_value(raw, aes_key)
                else:
                    decoded = value
            except Exception:
                decoded = value
        else:
            decoded = str(value) if value is not None else ""

        # Try to parse as JSON for cleaner output
        try:
            decoded_parsed = json.loads(decoded)
        except Exception:
            decoded_parsed = decoded

        backup_data["entries"].append({"key": key, "value": decoded_parsed})

    con.close()
    os.unlink(tmp.name)

    if out_path is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"vscdb_backup_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)

    print(f"Backup saved: {out_path}")
    print(f"Entries: {len(backup_data['entries'])}")


ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
OAUTH_KEY = "openai-codex-oauth-credentials"

# Known extension slots
EXTENSIONS = {
    "both":     None,
    "kilocode": "kilocode.kilo-code",
    "roo-cline": "rooveterinaryinc.roo-cline",
}

# Friendly names for display (reverse lookup by extensionId)
_EXT_DISPLAY = {v: k for k, v in EXTENSIONS.items() if v is not None}


def _accounts_dir() -> str:
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    return ACCOUNTS_DIR


def _ext_filter(ext: str | None) -> str | None:
    """Resolve extension shortname to DB key substring."""
    if ext is None or ext == "both":
        return None
    return EXTENSIONS.get(ext, ext)


# ── Current account detection ─────────────────────────────────────────────────

def account_fingerprint(value) -> str | None:
    """Compute a stable fingerprint for an OAuth entry.

    Uses SHA-256 of ``refresh_token`` when available, falls back to
    ``accountId``, returns ``None`` when neither is present.
    """
    if not isinstance(value, dict):
        return None
    import hashlib
    rt = value.get("refresh_token")
    if rt:
        return hashlib.sha256(rt.encode("utf-8")).hexdigest()
    aid = value.get("accountId")
    if aid:
        return hashlib.sha256(aid.encode("utf-8")).hexdigest()
    return None


def read_current_accounts() -> dict[str, dict]:
    """Read currently active OAuth accounts from ``state.vscdb``.

    Returns a dict mapping ``extensionId`` → ``{"accountId": ..., "fingerprint": ..., "expires": ...}``.
    Safe to call while VSCode is running (reads a temp copy).
    """
    import shutil, tempfile

    if not os.path.exists(DB_PATH):
        return {}

    aes_key = get_aes_key()

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(DB_PATH, tmp.name)

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


def save_account(name: str, ext: str | None = None):
    """Save current openai-codex-oauth-credentials as a named account.
    ext: 'kilocode', 'roo-cline', or None (both).
    """
    import shutil, tempfile
    aes_key = get_aes_key()
    ext_sub = _ext_filter(ext)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    shutil.copy2(DB_PATH, tmp.name)
    con = sqlite3.connect(tmp.name)

    entries = []
    for key, value in con.execute("SELECT key, value FROM ItemTable ORDER BY key"):
        if OAUTH_KEY not in key:
            continue
        if ext_sub and ext_sub not in key:
            continue
        decoded = _decode_entry(value, aes_key)
        try:
            decoded_parsed = json.loads(decoded)
        except Exception:
            decoded_parsed = decoded
        entries.append({"key": key, "value": decoded_parsed})

    con.close()
    os.unlink(tmp.name)

    if not entries:
        print(f"No {OAUTH_KEY} entries found{' for ' + ext if ext else ''}.")
        sys.exit(1)

    out = os.path.join(_accounts_dir(), f"{name}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "name": name,
            "ext": ext or "both",
            "saved_at": datetime.datetime.now().isoformat(),
            "entries": entries,
        }, f, indent=2, ensure_ascii=False)

    print(f"Account '{name}' saved [{ext or 'both'}] → {out}")
    for e in entries:
        v = e["value"]
        if isinstance(v, dict):
            print(f"  accountId: {v.get('accountId','?')}")
            exp = v.get("expires")
            if exp:
                exp_dt = datetime.datetime.fromtimestamp(exp / 1000)
                print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


def use_account(name: str, ext: str | None = None):
    """Switch to a saved named account (VSCode must be closed).
    ext: override which extension slot to write to.
    """
    path = os.path.join(_accounts_dir(), f"{name}.json")
    if not os.path.exists(path):
        print(f"Account '{name}' not found.")
        list_accounts()
        sys.exit(1)

    ext_sub = _ext_filter(ext)

    if ext_sub is None:
        restore(path, key_filter=None)
        return

    with open(path, "r", encoding="utf-8") as f:
        account_data = json.load(f)

    entries = account_data.get("entries", [])
    matching = [e for e in entries if ext_sub in e["key"]]

    if matching:
        restore(path, key_filter=ext_sub)
        return

    # Cross-extension: target ext not in account — remap from whatever is available
    source = next(iter(entries), None)
    if not source:
        print(f"No entries in account '{name}'.")
        sys.exit(1)

    print(f"[cross-ext] No '{ext_sub}' key found — remapping from: {source['key']}")
    new_key = f'secret://{{"extensionId":"{ext_sub}","key":"{OAUTH_KEY}"}}'
    remapped = {**account_data, "entries": [{"key": new_key, "value": source["value"]}]}

    import tempfile
    tmp_path = tempfile.mktemp(suffix=".json")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(remapped, f)
        restore(tmp_path, key_filter=ext_sub)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def list_accounts():
    """List all saved accounts with expiry info."""
    d = _accounts_dir()
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        print("No saved accounts.")
        return
    print(f"Saved accounts ({len(files)}):")
    for f in files:
        name = f[:-5]
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                data = json.load(fh)
            saved_at = data.get("saved_at", "?")[:16]
            # grab first entry with expires
            exp_str = ""
            for e in data.get("entries", []):
                v = e.get("value", {})
                if isinstance(v, dict) and "expires" in v:
                    exp_dt = datetime.datetime.fromtimestamp(v["expires"] / 1000)
                    exp_str = f"  expires {exp_dt.strftime('%Y-%m-%d')}"
                    break
            print(f"  {name:<20} saved {saved_at}{exp_str}")
        except Exception:
            print(f"  {name}  (unreadable)")


def import_codex_auth(auth_path: str, name: str, ext: str | None = None):
    """Import tokens from ~/.codex/auth.json and save as a named account."""
    if not os.path.exists(auth_path):
        print(f"File not found: {auth_path}")
        sys.exit(1)

    with open(auth_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id")

    if not access_token or not refresh_token:
        print("ERROR: access_token or refresh_token missing in auth.json")
        sys.exit(1)

    # Decode exp from JWT payload (no deps needed)
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        expires_ms = payload["exp"] * 1000
    except Exception as e:
        print(f"ERROR: could not decode JWT: {e}")
        sys.exit(1)

    value = {
        "type": "openai-codex",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires": expires_ms,
        "accountId": account_id,
    }

    # Build entries for requested extensions
    ext_sub = _ext_filter(ext)
    ext_slots = []
    for ext_name, ext_id in EXTENSIONS.items():
        if ext_name == "both":
            continue
        if ext_sub is None or ext_sub == ext_id:
            ext_slots.append(ext_id)

    entries = [
        {
            "key": f'secret://{{"extensionId":"{ext_id}","key":"{OAUTH_KEY}"}}',
            "value": value,
        }
        for ext_id in ext_slots
    ]

    out = os.path.join(_accounts_dir(), f"{name}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "name": name,
            "ext": ext or "both",
            "saved_at": datetime.datetime.now().isoformat(),
            "entries": entries,
        }, f, indent=2, ensure_ascii=False)

    exp_dt = datetime.datetime.fromtimestamp(expires_ms / 1000)
    print(f"Imported '{name}' [{ext or 'both'}] → {out}")
    print(f"  accountId: {account_id}")
    print(f"  expires:   {exp_dt.strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]

    def arg_val(flag):
        """Return value after flag, or None."""
        if flag in args:
            idx = args.index(flag)
            return args[idx + 1] if idx + 1 < len(args) else None
        return None

    if "--import-codex" in args:
        path = arg_val("--import-codex")
        name = arg_val("--name")
        if not path or not name:
            print("Usage: parse_vscdb.py --import-codex <auth.json> --name <account_name>")
            sys.exit(1)
        import_codex_auth(path, name, arg_val("--ext"))
    elif "--save-account" in args:
        name = arg_val("--save-account")
        if not name:
            print("Usage: parse_vscdb.py --save-account <name>")
            sys.exit(1)
        save_account(name)
    elif "--use-account" in args:
        name = arg_val("--use-account")
        if not name:
            print("Usage: parse_vscdb.py --use-account <name>")
            sys.exit(1)
        use_account(name)
    elif "--list-accounts" in args:
        list_accounts()
    elif "--backup" in args:
        backup(arg_val("--backup"))
    elif "--get" in args:
        pattern = arg_val("--get")
        if not pattern:
            print("Usage: parse_vscdb.py --get <pattern> [--out file.json]")
            sys.exit(1)
        get_key(pattern, arg_val("--out"))
    elif "--restore" in args:
        path = arg_val("--restore")
        if not path:
            print("Usage: parse_vscdb.py --restore <backup.json> [--key <pattern>]")
            sys.exit(1)
        restore(path, arg_val("--key"))
    else:
        main()

import base64
import datetime
import json
import os


def decode_jwt_exp_ms(token: str | None) -> int:
    if not token:
        return 0
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
        return int(payload.get("exp", 0)) * 1000
    except Exception:
        return 0


def read_codex_auth(auth_path: str) -> dict:
    if not os.path.exists(auth_path):
        return {}
    with open(auth_path, encoding="utf-8") as f:
        return json.load(f)


def write_codex_auth(auth_path: str, data: dict):
    os.makedirs(os.path.dirname(auth_path), exist_ok=True)
    with open(auth_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def to_codex_format(value: dict, existing: dict | None = None) -> dict:
    existing = existing if isinstance(existing, dict) else {}
    raw_existing_tokens = existing.get("tokens")
    existing_tokens: dict[str, object]
    if isinstance(raw_existing_tokens, dict):
        existing_tokens = raw_existing_tokens
    else:
        existing_tokens = {}

    account_id = value.get("accountId") or value.get("account_id", "")
    id_token = value.get("id_token")
    if not id_token and existing_tokens.get("account_id") == account_id:
        id_token = existing_tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise ValueError(
            "Codex auth.json requires id_token. It can only be reused from the same Codex account or imported from an existing Codex auth.json."
        )

    tokens = {
        "id_token": id_token,
        "access_token": value.get("access_token") or value.get("access", ""),
        "refresh_token": value.get("refresh_token") or value.get("refresh", ""),
        "account_id": account_id,
    }

    out = dict(existing)
    out["auth_mode"] = "chatgpt"
    out["OPENAI_API_KEY"] = None
    out["tokens"] = tokens
    out["last_refresh"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return out


def from_codex_format(value: dict) -> dict:
    raw_tokens = value.get("tokens")
    tokens: dict[str, object]
    if isinstance(raw_tokens, dict):
        tokens = raw_tokens
    else:
        tokens = {}

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str):
        access_token = value.get("access_token") or value.get("access")
    access_token_str = access_token if isinstance(access_token, str) else ""

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str):
        refresh_token = value.get("refresh_token") or value.get("refresh")
    refresh_token_str = refresh_token if isinstance(refresh_token, str) else ""

    account_id = tokens.get("account_id")
    if not isinstance(account_id, str):
        account_id = value.get("account_id") or value.get("accountId")
    account_id_str = account_id if isinstance(account_id, str) else ""

    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        id_token = value.get("id_token")
    id_token_str = id_token if isinstance(id_token, str) else None

    expires = value.get("expires")
    expires_ms = expires if isinstance(expires, int) else 0
    if not expires_ms:
        expires_ms = decode_jwt_exp_ms(access_token_str)

    return {
        "type": "openai-codex",
        "access_token": access_token_str,
        "refresh_token": refresh_token_str,
        "expires": expires_ms,
        "accountId": account_id_str,
        "id_token": id_token_str,
    }


def read_current_codex_account(auth_path: str, codex_key: str, fingerprint_func) -> dict[str, dict]:
    current = from_codex_format(read_codex_auth(auth_path))
    fingerprint = fingerprint_func(current)
    if not fingerprint:
        return {}

    info = {
        "accountId": current.get("accountId", "?"),
        "fingerprint": fingerprint,
        "expires": current.get("expires"),
    }
    return {codex_key: info}

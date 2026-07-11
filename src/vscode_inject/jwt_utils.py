from __future__ import annotations

import base64
import json
from typing import Any


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        claims = json.loads(decoded)
    except Exception:
        return None

    return claims if isinstance(claims, dict) else None


def decode_jwt_exp_ms(token: str | None) -> int:
    claims = decode_jwt_claims(token)
    if not claims:
        return 0
    try:
        return int(claims.get("exp", 0)) * 1000
    except (TypeError, ValueError):
        return 0

from __future__ import annotations

import datetime
import json
from typing import Any, Callable, Mapping
from urllib import error, request

from .time_utils import current_time_iso


DEFAULT_TIMEOUT_SECONDS = 20.0
OPENAI_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
OPENAI_USAGE_HEADERS = {
    "User-Agent": "codex-cli",
}


class UsageFetchError(RuntimeError):
    pass


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_number(value: float) -> int | float:
    if value.is_integer():
        return int(value)
    return value


def _parse_reset_at_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric >= 1_000_000_000_000:
            return int(numeric)
        if numeric >= 1_000_000_000:
            return int(numeric * 1000)
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _window_remaining_percent(window_payload: Mapping[str, Any]) -> int | float | None:
    direct_remaining = _numeric_value(window_payload.get("remaining_percent"))
    if direct_remaining is not None:
        return _normalize_number(max(0.0, direct_remaining))

    used_percent = _numeric_value(window_payload.get("used_percent"))
    if used_percent is None:
        return None

    limit = _numeric_value(window_payload.get("limit"))
    effective_limit = 100.0 if limit is None or limit <= 0 else limit
    return _normalize_number(max(0.0, effective_limit - used_percent))


def _window_seconds(window_payload: Mapping[str, Any]) -> int | None:
    for field in ("limit_window_seconds", "window_seconds"):
        value = _numeric_value(window_payload.get(field))
        if value is not None and value > 0:
            return int(value)
    return None


def _window_reset_at(window_payload: Mapping[str, Any]) -> Any:
    for field in ("reset_at", "resets_at", "expires_at", "expire_at"):
        value = window_payload.get(field)
        if value is not None:
            return value
    return None


def _build_limit_window(window_payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(window_payload, Mapping):
        return None

    remaining = _window_remaining_percent(window_payload)
    if remaining is None:
        return None

    limit_window: dict[str, Any] = {
        "remaining": remaining,
    }
    window_seconds = _window_seconds(window_payload)
    if window_seconds is not None:
        limit_window["windowSeconds"] = window_seconds

    reset_at = _window_reset_at(window_payload)
    if reset_at is not None:
        limit_window["resetAt"] = reset_at
        reset_at_ms = _parse_reset_at_ms(reset_at)
        if reset_at_ms is not None:
            limit_window["resetAtMs"] = reset_at_ms

    return limit_window


def parse_openai_usage_limits(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []

    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), Mapping) else {}
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), Mapping) else {}
    raw_windows = [
        rate_limit.get("primary_window"),
        rate_limit.get("secondary_window"),
        rate_limits.get("primary"),
        rate_limits.get("secondary"),
    ]

    limits: list[dict[str, Any]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    for raw_window in raw_windows:
        limit_window = _build_limit_window(raw_window)
        if limit_window is None:
            continue
        dedupe_key = (
            limit_window.get("windowSeconds"),
            limit_window.get("remaining"),
            limit_window.get("resetAtMs"),
            limit_window.get("resetAt"),
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        limits.append(limit_window)

    limits.sort(key=lambda item: (item.get("windowSeconds") is None, item.get("windowSeconds") or 0))
    return limits


def _http_error_details(body: str) -> str:
    try:
        payload = json.loads(body)
    except Exception:
        return body.strip() or "unknown HTTP error"

    if isinstance(payload, Mapping):
        for field in ("message", "error_description", "error"):
            value = payload.get(field)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, Mapping):
                nested_message = value.get("message")
                if isinstance(nested_message, str) and nested_message:
                    return nested_message
    return body.strip() or "unknown HTTP error"


def fetch_http_json_payload(
    endpoint: str,
    *,
    access_token: str,
    account_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    urlopen: Callable[..., Any] = request.urlopen,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if isinstance(account_id, str) and account_id:
        request_headers["ChatGPT-Account-Id"] = account_id
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                request_headers[key] = value

    req = request.Request(endpoint, headers=request_headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise UsageFetchError(f"HTTP {exc.code}: {_http_error_details(body)}") from exc
    except error.URLError as exc:
        raise UsageFetchError(f"Network error: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UsageFetchError("Usage endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UsageFetchError("Usage endpoint returned an invalid payload")
    return payload


def fetch_openai_usage_snapshot(
    access_token: str,
    *,
    account_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    urlopen: Callable[..., Any] = request.urlopen,
) -> dict[str, Any]:
    if not isinstance(access_token, str) or not access_token:
        raise UsageFetchError("Missing access token for usage fetch")

    payload = fetch_http_json_payload(
        OPENAI_USAGE_ENDPOINT,
        access_token=access_token,
        account_id=account_id,
        headers=OPENAI_USAGE_HEADERS,
        timeout=timeout,
        urlopen=urlopen,
    )
    limits = parse_openai_usage_limits(payload)
    if not limits:
        raise UsageFetchError("Usage endpoint returned no limits")

    return {
        "fetched_at": current_time_iso(),
        "limits": limits,
    }

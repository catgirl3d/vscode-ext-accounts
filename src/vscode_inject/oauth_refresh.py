from __future__ import annotations

import base64
import copy
import datetime
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib import error, parse, request


OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_STORAGE_KEY = "openai-codex-oauth-credentials"
OPENAI_CODEX_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
KILO_NEW_KEY = "kilo-new://openai"
CODEX_KEY = "codex://openai"
DEFAULT_EXPIRES_IN_SECONDS = 3600
TERMINAL_REFRESH_ERROR_CODES = {
    "access_denied",
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
}
TERMINAL_REFRESH_ERROR_HINTS = (
    "already been used",
    "already used",
    "invalid refresh token",
    "refresh token expired",
    "refresh token has already been used",
    "refresh token is invalid",
    "revoked",
    "session has expired",
    "sign in again",
)


class OAuthRefreshError(RuntimeError):
    """Base class for saved-snapshot OAuth refresh errors."""


class UnsupportedSavedAccountError(OAuthRefreshError):
    """Raised when a saved account does not contain any supported refreshable entries."""


class TokenExchangeError(OAuthRefreshError):
    """Raised when the upstream OAuth token endpoint rejects the refresh request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        error_description: str | None = None,
        terminal: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_description = error_description
        self.terminal = terminal


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires: int
    account_id: str = ""
    id_token: str | None = None


@dataclass(frozen=True)
class RefreshableEntry:
    index: int
    entry: Mapping[str, Any]
    provider: str
    bundle: TokenBundle


@dataclass(frozen=True)
class RefreshEntriesResult:
    entries: list[dict[str, Any]]
    refreshed_entries: int
    refreshed_groups: int
    refreshed_at: str


@dataclass(frozen=True)
class SavedAccountRecord:
    name: str
    path: str
    data: Mapping[str, Any]
    kind: str | None = None


@dataclass(frozen=True)
class RefreshGroupKey:
    provider: str
    refresh_token: str


@dataclass(frozen=True)
class RefreshableRecordEntry:
    record_name: str
    record_path: str
    entry_index: int
    group_key: RefreshGroupKey
    bundle: TokenBundle


@dataclass(frozen=True)
class RefreshGroup:
    key: RefreshGroupKey
    bundle: TokenBundle
    expires: int
    entries: tuple[RefreshableRecordEntry, ...]

    def account_names(self) -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.record_name not in seen:
                seen.append(entry.record_name)
        return tuple(seen)

    def record_paths(self) -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.record_path not in seen:
                seen.append(entry.record_path)
        return tuple(seen)


RefreshOperation = Callable[[TokenBundle], TokenBundle]


def current_time_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def current_time_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


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


def extract_account_id_from_claims(claims: Mapping[str, Any] | None) -> str | None:
    if not claims:
        return None

    direct = claims.get("chatgpt_account_id")
    if isinstance(direct, str) and direct:
        return direct

    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, Mapping):
        nested = auth_claims.get("chatgpt_account_id")
        if isinstance(nested, str) and nested:
            return nested

    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, Mapping):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id

    return None


def extract_account_id(access_token: str, id_token: str | None = None) -> str | None:
    id_claims = decode_jwt_claims(id_token)
    account_id = extract_account_id_from_claims(id_claims)
    if account_id:
        return account_id

    access_claims = decode_jwt_claims(access_token)
    return extract_account_id_from_claims(access_claims)


def is_terminal_token_exchange_failure(
    *,
    status_code: int | None,
    error_code: str | None,
    error_message: str | None,
) -> bool:
    normalized_code = (error_code or "").strip().lower()
    normalized_message = (error_message or "").strip().lower()

    if normalized_code in TERMINAL_REFRESH_ERROR_CODES:
        return True

    if status_code not in {400, 401, 403}:
        return False

    return any(hint in normalized_message for hint in TERMINAL_REFRESH_ERROR_HINTS)


def is_terminal_refresh_error(exc: Exception) -> bool:
    if not isinstance(exc, TokenExchangeError):
        return False
    if exc.terminal:
        return True
    return is_terminal_token_exchange_failure(
        status_code=exc.status_code,
        error_code=exc.error_code,
        error_message=exc.error_description or str(exc),
    )


def _oauth_error_details(error_text: str) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(error_text)
    except Exception:
        return None, None

    if not isinstance(payload, dict):
        return None, None

    error_field = payload.get("error")
    error_code = error_field if isinstance(error_field, str) and error_field else None

    if isinstance(error_field, Mapping) and not error_code:
        nested_code = error_field.get("type")
        if isinstance(nested_code, str) and nested_code:
            error_code = nested_code

    error_description = payload.get("error_description")
    if isinstance(error_description, str) and error_description:
        return error_code, error_description

    nested_message = error_field.get("message") if isinstance(error_field, Mapping) else None
    if isinstance(nested_message, str) and nested_message:
        return error_code, nested_message

    message = payload.get("message")
    if isinstance(message, str) and message:
        return error_code, message

    return error_code, None


def post_form_urlencoded(
    url: str,
    data: Mapping[str, str],
    *,
    timeout: float = 30.0,
    urlopen: Callable[..., Any] = request.urlopen,
) -> dict[str, Any]:
    body = parse.urlencode(data).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        error_code, error_message = _oauth_error_details(payload)
        details = error_message or payload or str(exc.reason)
        if error_code:
            details = f"{error_code}: {details}"
        raise TokenExchangeError(
            f"Token refresh failed: {exc.code} {details}",
            status_code=exc.code,
            error_code=error_code,
            error_description=error_message or payload or str(exc.reason),
            terminal=is_terminal_token_exchange_failure(
                status_code=exc.code,
                error_code=error_code,
                error_message=error_message or payload or str(exc.reason),
            ),
        ) from exc
    except error.URLError as exc:
        raise TokenExchangeError(
            f"Token refresh failed: {exc.reason}",
            error_description=str(exc.reason),
        ) from exc

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TokenExchangeError("Token refresh returned invalid JSON") from exc

    if not isinstance(parsed, dict):
        raise TokenExchangeError("Token refresh returned an invalid payload")
    return parsed


def token_bundle_from_value(value: Mapping[str, Any]) -> TokenBundle:
    raw_tokens = value.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, Mapping) else {}

    access_token = value.get("access_token") or value.get("access") or tokens.get("access_token") or ""
    refresh_token = value.get("refresh_token") or value.get("refresh") or tokens.get("refresh_token") or ""
    account_id = value.get("accountId") or value.get("account_id") or tokens.get("account_id") or ""
    id_token = value.get("id_token") or tokens.get("id_token")
    expires = value.get("expires")

    return TokenBundle(
        access_token=access_token if isinstance(access_token, str) else str(access_token or ""),
        refresh_token=refresh_token if isinstance(refresh_token, str) else str(refresh_token or ""),
        expires=expires if isinstance(expires, int) else 0,
        account_id=account_id if isinstance(account_id, str) else str(account_id or ""),
        id_token=id_token if isinstance(id_token, str) and id_token else None,
    )


def refresh_openai_codex_bundle(
    bundle: TokenBundle,
    *,
    post_form: Callable[[str, Mapping[str, str]], dict[str, Any]] = post_form_urlencoded,
    now_ms: Callable[[], int] = current_time_ms,
) -> TokenBundle:
    payload = post_form(
        OPENAI_CODEX_TOKEN_ENDPOINT,
        {
            "grant_type": "refresh_token",
            "client_id": OPENAI_CODEX_CLIENT_ID,
            "refresh_token": bundle.refresh_token,
        },
    )

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenExchangeError("Token refresh response is missing access_token")

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = bundle.refresh_token

    id_token = payload.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        id_token = bundle.id_token

    expires_in = payload.get("expires_in")
    if not isinstance(expires_in, int) or expires_in <= 0:
        expires_in = DEFAULT_EXPIRES_IN_SECONDS

    account_id = extract_account_id(access_token, id_token) or bundle.account_id

    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires=now_ms() + expires_in * 1000,
        account_id=account_id,
        id_token=id_token,
    )


def _entry_uses_openai_codex_key(entry_key: str) -> bool:
    if entry_key in {KILO_NEW_KEY, CODEX_KEY}:
        return True
    if not entry_key.startswith("secret://"):
        return False

    try:
        payload = json.loads(entry_key[len("secret://") :])
    except Exception:
        return False

    return isinstance(payload, dict) and payload.get("key") == OPENAI_CODEX_STORAGE_KEY


def _provider_for_entry(entry_key: str, value: Mapping[str, Any]) -> str | None:
    refresh_token = token_bundle_from_value(value).refresh_token
    if not refresh_token:
        return None
    if value.get("type") == OPENAI_CODEX_PROVIDER:
        return OPENAI_CODEX_PROVIDER
    if _entry_uses_openai_codex_key(entry_key):
        return OPENAI_CODEX_PROVIDER
    return None


def collect_refreshable_entries(entries: Sequence[Mapping[str, Any]]) -> list[RefreshableEntry]:
    refreshable: list[RefreshableEntry] = []

    for index, entry in enumerate(entries):
        key = entry.get("key")
        value = entry.get("value")
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue

        provider = _provider_for_entry(key, value)
        if not provider:
            continue

        refreshable.append(
            RefreshableEntry(
                index=index,
                entry=entry,
                provider=provider,
                bundle=token_bundle_from_value(value),
            )
        )

    return refreshable


def saved_account_records(records: Sequence[Mapping[str, Any]]) -> list[SavedAccountRecord]:
    normalized: list[SavedAccountRecord] = []

    for record in records:
        path = record.get("path")
        data = record.get("data")
        readable = record.get("readable", True)
        if readable is False or not isinstance(path, str) or not isinstance(data, Mapping):
            continue

        name = record.get("name")
        kind = record.get("kind")
        normalized.append(
            SavedAccountRecord(
                name=name if isinstance(name, str) else path,
                path=path,
                data=data,
                kind=kind if isinstance(kind, str) else None,
            )
        )

    return normalized


def _group_expires(entries: Sequence[RefreshableRecordEntry]) -> int:
    expires_values = [entry.bundle.expires for entry in entries]
    if any(expires <= 0 for expires in expires_values):
        return 0
    return min(expires_values, default=0)


def collect_refresh_groups(records: Sequence[SavedAccountRecord]) -> list[RefreshGroup]:
    grouped: OrderedDict[RefreshGroupKey, list[RefreshableRecordEntry]] = OrderedDict()

    for record in records:
        entries = record.data.get("entries", []) if isinstance(record.data, Mapping) else []
        if not isinstance(entries, list):
            continue

        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            key = entry.get("key")
            value = entry.get("value")
            if not isinstance(key, str) or not isinstance(value, Mapping):
                continue

            provider = _provider_for_entry(key, value)
            if not provider:
                continue

            bundle = token_bundle_from_value(value)
            group_key = RefreshGroupKey(provider=provider, refresh_token=bundle.refresh_token)
            grouped.setdefault(group_key, []).append(
                RefreshableRecordEntry(
                    record_name=record.name,
                    record_path=record.path,
                    entry_index=entry_index,
                    group_key=group_key,
                    bundle=bundle,
                )
            )

    return [
        RefreshGroup(
            key=group_key,
            bundle=entries[0].bundle,
            expires=_group_expires(entries),
            entries=tuple(entries),
        )
        for group_key, entries in grouped.items()
    ]


def refresh_due_at_ms(group: RefreshGroup, refresh_before_ms: int) -> int:
    if group.expires <= 0:
        return 0
    return max(0, group.expires - refresh_before_ms)


def apply_refreshed_group(
    records_by_path: Mapping[str, SavedAccountRecord],
    group: RefreshGroup,
    refreshed_bundle: TokenBundle,
    *,
    refreshed_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    timestamp = refreshed_at or current_time_iso()
    updated_records: dict[str, dict[str, Any]] = {}

    for refreshable in group.entries:
        record = records_by_path.get(refreshable.record_path)
        if record is None:
            raise OAuthRefreshError(f"Missing saved account record for path '{refreshable.record_path}'.")

        updated_record = updated_records.setdefault(refreshable.record_path, copy.deepcopy(dict(record.data)))
        entries = updated_record.get("entries")
        if not isinstance(entries, list) or refreshable.entry_index >= len(entries):
            raise OAuthRefreshError(
                f"Saved account '{record.name}' has an invalid entry layout for auto-refresh."
            )

        existing_entry = entries[refreshable.entry_index]
        if not isinstance(existing_entry, Mapping):
            raise OAuthRefreshError(f"Saved account '{record.name}' contains a non-object entry.")
        value = existing_entry.get("value")
        if not isinstance(value, Mapping):
            raise OAuthRefreshError(f"Saved account '{record.name}' contains an invalid OAuth value payload.")

        updated_entry = dict(existing_entry)
        updated_entry["value"] = apply_refreshed_bundle(value, refreshed_bundle)
        entries[refreshable.entry_index] = updated_entry
        updated_record["last_refreshed_at"] = timestamp
        updated_record["refresh_status"] = "ok"
        updated_record.pop("refresh_error", None)
        updated_record.pop("refresh_error_at", None)

    return updated_records


def apply_refresh_error(
    records_by_path: Mapping[str, SavedAccountRecord],
    group: RefreshGroup,
    *,
    status: str,
    error_message: str,
    error_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    timestamp = error_at or current_time_iso()
    updated_records: dict[str, dict[str, Any]] = {}

    for refreshable in group.entries:
        record = records_by_path.get(refreshable.record_path)
        if record is None:
            raise OAuthRefreshError(f"Missing saved account record for path '{refreshable.record_path}'.")

        updated_record = updated_records.setdefault(refreshable.record_path, copy.deepcopy(dict(record.data)))
        updated_record["refresh_status"] = status
        updated_record["refresh_error"] = error_message
        updated_record["refresh_error_at"] = timestamp

    return updated_records


def apply_refreshed_bundle(value: Mapping[str, Any], bundle: TokenBundle) -> dict[str, Any]:
    updated = dict(value)
    updated["access_token"] = bundle.access_token
    updated["refresh_token"] = bundle.refresh_token
    updated["expires"] = bundle.expires
    if bundle.account_id:
        updated["accountId"] = bundle.account_id
    if bundle.id_token:
        updated["id_token"] = bundle.id_token
    return updated


DEFAULT_REFRESHERS: dict[str, RefreshOperation] = {
    OPENAI_CODEX_PROVIDER: refresh_openai_codex_bundle,
}


def refresh_saved_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    refreshers: Mapping[str, RefreshOperation] | None = None,
) -> RefreshEntriesResult:
    refreshable_entries = collect_refreshable_entries(entries)
    if not refreshable_entries:
        raise UnsupportedSavedAccountError(
            "Selected account does not contain any supported OpenAI OAuth tokens with a refresh_token."
        )

    refreshers = dict(refreshers or DEFAULT_REFRESHERS)
    grouped: OrderedDict[tuple[str, str], list[RefreshableEntry]] = OrderedDict()
    for refreshable in refreshable_entries:
        group_key = (refreshable.provider, refreshable.bundle.refresh_token)
        grouped.setdefault(group_key, []).append(refreshable)

    updated_entries = copy.deepcopy(list(entries))
    for (provider, _refresh_token), grouped_entries in grouped.items():
        refresher = refreshers.get(provider)
        if refresher is None:
            raise UnsupportedSavedAccountError(f"No refresher is registered for provider '{provider}'.")

        refreshed_bundle = refresher(grouped_entries[0].bundle)
        for refreshable in grouped_entries:
            updated_entry = dict(updated_entries[refreshable.index])
            updated_entry["value"] = apply_refreshed_bundle(refreshable.entry.get("value", {}), refreshed_bundle)
            updated_entries[refreshable.index] = updated_entry

    return RefreshEntriesResult(
        entries=updated_entries,
        refreshed_entries=len(refreshable_entries),
        refreshed_groups=len(grouped),
        refreshed_at=current_time_iso(),
    )

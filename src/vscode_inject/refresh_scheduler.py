from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from . import oauth_refresh


@dataclass(frozen=True)
class RefreshPolicy:
    refresh_before_ms: int = 10 * 60 * 1000
    scan_interval_ms: int = 60 * 1000
    min_delay_ms: int = 5 * 1000
    initial_retry_ms: int = 30 * 1000
    max_retry_ms: int = 15 * 60 * 1000


@dataclass(frozen=True)
class GroupRuntimeState:
    failure_count: int = 0
    next_retry_at: int | None = None
    last_error: str | None = None
    last_success_at: int | None = None
    terminal: bool = False


@dataclass(frozen=True)
class RefreshFailure:
    group: oauth_refresh.RefreshGroup
    error_message: str
    terminal: bool = False
    next_retry_at: int | None = None


@dataclass(frozen=True)
class AutoRefreshResult:
    next_delay_ms: int
    due_groups: int = 0
    refreshed_groups: int = 0
    refreshed_accounts: int = 0
    refreshed_entries: int = 0
    failed_groups: int = 0
    terminal_failed_groups: int = 0
    ok: bool = True
    refresh_ui: bool = False
    message: str | None = None
    failures: tuple[RefreshFailure, ...] = ()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    word = singular if value == 1 else (plural or singular + "s")
    return f"{value} {word}"


class AutoRefreshScheduler:
    def __init__(
        self,
        *,
        list_saved_accounts: Callable[[], Sequence[Mapping[str, Any]]],
        write_saved_account_batch: Callable[[Mapping[str, dict[str, Any]]], None],
        persist_refreshed_group: Callable[[Mapping[str, dict[str, Any]], oauth_refresh.RefreshGroup], None],
        policy: RefreshPolicy | None = None,
        refreshers: Mapping[str, oauth_refresh.RefreshOperation] | None = None,
        now_ms: Callable[[], int] = oauth_refresh.current_time_ms,
        now_iso: Callable[[], str] = oauth_refresh.current_time_iso,
        operation_lock: Any | None = None,
    ):
        self._list_saved_accounts = list_saved_accounts
        self._write_saved_account_batch = write_saved_account_batch
        self._persist_refreshed_group = persist_refreshed_group
        self._policy = policy or RefreshPolicy()
        self._refreshers = dict(refreshers or oauth_refresh.DEFAULT_REFRESHERS)
        self._now_ms = now_ms
        self._now_iso = now_iso
        self._operation_lock = operation_lock or _NullLock()
        self._mutex = threading.Lock()
        self._group_states: dict[oauth_refresh.RefreshGroupKey, GroupRuntimeState] = {}

    @property
    def policy(self) -> RefreshPolicy:
        return self._policy

    def run_once(self) -> AutoRefreshResult:
        with self._mutex:
            with self._operation_lock:
                return self._run_once_locked()

    def _run_once_locked(self) -> AutoRefreshResult:
        now_ms = self._now_ms()
        records = oauth_refresh.saved_account_records(self._list_saved_accounts())
        groups = oauth_refresh.collect_refresh_groups(records)
        self._prune_group_states(groups)

        due_groups = [group for group in groups if self._is_group_due(group, now_ms)]
        if not due_groups:
            return AutoRefreshResult(next_delay_ms=self._compute_next_delay_ms(groups, now_ms))

        records_by_path = {record.path: record for record in records}
        refreshed_groups = 0
        refreshed_entries = 0
        refreshed_paths: set[str] = set()
        failures: list[RefreshFailure] = []

        for group in due_groups:
            exchanged_refresh_token = False
            try:
                refresher = self._refreshers.get(group.key.provider)
                if refresher is None:
                    raise oauth_refresh.UnsupportedSavedAccountError(
                        f"No refresher is registered for provider '{group.key.provider}'."
                    )

                refreshed_bundle = refresher(group.bundle)
                exchanged_refresh_token = True
                updated_records = oauth_refresh.apply_refreshed_group(
                    records_by_path,
                    group,
                    refreshed_bundle,
                    refreshed_at=self._now_iso(),
                )
                self._persist_refreshed_group(updated_records, group)

                for path, data in updated_records.items():
                    current = records_by_path[path]
                    records_by_path[path] = oauth_refresh.SavedAccountRecord(
                        name=current.name,
                        path=current.path,
                        kind=current.kind,
                        data=data,
                    )
                    refreshed_paths.add(path)

                self._group_states[group.key] = GroupRuntimeState(last_success_at=now_ms)
                refreshed_groups += 1
                refreshed_entries += len(group.entries)
            except Exception as exc:
                error_message = str(exc)
                terminal = oauth_refresh.is_terminal_refresh_error(exc) or exchanged_refresh_token
                self._group_states[group.key] = self._next_failure_state(group.key, error_message, now_ms, terminal=terminal)
                next_retry_at = None if terminal else self._group_states[group.key].next_retry_at
                failures.append(
                    RefreshFailure(
                        group=group,
                        error_message=error_message,
                        terminal=terminal,
                        next_retry_at=next_retry_at,
                    )
                )

                if terminal and not exchanged_refresh_token:
                    updated_records = oauth_refresh.apply_refresh_error(
                        records_by_path,
                        group,
                        status="terminal_error",
                        error_message=error_message,
                        error_at=self._now_iso(),
                    )
                    self._write_saved_account_batch(updated_records)
                    for path, data in updated_records.items():
                        current = records_by_path[path]
                        records_by_path[path] = oauth_refresh.SavedAccountRecord(
                            name=current.name,
                            path=current.path,
                            kind=current.kind,
                            data=data,
                        )

        final_groups = oauth_refresh.collect_refresh_groups(list(records_by_path.values()))
        self._prune_group_states(final_groups)
        terminal_failures = sum(1 for failure in failures if failure.terminal)

        return AutoRefreshResult(
            next_delay_ms=self._compute_next_delay_ms(final_groups, now_ms),
            due_groups=len(due_groups),
            refreshed_groups=refreshed_groups,
            refreshed_accounts=len(refreshed_paths),
            refreshed_entries=refreshed_entries,
            failed_groups=len(failures),
            terminal_failed_groups=terminal_failures,
            ok=not failures,
            refresh_ui=bool(refreshed_paths),
            message=self._build_message(refreshed_groups, len(refreshed_paths), failures),
            failures=tuple(failures),
        )

    def _is_group_due(self, group: oauth_refresh.RefreshGroup, now_ms: int) -> bool:
        state = self._group_states.get(group.key)
        if state and state.terminal:
            return False
        if not self._is_group_auto_refresh_eligible(group, now_ms):
            return False
        retry_at = state.next_retry_at if state else None
        due_at = oauth_refresh.refresh_due_at_ms(group, self._policy.refresh_before_ms)
        eligible_at = max(due_at, retry_at or 0)
        return eligible_at <= now_ms

    def _compute_next_delay_ms(self, groups: Sequence[oauth_refresh.RefreshGroup], now_ms: int) -> int:
        candidates = [self._policy.scan_interval_ms]
        for group in groups:
            state = self._group_states.get(group.key)
            if state and state.terminal:
                continue
            if not self._is_group_auto_refresh_eligible(group, now_ms):
                continue
            due_at = oauth_refresh.refresh_due_at_ms(group, self._policy.refresh_before_ms)
            retry_at = state.next_retry_at if state else None
            eligible_at = max(due_at, retry_at or 0)
            candidates.append(max(0, eligible_at - now_ms))

        next_delay_ms = min(candidates) if candidates else self._policy.scan_interval_ms
        return max(self._policy.min_delay_ms, next_delay_ms)

    def _is_group_auto_refresh_eligible(self, group: oauth_refresh.RefreshGroup, now_ms: int) -> bool:
        expires = group.expires
        return isinstance(expires, int) and expires > now_ms

    def _retry_delay_ms(self, failure_count: int) -> int:
        delay = self._policy.initial_retry_ms * (2 ** max(0, failure_count - 1))
        return min(delay, self._policy.max_retry_ms)

    def _next_failure_state(
        self,
        key: oauth_refresh.RefreshGroupKey,
        error_message: str,
        now_ms: int,
        *,
        terminal: bool,
    ) -> GroupRuntimeState:
        current = self._group_states.get(key, GroupRuntimeState())
        failure_count = current.failure_count + 1
        if terminal:
            return GroupRuntimeState(
                failure_count=failure_count,
                next_retry_at=None,
                last_error=error_message,
                last_success_at=current.last_success_at,
                terminal=True,
            )

        retry_delay_ms = self._retry_delay_ms(failure_count)
        return GroupRuntimeState(
            failure_count=failure_count,
            next_retry_at=now_ms + retry_delay_ms,
            last_error=error_message,
            last_success_at=current.last_success_at,
            terminal=False,
        )

    def _prune_group_states(self, groups: Sequence[oauth_refresh.RefreshGroup]) -> None:
        active_keys = {group.key for group in groups}
        stale_keys = [key for key in self._group_states if key not in active_keys]
        for key in stale_keys:
            self._group_states.pop(key, None)

    def _build_message(
        self,
        refreshed_groups: int,
        refreshed_accounts: int,
        failures: Sequence[RefreshFailure],
    ) -> str | None:
        parts: list[str] = []
        if refreshed_groups:
            parts.append(
                f"Auto-refreshed {_plural(refreshed_groups, 'token group')} in {_plural(refreshed_accounts, 'account')}"
            )

        if failures:
            sample = failures[0]
            names = ", ".join(sample.group.account_names()) or "saved account"
            if len(failures) == 1 and sample.terminal:
                parts.append(f"Auto-refresh disabled for {names}: {sample.error_message}")
            elif len(failures) == 1:
                retry_part = self._retry_message(sample.next_retry_at)
                parts.append(f"Auto-refresh failed for {names}: {sample.error_message}{retry_part}")
            else:
                terminal_failures = sum(1 for failure in failures if failure.terminal)
                terminal_part = f", {terminal_failures} terminal" if terminal_failures else ""
                retry_part = ""
                if not sample.terminal:
                    retry_part = self._retry_message(sample.next_retry_at)
                parts.append(f"{_plural(len(failures), 'token group')} failed{terminal_part}, first was {names}: {sample.error_message}{retry_part}")

        if not parts:
            return None
        return "; ".join(parts)

    def _retry_message(self, next_retry_at: int | None) -> str:
        if next_retry_at is None:
            return ""
        delay_ms = max(0, next_retry_at - self._now_ms())
        delay_seconds = max(1, delay_ms // 1000)
        return f" (retry in {delay_seconds}s)"

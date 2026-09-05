"""Optional notifications that cannot control normalized paper state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Protocol

import httpx

from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    SQLiteStore,
    StoreCorruptionError,
)


_TELEGRAM_TEXT_LIMIT = 4_000
_ALERT_VERSION = 1
_ALERT_SOURCE_TYPES = frozenset({"HEALTH_STATE", "PAPER_CYCLE"})


class Notifier(Protocol):
    def send(self, event: Mapping[str, object]) -> bool: ...


class NullNotifier:
    """A deterministic no-I/O notifier."""

    def send(self, event: Mapping[str, object]) -> bool:
        del event
        return True


class SafeNotifier:
    """Contain ordinary adapter failures without hiding process control."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Notifier) -> None:
        if not hasattr(adapter, "send"):
            raise TypeError("adapter must provide send")
        self._adapter = adapter

    def send(self, event: Mapping[str, object]) -> bool:
        try:
            return bool(self._adapter.send(event))
        except Exception:
            return False


class TelegramNotifier:
    """Telegram adapter whose credentials never enter persisted event state."""

    __slots__ = ("_token", "_chat_id", "_http_client")

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http_client: httpx.Client,
    ) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("telegram token must be non-empty")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("telegram chat value must be non-empty")
        self._token = token
        self._chat_id = chat_id
        self._http_client = http_client

    def __repr__(self) -> str:
        return "TelegramNotifier(<runtime credentials>)"

    def send(self, event: Mapping[str, object]) -> bool:
        try:
            response = self._http_client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                data={"chat_id": self._chat_id, "text": _bounded_event_text(event)},
                timeout=5.0,
                follow_redirects=False,
                auth=None,
            )
            response.raise_for_status()
        except Exception:
            raise RuntimeError("telegram delivery failed") from None
        return True


def deliver_alert_once(
    store: SQLiteStore,
    notifier: Notifier,
    *,
    source_event_id: str,
    source_event_type: str,
    source_payload: Mapping[str, object],
    logical_at: datetime,
    mutation_boundary: Callable[[], AbstractContextManager[object]] | None = None,
) -> bool | None:
    """Persist an attempt before delivery; an existing exact attempt is skipped."""
    source_id = _nonempty(source_event_id, "source event id")
    source_type = _nonempty(source_event_type, "source event type")
    if not isinstance(source_payload, Mapping):
        raise TypeError("source payload must be a mapping")
    if not isinstance(logical_at, datetime) or logical_at.tzinfo is None:
        raise ValueError("alert logical time must be timezone-aware")
    logical = logical_at.astimezone(timezone.utc)
    digest = sha256(f"{source_id}|{source_type}".encode("utf-8")).hexdigest()
    marker_id = f"alert-attempt:{digest}"
    marker = {
        "source_event_id": source_id,
        "source_event_type": source_type,
        "version": _ALERT_VERSION,
    }
    if mutation_boundary is None:
        _require_durable_source(store, source_id, source_type, source_payload)
        claimed = _claim_attempt(store, marker_id, marker, logical)
    else:
        with mutation_boundary():
            _require_durable_source(store, source_id, source_type, source_payload)
            claimed = _claim_attempt(store, marker_id, marker, logical)
    if not claimed:
        return None

    delivered = notifier.send(
        {
            "event_id": source_id,
            "event_type": source_type,
            "payload": dict(source_payload),
        }
    )
    if delivered:
        return True

    failure = {
        "failure_code": "DELIVERY_FAILED",
        "source_event_id": source_id,
        "source_event_type": source_type,
        "version": _ALERT_VERSION,
    }
    try:
        if mutation_boundary is None:
            _append_exact_once(
                store,
                f"alert-failure:{digest}",
                "ALERT_FAILURE",
                logical,
                failure,
            )
        else:
            with mutation_boundary():
                _append_exact_once(
                    store,
                    f"alert-failure:{digest}",
                    "ALERT_FAILURE",
                    logical,
                    failure,
                )
    except Exception:
        pass
    return False


def _require_durable_source(
    store: SQLiteStore,
    source_event_id: str,
    source_event_type: str,
    source_payload: Mapping[str, object],
) -> None:
    if source_event_type not in _ALERT_SOURCE_TYPES:
        raise ValueError("alert durable source type is not supported")
    matches = tuple(
        event
        for event in store.replay_state().event_evidence
        if event.event_id == source_event_id
    )
    if len(matches) != 1:
        raise ValueError("alert durable source is missing")
    source = matches[0]
    if (
        source.event_type != source_event_type
        or dict(source.payload) != dict(source_payload)
    ):
        raise ValueError("alert durable source does not match")


def _claim_attempt(
    store: SQLiteStore,
    event_id: str,
    payload: Mapping[str, object],
    logical_at: datetime,
) -> bool:
    snapshot = store.replay_state()
    matches = tuple(event for event in snapshot.event_evidence if event.event_id == event_id)
    if matches:
        _require_exact(matches, "ALERT_ATTEMPT", payload)
        return False
    try:
        store.append_event(
            event_id,
            "ALERT_ATTEMPT",
            _monotonic_time(snapshot, logical_at),
            payload,
        )
        return True
    except IdempotencyConflictError:
        matches = tuple(
            event
            for event in store.replay_state().event_evidence
            if event.event_id == event_id
        )
        _require_exact(matches, "ALERT_ATTEMPT", payload)
        return False


def _append_exact_once(
    store: SQLiteStore,
    event_id: str,
    event_type: str,
    logical_at: datetime,
    payload: Mapping[str, object],
) -> None:
    snapshot = store.replay_state()
    matches = tuple(event for event in snapshot.event_evidence if event.event_id == event_id)
    if matches:
        _require_exact(matches, event_type, payload)
        return
    try:
        store.append_event(
            event_id,
            event_type,
            _monotonic_time(snapshot, logical_at),
            payload,
        )
    except IdempotencyConflictError:
        matches = tuple(
            event
            for event in store.replay_state().event_evidence
            if event.event_id == event_id
        )
        _require_exact(matches, event_type, payload)


def _require_exact(
    matches: tuple[object, ...],
    event_type: str,
    payload: Mapping[str, object],
) -> None:
    if len(matches) != 1:
        raise StoreCorruptionError("alert identity is duplicated")
    event = matches[0]
    if (
        getattr(event, "event_type", None) != event_type
        or dict(getattr(event, "payload", {})) != dict(payload)
    ):
        raise StoreCorruptionError("alert identity has conflicting evidence")


def _monotonic_time(snapshot: object, logical_at: datetime) -> datetime:
    evidence = getattr(snapshot, "event_evidence", ())
    return max(logical_at, evidence[-1].occurred_at_utc) if evidence else logical_at


def _bounded_event_text(event: Mapping[str, object]) -> str:
    if not isinstance(event, Mapping):
        raise TypeError("notification event must be a mapping")
    rendered = json.dumps(
        dict(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(rendered) <= _TELEGRAM_TEXT_LIMIT:
        return rendered
    suffix = "...[truncated]"
    return rendered[: _TELEGRAM_TEXT_LIMIT - len(suffix)] + suffix


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty")
    return value

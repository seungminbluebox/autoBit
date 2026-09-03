"""UTC four-hour completed-candle scheduling without fixed-sleep drift."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Protocol, TypeVar


_FOUR_HOURS = timedelta(hours=4)
_MATURITY_DELAY = timedelta(minutes=10)
_DEFAULT_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 60.0
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ResultT = TypeVar("_ResultT")


class Clock(Protocol):
    def now(self) -> datetime: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class CandleService(Protocol[_ResultT]):
    def process_completed_candle(self, end_utc: datetime) -> _ResultT: ...


def next_cycle_at(now_utc: datetime) -> datetime:
    """Return the next strict UTC four-hour close plus ten minutes."""
    now = _as_utc(now_utc)
    try:
        latest_slot = _floor_four_hours(now - _MATURITY_DELAY) + _MATURITY_DELAY
        result = latest_slot + _FOUR_HOURS if latest_slot <= now else latest_slot
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime is outside the supported scheduler range") from error
    return result


def latest_completed_end(now_utc: datetime) -> datetime:
    """Return the latest close old enough to process at ``now_utc``."""
    now = _as_utc(now_utc)
    try:
        return _floor_four_hours(now - _MATURITY_DELAY)
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime is outside the supported scheduler range") from error


class PaperScheduler:
    """Sleep to a target, then derive work from the actual wake-up clock."""

    def __init__(
        self,
        service: CandleService[_ResultT],
        clock: Clock,
        sleeper: Sleeper,
        retry_delay_seconds: float = _DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        if isinstance(retry_delay_seconds, bool):
            raise ValueError("retry delay must be positive and at most sixty seconds")
        try:
            delay = float(retry_delay_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "retry delay must be positive and at most sixty seconds"
            ) from error
        if not math.isfinite(delay) or not 0.0 < delay <= _MAX_RETRY_DELAY_SECONDS:
            raise ValueError("retry delay must be positive and at most sixty seconds")
        self._service = service
        self._clock = clock
        self._sleeper = sleeper
        self._retry_delay_seconds = delay
        self._next_end: datetime | None = None

    def run_once(self) -> _ResultT:
        actual_wake = _as_utc(self._clock.now())
        if self._next_end is None:
            latest_matured = latest_completed_end(actual_wake)
            resolver = getattr(self._service, "oldest_required_end", None)
            required = resolver(latest_matured) if callable(resolver) else latest_matured
            self._next_end = _resolved_end(required)

        try:
            target = self._next_end + _MATURITY_DELAY
        except OverflowError as error:
            raise ValueError("scheduler target is outside datetime range") from error
        while actual_wake < target:
            self._sleeper.sleep((target - actual_wake).total_seconds())
            actual_wake = _as_utc(self._clock.now())

        if self._next_end > latest_completed_end(actual_wake):
            raise RuntimeError("scheduler target did not mature a completed candle")
        return self._process_pending()

    def _process_pending(self) -> _ResultT:
        if self._next_end is None:  # Defensive: initialized before every call.
            raise RuntimeError("scheduler has no pending completed candle")
        process_end = self._next_end
        try:
            following_end = process_end + _FOUR_HOURS
        except OverflowError as error:
            raise ValueError("scheduler cursor is outside datetime range") from error
        try:
            result = self._service.process_completed_candle(process_end)
        except Exception:
            self._sleeper.sleep(self._retry_delay_seconds)
            raise
        if getattr(result, "status", None) == "LEASE_HELD":
            self._sleeper.sleep(self._retry_delay_seconds)
            return result
        self._next_end = following_end
        return result


def _floor_four_hours(value: datetime) -> datetime:
    try:
        elapsed = value - _EPOCH
        slots = elapsed // _FOUR_HOURS
        return _EPOCH + slots * _FOUR_HOURS
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime is outside the supported scheduler range") from error


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    try:
        if value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime is outside the supported scheduler range") from error


def _resolved_end(value: datetime) -> datetime:
    resolved = _as_utc(value)
    if resolved.minute or resolved.second or resolved.microsecond or resolved.hour % 4:
        raise ValueError("resolved end must be an exact four-hour UTC boundary")
    return resolved

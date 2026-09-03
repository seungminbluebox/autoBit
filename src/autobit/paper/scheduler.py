"""UTC four-hour completed-candle scheduling without fixed-sleep drift."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar


_FOUR_HOURS = timedelta(hours=4)
_MATURITY_DELAY = timedelta(minutes=10)
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
    ) -> None:
        self._service = service
        self._clock = clock
        self._sleeper = sleeper
        self._next_end: datetime | None = None

    def run_once(self) -> _ResultT:
        actual_wake = _as_utc(self._clock.now())
        if self._next_end is None:
            self._next_end = latest_completed_end(actual_wake)
            startup_result, advanced = self._process_pending()
            if not advanced:
                return startup_result
            actual_wake = _as_utc(self._clock.now())

        target = self._next_end + _MATURITY_DELAY
        while actual_wake < target:
            self._sleeper.sleep((target - actual_wake).total_seconds())
            actual_wake = _as_utc(self._clock.now())

        result: _ResultT | None = None
        while self._next_end <= latest_completed_end(actual_wake):
            result, advanced = self._process_pending()
            if not advanced:
                return result
        if result is None:  # Defensive: a reached target always matures its pending close.
            raise RuntimeError("scheduler target did not mature a completed candle")
        return result

    def _process_pending(self) -> tuple[_ResultT, bool]:
        if self._next_end is None:  # Defensive: initialized before every call.
            raise RuntimeError("scheduler has no pending completed candle")
        process_end = self._next_end
        result = self._service.process_completed_candle(process_end)
        if getattr(result, "status", None) == "LEASE_HELD":
            return result, False
        self._next_end = process_end + _FOUR_HOURS
        return result, True


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

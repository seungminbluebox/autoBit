from datetime import datetime, timedelta, timezone

import pytest

from autobit.paper.scheduler import (
    PaperScheduler,
    latest_completed_end,
    next_cycle_at,
)


UTC = timezone.utc


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 23, 59, tzinfo=UTC),
            datetime(2026, 1, 2, 0, 10, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 14, 23, tzinfo=timezone(timedelta(hours=9))),
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
        ),
    ],
)
def test_next_cycle_uses_the_next_four_hour_close_plus_ten_minutes(
    now: datetime,
    expected: datetime,
) -> None:
    assert next_cycle_at(now) == expected


def test_scheduler_time_functions_reject_naive_values() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_cycle_at(datetime(2026, 1, 1, 8, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_completed_end(datetime(2026, 1, 1, 8, 10))


@pytest.mark.parametrize("value", [datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)])
def test_scheduler_time_functions_fail_cleanly_at_datetime_range_edges(
    value: datetime,
) -> None:
    with pytest.raises(ValueError, match="range"):
        next_cycle_at(value)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 1, 1, 8, 9, 59, tzinfo=UTC),
            datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 1, 12, 15, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
    ],
)
def test_latest_completed_end_never_selects_an_unmatured_candle(
    now: datetime,
    expected: datetime,
) -> None:
    assert latest_completed_end(now) == expected


class _JumpClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = iter(values)

    def now(self) -> datetime:
        return next(self._values)


class _RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


class _RecordingService:
    def __init__(self) -> None:
        self.ends: list[datetime] = []

    def process_completed_candle(self, end_utc: datetime) -> str:
        self.ends.append(end_utc)
        return end_utc.isoformat()


def test_run_once_catches_up_each_matured_end_after_a_late_wake() -> None:
    clock = _JumpClock(
        [
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 15, tzinfo=UTC),
        ]
    )
    sleeper = _RecordingSleeper()
    service = _RecordingService()

    result = PaperScheduler(service, clock, sleeper).run_once()

    assert sleeper.delays == [10_020.0]
    assert service.ends == [
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]
    assert result == "2026-01-01T12:00:00+00:00"


def test_two_scheduler_cycles_recompute_from_actual_clock_without_fixed_drift() -> None:
    clock = _JumpClock(
        [
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 12, tzinfo=UTC),
            datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        ]
    )
    sleeper = _RecordingSleeper()
    service = _RecordingService()
    scheduler = PaperScheduler(service, clock, sleeper)

    scheduler.run_once()
    scheduler.run_once()

    assert sleeper.delays == [10_020.0, 11_400.0]
    assert service.ends == [
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]


def test_spurious_early_wake_recomputes_and_sleeps_the_remaining_time() -> None:
    clock = _JumpClock(
        [
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 5, 23, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
        ]
    )
    sleeper = _RecordingSleeper()
    service = _RecordingService()

    PaperScheduler(service, clock, sleeper).run_once()

    assert sleeper.delays == [10_020.0, 300.0]
    assert service.ends == [datetime(2026, 1, 1, 8, 0, tzinfo=UTC)]

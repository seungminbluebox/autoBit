from datetime import datetime, timedelta, timezone

import pytest

from autobit.paper.scheduler import (
    PaperScheduler,
    latest_completed_end,
    next_cycle_at,
)
from autobit.paper.service import CycleResult, CycleStatus


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


class _RecordingService:
    def __init__(self, oldest_end: datetime | None = None) -> None:
        self.ends: list[datetime] = []
        self.oldest_end = oldest_end
        self.resolved_latest: list[datetime] = []

    def oldest_required_end(self, latest_matured: datetime) -> datetime:
        self.resolved_latest.append(latest_matured)
        return self.oldest_end or latest_matured

    def process_completed_candle(self, end_utc: datetime) -> str:
        self.ends.append(end_utc)
        return end_utc.isoformat()


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class _AdvancingSleeper:
    def __init__(self, clock: _MutableClock) -> None:
        self._clock = clock
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self._clock.value += timedelta(seconds=seconds)


class _QueuedWakeSleeper:
    def __init__(self, clock: _MutableClock, wake_at: list[datetime]) -> None:
        self._clock = clock
        self._wake_at = iter(wake_at)
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self._clock.value = next(self._wake_at)


class _StatusService:
    def __init__(self, statuses: list[CycleStatus]) -> None:
        self._statuses = iter(statuses)
        self.ends: list[datetime] = []

    def process_completed_candle(self, end_utc: datetime) -> CycleResult:
        self.ends.append(end_utc)
        return CycleResult(next(self._statuses), end_utc)


def test_fresh_scheduler_returns_one_immediate_cycle_then_waits_for_the_next() -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    service = _RecordingService()
    scheduler = PaperScheduler(service, clock, sleeper)

    first = scheduler.run_once()
    second = scheduler.run_once()

    assert first == "2026-01-01T08:00:00+00:00"
    assert second == "2026-01-01T12:00:00+00:00"
    assert service.ends == [
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]
    assert service.resolved_latest == [datetime(2026, 1, 1, 8, 0, tzinfo=UTC)]
    assert sleeper.delays == [14_340.0]


def test_fresh_scheduler_uses_the_services_durable_oldest_required_end() -> None:
    now = datetime(2026, 1, 1, 12, 11, tzinfo=UTC)
    oldest = datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    clock = _MutableClock(now)
    sleeper = _AdvancingSleeper(clock)
    service = _RecordingService(oldest)

    result = PaperScheduler(service, clock, sleeper).run_once()

    assert result == oldest.isoformat()
    assert service.resolved_latest == [datetime(2026, 1, 1, 12, 0, tzinfo=UTC)]
    assert service.ends == [oldest]
    assert sleeper.delays == []


def test_scheduler_retries_the_same_end_after_service_exception() -> None:
    class FailOnceService(_RecordingService):
        def process_completed_candle(self, end_utc: datetime) -> str:
            self.ends.append(end_utc)
            if len(self.ends) == 1:
                raise RuntimeError("injected cycle failure")
            return end_utc.isoformat()

    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    service = FailOnceService()
    scheduler = PaperScheduler(service, clock, sleeper)

    with pytest.raises(RuntimeError, match="injected cycle failure"):
        scheduler.run_once()
    result = scheduler.run_once()

    assert result == "2026-01-01T08:00:00+00:00"
    assert service.ends == [
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    ]
    assert sleeper.delays == [1.0]


def test_scheduler_reads_dynamic_health_delay_once_after_each_failure() -> None:
    class AlwaysFail(_RecordingService):
        def process_completed_candle(self, end_utc: datetime) -> str:
            self.ends.append(end_utc)
            raise RuntimeError("recoverable public-data failure")

    delays = iter((1.0, 2.0, 4.0, 8.0, 16.0, 300.0, 300.0))
    provider_calls = 0

    def current_health_delay() -> float:
        nonlocal provider_calls
        provider_calls += 1
        return next(delays)

    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    scheduler = PaperScheduler(
        AlwaysFail(),
        clock,
        sleeper,
        retry_delay_provider=current_health_delay,
    )

    for _ in range(7):
        with pytest.raises(RuntimeError, match="recoverable"):
            scheduler.run_once()

    assert provider_calls == 7
    assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 300.0, 300.0]


def test_scheduler_resolver_failures_have_one_retry_sleep_boundary() -> None:
    class ResolverFailure(_RecordingService):
        def oldest_required_end(self, latest_matured: datetime) -> datetime:
            self.resolved_latest.append(latest_matured)
            raise RuntimeError("resolver corruption")

    delays = iter((1.0, 2.0, 4.0))
    provider_calls = 0

    def current_delay() -> float:
        nonlocal provider_calls
        provider_calls += 1
        return next(delays)

    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    scheduler = PaperScheduler(
        ResolverFailure(),
        clock,
        sleeper,
        retry_delay_provider=current_delay,
    )

    for _ in range(3):
        with pytest.raises(RuntimeError, match="resolver corruption"):
            scheduler.run_once()

    assert provider_calls == 3
    assert sleeper.delays == [1.0, 2.0, 4.0]


def test_scheduler_uses_one_300_second_failsafe_when_delay_provider_fails() -> None:
    class ResolverFailure(_RecordingService):
        def oldest_required_end(self, latest_matured: datetime) -> datetime:
            del latest_matured
            raise RuntimeError("original resolver failure")

    provider_calls = 0

    def unreadable_health_delay() -> float:
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError("health store unreadable")

    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    scheduler = PaperScheduler(
        ResolverFailure(),
        clock,
        sleeper,
        retry_delay_provider=unreadable_health_delay,
    )

    with pytest.raises(RuntimeError, match="original resolver failure"):
        scheduler.run_once()

    assert provider_calls == 1
    assert sleeper.delays == [300.0]


def test_scheduler_process_failure_keeps_one_failsafe_sleep_if_provider_fails() -> None:
    class ProcessFailure(_RecordingService):
        def process_completed_candle(self, end_utc: datetime) -> str:
            self.ends.append(end_utc)
            raise RuntimeError("original process failure")

    provider_calls = 0

    def unreadable_health_delay() -> float:
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError("health store unreadable")

    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    scheduler = PaperScheduler(
        ProcessFailure(),
        clock,
        sleeper,
        retry_delay_provider=unreadable_health_delay,
    )

    with pytest.raises(RuntimeError, match="original process failure"):
        scheduler.run_once()

    assert provider_calls == 1
    assert sleeper.delays == [300.0]


def test_scheduler_retries_lease_held_end_then_advances_after_completion() -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    service = _StatusService(
        [CycleStatus.LEASE_HELD, CycleStatus.ALREADY_PROCESSED, CycleStatus.PROCESSED]
    )
    scheduler = PaperScheduler(service, clock, sleeper)

    first = scheduler.run_once()
    second = scheduler.run_once()
    third = scheduler.run_once()

    assert first.status is CycleStatus.LEASE_HELD
    assert second.status is CycleStatus.ALREADY_PROCESSED
    assert third.status is CycleStatus.PROCESSED
    assert service.ends == [
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]
    assert sleeper.delays == [1.0, 14_339.0]


def test_run_once_catches_up_each_matured_end_after_a_late_wake() -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 5, 23, tzinfo=UTC))
    sleeper = _QueuedWakeSleeper(
        clock,
        [datetime(2026, 1, 1, 12, 15, tzinfo=UTC)],
    )
    service = _RecordingService()
    scheduler = PaperScheduler(service, clock, sleeper)

    first = scheduler.run_once()
    second = scheduler.run_once()
    third = scheduler.run_once()

    assert sleeper.delays == [10_020.0]
    assert service.ends == [
        datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]
    assert first == "2026-01-01T04:00:00+00:00"
    assert second == "2026-01-01T08:00:00+00:00"
    assert third == "2026-01-01T12:00:00+00:00"


def test_scheduler_cycles_recompute_from_actual_clock_without_fixed_drift() -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 5, 23, tzinfo=UTC))
    sleeper = _AdvancingSleeper(clock)
    service = _RecordingService()
    scheduler = PaperScheduler(service, clock, sleeper)

    scheduler.run_once()
    clock.value = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    scheduler.run_once()
    scheduler.run_once()

    assert sleeper.delays == [11_400.0]
    assert service.ends == [
        datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    ]


def test_spurious_early_wake_recomputes_and_sleeps_the_remaining_time() -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 5, 23, tzinfo=UTC))
    sleeper = _QueuedWakeSleeper(
        clock,
        [
            datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
            datetime(2026, 1, 1, 8, 10, tzinfo=UTC),
        ],
    )
    service = _RecordingService()
    scheduler = PaperScheduler(service, clock, sleeper)

    scheduler.run_once()
    scheduler.run_once()

    assert sleeper.delays == [10_020.0, 300.0]
    assert service.ends == [
        datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
    ]


@pytest.mark.parametrize("delay", [0.0, -1.0, 60.000_001, float("inf"), float("nan")])
def test_scheduler_rejects_nonpositive_or_unbounded_retry_delays(delay: float) -> None:
    clock = _MutableClock(datetime(2026, 1, 1, 8, 11, tzinfo=UTC))

    with pytest.raises(ValueError, match="retry delay"):
        PaperScheduler(_RecordingService(), clock, _AdvancingSleeper(clock), delay)


def test_scheduler_normalizes_cursor_increment_overflow_to_contract_error() -> None:
    maximum = datetime(9999, 12, 31, 20, 0, tzinfo=UTC)
    clock = _MutableClock(datetime(9999, 12, 31, 20, 11, tzinfo=UTC))

    with pytest.raises(ValueError, match="outside datetime range"):
        PaperScheduler(
            _RecordingService(maximum),
            clock,
            _AdvancingSleeper(clock),
        ).run_once()

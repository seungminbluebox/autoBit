from datetime import datetime, timedelta, timezone
from math import inf, nan
from types import MappingProxyType

import pytest

from autobit.paper.health import (
    HEALTH_REASON_ORDER,
    HealthAction,
    HealthMonitor,
    RecoveryProgress,
    HealthSnapshot,
    HealthStage,
)


UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _recover_api(monitor: HealthMonitor, start: datetime = NOW) -> None:
    for minute in range(3):
        monitor.record_api_success(start + timedelta(minutes=minute))


def test_three_api_failures_halt_and_three_successes_auto_resume() -> None:
    monitor = HealthMonitor()
    monitor.record_api_failure(NOW)
    monitor.record_api_failure(NOW + timedelta(seconds=2))

    action = monitor.record_api_failure(NOW + timedelta(seconds=6))

    assert action.halt_entries
    assert action.reason == "API_FAILURES"
    monitor.record_api_success(NOW + timedelta(minutes=1))
    monitor.record_api_success(NOW + timedelta(minutes=2))
    action = monitor.record_api_success(NOW + timedelta(minutes=3))
    assert action.resume_reduced
    assert action.stage is HealthStage.REDUCED


def test_unresolved_order_blocks_resume_even_when_api_is_healthy() -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1)

    _recover_api(monitor)

    action = monitor.current_action()
    assert action.halt_entries
    assert not action.resume_reduced
    assert action.reasons == ("UNRESOLVED_ORDER",)
    assert action.progress.remaining_gates == ("UNRESOLVED_ORDER",)


def test_exact_duplicate_api_observation_is_idempotent() -> None:
    monitor = HealthMonitor()

    first = monitor.record_api_failure(NOW)
    duplicate = monitor.record_api_failure(NOW)

    assert duplicate == first
    assert monitor.snapshot().api_failures == 1


def test_duplicate_api_observation_remains_idempotent_after_another_result() -> None:
    monitor = HealthMonitor()
    monitor.record_api_failure(NOW)
    after_success = monitor.record_api_success(NOW + timedelta(seconds=1))

    duplicate = monitor.record_api_failure(NOW)

    assert duplicate == after_success
    assert monitor.snapshot().api_failures == 0
    assert monitor.snapshot().api_successes == 1


def test_multiple_faults_remain_independent_and_reasons_are_canonical() -> None:
    monitor = HealthMonitor()
    monitor.record_fill_check(expected_price=100.0, actual_price=106.0, at=NOW)
    monitor.record_timestamp_check(False, NOW + timedelta(seconds=1))
    monitor.set_unresolved_orders(2, NOW + timedelta(seconds=2))
    monitor.record_api_failure(NOW + timedelta(seconds=3))
    monitor.record_api_failure(NOW + timedelta(seconds=4))
    monitor.record_api_failure(NOW + timedelta(seconds=5))

    assert monitor.current_action().reasons == (
        "API_FAILURES",
        "TIMESTAMP_REVERSAL",
        "UNRESOLVED_ORDER",
        "FILL_DEVIATION",
    )
    monitor.record_timestamp_check(True, NOW + timedelta(seconds=6))
    assert monitor.current_action().reasons == (
        "API_FAILURES",
        "UNRESOLVED_ORDER",
        "FILL_DEVIATION",
    )
    assert HEALTH_REASON_ORDER == (
        "API_FAILURES",
        "STALE_CANDLE",
        "TIMESTAMP_REVERSAL",
        "SCHEMA_ERROR",
        "UNRESOLVED_ORDER",
        "LEDGER_MISMATCH",
        "FILL_DEVIATION",
    )


def test_new_non_api_fault_requires_three_fresh_api_successes() -> None:
    monitor = HealthMonitor()
    monitor.record_api_success(NOW)
    monitor.record_api_success(NOW + timedelta(seconds=1))

    monitor.set_unresolved_orders(1, NOW + timedelta(seconds=2))
    monitor.set_unresolved_orders(0, NOW + timedelta(seconds=3))

    assert monitor.current_action().halt_entries
    assert monitor.current_action().progress.successes_observed == 0
    _recover_api(monitor, NOW + timedelta(minutes=1))
    assert monitor.current_action().resume_reduced


@pytest.mark.parametrize(
    ("checked_at", "observed_end", "expected_halt"),
    [
        (NOW + timedelta(minutes=9, seconds=59), None, False),
        (NOW + timedelta(minutes=10), None, True),
        (NOW + timedelta(minutes=10), NOW, False),
        (NOW + timedelta(minutes=10), NOW - timedelta(hours=4), True),
    ],
)
def test_stale_candle_exact_ten_minute_boundary(
    checked_at: datetime,
    observed_end: datetime | None,
    expected_halt: bool,
) -> None:
    monitor = HealthMonitor()

    action = monitor.record_candle_check(
        expected_end=NOW,
        observed_end=observed_end,
        checked_at=checked_at,
        structurally_valid=True,
    )

    assert ("STALE_CANDLE" in action.reasons) is expected_halt


def test_valid_exact_candle_clears_only_stale_latch() -> None:
    monitor = HealthMonitor()
    monitor.record_candle_check(
        expected_end=NOW,
        observed_end=None,
        checked_at=NOW + timedelta(minutes=10),
    )
    monitor.set_unresolved_orders(1, NOW + timedelta(minutes=11))

    action = monitor.record_candle_check(
        expected_end=NOW,
        observed_end=NOW,
        checked_at=NOW + timedelta(minutes=12),
        structurally_valid=True,
    )

    assert action.reasons == ("UNRESOLVED_ORDER",)


@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "1", None])
def test_malformed_unresolved_order_count_fails_closed(bad: object) -> None:
    monitor = HealthMonitor()

    with pytest.raises(ValueError, match="unresolved order count"):
        monitor.set_unresolved_orders(bad)  # type: ignore[arg-type]

    assert monitor.current_action().halt_entries
    assert monitor.current_action().reason == "SCHEMA_ERROR"


@pytest.mark.parametrize(
    ("cash_delta", "btc_delta", "matches"),
    [
        (1e-10, 1e-10, True),
        (1.0000001e-10, 0.0, False),
        (0.0, 1.0000001e-10, False),
    ],
)
def test_ledger_match_exact_tolerance(
    cash_delta: float,
    btc_delta: float,
    matches: bool,
) -> None:
    monitor = HealthMonitor()

    action = monitor.record_ledger_check(
        stored_cash=0.0,
        actual_cash=cash_delta,
        stored_btc=0.0,
        actual_btc=btc_delta,
        at=NOW,
    )

    assert ("LEDGER_MISMATCH" not in action.reasons) is matches


@pytest.mark.parametrize("bad", [-1.0, True, nan, inf, "not-a-number", "1.0"])
def test_invalid_ledger_values_activate_schema_fault(bad: object) -> None:
    monitor = HealthMonitor()

    with pytest.raises(ValueError, match="ledger values"):
        monitor.record_ledger_check(
            stored_cash=bad,  # type: ignore[arg-type]
            actual_cash=0.0,
            stored_btc=0.0,
            actual_btc=0.0,
            at=NOW,
        )

    assert monitor.current_action().reason == "SCHEMA_ERROR"


@pytest.mark.parametrize(
    ("actual", "excessive"),
    [(105.0, False), (105.0000001, True), (95.0, False), (94.9999999, True)],
)
def test_fill_deviation_exact_five_percent_boundary(
    actual: float,
    excessive: bool,
) -> None:
    monitor = HealthMonitor()

    action = monitor.record_fill_check(
        expected_price=100.0,
        actual_price=actual,
        at=NOW,
    )

    assert ("FILL_DEVIATION" in action.reasons) is excessive


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (0.0, 100.0),
        (-1.0, 100.0),
        (100.0, 0.0),
        (nan, 100.0),
        (100.0, inf),
        ("100.0", 100.0),
    ],
)
def test_invalid_fill_prices_activate_schema_fault(
    expected: object,
    actual: object,
) -> None:
    monitor = HealthMonitor()

    with pytest.raises(ValueError, match="fill prices"):
        monitor.record_fill_check(  # type: ignore[arg-type]
            expected_price=expected,
            actual_price=actual,
            at=NOW,
        )

    assert monitor.current_action().reason == "SCHEMA_ERROR"
    assert "FILL_DEVIATION" not in monitor.current_action().reasons


def test_timestamp_and_schema_faults_clear_only_on_valid_full_checks() -> None:
    monitor = HealthMonitor()
    monitor.record_timestamp_check(False, NOW)
    monitor.record_schema_check(False, NOW + timedelta(seconds=1))

    monitor.record_timestamp_check(True, NOW + timedelta(seconds=2))

    assert monitor.current_action().reasons == ("SCHEMA_ERROR",)
    monitor.record_schema_check(True, NOW + timedelta(seconds=3))
    assert monitor.current_action().halt_entries
    assert monitor.current_action().progress.remaining_gates == ("API_SUCCESSES",)


def test_failed_health_attempt_backoff_and_healthy_reset_are_exact() -> None:
    monitor = HealthMonitor()
    delays = [monitor.set_unresolved_orders(1).retry_delay_seconds]
    for second in range(1, 7):
        delays.append(
            monitor.set_unresolved_orders(1, NOW + timedelta(seconds=second)).retry_delay_seconds,
        )

    assert delays == [1, 2, 4, 8, 16, 300, 300]
    monitor.set_unresolved_orders(0, NOW + timedelta(seconds=8))
    _recover_api(monitor, NOW + timedelta(minutes=1))
    assert monitor.current_action().retry_delay_seconds == 0
    assert monitor.snapshot().retry_attempts == 0


def test_recovery_cycle_promotes_reduced_to_normal_without_human_approval() -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1, NOW)
    monitor.set_unresolved_orders(0, NOW + timedelta(seconds=1))
    _recover_api(monitor, NOW + timedelta(minutes=1))
    assert monitor.current_action().resume_reduced

    action = monitor.record_recovery_cycle_success(NOW + timedelta(minutes=4))

    assert action.stage is HealthStage.NORMAL
    assert not action.halt_entries
    assert not action.resume_reduced


def test_fault_during_reduced_returns_to_halted_and_restarts_progress() -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1, NOW)
    monitor.set_unresolved_orders(0, NOW + timedelta(seconds=1))
    _recover_api(monitor, NOW + timedelta(minutes=1))

    action = monitor.record_fill_check(
        expected_price=100.0,
        actual_price=106.0,
        at=NOW + timedelta(minutes=5),
    )

    assert action.stage is HealthStage.HALTED
    assert action.progress.successes_observed == 0
    assert action.reasons == ("FILL_DEVIATION",)


def test_snapshot_canonical_round_trip_is_immutable_and_restart_exact() -> None:
    monitor = HealthMonitor()
    monitor.record_api_failure(NOW)
    monitor.record_api_failure(NOW + timedelta(seconds=1))
    monitor.record_api_failure(NOW + timedelta(seconds=2))
    monitor.record_api_success(NOW + timedelta(seconds=3))
    monitor.set_unresolved_orders(1, NOW + timedelta(seconds=4))
    snapshot = monitor.snapshot()

    mapping = snapshot.to_mapping()
    restored = HealthMonitor(HealthSnapshot.from_mapping(mapping))

    assert isinstance(mapping, MappingProxyType)
    assert restored.snapshot() == snapshot
    assert restored.current_action() == monitor.current_action()
    with pytest.raises(TypeError):
        mapping["stage"] = "NORMAL"  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("extra", 1),
        lambda p: p.__setitem__("version", 2),
        lambda p: p.__setitem__("retry_attempts", True),
        lambda p: p.__setitem__("api_failures", -1),
        lambda p: p.__setitem__("ledger_cash_difference", inf),
        lambda p: p.__setitem__("last_failure_at_utc", "2026-01-01T00:00:00+00:00"),
        lambda p: p.__setitem__("fill_deviation", "0.0"),
        lambda p: p.__setitem__("reasons", ["SCHEMA_ERROR", "API_FAILURES"]),
        lambda p: p.__setitem__("halt_entries", False),
        lambda p: p.__setitem__("stage", "NORMAL"),
    ],
)
def test_strict_snapshot_rejects_extra_malformed_or_contradictory_state(mutate) -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1, NOW)
    payload = dict(monitor.snapshot().to_mapping())
    mutate(payload)

    with pytest.raises(ValueError):
        HealthSnapshot.from_mapping(payload)


def test_strict_snapshot_rejects_recovered_halted_stage_and_impossible_api_counters() -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1, NOW)
    payload = dict(monitor.snapshot().to_mapping())
    payload["unresolved_orders"] = 0
    payload["reasons"] = []
    payload["api_successes"] = 3

    with pytest.raises(ValueError):
        HealthSnapshot.from_mapping(payload)

    healthy = dict(HealthSnapshot().to_mapping())
    healthy["api_failures"] = 3
    with pytest.raises(ValueError):
        HealthSnapshot.from_mapping(healthy)


def test_empty_mapping_is_fresh_normal_for_task_three_compatibility() -> None:
    restored = HealthMonitor.from_mapping({})

    assert restored.current_action().stage is HealthStage.NORMAL
    assert not restored.current_action().halt_entries
    assert restored.snapshot().reasons == ()


def test_public_actions_and_progress_copy_mutable_reason_inputs() -> None:
    reasons = ["API_FAILURES"]
    gates = ["API_FAILURES", "API_SUCCESSES"]
    progress = RecoveryProgress(0, 3, gates)  # type: ignore[arg-type]
    action = HealthAction(
        True,
        False,
        "API_FAILURES",
        reasons,  # type: ignore[arg-type]
        1,
        HealthStage.HALTED,
        progress,
    )

    reasons.clear()
    gates.clear()

    assert action.reasons == ("API_FAILURES",)
    assert action.progress.remaining_gates == ("API_FAILURES", "API_SUCCESSES")

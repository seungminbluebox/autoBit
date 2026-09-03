from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from autobit.config import CostConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.paper.health import HealthMonitor, HealthSnapshot, HealthStage
from autobit.paper.service import CycleStatus, PaperService
from autobit.paper.service import _apply_health_action
from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    SQLiteStore,
)
from autobit.risk.breakers import RiskDecision


UTC = timezone.utc
END = datetime(2026, 5, 11, 4, tzinfo=UTC)
BAR_AT = END - timedelta(hours=4)


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Source:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[datetime] = []

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        self.calls.append(end_utc)
        return self.frame.copy()


def _history(end: datetime, bars: int = 601, *, breakout: bool = False) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp(end) - pd.Timedelta(hours=4),
        periods=bars,
        freq="4h",
        tz="UTC",
    )
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )
    if breakout:
        frame.iloc[-1, frame.columns.get_loc("close")] = 102.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 103.0
    return frame


def _service(
    path: Path,
    frame: pd.DataFrame,
    *,
    store: SQLiteStore | None = None,
    broker: PaperBroker | None = None,
    end: datetime = END,
    owner: str = "health-worker",
    fault_hook=None,
) -> tuple[SQLiteStore, PaperBroker, PaperService]:
    if store is None:
        store = SQLiteStore(path)
        store.initialize()
    if broker is None:
        broker = PaperBroker(store, CostConfig(0.0, 0.0))
    service = PaperService(
        source=_Source(frame),
        store=store,
        broker=broker,
        clock=_Clock(end + timedelta(minutes=10)),
        lease_owner=owner,
        lease_token=f"{owner}-token",
        costs=CostConfig(0.0, 0.0),
        fault_hook=fault_hook,
    )
    return store, broker, service


def _halted_api_monitor() -> HealthMonitor:
    monitor = HealthMonitor()
    monitor.record_api_failure(BAR_AT - timedelta(seconds=3))
    monitor.record_api_failure(BAR_AT - timedelta(seconds=2))
    monitor.record_api_failure(BAR_AT - timedelta(seconds=1))
    return monitor


def _reduced_monitor() -> HealthMonitor:
    monitor = _halted_api_monitor()
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=3))
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=2))
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=1))
    assert monitor.current_action().stage is HealthStage.REDUCED
    return monitor


def _persist(monitor: HealthMonitor, store: SQLiteStore, identity: str) -> int:
    return monitor.persist(
        store,
        event_id=identity,
        logical_at=BAR_AT,
    )


def test_health_snapshot_persists_reopens_and_exact_duplicate_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "health.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = _halted_api_monitor()

    sequence = _persist(monitor, store, "health:api-halt")
    duplicate_sequence = _persist(monitor, store, "health:api-halt")
    store.close()
    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)

    assert duplicate_sequence == sequence
    assert restored.snapshot() == monitor.snapshot()
    assert restored.current_action().halt_entries
    assert len(
        [event for event in reopened.replay_state().event_evidence if event.event_type == "HEALTH_STATE"]
    ) == 1


def test_stale_failure_duplicate_after_reopen_does_not_halt_early(tmp_path: Path) -> None:
    path = tmp_path / "failure-cursor.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = HealthMonitor()
    monitor.record_api_failure(BAR_AT - timedelta(seconds=2))
    monitor.record_api_failure(BAR_AT - timedelta(seconds=1))
    monitor.persist(store, event_id="health:two-failures", logical_at=BAR_AT)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)
    action = restored.record_api_failure(BAR_AT - timedelta(seconds=2))

    assert restored.snapshot().api_failures == 2
    assert action.stage is HealthStage.NORMAL
    assert restored.record_api_failure(BAR_AT).halt_entries


def test_stale_success_duplicate_after_reopen_does_not_resume_early(tmp_path: Path) -> None:
    path = tmp_path / "success-cursor.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = _halted_api_monitor()
    monitor.record_api_success(BAR_AT + timedelta(seconds=10))
    monitor.record_api_success(BAR_AT + timedelta(seconds=11))
    monitor.persist(store, event_id="health:two-successes", logical_at=BAR_AT)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)
    action = restored.record_api_success(BAR_AT + timedelta(seconds=10))

    assert restored.snapshot().api_successes == 2
    assert action.stage is HealthStage.HALTED
    assert not action.resume_reduced
    assert restored.record_api_success(BAR_AT + timedelta(seconds=12)).resume_reduced


def test_persisted_identity_reuse_with_different_health_fails_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "health.sqlite3")
    store.initialize()
    halted = _halted_api_monitor()
    _persist(halted, store, "health:same")
    recovered = _reduced_monitor()

    with pytest.raises(IdempotencyConflictError):
        _persist(recovered, store, "health:same")


def test_reopen_preserves_recovery_progress_reduced_and_normal_promotion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "health.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = _halted_api_monitor()
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=2))
    _persist(monitor, store, "health:progress")
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)
    assert restored.current_action().progress.successes_observed == 1
    assert restored.current_action().retry_delay_seconds == 2
    restored.record_api_success(BAR_AT - timedelta(milliseconds=1))
    restored.record_api_success(BAR_AT)
    assert restored.current_action().stage is HealthStage.REDUCED
    restored.persist(reopened, event_id="health:reduced", logical_at=BAR_AT)
    reopened.close()

    second = SQLiteStore(path)
    second.initialize()
    reduced = HealthMonitor.from_store(second)
    assert reduced.current_action().stage is HealthStage.REDUCED
    reduced.record_recovery_cycle_success(END)
    reduced.persist(second, event_id="health:normal", logical_at=BAR_AT)
    assert HealthMonitor.from_store(second).current_action().stage is HealthStage.NORMAL


def test_malformed_latest_public_health_payload_cannot_enable_entry(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    store.append_event(
        "health:malformed",
        "HEALTH_STATE",
        BAR_AT,
        {"halt_entries": False, "unexpected": "truthy bypass"},
    )
    _, broker, service = _service(
        path,
        _history(END, breakout=True),
        store=store,
    )

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.PROCESSED
    assert "system_unhealthy" in result.reasons
    assert broker.reconcile().active_orders == ()


def test_malformed_older_health_event_cannot_be_hidden_by_a_valid_tail(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "health.sqlite3")
    store.initialize()
    store.append_event(
        "health:malformed-old",
        "HEALTH_STATE",
        BAR_AT,
        {"halt_entries": False, "unexpected": "invalid"},
    )
    healthy = HealthMonitor()
    healthy.persist(store, event_id="health:valid-tail", logical_at=BAR_AT)

    with pytest.raises(ValueError):
        HealthMonitor.from_store(store)


def test_three_failures_block_otherwise_valid_entry_but_keep_cycle_running(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    _persist(_halted_api_monitor(), store, "health:halt")
    _, broker, service = _service(path, _history(END, breakout=True), store=store)

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.PROCESSED
    assert result.reasons == ("system_unhealthy",)
    assert broker.reconcile().active_orders == ()
    assert len([e for e in store.replay_state().event_evidence if e.event_type == "PAPER_CYCLE"]) == 1


def test_halted_health_still_reconciles_fill_protects_position_and_submits_exit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    entry = broker.submit_entry(BAR_AT - timedelta(hours=4), quantity=0.2)
    _persist(_halted_api_monitor(), store, "health:halt")
    _, _, service = _service(
        path,
        _history(END),
        store=store,
        broker=broker,
    )

    result = service.process_completed_candle(END)
    account = broker.reconcile()

    assert entry.order_id in result.filled_order_ids
    assert account.btc_quantity == pytest.approx(0.2)
    assert account.active_stop is not None
    exit_order, = account.active_orders
    assert exit_order.side == "SELL"
    assert exit_order.reason == "SYSTEM_EXIT"


def test_api_recovery_cannot_enter_while_another_fault_remains(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = _halted_api_monitor()
    monitor.set_unresolved_orders(1, BAR_AT - timedelta(milliseconds=4))
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=3))
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=2))
    monitor.record_api_success(BAR_AT - timedelta(milliseconds=1))
    _persist(monitor, store, "health:multi-fault")
    _, broker, service = _service(path, _history(END, breakout=True), store=store)

    result = service.process_completed_candle(END)

    assert result.reasons == ("system_unhealthy",)
    assert broker.reconcile().active_orders == ()


def test_reduced_health_halves_existing_risk_once_and_restart_does_not_duplicate(
    tmp_path: Path,
) -> None:
    normal_path = tmp_path / "normal.sqlite3"
    normal_store, normal_broker, normal_service = _service(
        normal_path,
        _history(END, breakout=True),
        owner="normal-worker",
    )
    normal_result = normal_service.process_completed_candle(END)
    normal_order, = normal_broker.reconcile().active_orders

    reduced_path = tmp_path / "reduced.sqlite3"
    reduced_store = SQLiteStore(reduced_path)
    reduced_store.initialize()
    _persist(_reduced_monitor(), reduced_store, "health:reduced")
    _, reduced_broker, reduced_service = _service(
        reduced_path,
        _history(END, breakout=True),
        store=reduced_store,
        owner="reduced-worker",
    )
    reduced_result = reduced_service.process_completed_candle(END)
    reduced_order, = reduced_broker.reconcile().active_orders
    before_restart = reduced_store.replay_state()
    reduced_store.close()

    reopened = SQLiteStore(reduced_path)
    reopened.initialize()
    _, reopened_broker, retry = _service(
        reduced_path,
        _history(END, breakout=True),
        store=reopened,
        broker=PaperBroker(reopened, CostConfig(0.0, 0.0)),
        owner="restarted-reduced-worker",
    )
    duplicate = retry.process_completed_candle(END)
    after_restart = reopened.replay_state()

    assert normal_result.reasons == ()
    assert reduced_result.reasons == ("health_recovery_reduced",)
    assert reduced_order.requested_quantity == pytest.approx(
        normal_order.requested_quantity / 2.0,
        rel=0.0,
        abs=1e-15,
    )
    reduced_risk = after_restart.breaker_state
    assert reduced_risk["decision_risk_rate"] == pytest.approx(0.01)
    assert reduced_risk["decision_exposure_cap"] == pytest.approx(0.35)
    assert duplicate.status is CycleStatus.ALREADY_PROCESSED
    assert after_restart.last_sequence == before_restart.last_sequence
    assert len(reopened_broker.reconcile().active_orders) == 1


def test_persisted_recovery_cycle_promotion_restores_normal_service_size(
    tmp_path: Path,
) -> None:
    next_end = END + timedelta(hours=4)
    reference_path = tmp_path / "reference.sqlite3"
    _, reference_broker, reference_service = _service(
        reference_path,
        _history(next_end, breakout=True),
        end=next_end,
        owner="reference-worker",
    )
    reference_service.process_completed_candle(next_end)
    reference_order, = reference_broker.reconcile().active_orders

    path = tmp_path / "promoted.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = _reduced_monitor()
    _persist(monitor, store, "health:reduced")
    _, _, reduced_service = _service(
        path,
        _history(END),
        store=store,
        owner="reduced-cycle-worker",
    )
    reduced_result = reduced_service.process_completed_candle(END)
    assert reduced_result.reasons == ("health_recovery_reduced",)

    monitor = HealthMonitor.from_store(store)
    monitor.record_recovery_cycle_success(END + timedelta(minutes=1))
    assert monitor.current_action().stage is HealthStage.NORMAL
    monitor.persist(store, event_id="health:normal", logical_at=END)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)
    assert restored.current_action().stage is HealthStage.NORMAL
    _, broker, service = _service(
        path,
        _history(next_end, bars=602, breakout=True),
        store=reopened,
        end=next_end,
        owner="promoted-worker",
    )
    result = service.process_completed_candle(next_end)
    order, = broker.reconcile().active_orders

    assert result.reasons == ()
    assert order.requested_quantity == pytest.approx(reference_order.requested_quantity)


def test_reduced_crash_restart_reuses_the_once_halved_persisted_decision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reduced-crash.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    _persist(_reduced_monitor(), store, "health:reduced")

    def crash(boundary: str) -> None:
        if boundary == "after_order_acceptance":
            raise RuntimeError("crash after reduced acceptance")

    _, broker, crashing = _service(
        path,
        _history(END, breakout=True),
        store=store,
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError, match="reduced acceptance"):
        crashing.process_completed_candle(END)
    accepted, = broker.reconcile().active_orders
    risk_after_crash = store.replay_state().breaker_state
    assert risk_after_crash["decision_risk_rate"] == pytest.approx(0.01)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    _, reopened_broker, retry = _service(
        path,
        _history(END, breakout=True),
        store=reopened,
        broker=PaperBroker(reopened, CostConfig(0.0, 0.0)),
        owner="reduced-restart-worker",
    )
    result = retry.process_completed_candle(END)
    same_order, = reopened_broker.reconcile().active_orders
    state = reopened.replay_state()

    assert result.reasons == ("health_recovery_reduced",)
    assert same_order == accepted
    assert state.breaker_state["decision_risk_rate"] == pytest.approx(0.01)
    assert len([e for e in state.event_evidence if e.event_type == "BREAKER_STATE"]) == 1
    assert len([e for e in state.event_evidence if e.event_type == "ORDER_CREATED"]) == 1


@pytest.mark.parametrize("late_stage", [HealthStage.HALTED, HealthStage.REDUCED])
def test_late_same_bar_health_change_persists_an_idempotent_risk_followup(
    tmp_path: Path,
    late_stage: HealthStage,
) -> None:
    path = tmp_path / f"late-{late_stage.value.lower()}.sqlite3"

    def crash(boundary: str) -> None:
        if boundary == "after_order_acceptance":
            raise RuntimeError("crash before cycle completion")

    store, broker, crashing = _service(
        path,
        _history(END, breakout=True),
        fault_hook=crash,
    )
    with pytest.raises(RuntimeError, match="cycle completion"):
        crashing.process_completed_candle(END)
    accepted, = broker.reconcile().active_orders
    initial = store.replay_state()
    assert initial.breaker_state["decision_risk_rate"] == pytest.approx(0.02)
    assert len([e for e in initial.event_evidence if e.event_type == "BREAKER_STATE"]) == 1

    late_health = (
        _halted_api_monitor()
        if late_stage is HealthStage.HALTED
        else _reduced_monitor()
    )
    late_health.persist(
        store,
        event_id=f"health:late-{late_stage.value.lower()}",
        logical_at=END,
    )
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()

    def crash_after_followup(boundary: str) -> None:
        if boundary == "after_health_risk_followup":
            raise RuntimeError("crash after health risk followup")

    _, reopened_broker, retry = _service(
        path,
        _history(END, breakout=True),
        store=reopened,
        broker=PaperBroker(reopened, CostConfig(0.0, 0.0)),
        owner=f"retry-{late_stage.value.lower()}",
        fault_hook=crash_after_followup,
    )
    with pytest.raises(RuntimeError, match="health risk followup"):
        retry.process_completed_candle(END)
    after_crash = reopened.replay_state()
    assert len(
        [e for e in after_crash.event_evidence if e.event_type == "BREAKER_STATE"]
    ) == 2
    assert not [e for e in after_crash.event_evidence if e.event_type == "PAPER_CYCLE"]
    reopened.close()

    final_store = SQLiteStore(path)
    final_store.initialize()
    _, reopened_broker, final_retry = _service(
        path,
        _history(END, breakout=True),
        store=final_store,
        broker=PaperBroker(final_store, CostConfig(0.0, 0.0)),
        owner=f"final-{late_stage.value.lower()}",
    )
    result = final_retry.process_completed_candle(END)
    after = final_store.replay_state()
    same_order, = reopened_broker.reconcile().active_orders
    risk_events = [e for e in after.event_evidence if e.event_type == "BREAKER_STATE"]
    followups = [e for e in risk_events if e.event_id.startswith("risk-health:")]

    assert same_order == accepted
    assert len(risk_events) == 2
    assert len(followups) == 1
    assert followups[0].occurred_at_utc == END
    assert ":v1:" in followups[0].event_id
    assert after.breaker_state["last_risk_at_utc"] == "2026-05-11T00:00:00Z"
    decision_keys = {
        "decision_exposure_cap",
        "decision_halted_until_utc",
        "decision_reasons",
        "decision_risk_rate",
        "halt_entries",
    }
    assert {
        key: value
        for key, value in after.breaker_state.items()
        if key not in decision_keys
    } == {
        key: value
        for key, value in initial.breaker_state.items()
        if key not in decision_keys
    }
    if late_stage is HealthStage.HALTED:
        assert result.reasons == ("system_unhealthy",)
        assert after.breaker_state["decision_risk_rate"] == 0.0
        assert after.breaker_state["decision_exposure_cap"] == 0.0
    else:
        assert result.reasons == ("health_recovery_reduced",)
        assert after.breaker_state["decision_risk_rate"] == pytest.approx(0.01)
        assert after.breaker_state["decision_exposure_cap"] == pytest.approx(0.35)

    before_duplicate = after.last_sequence
    assert final_retry.process_completed_candle(END).status is CycleStatus.ALREADY_PROCESSED
    duplicate = final_store.replay_state()
    assert duplicate.last_sequence == before_duplicate
    assert len([e for e in duplicate.event_evidence if e.event_type == "BREAKER_STATE"]) == 2
    assert len([e for e in duplicate.event_evidence if e.event_type == "ORDER_CREATED"]) == 1
    assert len([e for e in duplicate.event_evidence if e.event_type == "PAPER_CYCLE"]) == 1

    assert final_retry.oldest_required_end(END) == END + timedelta(hours=4)


def test_reduced_health_preserves_stricter_core_risk_reasons_before_halving() -> None:
    base = RiskDecision(
        risk_rate=0.005,
        exposure_cap=0.30,
        halted_until=None,
        reasons=("weekly_loss_reduced", "loss_streak_reduced"),
    )

    reduced = _apply_health_action(base, _reduced_monitor().current_action())

    assert reduced.risk_rate == pytest.approx(0.0025)
    assert reduced.exposure_cap == pytest.approx(0.15)
    assert reduced.reasons == (
        "weekly_loss_reduced",
        "loss_streak_reduced",
        "health_recovery_reduced",
    )


def test_strict_health_snapshot_parser_rejects_mapping_proxy_mutation_bypass() -> None:
    monitor = _reduced_monitor()
    payload = dict(monitor.snapshot().to_mapping())
    payload["resume_reduced"] = False

    with pytest.raises(ValueError):
        HealthSnapshot.from_mapping(payload)

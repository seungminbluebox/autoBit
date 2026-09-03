from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest

from autobit.config import CostConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.paper.health import (
    HealthMonitor,
    HealthSnapshot,
    HealthStage,
    HealthStateError,
)
from autobit.paper.service import CycleStatus, PaperService
from autobit.paper.service import _apply_health_action
from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    SQLiteStore,
    StoreCorruptionError,
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


def _store_with_empty_health_event(path: Path, position: str) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    halted = _halted_api_monitor()
    if position == "older":
        store.append_event("health:empty", "HEALTH_STATE", BAR_AT, {})
        halted.persist(store, event_id="health:halted", logical_at=BAR_AT)
    else:
        halted.persist(store, event_id="health:halted", logical_at=BAR_AT)
        store.append_event("health:empty", "HEALTH_STATE", BAR_AT, {})
    return store


def _normal_breaker_payload(tmp_path: Path, identity: str) -> dict[str, object]:
    store, _, service = _service(
        tmp_path / f"normal-risk-{identity}.sqlite3",
        _history(END),
        owner=f"normal-risk-{identity}",
    )
    service.process_completed_candle(END)
    return dict(store.replay_state().breaker_state)


def _reduced_payload(payload: dict[str, object], *, halve: bool) -> dict[str, object]:
    reduced = dict(payload)
    reduced["decision_reasons"] = ["health_recovery_reduced"]
    if halve:
        reduced["decision_risk_rate"] = 0.01
        reduced["decision_exposure_cap"] = 0.35
    return reduced


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


@pytest.mark.parametrize("position", ["older", "tail"])
def test_real_empty_health_event_is_rejected_anywhere_in_store_history(
    tmp_path: Path,
    position: str,
) -> None:
    path = tmp_path / f"empty-health-{position}.sqlite3"
    store = _store_with_empty_health_event(path, position)
    before = store.replay_state()
    store.close()
    reopened = SQLiteStore(path)
    reopened.initialize()

    with pytest.raises(HealthStateError, match="empty|event"):
        HealthMonitor.from_store(reopened)

    assert reopened.replay_state() == before


@pytest.mark.parametrize("position", ["older", "tail"])
@pytest.mark.parametrize("public_path", ["process", "oldest"])
def test_empty_health_event_fails_before_any_public_service_side_effect(
    tmp_path: Path,
    position: str,
    public_path: str,
) -> None:
    path = tmp_path / f"empty-health-{position}-{public_path}.sqlite3"
    store = _store_with_empty_health_event(path, position)
    source = _Source(_history(END, breakout=True))
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    service = PaperService(
        source=source,
        store=store,
        broker=broker,
        clock=_Clock(END + timedelta(minutes=10)),
        lease_owner=f"empty-health-{position}-{public_path}",
        lease_token=f"empty-health-{position}-{public_path}-token",
        costs=CostConfig(0.0, 0.0),
    )
    before = store.replay_state()

    with pytest.raises(HealthStateError, match="empty|event"):
        if public_path == "process":
            service.process_completed_candle(END)
        else:
            service.oldest_required_end(END)

    assert source.calls == []
    assert broker.reconcile().active_orders == ()
    assert broker.reconcile().fills == ()
    assert store.replay_state() == before


def test_store_without_health_events_keeps_task_three_normal_compatibility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "no-health-events.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    assert HealthMonitor.from_store(store).current_action().stage is HealthStage.NORMAL
    _, broker, service = _service(path, _history(END, breakout=True), store=store)

    result = service.process_completed_candle(END)

    assert result.reasons == ()
    assert len(broker.reconcile().active_orders) == 1


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


@pytest.mark.parametrize(
    ("partial_successes", "renewed_failures"),
    [(1, 1), (1, 2), (2, 1), (2, 2)],
)
def test_reachable_partial_recovery_outage_persists_and_reopens(
    tmp_path: Path,
    partial_successes: int,
    renewed_failures: int,
) -> None:
    path = tmp_path / f"reachable-latch-{partial_successes}-{renewed_failures}.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    monitor = HealthMonitor()
    for second in range(3):
        monitor.record_api_failure(BAR_AT + timedelta(seconds=second))
    for second in range(partial_successes):
        monitor.record_api_success(BAR_AT + timedelta(seconds=10 + second))
    for second in range(renewed_failures):
        monitor.record_api_failure(BAR_AT + timedelta(seconds=20 + second))
    monitor.persist(store, event_id="health:reachable-latch", logical_at=END)
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restored = HealthMonitor.from_store(reopened)

    assert restored.snapshot().api_failure_latched
    assert restored.snapshot().api_failures == renewed_failures
    assert restored.current_action().stage is HealthStage.HALTED
    for second in range(3):
        action = restored.record_api_success(BAR_AT + timedelta(seconds=30 + second))
    assert action.stage is HealthStage.REDUCED
    assert action.resume_reduced


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
    risk_events = [
        event
        for event in after_restart.event_evidence
        if event.event_type == "BREAKER_STATE"
    ]
    assert risk_events[0].event_id == "risk:2026-05-11T00:00:00Z"
    assert "health_recovery_reduced" not in risk_events[0].payload["decision_reasons"]
    reduced_digest = sha256("health:reduced".encode("utf-8")).hexdigest()
    assert risk_events[1].event_id == (
        "risk-health:2026-05-11T00:00:00Z:1:v2:"
        f"{reduced_digest}"
    )
    assert risk_events[1].payload["decision_reasons"] == ("health_recovery_reduced",)
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
    risk_events = [e for e in state.event_evidence if e.event_type == "BREAKER_STATE"]
    assert len(risk_events) == 2
    assert risk_events[0].event_id == "risk:2026-05-11T00:00:00Z"
    assert risk_events[1].event_id.startswith("risk-health:2026-05-11T00:00:00Z:")
    assert len([e for e in state.event_evidence if e.event_type == "ORDER_CREATED"]) == 1


@pytest.mark.parametrize(
    "forged_event_id",
    [
        "breaker:forged-reduced",
        "risk:2026-05-11T00:00:00Z",
        "risk-health:forged",
    ],
)
@pytest.mark.parametrize("public_path", ["process", "oldest"])
def test_unbound_reduced_marker_fails_closed_before_public_service_mutation(
    tmp_path: Path,
    forged_event_id: str,
    public_path: str,
) -> None:
    path = tmp_path / f"unbound-{public_path}-{forged_event_id.split(':')[0]}.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    _persist(_reduced_monitor(), store, "health:reduced")
    payload = _reduced_payload(
        _normal_breaker_payload(tmp_path, f"{public_path}-{forged_event_id.replace(':', '-')}"),
        halve=False,
    )
    store.append_event(forged_event_id, "BREAKER_STATE", BAR_AT, payload)
    _, broker, service = _service(path, _history(END, breakout=True), store=store)
    before = store.replay_state().last_sequence

    with pytest.raises(StoreCorruptionError):
        if public_path == "process":
            service.process_completed_candle(END)
        else:
            service.oldest_required_end(END)

    after = store.replay_state()
    assert after.last_sequence == before
    assert broker.reconcile().active_orders == ()
    assert not [event for event in after.event_evidence if event.event_type == "PAPER_CYCLE"]


def test_reduced_followup_bound_to_old_health_evidence_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "old-health-binding.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    old_id = "health:old-reduced"
    old_sequence = _persist(_reduced_monitor(), store, old_id)
    latest = _reduced_monitor()
    latest.persist(store, event_id="health:latest-reduced", logical_at=BAR_AT)
    base = _normal_breaker_payload(tmp_path, "old-health-binding")
    store.append_event("risk:2026-05-11T00:00:00Z", "BREAKER_STATE", BAR_AT, base)
    old_version = latest.snapshot().version
    old_digest = sha256(old_id.encode("utf-8")).hexdigest()
    forged_id = (
        "risk-health:2026-05-11T00:00:00Z:"
        f"{old_sequence}:v{old_version}:{old_digest}"
    )
    store.append_event(
        forged_id,
        "BREAKER_STATE",
        BAR_AT,
        _reduced_payload(base, halve=True),
    )
    _, broker, service = _service(path, _history(END, breakout=True), store=store)
    before = store.replay_state().last_sequence

    with pytest.raises(StoreCorruptionError):
        service.process_completed_candle(END)

    assert store.replay_state().last_sequence == before
    assert broker.reconcile().active_orders == ()


def test_health_bound_reduced_followup_rejects_mismatched_payload(tmp_path: Path) -> None:
    path = tmp_path / "mismatched-health-overlay.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    health_id = "health:latest-reduced"
    monitor = _reduced_monitor()
    health_sequence = _persist(monitor, store, health_id)
    base = _normal_breaker_payload(tmp_path, "mismatched-health-overlay")
    store.append_event("risk:2026-05-11T00:00:00Z", "BREAKER_STATE", BAR_AT, base)
    health_digest = sha256(health_id.encode("utf-8")).hexdigest()
    bound_id = (
        "risk-health:2026-05-11T00:00:00Z:"
        f"{health_sequence}:v{monitor.snapshot().version}:{health_digest}"
    )
    store.append_event(
        bound_id,
        "BREAKER_STATE",
        BAR_AT,
        _reduced_payload(base, halve=False),
    )
    _, broker, service = _service(path, _history(END, breakout=True), store=store)
    before = store.replay_state().last_sequence

    with pytest.raises(StoreCorruptionError):
        service.process_completed_candle(END)

    assert store.replay_state().last_sequence == before
    assert broker.reconcile().active_orders == ()


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
    assert ":v2:" in followups[0].event_id
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

"""Historical v1 evidence is read strictly, never canonically reinvented."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, _DonchianBacktestStrategy, run_backtest
from autobit.config import CostConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.paper.health import HealthMonitor
from autobit.paper.service import (
    PaperService, _forced_exit_reason,
    _validate_health_and_risk_chain,
)
from autobit.persistence.sqlite_store import (
    CycleLeaseLostError,
    SQLiteStore,
    StoreCorruptionError,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "paper_risk_v1.json"
ROW = pd.Series({
    "close": 60.0, "atr_14": 1.2, "baseline_atr_pct": .02,
    "warmup_complete": True, "entry_data_valid": True,
})


class _OfflineSource:
    def load_completed_candles(self, end_utc):
        raise AssertionError("risk-boundary test must not fetch data")


class _Clock:
    def now(self):
        return START + timedelta(days=12)


def _service(store):
    return PaperService(
        source=_OfflineSource(), store=store, broker=PaperBroker(store, CostConfig(0, 0)),
        clock=_Clock(), lease_owner="migration", lease_token="migration",
        costs=CostConfig(0, 0),
    )


def _old_store(path, *, mutate=None):
    store = SQLiteStore(path)
    store.initialize()
    # A real, still-open position marks to 84 at close=60. No closed-trade
    # cursor is invented: .4 BTC at 100 leaves 60 cash and zero closed trades.
    broker = PaperBroker(store, CostConfig(0, 0))
    entry = broker.submit_entry(START - timedelta(hours=8), quantity=.4)
    broker.process_open(entry.order_id, START - timedelta(hours=4), open_price=100)
    monitor = HealthMonitor()
    monitor.record_api_success(START - timedelta(seconds=1))
    monitor.persist(store, event_id="health:legacy", logical_at=START)
    payloads = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if mutate:
        mutate(payloads)
    for payload in payloads:
        at = payload["last_risk_at_utc"]
        store.append_event(f"risk:{at}", "BREAKER_STATE", at, payload)
    return store


def _decide(store, at, equity=84.0):
    service = _service(store)
    assert service.acquire_cycle_lease()
    try:
        return service._risk_decision(
            at,
            ROW,
            equity,
            PaperBroker(store).reconcile(),
        )
    finally:
        assert service.release_cycle_lease()


def test_direct_risk_migration_mutation_requires_an_acquired_lease(tmp_path):
    store = _old_store(tmp_path / "unleased-migration.sqlite3")
    before = store.replay_state()

    with pytest.raises(CycleLeaseLostError, match="no acquired lease epoch"):
        _service(store)._risk_decision(
            START + timedelta(hours=244),
            ROW,
            84.0,
            PaperBroker(store).reconcile(),
        )

    assert store.replay_state() == before
    store.close()


def test_real_v1_sqlite_history_migrates_without_rewriting_and_replays_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = _old_store(path)
    original = store.replay_state()
    assert _validate_health_and_risk_chain(original).projection.decision.risk_rate == 0
    store.close()
    store = SQLiteStore(path)
    store.initialize()
    assert store.replay_state() == original
    for hours in (244, 248, 252):
        at = START + timedelta(hours=hours)
        decision = _decide(store, at)
        assert (decision.risk_rate, decision.exposure_cap) == (.0025, .15)
        assert decision.reasons == ("recovery",)
        assert _forced_exit_reason(decision) is None
        assert not _DonchianBacktestStrategy._requires_forced_exit(decision)
        after = store.replay_state()
        assert after.event_evidence[:len(original.event_evidence)] == original.event_evidence
        assert after.breaker_state["version"] == 2
        assert after.breaker_state["equity_peak"] == 100.0
        assert after.breaker_state["recovery_started_at_utc"] == "2026-01-01T00:00:00Z"
        assert after.breaker_state["last_equity"] == 84.0
        assert _decide(store, at) == decision
        assert store.replay_state() == after
        store.close()
        store = SQLiteStore(path)
        store.initialize()
        assert store.replay_state() == after
        assert _decide(store, at) == decision
        assert store.replay_state() == after
    store.close()


def test_recovered_peak_clears_episode_and_new_drawdown_restarts_cooldown(tmp_path):
    store = _old_store(tmp_path / "episode.sqlite3")
    _decide(store, START + timedelta(hours=244))
    normal = _decide(store, START + timedelta(hours=248), equity=100)
    assert (normal.risk_rate, normal.exposure_cap) == (.02, .70)
    assert store.replay_state().breaker_state["recovery_started_at_utc"] is None
    new_start = START + timedelta(hours=252)
    halted = _decide(store, new_start)
    assert "drawdown_halt" in halted.reasons
    assert halted.halted_until == new_start + timedelta(hours=72)
    assert _forced_exit_reason(halted) == "RISK_EXIT"
    state = store.replay_state().breaker_state
    assert state["equity_peak"] == 100.0
    assert state["recovery_started_at_utc"] == "2026-01-11T12:00:00Z"
    store.close()


@pytest.mark.parametrize("version", [0, 3, True, 1.0, "1"])
def test_rejects_unknown_or_noninteger_version_before_new_decision(tmp_path, version):
    store = _old_store(tmp_path / "version.sqlite3", mutate=lambda p: p[1].update(version=version))
    before = store.replay_state()
    with pytest.raises(StoreCorruptionError, match="schema|version"):
        _decide(store, START + timedelta(hours=244))
    assert store.replay_state() == before
    store.close()


def test_rejects_v2_to_v1_downgrade_in_event_chain(tmp_path):
    store = _old_store(tmp_path / "downgrade.sqlite3")
    _decide(store, START + timedelta(hours=244))
    snapshot = store.replay_state()
    assert snapshot.breaker_state["version"] == 2
    at = "2026-01-11T08:00:00Z"
    payload = dict(snapshot.breaker_state) | {
        "version": 1, "last_risk_at_utc": at,
        "equity_history": [*snapshot.breaker_state["equity_history"], {"at_utc": at, "equity": 84.0}],
    }
    store.append_event(f"risk:{at}", "BREAKER_STATE", at, payload)
    before = store.replay_state()
    with pytest.raises(StoreCorruptionError, match="downgrade"):
        _decide(store, START + timedelta(hours=252))
    assert store.replay_state() == before
    store.close()


@pytest.mark.parametrize("mutation", [
    lambda p: p[1].update(unexpected_field=True),
    lambda p: p[1].update(halt_entries=False),
    lambda p: p[1]["equity_history"][-1].update(at_utc="2026-01-03T00:00:00Z"),
])
def test_rejects_detectable_v1_schema_halt_flag_and_cursor_corruption(tmp_path, mutation):
    store = _old_store(tmp_path / "corrupt.sqlite3", mutate=mutation)
    with pytest.raises(StoreCorruptionError):
        _validate_health_and_risk_chain(store.replay_state())
    store.close()


def test_rejects_breaker_projection_version_disagreeing_with_latest_evidence(tmp_path):
    store = _old_store(tmp_path / "projection.sqlite3")
    snapshot = store.replay_state()
    forged = replace(snapshot, breaker_state=dict(snapshot.breaker_state) | {"version": 2})
    with pytest.raises(StoreCorruptionError, match="projection"):
        _validate_health_and_risk_chain(forged)
    store.close()


@pytest.mark.parametrize("overlay_version", [1, 2])
def test_rejects_detectable_health_followup_decision_tampering(tmp_path, overlay_version):
    store = _old_store(tmp_path / "health.sqlite3")
    at = START + timedelta(hours=240)
    monitor = HealthMonitor.from_store(store)
    for seconds in (1, 2, 3):
        monitor.record_api_failure(at + timedelta(seconds=seconds))
    monitor.persist(store, event_id="health:halt", logical_at=at + timedelta(seconds=3))
    # Same historical bar: only the health overlay changes, never the old
    # canonical decision. New evidence still writes the current payload version.
    assert _decide(store, at).reasons == ("system_unhealthy",)
    snapshot = store.replay_state()
    assert snapshot.breaker_state["version"] == 2
    overlay = snapshot.event_evidence[-1]
    historical_payload = dict(overlay.payload) | {"version": overlay_version}
    historical = replace(snapshot,
                         event_evidence=(*snapshot.event_evidence[:-1], replace(overlay, payload=historical_payload)),
                         breaker_state=historical_payload)
    assert _validate_health_and_risk_chain(historical).projection.decision.reasons == ("system_unhealthy",)
    payload = historical_payload | {"decision_exposure_cap": .15}
    forged = replace(snapshot, event_evidence=(*snapshot.event_evidence[:-1], replace(overlay, payload=payload)),
                     breaker_state=payload)
    with pytest.raises(StoreCorruptionError, match="followup decision"):
        _validate_health_and_risk_chain(forged)
    store.close()


def test_empty_breaker_event_is_reported_as_store_corruption(tmp_path):
    store = _old_store(tmp_path / "empty.sqlite3")
    at = "2026-01-11T04:00:00Z"
    store.append_event(f"risk:{at}", "BREAKER_STATE", at, {})
    with pytest.raises(StoreCorruptionError):
        _validate_health_and_risk_chain(store.replay_state())
    store.close()


def test_real_backtest_reenters_at_expiry_without_recurring_recovery_exit():
    from test_backtest_risk_time import _entry_signal, _scenario_frame

    rows = [{} for _ in range(25)]
    rows[0] = _entry_signal()
    rows[1] = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    rows[2] = {"open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0}
    # The gap-stop loss leaves 80 equity against its unchanged historical peak.
    # A signal one bar before expiry must be blocked; exact 72h may re-enter.
    rows[19] = _entry_signal()
    rows[20] = _entry_signal()
    for index in range(21, 25):
        rows[index] = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    frame = _scenario_frame(rows)
    result = run_backtest(frame, BacktestConfig(costs=CostConfig(0, 0)))
    entries = [order for order in result.orders if order.side == "BUY" and order.status == "COMPLETED"]
    assert len(entries) == 2
    assert entries[-1].signal_time == frame.index[630]
    assert entries[-1].fill_time == frame.index[631]
    assert not any(order.reason == "RISK_EXIT" for order in result.orders)
    assert result.equity_curve[-1].equity < 85.0

from datetime import timedelta
import json
import sqlite3

import pytest

from autobit.config import CostConfig
from autobit.domain.models import OrderStatus
from autobit.execution.paper_broker import PaperBroker, PaperReconciliationError
from autobit.paper.service import PaperService
from autobit.persistence.sqlite_store import SQLiteStore
from autobit.persistence.sqlite_store import StoreCorruptionError
from test_paper_cli import END, _Clock, _FrameSource, _history
from test_paper_auto_recovery import _reduced_monitor


def _refresh_fixture_snapshots(path):
    """Build generic prefix snapshots; broker/service semantic checks stay real."""
    from autobit.persistence.sqlite_store import _canonical_json_value

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        replay = SQLiteStore(path)._rebuild_from_events(connection)
        connection.execute("DELETE FROM snapshots")
        connection.executemany(
            "INSERT INTO snapshots(sequence,state_json,created_at_utc) VALUES(?,?,?)",
            [(index, _canonical_json_value(mapping), timestamp)
             for index, (mapping, timestamp) in enumerate(zip(replay.snapshot_mappings, replay.snapshot_timestamps, strict=True))],
        )


def _signal(store, *, broker=None, reduced=False, costs=CostConfig(0, 0)):
    if reduced:
        _reduced_monitor().persist(store, event_id="health:reduced-fixture", logical_at=END - timedelta(hours=4))
    frame = _history(END)
    frame.loc[:, ["high", "low"]] = [100.1, 99.9]
    frame.iloc[-1] = [100, 100.3, 99.9, 100.2, 1]
    broker = broker or PaperBroker(store, costs)
    service = PaperService(source=_FrameSource([frame]), store=store, broker=broker,
                           clock=_Clock(END + timedelta(minutes=10)), costs=costs,
                           lease_owner="followup", lease_token="followup-signal")
    service.process_completed_candle(END)
    return broker.reconcile().active_orders[0], frame


def test_orphan_execution_evidence_is_rejected_before_broker_mutation(tmp_path):
    store = SQLiteStore(tmp_path / "orphan.sqlite3")
    store.initialize()
    store.append_event("forged-execution", "PAPER_EXECUTION", END,
                       {"version": 1, "order_id": "missing", "reference_price": 110.0,
                        "attempted_quantity": .2})
    before = store.replay_state()
    with pytest.raises(PaperReconciliationError, match="execution"):
        PaperBroker(store).submit_entry(END, quantity=.1)
    assert store.replay_state() == before
    store.close()


def test_context_bound_execution_rolls_back_all_evidence_at_fill_crash(tmp_path):
    store = SQLiteStore(tmp_path / "atomic-execution.sqlite3")
    store.initialize()
    order, _ = _signal(store)
    before = store.replay_state()

    def crash(boundary):
        if boundary == "fill":
            raise RuntimeError("fill interrupted")

    with pytest.raises(RuntimeError, match="fill interrupted"):
        PaperBroker(store, fault_hook=crash).process_open(order.order_id, END, open_price=110)
    assert store.replay_state() == before
    fill = PaperBroker(store).process_open(order.order_id, END, open_price=110)
    assert fill.quantity == pytest.approx(70 / 110)
    assert len([event for event in store.replay_state().event_evidence if event.event_type == "PAPER_EXECUTION"]) == 1
    store.close()


def test_application_legacy_max_hold_backfill_failure_retries_and_exits_next_open(tmp_path):
    from autobit.cli import _PaperApplication
    from test_paper_cli import _Sleeper

    fill_at = END - timedelta(hours=4 * 1096)
    full = _history(END + timedelta(hours=4), 1700)
    full.loc[fill_at, "high"] = 106

    class Source:
        backfills = 0

        def load_completed_candles(self, end):
            return full.loc[full.index < end].tail(601).copy()

        def load_position_candles(self, start, end):
            self.backfills += 1
            if self.backfills == 1:
                raise RuntimeError("historical public outage")
            assert (start, end) == (fill_at, END)
            return full.loc[(full.index >= start) & (full.index < end)].copy()

    path = tmp_path / "legacy-app-max.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0, 0))
    order = broker.submit_entry(fill_at - timedelta(hours=4), quantity=.2)
    broker.process_open(order.order_id, fill_at, open_price=100)
    broker.set_stop(fill_at, 95, active_after=fill_at, source_id=order.order_id)
    # Validated terminal-cycle cursor for the already-held legacy state, not a
    # claim to simulate1095 earlier strategy decisions in this fixture.
    store.close()
    with sqlite3.connect(path) as connection:
        for index in range(1096):
            end_text = (fill_at + timedelta(hours=4 * index)).isoformat().replace("+00:00", "Z")
            payload = {"created_order_ids": [], "filled_order_ids": [], "reason_codes": [],
                       "end_utc": end_text, "status": "PROCESSED"}
            connection.execute(
                "INSERT INTO events(event_id,event_type,occurred_at_utc,payload_json) VALUES(?,?,?,?)",
                (f"cycle:{end_text}", "PAPER_CYCLE", end_text,
                 json.dumps(payload, sort_keys=True, separators=(",", ":"))),
            )
    _refresh_fixture_snapshots(path)
    store = SQLiteStore(path)
    store.initialize()
    source = Source()
    clock = _Clock(END + timedelta(minutes=10))

    def app():
        return _PaperApplication(source=source, store=store, clock=clock, sleeper=_Sleeper(clock),
                                 notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-app", lease_token="legacy-app")

    with pytest.raises(RuntimeError, match="historical public outage"):
        app().run_once()
    assert PaperBroker(store).reconcile().btc_quantity == .2
    assert PaperBroker(store).active_stop().stop_price == 95
    store.close()
    store = SQLiteStore(path)
    store.initialize()
    app().run_once()
    exit_order, = PaperBroker(store).reconcile().active_orders
    assert exit_order.reason == "MAX_HOLD_EXIT"
    clock.value = END + timedelta(hours=4, minutes=10)
    app().run_once()
    trade, = PaperBroker(store).reconcile().completed_trades
    assert trade.exit_time == END
    assert trade.exit_reason == "MAX_HOLD_EXIT"
    assert source.backfills == 2
    store.close()


@pytest.mark.parametrize("fraction", [.25, 1.0])
@pytest.mark.parametrize("reduced", [False, True])
def test_bound_execution_records_actual_open_partial_and_reduced_cap(tmp_path, fraction, reduced):
    store = SQLiteStore(tmp_path / "execution.sqlite3")
    store.initialize()
    costs = CostConfig(.0005, .0005)
    order, _ = _signal(store, costs=costs, reduced=reduced)
    broker = PaperBroker(store, costs)
    attempted = order.requested_quantity * fraction
    fill = broker.process_open(order.order_id, END, open_price=110, actual_quantity=attempted)
    event, = [event for event in store.replay_state().event_evidence if event.event_type == "PAPER_EXECUTION"]
    assert event.payload["attempted_quantity"] == attempted
    assert event.payload["reference_price"] == 110
    assert fill.quantity == pytest.approx(min(attempted, (35 if reduced else 70) / 110.055))
    assert order.execution_context["risk_rate"] == (.01 if reduced else .02)
    assert order.execution_context["exposure_cap"] == (.35 if reduced else .70)
    assert broker.order(order.order_id).status is OrderStatus.CANCELED
    state = broker.reconcile()
    store.close()
    store = SQLiteStore(tmp_path / "execution.sqlite3")
    store.initialize()
    assert PaperBroker(store).reconcile() == state
    assert PaperBroker(store).process_open(order.order_id, END, open_price=110) is None
    store.close()


def test_zero_execution_cap_has_price_evidence_and_no_fill(tmp_path):
    store = SQLiteStore(tmp_path / "zero.sqlite3")
    store.initialize()
    order, _ = _signal(store)
    broker = PaperBroker(store)
    assert broker.process_open(order.order_id, END, open_price=.1) is None
    assert broker.order(order.order_id).status is OrderStatus.REJECTED
    event, = [event for event in store.replay_state().event_evidence if event.event_type == "PAPER_EXECUTION"]
    assert event.payload["reference_price"] == .1
    assert broker.reconcile().cash == 100
    assert broker.reconcile().fills == ()
    store.close()


def test_legacy_pending_service_entry_is_bound_before_actual_open(tmp_path):
    from autobit.cli import _PaperApplication
    from autobit.paper.health import HealthMonitor
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    path = tmp_path / "legacy-pending.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(store, broker=LegacyServiceBroker(store, CostConfig(0, 0)))
    assert order.execution_context is None
    store.close()
    store = SQLiteStore(path)
    store.initialize()
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))
    app = _PaperApplication(source=_FrameSource([next_frame.tail(601), signal_frame]), store=store,
                            clock=clock, sleeper=_Sleeper(clock), notifier=None, costs=CostConfig(0, 0),
                            lease_owner="followup", lease_token="legacy-fill")
    app.run_once()
    broker = PaperBroker(store)
    fill, = broker.reconcile().fills
    assert fill.quantity == pytest.approx(70 / 110)
    assert broker.order(order.order_id).execution_context is not None
    assert HealthMonitor.from_store(store).snapshot().api_successes == 1
    store.close()


def test_legacy_pending_history_api_failure_survives_return_and_reopen(tmp_path):
    """A failed original-signal lookup must not roll back durable health evidence."""
    from autobit.cli import _PaperApplication
    from autobit.paper.health import HealthMonitor
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    path = tmp_path / "legacy-history-api.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(
        store, broker=LegacyServiceBroker(store, CostConfig(0, 0)),
    )
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))

    class Source:
        def load_completed_candles(self, end):
            if end == END + timedelta(hours=4):
                clock.value += timedelta(seconds=1)
                return next_frame.tail(601).copy()
            assert end == END
            clock.value += timedelta(seconds=5)
            raise RuntimeError("legacy signal public outage")

    with pytest.raises(RuntimeError, match="legacy signal public outage"):
        _PaperApplication(
            source=Source(), store=store, clock=clock, sleeper=_Sleeper(clock),
            notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-history-api",
            lease_token="legacy-history-api",
        ).run_once()

    health = HealthMonitor.from_store(store).snapshot()
    assert health.api_failures == 1
    assert health.api_successes == 0
    assert health.last_failure_at_utc == END + timedelta(hours=4, minutes=10, seconds=1)
    state = PaperBroker(store).reconcile()
    assert state.active_orders == (order,)
    assert state.fills == ()
    assert PaperBroker(store).order(order.order_id).execution_context is None
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    reopened = HealthMonitor.from_store(store).snapshot()
    assert reopened.api_failures == 1
    assert reopened.last_failure_at_utc == health.last_failure_at_utc
    assert PaperBroker(store).reconcile().active_orders == (order,)
    assert PaperBroker(store).reconcile().fills == ()
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    clock.value += timedelta(hours=4)
    with pytest.raises(RuntimeError, match="legacy signal public outage"):
        _PaperApplication(
            source=Source(), store=store, clock=clock, sleeper=_Sleeper(clock),
            notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-history-api",
            lease_token="legacy-history-api-retry",
        ).run_once()
    repeated = HealthMonitor.from_store(store).snapshot()
    health_events = [
        event for event in store.replay_state().event_evidence
        if event.event_type == "HEALTH_STATE"
    ]
    failures = [event for event in health_events if event.payload["api_failures"] == 1]
    assert repeated.api_failures == 1
    assert repeated.last_failure_at_utc > health.last_failure_at_utc
    assert [event.payload["api_successes"] for event in health_events] == [1, 0, 1, 0]
    assert [event.payload["api_failures"] for event in health_events] == [0, 1, 0, 1]
    assert len(failures) == 2
    assert failures[-1].payload["last_failure_at_utc"] > failures[-2].payload["last_failure_at_utc"]
    assert failures[-1].sequence > failures[-2].sequence
    assert PaperBroker(store).reconcile().active_orders == (order,)
    assert PaperBroker(store).reconcile().fills == ()
    assert PaperBroker(store).order(order.order_id).execution_context is None
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    reopened_again = HealthMonitor.from_store(store).snapshot()
    assert reopened_again.api_failures == 1
    assert reopened_again.last_failure_at_utc == repeated.last_failure_at_utc
    assert PaperBroker(store).reconcile().active_orders == (order,)
    assert PaperBroker(store).reconcile().fills == ()
    assert PaperBroker(store).order(order.order_id).execution_context is None
    store.close()


def test_legacy_pending_history_schema_failure_halts_durably(tmp_path):
    """Malformed original-signal evidence must remain HALTED after the failed fill."""
    from autobit.cli import _PaperApplication
    from autobit.paper.health import HealthMonitor, HealthStage
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    path = tmp_path / "legacy-history-schema.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(
        store, broker=LegacyServiceBroker(store, CostConfig(0, 0)),
    )
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))

    class Source:
        def load_completed_candles(self, end):
            if end == END + timedelta(hours=4):
                clock.value += timedelta(seconds=1)
                return next_frame.tail(601).copy()
            assert end == END
            return signal_frame.drop(columns=["high"])

    with pytest.raises(ValueError, match="schema"):
        _PaperApplication(
            source=Source(), store=store, clock=clock, sleeper=_Sleeper(clock),
            notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-history-schema",
            lease_token="legacy-history-schema",
        ).run_once()

    health = HealthMonitor.from_store(store).snapshot()
    assert health.stage is HealthStage.HALTED
    assert not health.schema_valid
    assert health.api_successes == 0
    assert PaperBroker(store).reconcile().active_orders == (order,)
    assert PaperBroker(store).reconcile().fills == ()
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    reopened = HealthMonitor.from_store(store).snapshot()
    assert reopened.stage is HealthStage.HALTED
    assert not reopened.schema_valid
    assert PaperBroker(store).reconcile().active_orders == (order,)
    assert PaperBroker(store).reconcile().fills == ()
    store.close()


def test_legacy_pending_history_retry_binds_one_capped_fill_after_reopen(tmp_path):
    """A durable failed lookup recovers through the real application path once."""
    from autobit.cli import _PaperApplication
    from autobit.paper.service import CycleStatus
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    path = tmp_path / "legacy-history-retry.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(
        store, broker=LegacyServiceBroker(store, CostConfig(0, 0)),
    )
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))

    class FailingSource:
        def load_completed_candles(self, end):
            if end == END + timedelta(hours=4):
                clock.value += timedelta(seconds=1)
                return next_frame.tail(601).copy()
            assert end == END
            clock.value += timedelta(seconds=5)
            raise RuntimeError("legacy signal public outage")

    with pytest.raises(RuntimeError, match="legacy signal public outage"):
        _PaperApplication(
            source=FailingSource(), store=store, clock=clock, sleeper=_Sleeper(clock),
            notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-history-retry",
            lease_token="legacy-history-retry-failed",
        ).run_once()
    assert PaperBroker(store).reconcile().active_orders == (order,)
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    clock.value = END + timedelta(hours=4, minutes=20)
    recovered = _PaperApplication(
        source=_FrameSource([next_frame.tail(601), signal_frame]), store=store,
        clock=clock, sleeper=_Sleeper(clock), notifier=None, costs=CostConfig(0, 0),
        lease_owner="legacy-history-retry", lease_token="legacy-history-retry-success",
    )
    assert recovered.run_once().status is CycleStatus.PROCESSED
    reconciliation = PaperBroker(store).reconcile()
    fill, = reconciliation.fills
    assert fill.order_id == order.order_id
    assert fill.quantity == pytest.approx(70 / 110)
    assert fill.quantity * fill.fill_price <= 70
    assert PaperBroker(store).order(order.order_id).execution_context is not None
    assert len(reconciliation.fills) == 1
    expected = store.replay_state()
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    assert store.replay_state() == expected
    assert PaperBroker(store).reconcile().fills == (fill,)
    store.close()


def test_legacy_history_failure_alert_has_durable_exactly_once_lineage(tmp_path):
    """Delivered health alerts retain their source and one durable attempt."""
    from autobit.cli import _PaperApplication
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    class CapturingNotifier:
        def __init__(self):
            self.delivered: list[dict[str, object]] = []

        def send(self, event):
            self.delivered.append(dict(event))
            return True

    path = tmp_path / "legacy-history-alert.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    _, signal_frame = _signal(
        store, broker=LegacyServiceBroker(store, CostConfig(0, 0)),
    )
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))

    class Source:
        def load_completed_candles(self, end):
            if end == END + timedelta(hours=4):
                clock.value += timedelta(seconds=1)
                return next_frame.tail(601).copy()
            assert end == END
            clock.value += timedelta(seconds=5)
            raise RuntimeError("legacy signal public outage")

    notifier = CapturingNotifier()
    app = _PaperApplication(
        source=Source(), store=store, clock=clock, sleeper=_Sleeper(clock),
        notifier=notifier, costs=CostConfig(0, 0), lease_owner="legacy-history-alert",
        lease_token="legacy-history-alert",
    )
    with pytest.raises(RuntimeError, match="legacy signal public outage"):
        app.run_once()

    evidence = store.replay_state().event_evidence
    failed_health, = [
        event for event in evidence
        if event.event_type == "HEALTH_STATE" and event.payload["api_failures"] == 1
    ]
    attempts = [event for event in evidence if event.event_type == "ALERT_ATTEMPT"]
    assert any(event.payload["source_event_id"] == failed_health.event_id for event in attempts)
    assert {event["event_id"] for event in notifier.delivered} == {
        event.payload["source_event_id"] for event in attempts
    }
    delivered_before_retry = tuple(notifier.delivered)
    app._source._notify(failed_health)
    assert tuple(notifier.delivered) == delivered_before_retry
    assert len([event for event in store.replay_state().event_evidence
                if event.event_type == "ALERT_ATTEMPT"
                and event.payload["source_event_id"] == failed_health.event_id]) == 1
    expected = store.replay_state()
    store.close()

    store = SQLiteStore(path)
    store.initialize()
    assert store.replay_state() == expected
    assert any(
        event.event_type == "ALERT_ATTEMPT"
        and event.payload["source_event_id"] == failed_health.event_id
        for event in store.replay_state().event_evidence
    )
    store.close()


def test_slow_legacy_context_lookup_revalidates_lost_cycle_lease(tmp_path):
    """A lease takeover during historical preparation blocks the trading mutation."""
    from autobit.cli import _PaperApplication
    from autobit.paper.service import CycleStatus
    from test_paper_cli import _Sleeper

    class LegacyServiceBroker(PaperBroker):
        def submit_entry(self, signal_at, *, quantity, reason="ENTRY", execution_context=None):
            return super().submit_entry(signal_at, quantity=quantity, reason=reason)

    path = tmp_path / "legacy-history-lease.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(
        store, broker=LegacyServiceBroker(store, CostConfig(0, 0)),
    )
    next_frame = _history(END + timedelta(hours=4), 602)
    next_frame.iloc[:601] = signal_frame.to_numpy()
    next_frame.iloc[601] = [110, 111, 109.9, 110, 1]
    clock = _Clock(END + timedelta(hours=4, minutes=10))
    competitor: SQLiteStore | None = None

    class Source:
        def load_completed_candles(self, end):
            nonlocal competitor
            if end == END + timedelta(hours=4):
                return next_frame.tail(601).copy()
            assert end == END
            clock.value += timedelta(minutes=6)
            competitor = SQLiteStore(path)
            competitor.initialize()
            assert competitor.acquire_cycle_lease(
                "competing-worker", "competing-token", clock.value,
                clock.value + timedelta(minutes=5),
            )
            return signal_frame.copy()

    result = _PaperApplication(
        source=Source(), store=store, clock=clock, sleeper=_Sleeper(clock),
        notifier=None, costs=CostConfig(0, 0), lease_owner="legacy-history-lease",
        lease_token="legacy-history-lease",
    ).run_once()
    assert result.status is CycleStatus.LEASE_HELD
    reconciliation = PaperBroker(store).reconcile()
    assert reconciliation.active_orders == (order,)
    assert reconciliation.fills == ()
    assert PaperBroker(store).order(order.order_id).execution_context is None
    assert competitor is not None
    competitor.close()
    store.close()


def test_old_high_water_survives_observation_crash_and_rolling_window(tmp_path):
    fill_at = END - timedelta(hours=4 * 701)
    full = _history(END + timedelta(hours=4), 1303)
    full.loc[:, ["open", "high", "low", "close"]] = [115, 116, 114, 115]
    full.loc[fill_at] = [100, 120, 99, 115, 1]
    full.loc[END] = [115, 116, 113, 115, 1]

    class Source:
        calls = 0

        def load_completed_candles(self, end):
            from autobit.cli import _validated_paper_frame
            return _validated_paper_frame(full.loc[full.index < end].tail(601), end)

        def load_position_candles(self, start, end):
            self.calls += 1
            assert self.calls == 1
            assert start == fill_at
            return full.loc[(full.index >= start) & (full.index < end)].copy()

    source = Source()
    path = tmp_path / "old-high.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0, 0))
    order = broker.submit_entry(fill_at - timedelta(hours=4), quantity=.2)
    broker.process_open(order.order_id, fill_at, open_price=100)
    broker.set_stop(fill_at, 95, active_after=fill_at, source_id=order.order_id)

    def crash(boundary):
        if boundary == "after_position_observation":
            raise RuntimeError("observation committed")

    def cycle(end, hook=None):
        return PaperService(source=source, store=store, broker=PaperBroker(store, CostConfig(0, 0)),
                            clock=_Clock(end + timedelta(minutes=10)), costs=CostConfig(0, 0),
                            lease_owner="old-high", lease_token=f"old-high-{end}", fault_hook=hook).process_completed_candle(end)

    with pytest.raises(RuntimeError, match="observation committed"):
        cycle(END, crash)
    observation = broker.position_observation(order.order_id)
    assert observation.payload["initial_stop"] == 95
    assert observation.payload["high_water"] == 120
    store.close()
    store = SQLiteStore(path)
    store.initialize()
    cycle(END)
    assert PaperBroker(store).active_stop().stop_price == pytest.approx(114)
    assert PaperBroker(store).active_stop().active_after_utc == END
    cycle(END + timedelta(hours=4))
    trade, = PaperBroker(store).reconcile().completed_trades
    assert trade.exit_price == 114
    assert trade.exit_reason == "TRAILING_STOP"
    assert source.calls == 1
    store.close()


@pytest.mark.parametrize("kind,field,value", [
    ("PAPER_ENTRY_CONTEXT", "risk_event_id", "missing-risk"),
    ("PAPER_ENTRY_CONTEXT", "risk_rate", .03),
    ("PAPER_EXECUTION", "reference_price", 111),
    ("PAPER_EXECUTION", "attempted_quantity", 20),
    ("PAPER_EXECUTION", "version", True),
    ("PAPER_EXECUTION", "order_id", "missing-order"),
])
def test_execution_evidence_corruption_blocks_mutations(tmp_path, kind, field, value):
    path = tmp_path / "corruption.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, _ = _signal(store)
    PaperBroker(store).process_open(order.order_id, END, open_price=110)
    store.close()
    with sqlite3.connect(path) as connection:
        event_id, encoded = connection.execute("SELECT event_id, payload_json FROM events WHERE event_type=?", (kind,)).fetchone()
        payload = json.loads(encoded)
        if kind == "PAPER_ENTRY_CONTEXT":
            payload["context"][field] = value
        else:
            payload[field] = value
        connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), event_id))
    _refresh_fixture_snapshots(path)
    store = SQLiteStore(path)
    store.initialize()
    before = store.replay_state()
    with pytest.raises((PaperReconciliationError, StoreCorruptionError)):
        PaperBroker(store).submit_exit(END, quantity=.1, owned_quantity=before.btc_quantity, reason="CLOSE_EXIT")
    assert store.replay_state() == before
    store.close()


@pytest.mark.parametrize("field,value", [
    ("initial_stop", 100.7), ("high_water", 999),
    ("previous_event_id", "missing-observation"), ("fill_id", "missing-fill"),
    ("candle_highs", [["2026-05-11T08:00:00Z", 101]]),
])
def test_position_observation_corruption_blocks_mutations(tmp_path, field, value):
    path = tmp_path / "position-corruption.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    order, signal_frame = _signal(store)
    frame = _history(END + timedelta(hours=4), 602)
    frame.iloc[:601] = signal_frame.to_numpy()
    frame.iloc[-1] = [100.2, 101, 100, 100.2, 1]
    PaperService(source=_FrameSource([frame.tail(601)]), store=store,
                 broker=PaperBroker(store, CostConfig(0, 0)),
                 clock=_Clock(END + timedelta(hours=4, minutes=10)), costs=CostConfig(0, 0),
                 lease_owner="position-corruption", lease_token="position-corruption").process_completed_candle(END + timedelta(hours=4))
    assert PaperBroker(store).position_observation(order.order_id) is not None
    store.close()
    with sqlite3.connect(path) as connection:
        event_id, encoded = connection.execute("SELECT event_id, payload_json FROM events WHERE event_type='PAPER_POSITION'").fetchone()
        payload = json.loads(encoded)
        payload[field] = value
        connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), event_id))
    _refresh_fixture_snapshots(path)
    store = SQLiteStore(path)
    store.initialize()
    before = store.replay_state()
    with pytest.raises(PaperReconciliationError, match="position"):
        PaperBroker(store).set_stop(END + timedelta(hours=4), 100)
    assert store.replay_state() == before
    store.close()

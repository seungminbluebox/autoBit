from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pandas as pd
import pytest

from autobit.config import CostConfig, StrategyConfig
from autobit.domain.models import PositionState
from autobit.execution.paper_broker import PaperBroker
from autobit.paper.scheduler import PaperScheduler
from autobit.paper.service import CycleStatus, PaperService
from autobit.persistence.sqlite_store import SQLiteStore


UTC = timezone.utc
END = datetime(2026, 5, 11, 4, 0, tzinfo=UTC)


@dataclass
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class _MutableClock:
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


class _BlockingSource(_Source):
    def __init__(self, frame: pd.DataFrame, entered: Event, release: Event) -> None:
        super().__init__(frame)
        self._entered = entered
        self._release = release

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        self._entered.set()
        assert self._release.wait(timeout=5)
        return super().load_completed_candles(end_utc)


class _ClockAdvancingSleeper:
    def __init__(self, clock: _MutableClock) -> None:
        self._clock = clock
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self._clock.value += timedelta(seconds=seconds)


def _history(end_utc: datetime, bars: int = 601) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp(end_utc) - pd.Timedelta(hours=4),
        periods=bars,
        freq="4h",
        tz="UTC",
    )
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )


def _breakout_history(end_utc: datetime, bars: int = 601) -> pd.DataFrame:
    frame = _history(end_utc, bars)
    frame.iloc[-1, frame.columns.get_loc("close")] = 102.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 103.0
    return frame


def _service(
    path: Path,
    source: _Source,
    *,
    store: SQLiteStore | None = None,
    broker: PaperBroker | None = None,
    clock_at: datetime | None = None,
    owner: str = "worker-a",
    token: str = "cycle-token-a",
    strategy: StrategyConfig = StrategyConfig(),
    fault_hook=None,
) -> tuple[SQLiteStore, PaperService]:
    if store is None:
        store = SQLiteStore(path)
        store.initialize()
    if broker is None:
        broker = PaperBroker(store, CostConfig(fee_rate=0.0, slippage_rate=0.0))
    service = PaperService(
        source=source,
        store=store,
        broker=broker,
        clock=_FrozenClock(clock_at or END + timedelta(minutes=10)),
        lease_owner=owner,
        lease_token=token,
        costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
        strategy_config=strategy,
        fault_hook=fault_hook,
    )
    return store, service


def test_completed_candle_is_processed_once_without_refetch_or_ledger_growth(
    tmp_path: Path,
) -> None:
    source = _Source(_history(END))
    store, service = _service(tmp_path / "paper.sqlite3", source)

    first = service.process_completed_candle(END)
    after_first = store.replay_state()
    second = service.process_completed_candle(END)
    after_second = store.replay_state()

    assert first.status is CycleStatus.PROCESSED
    assert second.status is CycleStatus.ALREADY_PROCESSED
    assert source.calls == [END]
    assert after_second.last_sequence == after_first.last_sequence
    cycle_events = [
        event
        for event in after_second.event_evidence
        if event.event_id == "cycle:2026-05-11T04:00:00Z"
    ]
    assert len(cycle_events) == 1
    assert cycle_events[0].event_type == "PAPER_CYCLE"


def test_brand_new_service_starts_at_the_latest_matured_end(tmp_path: Path) -> None:
    latest = END + timedelta(hours=8)
    source = _Source(_history(latest))
    _, service = _service(tmp_path / "paper.sqlite3", source)

    assert service.oldest_required_end(latest) == latest
    assert source.calls == []


@pytest.mark.parametrize(
    "end_utc",
    [
        datetime(2026, 5, 11, 4, 0),
        datetime(2026, 5, 11, 5, 0, tzinfo=UTC),
        datetime(2026, 5, 11, 13, 0, tzinfo=timezone(timedelta(hours=9))),
    ],
)
def test_invalid_service_boundary_fails_before_data_or_ledger_mutation(
    tmp_path: Path,
    end_utc: datetime,
) -> None:
    source = _Source(_history(END))
    store, service = _service(tmp_path / "paper.sqlite3", source)

    with pytest.raises(ValueError, match="UTC|four-hour"):
        service.process_completed_candle(end_utc)

    assert source.calls == []
    assert store.replay_state().last_sequence == 0


def test_unmatured_completed_end_is_rejected_before_source_access(tmp_path: Path) -> None:
    source = _Source(_history(END))
    store, service = _service(
        tmp_path / "paper.sqlite3",
        source,
        clock_at=END + timedelta(minutes=9, seconds=59),
    )

    with pytest.raises(ValueError, match="ten minutes"):
        service.process_completed_candle(END)

    assert source.calls == []
    assert store.replay_state().last_sequence == 0


def test_source_failure_releases_lease_and_does_not_mark_the_cycle_complete(
    tmp_path: Path,
) -> None:
    class FailingSource:
        def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
            del end_utc
            raise RuntimeError("public data unavailable")

    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    service = PaperService(
        source=FailingSource(),
        store=store,
        broker=broker,
        clock=_FrozenClock(END + timedelta(minutes=10)),
        lease_owner="worker-a",
        lease_token="token-a",
        costs=CostConfig(0.0, 0.0),
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        service.process_completed_candle(END)

    assert store.replay_state().last_sequence == 0
    competitor = SQLiteStore(path)
    competitor.initialize()
    now = END + timedelta(minutes=10)
    assert competitor.acquire_cycle_lease(
        "worker-b",
        "token-b",
        now,
        now + timedelta(minutes=5),
    )


def test_rows_at_or_after_exclusive_end_cannot_create_a_signal(tmp_path: Path) -> None:
    frame = _history(END)
    future = pd.DataFrame(
        {"open": [100.0], "high": [150.0], "low": [99.0], "close": [149.0], "volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp(END)]),
    )
    source = _Source(pd.concat([frame, future]))
    store, service = _service(tmp_path / "paper.sqlite3", source)

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.PROCESSED
    assert store.replay_state().pending_orders == ()


@pytest.mark.parametrize("unsafe_kind", ["missing", "quarantined", "filled"])
def test_unsafe_exact_latest_bar_completes_fail_closed_without_an_order(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    frame = _breakout_history(END)
    if unsafe_kind == "missing":
        frame = frame.iloc[:-1]
    elif unsafe_kind == "quarantined":
        frame.iloc[-1, frame.columns.get_loc("open")] = -1.0
    else:
        frame = frame.drop(frame.index[-1])
    source = _Source(frame)
    store, service = _service(tmp_path / f"{unsafe_kind}.sqlite3", source)

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.UNSAFE_DATA
    assert service.process_completed_candle(END).status is CycleStatus.ALREADY_PROCESSED
    snapshot = store.replay_state()
    assert snapshot.pending_orders == ()
    event, = [item for item in snapshot.event_evidence if item.event_type == "PAPER_CYCLE"]
    assert event.payload["status"] == "UNSAFE_DATA"


def test_unsafe_latest_with_pending_execution_is_retryable_and_not_completed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    missing = _history(END).iloc[:-1]
    source = _Source(missing)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    order = broker.submit_entry(bar_at - timedelta(hours=4), quantity=0.2)
    _, service = _service(path, source, store=store, broker=broker)

    with pytest.raises(Exception, match="unsafe.*obligation"):
        service.process_completed_candle(END)

    evidence = store.replay_state().event_evidence
    assert not any(event.event_type == "PAPER_CYCLE" for event in evidence)
    assert broker.reconcile().active_orders == (order,)

    source.frame = _history(END)
    result = service.process_completed_candle(END)
    assert result.status is CycleStatus.PROCESSED
    assert broker.reconcile().fills[0].fill_time == bar_at


def test_unexpired_foreign_lease_returns_without_fetch_or_mutation(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_history(END))
    store, service = _service(path, source)
    other = SQLiteStore(path)
    other.initialize()
    now = END + timedelta(minutes=10)
    assert other.acquire_cycle_lease("other", "other-token", now, now + timedelta(minutes=5))

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.LEASE_HELD
    assert source.calls == []
    assert store.replay_state().last_sequence == 0


def test_expired_foreign_lease_is_recovered_automatically(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_history(END))
    store, service = _service(path, source)
    now = END + timedelta(minutes=10)
    assert store.acquire_cycle_lease(
        "old-worker",
        "old-token",
        now - timedelta(minutes=10),
        now - timedelta(minutes=5),
    )

    result = service.process_completed_candle(END)

    assert result.status is CycleStatus.PROCESSED
    assert source.calls == [END]


def test_concurrent_service_instances_allow_only_the_lease_holder_to_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    entered = Event()
    release = Event()
    first_source = _BlockingSource(_history(END), entered, release)
    second_source = _Source(_history(END))
    first_store, first_service = _service(path, first_source, owner="a", token="a-token")
    second_store = SQLiteStore(path)
    second_store.initialize()
    _, second_service = _service(
        path,
        second_source,
        store=second_store,
        owner="b",
        token="b-token",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first_service.process_completed_candle, END)
        assert entered.wait(timeout=5)
        second_result = second_service.process_completed_candle(END)
        release.set()
        first_result = future.result(timeout=5)

    assert first_result.status is CycleStatus.PROCESSED
    assert second_result.status is CycleStatus.LEASE_HELD
    assert second_source.calls == []
    cycle_events = [
        event
        for event in first_store.replay_state().event_evidence
        if event.event_type == "PAPER_CYCLE"
    ]
    assert len(cycle_events) == 1


def test_service_rechecks_completion_after_an_expired_lease_was_taken_over(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    first_store = SQLiteStore(path)
    first_store.initialize()
    second_store = SQLiteStore(path)
    second_store.initialize()
    clock = _MutableClock(END + timedelta(minutes=10))
    second_source = _Source(_history(END))
    second = PaperService(
        source=second_source,
        store=second_store,
        broker=PaperBroker(second_store, CostConfig(0.0, 0.0)),
        clock=clock,
        lease_owner="worker-b",
        lease_token="token-b",
        costs=CostConfig(0.0, 0.0),
        lease_ttl=timedelta(minutes=1),
    )

    class TakeoverSource:
        def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
            clock.value = END + timedelta(minutes=12)
            assert second.process_completed_candle(end_utc).status is CycleStatus.PROCESSED
            return _history(end_utc)

    first = PaperService(
        source=TakeoverSource(),
        store=first_store,
        broker=PaperBroker(first_store, CostConfig(0.0, 0.0)),
        clock=clock,
        lease_owner="worker-a",
        lease_token="token-a",
        costs=CostConfig(0.0, 0.0),
        lease_ttl=timedelta(minutes=1),
    )

    result = first.process_completed_candle(END)

    assert result.status is CycleStatus.ALREADY_PROCESSED
    events = first_store.replay_state().event_evidence
    assert len([event for event in events if event.event_type == "PAPER_CYCLE"]) == 1
    assert len([event for event in events if event.event_type == "BREAKER_STATE"]) == 1


def test_pending_entry_fills_only_at_current_open_and_gets_next_bar_protection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_history(END))
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    order = broker.submit_entry(bar_at - timedelta(hours=4), quantity=0.2)
    _, service = _service(path, source, store=store, broker=broker)

    result = service.process_completed_candle(END)
    reconciliation = broker.reconcile()

    assert result.status is CycleStatus.PROCESSED
    fill, = reconciliation.fills
    assert fill.order_id == order.order_id
    assert fill.fill_time == bar_at
    assert fill.reference_price == 100.0
    assert reconciliation.position_state is PositionState.LONG
    assert reconciliation.active_stop is not None
    assert reconciliation.active_stop.observed_at_utc == bar_at
    assert reconciliation.active_stop.active_after_utc == END


def test_newly_persisted_stop_does_not_trigger_on_the_entry_fill_bar(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("low")] = 90.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    broker.submit_entry(bar_at - timedelta(hours=4), quantity=0.2)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    assert reconciliation.position_state is PositionState.LONG
    assert len(reconciliation.fills) == 1
    assert reconciliation.active_stop is not None
    assert reconciliation.active_stop.active_after_utc == END


def test_gap_down_entry_persists_fill_price_based_stop_for_next_bar(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("open")] = 90.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 89.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    broker.submit_entry(bar_at - timedelta(hours=4), quantity=0.2)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    assert reconciliation.position_state is PositionState.LONG
    assert reconciliation.fills[0].fill_price == 90.0
    assert reconciliation.active_stop is not None
    assert reconciliation.active_stop.stop_price == pytest.approx(85.0)
    assert reconciliation.active_stop.stop_price < reconciliation.fills[0].fill_price
    assert reconciliation.active_stop.active_after_utc == END


def test_prior_stop_uses_current_gap_or_low_before_close_decisions(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("open")] = 94.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 100.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 90.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 99.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    broker.submit_entry(bar_at - timedelta(hours=12), quantity=0.2)
    broker.process_open(
        broker.reconcile().active_orders[0].order_id,
        bar_at - timedelta(hours=8),
        open_price=100.0,
    )
    broker.set_stop(bar_at - timedelta(hours=4), 95.0, reason="HARD_STOP")
    _, service = _service(path, source, store=store, broker=broker)

    result = service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    assert reconciliation.position_state is PositionState.FLAT
    assert reconciliation.fills[-1].fill_time == bar_at
    assert reconciliation.fills[-1].reference_price == 94.0
    assert reconciliation.completed_trades[-1].exit_reason == "HARD_STOP"
    assert result.filled_order_ids[-1] == reconciliation.fills[-1].order_id


def test_long_risk_uses_current_close_mtm_instead_of_last_fill_equity(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("open")] = 75.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 76.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 69.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 70.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    entry = broker.submit_entry(bar_at - timedelta(hours=8), quantity=0.7)
    broker.process_open(entry.order_id, bar_at - timedelta(hours=4), open_price=100.0)
    assert broker.reconcile().equity == 100.0
    _, service = _service(path, source, store=store, broker=broker)

    result = service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    exit_order, = reconciliation.active_orders
    assert exit_order.side == "SELL"
    assert exit_order.reason == "RISK_EXIT"
    assert result.equity == 79.0


def test_donchian_exit_wins_when_stagnant_condition_is_also_true(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("open")] = 100.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 101.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 97.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 98.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    fill_at = bar_at - timedelta(hours=4 * 60)
    entry = broker.submit_entry(fill_at - timedelta(hours=4), quantity=0.2)
    broker.process_open(entry.order_id, fill_at, open_price=100.0)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    exit_order, = broker.reconcile().active_orders
    assert exit_order.reason == "CLOSE_EXIT"


def test_stagnant_exit_does_not_fire_before_sixty_completed_held_bars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_history(END))
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    fill_at = bar_at - timedelta(hours=4 * 59)
    entry = broker.submit_entry(fill_at - timedelta(hours=4), quantity=0.2)
    broker.process_open(entry.order_id, fill_at, open_price=100.0)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    assert reconciliation.active_orders == ()
    assert reconciliation.active_stop is not None
    assert reconciliation.position_state is PositionState.LONG


def test_max_hold_exit_does_not_fire_before_1095_completed_held_bars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END, bars=1200)
    bar_at = END - timedelta(hours=4)
    fill_at = bar_at - timedelta(hours=4 * 1094)
    frame.loc[pd.Timestamp(fill_at), "high"] = 110.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    entry = broker.submit_entry(fill_at - timedelta(hours=4), quantity=0.2)
    broker.process_open(entry.order_id, fill_at, open_price=100.0)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    reconciliation = broker.reconcile()
    assert reconciliation.active_orders == ()
    assert reconciliation.active_stop is not None
    assert reconciliation.position_state is PositionState.LONG


def test_max_hold_exit_fires_at_exactly_1095_completed_held_bars(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END, bars=1200)
    bar_at = END - timedelta(hours=4)
    fill_at = bar_at - timedelta(hours=4 * 1095)
    frame.loc[pd.Timestamp(fill_at), "high"] = 110.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    entry = broker.submit_entry(fill_at - timedelta(hours=4), quantity=0.2)
    broker.process_open(entry.order_id, fill_at, open_price=100.0)
    _, service = _service(path, source, store=store, broker=broker)

    service.process_completed_candle(END)

    exit_order, = broker.reconcile().active_orders
    assert exit_order.reason == "MAX_HOLD_EXIT"


def test_exact_600_prior_valid_bars_are_required_before_entry(tmp_path: Path) -> None:
    not_ready_source = _Source(_breakout_history(END, bars=600))
    not_ready_store, not_ready = _service(tmp_path / "not-ready.sqlite3", not_ready_source)

    not_ready.process_completed_candle(END)

    assert not_ready_store.replay_state().pending_orders == ()

    ready_source = _Source(_breakout_history(END, bars=601))
    ready_store, ready = _service(
        tmp_path / "ready.sqlite3",
        ready_source,
        owner="worker-b",
        token="token-b",
    )

    ready.process_completed_candle(END)

    order, = PaperBroker(ready_store, CostConfig(0.0, 0.0)).reconcile().active_orders
    assert order.side == "BUY"
    assert order.signal_at_utc == END - timedelta(hours=4)
    assert order.eligible_open_utc == END
    assert 0.0 < order.requested_quantity < 1.0


def test_crash_after_acceptance_reopens_without_duplicate_and_fills_next_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_breakout_history(END))

    def crash_after_acceptance(boundary: str) -> None:
        if boundary == "after_order_acceptance":
            raise RuntimeError("injected post-acceptance crash")

    store, crashing = _service(path, source, fault_hook=crash_after_acceptance)
    with pytest.raises(RuntimeError, match="post-acceptance"):
        crashing.process_completed_candle(END)
    accepted, = PaperBroker(store, CostConfig(0.0, 0.0)).reconcile().active_orders
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened_source = _Source(_breakout_history(END))
    _, retry = _service(
        path,
        reopened_source,
        store=reopened_store,
        owner="worker-restarted",
        token="token-restarted",
    )

    retry_result = retry.process_completed_candle(END)
    after_retry = PaperBroker(reopened_store, CostConfig(0.0, 0.0)).reconcile()

    assert retry_result.status is CycleStatus.PROCESSED
    assert after_retry.active_orders == (accepted,)
    assert after_retry.fills == ()
    assert len([event for event in reopened_store.replay_state().event_evidence if event.event_type == "ORDER_CREATED"]) == 1
    assert len([event for event in reopened_store.replay_state().event_evidence if event.event_type == "PAPER_CYCLE"]) == 1
    assert len([event for event in reopened_store.replay_state().event_evidence if event.event_type == "BREAKER_STATE"]) == 1

    next_end = END + timedelta(hours=4)
    next_source = _Source(_history(next_end, bars=602))
    _, next_service = _service(
        path,
        next_source,
        store=reopened_store,
        clock_at=next_end + timedelta(minutes=10),
        owner="worker-next",
        token="token-next",
    )
    next_service.process_completed_candle(next_end)
    final = PaperBroker(reopened_store, CostConfig(0.0, 0.0)).reconcile()
    assert len(final.fills) == 1
    assert final.fills[0].order_id == accepted.order_id
    assert final.fills[0].fill_time == END
    assert len([event for event in reopened_store.replay_state().event_evidence if event.event_type == "ORDER_CREATED"]) == 1


def test_restart_reconstructs_high_water_and_raises_the_persisted_stop(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    bar_at = END - timedelta(hours=4)
    fill_at = bar_at - timedelta(hours=8)
    frame.loc[pd.Timestamp(fill_at), "high"] = 115.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    entry = broker.submit_entry(fill_at - timedelta(hours=4), quantity=0.2)
    broker.process_open(entry.order_id, fill_at, open_price=100.0)
    broker.set_stop(fill_at, 95.0, reason="HARD_STOP")
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened_broker = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    _, service = _service(
        path,
        source,
        store=reopened_store,
        broker=reopened_broker,
        owner="restarted",
        token="restart-token",
    )

    service.process_completed_candle(END)

    raised = reopened_broker.active_stop()
    assert raised is not None
    assert raised.reason == "TRAILING_STOP"
    assert raised.stop_price > 100.0
    assert raised.observed_at_utc == bar_at
    reopened_store.close()
    verified_store = SQLiteStore(path)
    verified_store.initialize()
    assert PaperBroker(verified_store, CostConfig(0.0, 0.0)).active_stop() == raised


def test_corrupt_risk_bookkeeping_fails_closed_without_completion_and_releases_lease(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    source = _Source(_history(END))
    store = SQLiteStore(path)
    store.initialize()
    store.append_event(
        "bad-risk",
        "BREAKER_STATE",
        END - timedelta(hours=8),
        {"version": True},
    )
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    _, service = _service(path, source, store=store, broker=broker)

    with pytest.raises(Exception, match="breaker state"):
        service.process_completed_candle(END)

    assert not any(
        event.event_type == "PAPER_CYCLE" for event in store.replay_state().event_evidence
    )
    competitor = SQLiteStore(path)
    competitor.initialize()
    now = END + timedelta(minutes=10)
    assert competitor.acquire_cycle_lease(
        "other-worker",
        "other-token",
        now,
        now + timedelta(minutes=5),
    )


def test_restarted_scheduler_replays_clean_pending_open_and_contiguous_backlog(
    tmp_path: Path,
) -> None:
    class RangeSource:
        def __init__(self) -> None:
            self.calls: list[datetime] = []

        def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
            self.calls.append(end_utc)
            return _history(end_utc, bars=602)

    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    previous_end = END - timedelta(hours=4)
    pending = broker.submit_entry(previous_end - timedelta(hours=4), quantity=0.2)
    previous_text = previous_end.isoformat().replace("+00:00", "Z")
    store.append_event(
        f"cycle:{previous_text}",
        "PAPER_CYCLE",
        previous_end,
        {
            "created_order_ids": [pending.order_id],
            "end_utc": previous_text,
            "filled_order_ids": [],
            "reason_codes": [],
            "status": "PROCESSED",
        },
    )
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened_broker = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    clock = _MutableClock(END + timedelta(hours=4, minutes=11))
    source = RangeSource()
    service = PaperService(
        source=source,
        store=reopened_store,
        broker=reopened_broker,
        clock=clock,
        lease_owner="restarted-worker",
        lease_token="restarted-token",
        costs=CostConfig(0.0, 0.0),
    )
    sleeper = _ClockAdvancingSleeper(clock)

    first = PaperScheduler(service, clock, sleeper).run_once()
    second = PaperScheduler(service, clock, sleeper).run_once()

    reconciliation = reopened_broker.reconcile()
    assert first.status is CycleStatus.PROCESSED
    assert first.end_utc == END
    assert second.end_utc == END + timedelta(hours=4)
    assert source.calls == [END, END + timedelta(hours=4)]
    assert sleeper.delays == []
    assert len(reconciliation.fills) == 1
    assert reconciliation.fills[0].order_id == pending.order_id
    assert reconciliation.fills[0].fill_time == previous_end
    assert reconciliation.active_stop is not None
    assert [
        event.occurred_at_utc
        for event in reopened_store.replay_state().event_evidence
        if event.event_type == "PAPER_CYCLE"
    ] == [previous_end, END, END + timedelta(hours=4)]


def test_crash_after_acceptance_restarts_oldest_cycle_then_exact_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"

    def crash_after_acceptance(boundary: str) -> None:
        if boundary == "after_order_acceptance":
            raise RuntimeError("injected post-acceptance crash")

    crashing_source = _Source(_breakout_history(END))
    store, crashing = _service(path, crashing_source, fault_hook=crash_after_acceptance)
    with pytest.raises(RuntimeError, match="post-acceptance"):
        crashing.process_completed_candle(END)
    accepted, = PaperBroker(store, CostConfig(0.0, 0.0)).reconcile().active_orders
    store.close()

    class RangeSource:
        def __init__(self) -> None:
            self.calls: list[datetime] = []

        def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
            self.calls.append(end_utc)
            if end_utc == END:
                return _breakout_history(end_utc, bars=602)
            return _history(end_utc, bars=602)

    reopened = SQLiteStore(path)
    reopened.initialize()
    broker = PaperBroker(reopened, CostConfig(0.0, 0.0))
    source = RangeSource()
    clock = _MutableClock(END + timedelta(hours=8, minutes=11))
    service = PaperService(
        source=source,
        store=reopened,
        broker=broker,
        clock=clock,
        lease_owner="restart-worker",
        lease_token="restart-token",
        costs=CostConfig(0.0, 0.0),
    )
    sleeper = _ClockAdvancingSleeper(clock)

    first = PaperScheduler(service, clock, sleeper).run_once()
    after_retry = broker.reconcile()
    second = PaperScheduler(service, clock, sleeper).run_once()
    third = PaperScheduler(service, clock, sleeper).run_once()

    final = broker.reconcile()
    assert [first.end_utc, second.end_utc, third.end_utc] == [
        END,
        END + timedelta(hours=4),
        END + timedelta(hours=8),
    ]
    assert source.calls == [END, END + timedelta(hours=4), END + timedelta(hours=8)]
    assert sleeper.delays == []
    assert after_retry.active_orders == (accepted,)
    assert after_retry.fills == ()
    assert len(final.fills) == 1
    assert final.fills[0].order_id == accepted.order_id
    assert final.fills[0].fill_time == END
    events = reopened.replay_state().event_evidence
    assert len([event for event in events if event.event_type == "ORDER_CREATED"]) == 1
    assert [
        event.occurred_at_utc for event in events if event.event_type == "PAPER_CYCLE"
    ] == [END, END + timedelta(hours=4), END + timedelta(hours=8)]


def test_entry_fill_is_protected_before_corrupt_risk_state_can_abort_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("low")] = 90.0
    source = _Source(frame)
    store = SQLiteStore(path)
    store.initialize()
    store.append_event(
        "bad-risk",
        "BREAKER_STATE",
        END - timedelta(hours=8),
        {"version": True},
    )
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    bar_at = END - timedelta(hours=4)
    pending = broker.submit_entry(bar_at - timedelta(hours=4), quantity=0.2)
    _, service = _service(path, source, store=store, broker=broker)

    with pytest.raises(Exception, match="breaker state"):
        service.process_completed_candle(END)

    after_failure = broker.reconcile()
    assert after_failure.position_state is PositionState.LONG
    assert len(after_failure.fills) == 1
    assert after_failure.fills[0].order_id == pending.order_id
    assert after_failure.active_stop is not None
    assert after_failure.active_stop.stop_price == pytest.approx(95.0)
    assert after_failure.active_stop.active_after_utc == END
    assert not any(
        event.event_type == "PAPER_CYCLE"
        for event in store.replay_state().event_evidence
    )
    first_stop = after_failure.active_stop
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened_broker = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    _, retry = _service(
        path,
        source,
        store=reopened_store,
        broker=reopened_broker,
        owner="retry-worker",
        token="retry-token",
    )

    with pytest.raises(Exception, match="breaker state"):
        retry.process_completed_candle(END)

    after_retry = reopened_broker.reconcile()
    stop_sets = [
        event
        for event in reopened_store.replay_state().event_evidence
        if event.event_type == "PAPER_STOP" and event.payload.get("action") == "SET"
    ]
    assert len(after_retry.fills) == 1
    assert after_retry.active_stop == first_stop
    assert len(stop_sets) == 1
    assert not any(
        event.event_type == "PAPER_CYCLE"
        for event in reopened_store.replay_state().event_evidence
    )

from pathlib import Path

import pandas as pd
import pytest
import backtrader as bt

from autobit.backtest.engine import (
    BacktestConfig,
    EnrichedPandasData,
    _DonchianBacktestStrategy,
    _prepare_frame,
    run_backtest,
)
from autobit.config import StrategyConfig
from autobit.config import CostConfig
from autobit.execution.backtest_broker import EventBacktestBroker


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _fixture(name: str) -> pd.DataFrame:
    return pd.read_csv(
        FIXTURES / name,
        parse_dates=["timestamp"],
        index_col="timestamp",
    )


def test_close_signal_fills_at_next_open() -> None:
    result = run_backtest(
        _fixture("entry_next_open.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entry = next(
        order
        for order in result.orders
        if order.side == "BUY" and order.status == "COMPLETED"
    )
    assert entry.signal_time == pd.Timestamp("2025-01-05T00:00:00Z")
    assert entry.fill_time == pd.Timestamp("2025-01-05T04:00:00Z")
    assert entry.fill_price == pytest.approx(105.0)


def test_gap_below_stop_fills_at_worse_open() -> None:
    result = run_backtest(
        _fixture("gap_stop.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    stop = next(
        order
        for order in result.orders
        if order.reason == "HARD_STOP" and order.status == "COMPLETED"
    )
    assert stop.stop_price == pytest.approx(95.0)
    assert stop.fill_price == pytest.approx(90.0)


def test_close_exit_signal_fills_at_following_open() -> None:
    result = run_backtest(
        _fixture("entry_next_open.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    exit_fill = next(
        order
        for order in result.orders
        if order.reason == "CLOSE_EXIT" and order.status == "COMPLETED"
    )
    assert exit_fill.signal_time == pd.Timestamp("2025-01-05T08:00:00Z")
    assert exit_fill.fill_time == pd.Timestamp("2025-01-05T12:00:00Z")
    assert exit_fill.fill_price == pytest.approx(101.0)


def test_new_trailing_stop_becomes_active_on_next_bar() -> None:
    result = run_backtest(
        _fixture("trailing_activation.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    trailing_fill = next(
        order
        for order in result.orders
        if order.reason == "TRAILING_STOP" and order.status == "COMPLETED"
    )
    assert trailing_fill.signal_time == pd.Timestamp("2025-01-05T08:00:00Z")
    assert trailing_fill.fill_time == pd.Timestamp("2025-01-05T12:00:00Z")
    assert trailing_fill.stop_price == pytest.approx(105.0)
    assert trailing_fill.fill_price == pytest.approx(105.0)


def test_old_stop_wins_when_bar_also_reaches_two_r() -> None:
    result = run_backtest(
        _fixture("old_stop_precedence.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    exit_fill = next(
        order
        for order in result.orders
        if order.side == "SELL" and order.status == "COMPLETED"
    )
    assert exit_fill.reason == "HARD_STOP"
    assert exit_fill.stop_price == pytest.approx(95.0)
    assert exit_fill.fill_price == pytest.approx(95.0)
    assert not any(order.reason == "TRAILING_STOP" for order in result.orders)


def test_gap_that_exceeds_close_sizing_is_execution_capped() -> None:
    fee_rate = 0.0005
    slippage_rate = 0.0005
    result = run_backtest(
        _fixture("margin_gap.csv"),
        BacktestConfig(
            costs=CostConfig(fee_rate=fee_rate, slippage_rate=slippage_rate)
        ),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    partial = next(order for order in entry_events if order.status == "PARTIAL")
    canceled = next(order for order in entry_events if order.status == "CANCELED")
    assert partial.fill_time == pd.Timestamp("2025-01-05T04:00:00Z")
    assert 0.0 < partial.filled_quantity < partial.requested_quantity
    assert partial.reason == "EXECUTION_CAP"
    assert canceled.reason == "END_OF_DATA"
    assert not any(order.status == "INSUFFICIENT_CASH" for order in entry_events)
    filled_notional = partial.filled_quantity * partial.fill_price
    effective_risk = partial.filled_quantity * (
        5.0 + partial.fill_price * fee_rate + (partial.fill_price - 5.0) * fee_rate
    )
    assert filled_notional <= 70.0 + 1e-9
    assert filled_notional * (1.0 + fee_rate) <= 100.0 + 1e-9
    assert effective_risk <= 2.0 + 1e-9
    assert result.final_equity >= 0.0


def test_final_bar_entry_has_truthful_ordered_terminal_lifecycle() -> None:
    frame = _fixture("entry_next_open.csv").iloc[:611]
    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    assert [order.status.value for order in entry_events] == [
        "CREATED",
        "SUBMITTED",
        "CANCELED",
    ]
    assert entry_events[-1].reason == "END_OF_DATA"
    keys = [(order.order_id, order.status, order.filled_quantity) for order in entry_events]
    assert len(keys) == len(set(keys))


def test_final_bar_partial_entry_remainder_is_terminally_canceled() -> None:
    frame = _fixture("partial_entry.csv").iloc[:612]
    result = run_backtest(
        frame,
        BacktestConfig(
            costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
            entry_fill_fraction=0.5,
        ),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    assert [order.status.value for order in entry_events] == [
        "CREATED",
        "SUBMITTED",
        "ACCEPTED",
        "PARTIAL",
        "CANCELED",
    ]
    assert entry_events[-1].reason == "END_OF_DATA"
    assert entry_events[-1].filled_quantity == pytest.approx(entry_events[-2].filled_quantity)
    assert entry_events[-1].remainder_quantity == pytest.approx(
        entry_events[-2].remainder_quantity
    )
    order_ids = {order.order_id for order in result.orders}
    for order_id in order_ids:
        lifecycle = [order for order in result.orders if order.order_id == order_id]
        assert len(lifecycle) > 1
        assert lifecycle[-1].status in {
            "COMPLETED",
            "CANCELED",
            "EXPIRED",
            "INSUFFICIENT_CASH",
            "REJECTED",
        }


def test_repeated_entry_signal_does_not_pyramid() -> None:
    frame = _fixture("partial_entry.csv")
    frame.loc[pd.Timestamp("2025-01-05T04:00:00Z"), [
        "close",
        "ema_200",
        "entry_high",
        "previous_close",
        "previous_entry_high",
    ]] = [106.0, 99.0, 105.0, 100.0, 100.0]
    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    assert len({order.order_id for order in entry_events}) == 1
    assert sum(order.status == "COMPLETED" for order in entry_events) == 1


class _BrokerProbe(bt.Strategy):
    params = (("scenario", "oversell"),)

    def __init__(self) -> None:
        self.statuses: list[tuple[int, int]] = []
        self.submitted = False

    def next(self) -> None:
        if self.submitted:
            return
        self.submitted = True
        if self.p.scenario == "oversell":
            self.sell(size=1.0)
        elif self.p.scenario == "duplicate":
            self.buy(size=0.1)
            self.buy(size=0.1)
        elif self.p.scenario == "expired":
            self.buy(
                size=0.1,
                exectype=bt.Order.Limit,
                price=1.0,
                valid=self.data.datetime.datetime(0),
            )

    def notify_order(self, order: bt.Order) -> None:
        self.statuses.append((order.ref, order.status))


def _run_broker_probe(scenario: str) -> _BrokerProbe:
    index = pd.date_range("2025-01-01", periods=3, freq="4h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0],
            "volume": [1.0, 1.0, 1.0],
        },
        index=index,
    )
    cerebro = bt.Cerebro(cheat_on_open=False, stdstats=False)
    broker = EventBacktestBroker()
    cerebro.setbroker(broker)
    broker.set_coc(False)
    broker.setcash(100.0)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(_BrokerProbe, scenario=scenario)
    return cerebro.run()[0]


def test_broker_rejects_sell_beyond_current_btc() -> None:
    strategy = _run_broker_probe("oversell")
    assert any(status == bt.Order.Rejected for _, status in strategy.statuses)
    assert not any(status == bt.Order.Completed for _, status in strategy.statuses)


def test_broker_rejects_second_live_buy_order() -> None:
    strategy = _run_broker_probe("duplicate")
    terminal = [status for _, status in strategy.statuses if status in (bt.Order.Completed, bt.Order.Rejected)]
    assert terminal.count(bt.Order.Completed) == 1
    assert terminal.count(bt.Order.Rejected) == 1


class _RejectedLedgerProbe(_DonchianBacktestStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.probe_submitted = False

    def next(self) -> None:
        if self.probe_submitted:
            return
        self.probe_submitted = True
        self.sell(
            size=1.0,
            signal_time=self._now().isoformat(),
            reason="CLOSE_EXIT",
        )


def test_rejected_ledger_record_prefers_broker_guard_reason() -> None:
    config = BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0))
    frame = _prepare_frame(_fixture("entry_next_open.csv").iloc[:3], StrategyConfig())
    reference_opens = {
        pd.Timestamp(timestamp): float(open_price)
        for timestamp, open_price in frame["open"].items()
    }
    cerebro = bt.Cerebro(cheat_on_open=False, stdstats=False)
    broker = EventBacktestBroker()
    cerebro.setbroker(broker)
    broker.set_coc(False)
    broker.setcash(100.0)
    cerebro.adddata(EnrichedPandasData(dataname=frame))
    cerebro.addstrategy(
        _RejectedLedgerProbe,
        adapter_config=config,
        reference_opens=reference_opens,
    )
    strategy = cerebro.run()[0]

    rejected = next(order for order in strategy.order_records if order.status == "REJECTED")
    assert rejected.reason == "OVERSELL"


def test_real_broker_order_can_expire() -> None:
    strategy = _run_broker_probe("expired")
    assert any(status == bt.Order.Expired for _, status in strategy.statuses)

from pathlib import Path

import pandas as pd
import pytest
import backtrader as bt

from autobit.backtest.engine import BacktestConfig, run_backtest
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


def test_gap_that_exceeds_cash_reports_margin_without_negative_equity() -> None:
    result = run_backtest(
        _fixture("margin_gap.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    assert any(order.status == "INSUFFICIENT_CASH" for order in entry_events)
    assert not any(order.status == "COMPLETED" for order in entry_events)
    assert result.final_equity == pytest.approx(100.0)


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


def test_real_broker_order_can_expire() -> None:
    strategy = _run_broker_probe("expired")
    assert any(status == bt.Order.Expired for _, status in strategy.statuses)

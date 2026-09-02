from pathlib import Path

import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _fixture(name: str) -> pd.DataFrame:
    return pd.read_csv(
        FIXTURES / name,
        parse_dates=["timestamp"],
        index_col="timestamp",
    )


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.1, float("nan"), None, True])
def test_partial_fill_fraction_must_be_in_open_closed_unit_interval(fraction: object) -> None:
    with pytest.raises(ValueError, match="fill fraction"):
        BacktestConfig(entry_fill_fraction=fraction)


@pytest.mark.parametrize("initial_equity", [0.0, -1.0, float("nan"), None, True])
def test_initial_equity_must_be_finite_and_positive(initial_equity: object) -> None:
    with pytest.raises(ValueError, match="initial equity"):
        BacktestConfig(initial_equity=initial_equity)


def test_half_entry_fills_once_then_cancels_remainder_without_retry() -> None:
    result = run_backtest(
        _fixture("partial_entry.csv"),
        BacktestConfig(
            costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
            entry_fill_fraction=0.5,
        ),
    )

    entry_events = [order for order in result.orders if order.side == "BUY"]
    assert len({order.order_id for order in entry_events}) == 1
    partial = next(order for order in entry_events if order.status == "PARTIAL")
    canceled = next(order for order in entry_events if order.status == "CANCELED")
    assert partial.filled_quantity == pytest.approx(partial.requested_quantity * 0.5)
    assert partial.remainder_quantity == pytest.approx(partial.requested_quantity * 0.5)
    assert canceled.filled_quantity == pytest.approx(partial.filled_quantity)
    assert canceled.remainder_quantity == pytest.approx(partial.remainder_quantity)
    assert not any(order.status == "COMPLETED" for order in entry_events)


def test_half_exit_reissues_exact_remainder_once_for_next_open() -> None:
    slippage_rate = 0.001
    result = run_backtest(
        _fixture("partial_exit.csv"),
        BacktestConfig(
            costs=CostConfig(fee_rate=0.0, slippage_rate=slippage_rate),
            exit_fill_fraction=0.5,
        ),
    )

    exit_events = [order for order in result.orders if order.reason == "CLOSE_EXIT"]
    assert len({order.order_id for order in exit_events}) == 2
    partial = next(order for order in exit_events if order.status == "PARTIAL")
    canceled = next(order for order in exit_events if order.status == "CANCELED")
    completed = next(order for order in exit_events if order.status == "COMPLETED")
    assert partial.fill_time == pd.Timestamp("2025-01-05T12:00:00Z")
    assert partial.fill_price == pytest.approx(101.0 * (1.0 - slippage_rate))
    assert canceled.remainder_quantity == pytest.approx(partial.remainder_quantity)
    assert completed.requested_quantity == pytest.approx(partial.remainder_quantity)
    assert completed.filled_quantity == pytest.approx(partial.remainder_quantity)
    assert completed.remainder_quantity == pytest.approx(0.0)
    assert completed.fill_time == pd.Timestamp("2025-01-05T16:00:00Z")
    assert completed.fill_price == pytest.approx(99.0 * (1.0 - slippage_rate))
    trade, = result.trades
    entry = next(
        order
        for order in result.orders
        if order.side == "BUY" and order.status == "COMPLETED"
    )
    expected_exit_price = (partial.fill_price + completed.fill_price) / 2.0
    assert trade.quantity == pytest.approx(entry.filled_quantity)
    assert trade.exit_price == pytest.approx(expected_exit_price)
    assert trade.exit_time == pd.Timestamp("2025-01-05T16:00:00Z")

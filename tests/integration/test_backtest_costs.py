import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig

from test_backtest_execution import _fixture


def _completed_round_trip(result):
    fills = [
        order
        for order in result.orders
        if order.status in ("PARTIAL", "COMPLETED") and order.filled_quantity > 0.0
    ]
    return (
        next(order for order in fills if order.side == "BUY"),
        next(order for order in fills if order.reason == "CLOSE_EXIT"),
    )


def test_fee_is_charged_once_on_both_sides() -> None:
    fee_rate = 0.0005
    result = run_backtest(
        _fixture("entry_next_open.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=fee_rate, slippage_rate=0.0)),
    )
    entry, exit_fill = _completed_round_trip(result)

    expected_entry_fee = entry.filled_quantity * entry.fill_price * fee_rate
    expected_exit_fee = exit_fill.filled_quantity * exit_fill.fill_price * fee_rate
    assert entry.fee == pytest.approx(expected_entry_fee)
    assert exit_fill.fee == pytest.approx(expected_exit_fee)
    assert result.total_fees == pytest.approx(expected_entry_fee + expected_exit_fee)
    assert result.final_equity == pytest.approx(
        100.0
        + exit_fill.filled_quantity * (exit_fill.fill_price - entry.fill_price)
        - expected_entry_fee
        - expected_exit_fee
    )
    trade, = result.trades
    assert trade.entry_time == entry.fill_time
    assert trade.exit_time == exit_fill.fill_time
    assert trade.quantity == pytest.approx(entry.filled_quantity)
    assert trade.entry_price == pytest.approx(entry.fill_price)
    assert trade.exit_price == pytest.approx(exit_fill.fill_price)
    assert trade.gross_pnl == pytest.approx(
        entry.filled_quantity * (exit_fill.fill_price - entry.fill_price)
    )
    assert trade.fees == pytest.approx(expected_entry_fee + expected_exit_fee)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees)
    assert trade.exit_reason == "CLOSE_EXIT"
    assert result.equity_curve[-1].equity == pytest.approx(result.final_equity)


def test_slippage_is_directional_and_reported_as_nonnegative_cost() -> None:
    slippage_rate = 0.0005
    result = run_backtest(
        _fixture("entry_next_open.csv"),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=slippage_rate)),
    )
    entry, exit_fill = _completed_round_trip(result)

    assert entry.fill_price == pytest.approx(105.0 * (1.0 + slippage_rate))
    assert exit_fill.fill_price == pytest.approx(101.0 * (1.0 - slippage_rate))
    expected_cost = entry.filled_quantity * (entry.fill_price - 105.0)
    expected_cost += exit_fill.filled_quantity * (101.0 - exit_fill.fill_price)
    assert entry.slippage >= 0.0
    assert exit_fill.slippage >= 0.0
    assert result.total_slippage == pytest.approx(expected_cost)

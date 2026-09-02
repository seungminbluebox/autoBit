from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
import math

import pytest

from autobit.backtest.analyzers import PerformanceMetrics, calculate_metrics
from autobit.backtest.benchmark import BuyAndHoldResult, run_buy_and_hold
from autobit.backtest.engine import EquityPoint, OrderRecord, TradeRecord
from autobit.config import CostConfig
from autobit.domain.models import OrderStatus

import pandas as pd


EXPECTED_FIELDS = (
    "total_return",
    "annualized_return",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "max_drawdown",
    "max_drawdown_duration_bars",
    "profit_factor",
    "expectancy",
    "win_rate",
    "average_win",
    "average_loss",
    "average_win_loss_ratio",
    "trade_count",
    "mean_holding_bars",
    "median_holding_bars",
    "exposure",
    "turnover",
    "total_fees",
    "total_slippage",
)


def test_profit_factor_and_expectancy_use_closed_trades() -> None:
    metrics = calculate_metrics([10.0, -4.0, 6.0, -2.0], periods_per_year=2190)

    assert tuple(field.name for field in fields(metrics)) == EXPECTED_FIELDS
    assert metrics.profit_factor == pytest.approx(16.0 / 6.0)
    assert metrics.expectancy == pytest.approx(2.5)
    assert metrics.win_rate == pytest.approx(0.5)
    assert metrics.average_win == pytest.approx(8.0)
    assert metrics.average_loss == pytest.approx(-3.0)
    assert metrics.average_win_loss_ratio == pytest.approx(8.0 / 3.0)
    with pytest.raises(FrozenInstanceError):
        metrics.trade_count = 99  # type: ignore[misc]


def test_equity_metrics_use_bars_not_trade_count_for_annualization() -> None:
    metrics = calculate_metrics(
        [20.0],
        equity_curve=[100.0, 110.0, 99.0, 121.0],
        periods_per_year=3,
    )

    assert metrics.total_return == pytest.approx(0.21)
    assert metrics.annualized_return == pytest.approx(0.21)
    assert metrics.max_drawdown == pytest.approx(0.10)
    assert metrics.max_drawdown_duration_bars == 1
    assert metrics.calmar_ratio == pytest.approx(2.1)


def test_real_trade_metadata_derives_holding_exposure_turnover_and_costs() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    equity = tuple(
        EquityPoint(start + timedelta(hours=4 * index), value)
        for index, value in enumerate((100.0, 100.0, 110.0, 110.0, 110.0))
    )
    trade = TradeRecord(
        entry_time=start + timedelta(hours=4),
        exit_time=start + timedelta(hours=12),
        quantity=0.5,
        entry_price=100.0,
        exit_price=120.0,
        gross_pnl=10.0,
        net_pnl=9.0,
        fees=1.0,
        exit_reason="CLOSE_EXIT",
    )

    metrics = calculate_metrics(
        equity_curve=equity,
        trades=[trade],
        periods_per_year=2190,
        total_slippage=0.25,
    )

    assert metrics.trade_count == 1
    assert metrics.mean_holding_bars == pytest.approx(2.0)
    assert metrics.median_holding_bars == pytest.approx(2.0)
    assert metrics.exposure == pytest.approx(0.5)
    assert metrics.turnover == pytest.approx(110.0 / 106.0)
    assert metrics.total_fees == pytest.approx(1.0)
    assert metrics.total_slippage == pytest.approx(0.25)


def test_zero_trade_no_loss_and_flat_equity_conventions_are_finite() -> None:
    empty = calculate_metrics([], periods_per_year=2190)
    no_loss = calculate_metrics([4.0, 2.0], equity_curve=[100.0, 102.0, 106.0])
    flat = calculate_metrics([], equity_curve=[100.0, 100.0, 100.0])

    assert empty == PerformanceMetrics()
    assert no_loss.profit_factor == 0.0
    assert no_loss.average_loss == 0.0
    assert no_loss.average_win_loss_ratio == 0.0
    assert flat.sharpe_ratio == 0.0
    assert flat.sortino_ratio == 0.0
    assert flat.calmar_ratio == 0.0
    for metrics in (empty, no_loss, flat):
        assert all(
            math.isfinite(float(getattr(metrics, field.name)))
            for field in fields(metrics)
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"closed_pnls": [float("nan")]}, "closed PnL"),
        ({"equity_curve": [100.0, float("inf")]}, "equity"),
        ({"periods_per_year": 0}, "periods_per_year"),
        ({"exposure": 1.1}, "exposure"),
        ({"turnover": -0.1}, "turnover"),
        ({"total_fees": float("nan")}, "total_fees"),
    ],
)
def test_invalid_or_nonfinite_metric_inputs_are_rejected(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_metrics(**kwargs)


def test_buy_and_hold_enters_at_first_next_open_and_liquidates_last_close() -> None:
    index = pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [90.0, 100.0, 115.0],
            "close": [95.0, 110.0, 120.0],
        },
        index=index,
    )

    result = run_buy_and_hold(
        frame,
        CostConfig(fee_rate=0.01, slippage_rate=0.10),
    )

    assert result.initial_equity == 100.0
    assert result.entry_time == index[1].to_pydatetime()
    assert result.exit_time == index[2].to_pydatetime()
    assert result.entry_reference_price == 100.0
    assert result.exit_reference_price == 120.0
    assert result.entry_price == pytest.approx(110.0)
    assert result.exit_price == pytest.approx(108.0)
    assert result.quantity == pytest.approx(0.9000900090009001)
    assert result.entry_fee == pytest.approx(0.9900990099009902)
    assert result.exit_fee == pytest.approx(0.9720972097209721)
    assert result.total_fees == pytest.approx(1.9621962196219623)
    assert result.total_slippage == pytest.approx(19.801980198019802)
    assert result.final_equity == pytest.approx(96.23762376237624)
    assert result.total_return == pytest.approx(-0.0376237623762376)
    with pytest.raises(FrozenInstanceError):
        result.quantity = 1.0  # type: ignore[misc]


def test_buy_and_hold_with_no_next_open_returns_finite_cash_only_result() -> None:
    frame = pd.DataFrame(
        {"open": [100.0], "close": [105.0]},
        index=pd.date_range("2026-01-01", periods=1, freq="4h", tz="UTC"),
    )

    result = run_buy_and_hold(frame, CostConfig())

    assert result == BuyAndHoldResult()


@pytest.mark.parametrize(
    "costs",
    [
        CostConfig(fee_rate=-0.1, slippage_rate=0.0),
        CostConfig(fee_rate=0.0, slippage_rate=1.0),
        CostConfig(fee_rate=0.0, slippage_rate=float("nan")),
    ],
)
def test_buy_and_hold_rejects_invalid_costs(costs: CostConfig) -> None:
    frame = pd.DataFrame(
        {"open": [100.0, 100.0], "close": [100.0, 100.0]},
        index=pd.date_range("2026-01-01", periods=2, freq="4h", tz="UTC"),
    )

    with pytest.raises(ValueError, match="cost"):
        run_buy_and_hold(frame, costs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity_curve": [1.0, 1e308]},
        {"closed_pnls": [1e308, 1e308, -1.0]},
    ],
)
def test_finite_inputs_that_overflow_derived_metrics_are_rejected(
    kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="finite"):
        calculate_metrics(**kwargs)


def test_buy_and_hold_rejects_arithmetic_that_would_make_nonfinite_output() -> None:
    frame = pd.DataFrame(
        {"open": [1.0, 1.5e308], "close": [1.0, 1.5e308]},
        index=pd.date_range("2026-01-01", periods=2, freq="4h", tz="UTC"),
    )

    with pytest.raises(ValueError, match="finite"):
        run_buy_and_hold(frame, CostConfig(fee_rate=0.5, slippage_rate=0.5))


def test_open_position_partial_fills_drive_exposure_and_turnover_without_duplicates() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    equity = tuple(
        EquityPoint(start + timedelta(hours=4 * index), 100.0)
        for index in range(5)
    )

    def event(
        order_id: str,
        status: OrderStatus,
        side: str,
        filled: float,
        fill_price: float,
        fill_bar: int,
    ) -> OrderRecord:
        timestamp = start + timedelta(hours=4 * fill_bar)
        return OrderRecord(
            order_id=order_id,
            status=status,
            side=side,
            requested_quantity=0.5,
            filled_quantity=filled,
            remainder_quantity=0.5 - filled,
            occurred_at=timestamp,
            signal_time=start,
            fill_time=timestamp,
            fill_price=fill_price,
        )

    orders = (
        event("buy", OrderStatus.PARTIAL, "BUY", 0.2, 100.0, 1),
        event("buy", OrderStatus.COMPLETED, "BUY", 0.5, 104.0, 2),
        event("sell", OrderStatus.PARTIAL, "SELL", 0.1, 110.0, 3),
        event("sell", OrderStatus.CANCELED, "SELL", 0.1, 110.0, 3),
    )

    metrics = calculate_metrics(equity_curve=equity, trades=[], orders=orders)

    assert metrics.trade_count == 0
    assert metrics.exposure == pytest.approx(0.8)
    assert metrics.turnover == pytest.approx(0.63)

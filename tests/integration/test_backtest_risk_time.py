from pathlib import Path

import pandas as pd

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _scenario_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.read_csv(
        FIXTURES / "entry_next_open.csv",
        parse_dates=["timestamp"],
        index_col="timestamp",
    ).iloc[:610]
    start = frame.index[-1] + pd.Timedelta(hours=4)
    defaults: dict[str, object] = {
        "open": 99.0,
        "high": 100.0,
        "low": 98.0,
        "close": 99.0,
        "volume": 1.0,
        "warmup_complete": True,
        "entry_data_valid": True,
        "ema_200": 90.0,
        "entry_high": 110.0,
        "previous_close": 99.0,
        "previous_entry_high": 99.0,
        "atr_14": 2.0,
        "exit_low": 80.0,
        "baseline_atr_pct": 0.02,
    }
    records = [{**defaults, **row} for row in rows]
    appended = pd.DataFrame(
        records,
        index=pd.date_range(start, periods=len(records), freq="4h", tz="UTC"),
    )
    return pd.concat([frame, appended]).loc[:, frame.columns]


def _entry_signal() -> dict[str, object]:
    return {
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "close": 100.0,
        "entry_high": 99.0,
        "previous_close": 99.0,
        "previous_entry_high": 99.0,
    }


def test_drawdown_and_rolling_weekly_reductions_constrain_the_next_entry() -> None:
    rows: list[dict[str, object]] = [
        _entry_signal(),
        {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
        {"open": 92.5, "high": 94.0, "low": 91.0, "close": 93.0},
        {},
        {},
        {},
        _entry_signal(),
        {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
        {"open": 92.0, "high": 94.0, "low": 91.0, "close": 93.0},
        {},
        {},
        {},
        _entry_signal(),
        {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
    ]
    result = run_backtest(
        _scenario_frame(rows),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entries = [
        order
        for order in result.orders
        if order.side == "BUY" and order.status == "COMPLETED"
    ]
    assert len(entries) == 3
    third = entries[2]
    signal_equity = next(
        point.equity for point in result.equity_curve if point.timestamp == third.signal_time
    )
    assert third.requested_quantity * 5.0 <= signal_equity * 0.005 + 1e-12
    assert third.requested_quantity * 100.0 <= signal_equity * 0.25 + 1e-12


def test_volatility_halt_blocks_entries_until_three_stable_valid_bars() -> None:
    high_volatility_signal = {**_entry_signal(), "atr_14": 8.0}
    stable_signal = _entry_signal()
    result = run_backtest(
        _scenario_frame(
            [
                high_volatility_signal,
                stable_signal,
                stable_signal,
                stable_signal,
                {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
            ]
        ),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entry = next(
        order
        for order in result.orders
        if order.side == "BUY" and order.status in {"PARTIAL", "COMPLETED"}
    )
    assert entry.signal_time == pd.Timestamp("2025-01-05T12:00:00Z")
    assert entry.fill_time == pd.Timestamp("2025-01-05T16:00:00Z")


def test_daily_loss_cooldown_survives_utc_reset_and_releases_at_24_hours() -> None:
    repeated_signal = _entry_signal()
    result = run_backtest(
        _scenario_frame(
            [
                repeated_signal,
                {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
                {
                    "open": 89.0,
                    "high": 91.0,
                    "low": 88.0,
                    "close": 90.0,
                    "ema_200": 80.0,
                    "entry_high": 89.0,
                    "previous_close": 100.0,
                    "previous_entry_high": 100.0,
                },
                repeated_signal,
                repeated_signal,
                repeated_signal,
                repeated_signal,
                repeated_signal,
                repeated_signal,
                {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
            ]
        ),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entries = [
        order
        for order in result.orders
        if order.side == "BUY" and order.status in {"PARTIAL", "COMPLETED"}
    ]
    assert len(entries) == 2
    assert entries[1].signal_time == pd.Timestamp("2025-01-06T08:00:00Z")
    assert entries[1].fill_time == pd.Timestamp("2025-01-06T12:00:00Z")


def test_hard_drawdown_forces_next_open_exit_before_donchian_exit() -> None:
    rows: list[dict[str, object]] = [{} for _ in range(32)]
    rows[0] = _entry_signal()
    rows[1] = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    rows[2] = {"open": 82.5, "high": 84.0, "low": 82.0, "close": 83.0}
    rows[14] = _entry_signal()
    rows[15] = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    rows[16] = {"open": 15.9139784946, "high": 17.0, "low": 15.0, "close": 16.0}
    rows[28] = _entry_signal()
    rows[29] = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    rows[30] = {
        "open": 100.0,
        "high": 100.5,
        "low": 95.5,
        "close": 95.5,
        "exit_low": 96.0,
    }
    rows[31] = {"open": 95.4, "high": 96.0, "low": 95.0, "close": 95.5}

    result = run_backtest(
        _scenario_frame(rows),
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    entries = [
        order
        for order in result.orders
        if order.side == "BUY" and order.status in {"PARTIAL", "COMPLETED"}
    ]
    assert len(entries) == 3
    forced = next(
        order
        for order in result.orders
        if order.reason == "RISK_EXIT" and order.status == "COMPLETED"
    )
    assert forced.signal_time == pd.Timestamp("2025-01-10T00:00:00Z")
    assert forced.fill_time == pd.Timestamp("2025-01-10T04:00:00Z")
    assert forced.filled_quantity == entries[-1].filled_quantity
    assert not any(
        order.reason == "CLOSE_EXIT" and order.signal_time == forced.signal_time
        for order in result.orders
    )


def test_stagnant_position_exits_next_open_after_60_completed_held_bars() -> None:
    rows: list[dict[str, object]] = [
        _entry_signal(),
        {"open": 100.0, "high": 104.0, "low": 96.0, "close": 100.0},
    ]
    rows.extend(
        {"open": 100.0, "high": 104.9, "low": 96.0, "close": 100.0}
        for _ in range(60)
    )
    rows.append({"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0})
    frame = _scenario_frame(rows)

    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    exit_fill = next(
        order
        for order in result.orders
        if order.reason == "STAGNANT_EXIT" and order.status == "COMPLETED"
    )
    assert exit_fill.signal_time == frame.index[610 + 61]
    assert exit_fill.fill_time == frame.index[610 + 62]


def test_one_r_high_water_equality_disables_stagnant_and_max_hold_exits_next_open() -> None:
    rows: list[dict[str, object]] = [
        _entry_signal(),
        {"open": 100.0, "high": 105.0, "low": 96.0, "close": 100.0},
    ]
    rows.extend(
        {"open": 100.0, "high": 104.0, "low": 96.0, "close": 100.0}
        for _ in range(1095)
    )
    rows.append({"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0})
    frame = _scenario_frame(rows)

    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    exit_fill = next(
        order
        for order in result.orders
        if order.reason == "MAX_HOLD_EXIT" and order.status == "COMPLETED"
    )
    assert exit_fill.signal_time == frame.index[610 + 1096]
    assert exit_fill.fill_time == frame.index[610 + 1097]
    assert not any(order.reason == "STAGNANT_EXIT" for order in result.orders)


def test_donchian_close_has_priority_over_stagnant_exit_on_same_close() -> None:
    rows: list[dict[str, object]] = [
        _entry_signal(),
        {"open": 100.0, "high": 104.0, "low": 96.0, "close": 100.0},
    ]
    rows.extend(
        {"open": 100.0, "high": 104.9, "low": 96.0, "close": 100.0}
        for _ in range(59)
    )
    rows.append(
        {
            "open": 100.0,
            "high": 104.9,
            "low": 96.0,
            "close": 100.0,
            "exit_low": 101.0,
        }
    )
    rows.append({"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0})
    frame = _scenario_frame(rows)

    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    close_exit = next(
        order
        for order in result.orders
        if order.reason == "CLOSE_EXIT" and order.status == "COMPLETED"
    )
    assert close_exit.signal_time == frame.index[610 + 61]
    assert close_exit.fill_time == frame.index[610 + 62]
    assert not any(order.reason == "STAGNANT_EXIT" for order in result.orders)

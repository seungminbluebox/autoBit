from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig, StrategyConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.persistence.sqlite_store import SQLiteStore
from autobit.risk.position_sizer import calculate_size
from autobit.strategy.donchian_trend import evaluate_close_exit, evaluate_entry


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _timestamp(value: object) -> str:
    return pd.Timestamp(value).isoformat().replace("+00:00", "Z")


def _paper_golden(
    path: Path,
    *,
    entry_candle_breach: bool = False,
) -> tuple[object, ...]:
    frame = pd.read_csv(
        FIXTURES / "core_golden.csv",
        parse_dates=["timestamp"],
        index_col="timestamp",
    )
    if entry_candle_breach:
        fill_at = pd.Timestamp("2025-01-05T04:00:00Z")
        frame.loc[fill_at, "low"] = 99.0
        frame = frame.loc[:fill_at].copy()
    costs = CostConfig(fee_rate=0.0, slippage_rate=0.0)
    config = StrategyConfig()
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, costs)

    for timestamp, row in frame.iterrows():
        candle_at = _timestamp(timestamp)
        reconciliation = broker.reconcile()
        for pending in reconciliation.active_orders:
            if pending.order_kind == "MARKET" and pd.Timestamp(candle_at) > pd.Timestamp(
                pending.signal_at_utc
            ):
                fill = broker.process_open(
                    pending.order_id,
                    candle_at,
                    open_price=float(row["open"]),
                )
                if fill is not None and fill.side == "BUY":
                    broker.set_stop(
                        candle_at,
                        fill.fill_price - config.initial_atr_mult * float(row["atr_14"]),
                        active_after=candle_at,
                        reason="HARD_STOP",
                    )

        if broker.reconcile().btc_quantity > 0.0:
            broker.process_intrabar_stop(
                candle_at,
                open_price=float(row["open"]),
                low_price=float(row["low"]),
            )

        state = broker.reconcile()
        if state.btc_quantity > 0.0:
            if evaluate_close_exit(row):
                broker.submit_exit(
                    candle_at,
                    quantity=state.btc_quantity,
                    owned_quantity=state.btc_quantity,
                    reason="CLOSE_EXIT",
                )
        elif not state.active_orders and evaluate_entry(row, is_flat=True, config=config):
            entry_reference = float(row["close"])
            atr = float(row["atr_14"])
            quantity = calculate_size(
                equity=state.equity,
                cash=state.cash,
                entry=entry_reference,
                stop=entry_reference - config.initial_atr_mult * atr,
                current_atr_pct=atr / entry_reference,
                baseline_atr_pct=float(row["baseline_atr_pct"]),
                risk_rate=0.02,
                exposure_cap=0.70,
                costs=costs,
            ).quantity
            broker.submit_entry(candle_at, quantity=quantity)

    result = broker.reconcile()
    trade, = result.completed_trades
    golden = (
        _timestamp(trade.entry_time),
        _timestamp(trade.exit_time),
        trade.entry_price,
        trade.exit_price,
        trade.exit_reason,
        result.equity,
    )
    store.close()
    return golden


def test_real_sqlite_candle_by_candle_paper_matches_core_golden(tmp_path: Path) -> None:
    expected = tuple(
        json.loads(
            (FIXTURES / "core_golden_expected.json").read_text(encoding="utf-8")
        )["result"]
    )
    frame = pd.read_csv(
        FIXTURES / "core_golden.csv",
        parse_dates=["timestamp"],
        index_col="timestamp",
    )
    core = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )

    first = _paper_golden(tmp_path / "first.sqlite3")
    second = _paper_golden(tmp_path / "second.sqlite3")

    assert first[:2] == tuple(expected[:2])
    assert first[4] == expected[4]
    assert first[2] == pytest.approx(expected[2])
    assert first[3] == pytest.approx(expected[3])
    assert first[5] == pytest.approx(expected[5])
    assert second == first
    assert first[:2] == (
        core.trades[0].entry_time.isoformat().replace("+00:00", "Z"),
        core.trades[0].exit_time.isoformat().replace("+00:00", "Z"),
    )
    assert first[2] == pytest.approx(core.trades[0].entry_price)
    assert first[3] == pytest.approx(core.trades[0].exit_price)
    assert first[4] == core.trades[0].exit_reason
    assert first[5] == pytest.approx(core.final_equity)


def test_real_sqlite_paper_matches_core_on_entry_candle_hard_stop(
    tmp_path: Path,
) -> None:
    frame = pd.read_csv(
        FIXTURES / "core_golden.csv",
        parse_dates=["timestamp"],
        index_col="timestamp",
    )
    fill_at = pd.Timestamp("2025-01-05T04:00:00Z")
    frame.loc[fill_at, "low"] = 99.0
    frame = frame.loc[:fill_at].copy()

    core = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )
    paper = _paper_golden(
        tmp_path / "entry-candle-stop.sqlite3",
        entry_candle_breach=True,
    )

    trade, = core.trades
    assert paper[:2] == (
        trade.entry_time.isoformat().replace("+00:00", "Z"),
        trade.exit_time.isoformat().replace("+00:00", "Z"),
    )
    assert paper[2] == pytest.approx(trade.entry_price)
    assert paper[3] == pytest.approx(trade.exit_price)
    assert paper[4] == trade.exit_reason == "HARD_STOP"
    assert paper[5] == pytest.approx(core.final_equity)

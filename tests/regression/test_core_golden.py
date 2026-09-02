import json
from pathlib import Path

import pandas as pd

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _run_golden() -> tuple[object, ...]:
    frame = pd.read_csv(
        FIXTURES / "core_golden.csv",
        parse_dates=["timestamp"],
        index_col="timestamp",
    )
    assert len(frame) == 614
    assert not frame.iloc[:610]["warmup_complete"].astype(bool).any()
    assert frame.iloc[610:]["warmup_complete"].astype(bool).all()
    result = run_backtest(
        frame,
        BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)),
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    return (
        trade.entry_time.isoformat().replace("+00:00", "Z"),
        trade.exit_time.isoformat().replace("+00:00", "Z"),
        trade.entry_price,
        trade.exit_price,
        trade.exit_reason,
        result.final_equity,
    )


def test_zero_cost_core_result_matches_literal_golden_tuple_deterministically() -> None:
    expected = json.loads(
        (FIXTURES / "core_golden_expected.json").read_text(encoding="utf-8")
    )

    assert _run_golden() == tuple(expected["result"])
    assert _run_golden() == tuple(expected["result"])

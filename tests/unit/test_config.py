from dataclasses import FrozenInstanceError

import pytest

from autobit.config import CostConfig, DataConfig, ExchangeRulesConfig, RiskConfig, StrategyConfig


def test_approved_defaults_are_immutable() -> None:
    strategy = StrategyConfig()

    assert (strategy.ema_period, strategy.entry_period, strategy.exit_period) == (200, 50, 20)
    assert (strategy.atr_period, strategy.initial_atr_mult, strategy.trailing_atr_mult) == (14, 2.5, 3.0)
    assert CostConfig().fee_rate == pytest.approx(0.0005)
    assert RiskConfig().base_risk_rate == pytest.approx(0.02)
    assert DataConfig().market == "KRW-BTC"
    rules = ExchangeRulesConfig()
    assert rules.min_order_krw == 5_000
    assert rules.krw_btc_tick_size == 1_000
    assert rules.checked_at_utc == "2026-09-02T00:00:00Z"
    with pytest.raises(FrozenInstanceError):
        strategy.ema_period = 20


def test_approved_config_defaults_are_complete() -> None:
    assert StrategyConfig() == StrategyConfig(
        ema_period=200,
        entry_period=50,
        exit_period=20,
        atr_period=14,
        initial_atr_mult=2.5,
        profit_activation_r=2.0,
        trailing_atr_mult=3.0,
        stagnant_bars=60,
        stagnant_min_r=1.0,
        max_holding_bars=1095,
        warmup_bars=600,
    )
    assert CostConfig() == CostConfig(fee_rate=0.0005, slippage_rate=0.0005)
    assert RiskConfig() == RiskConfig(
        base_risk_rate=0.02,
        max_exposure=0.70,
        hard_drawdown=0.15,
        daily_loss_limit=0.04,
        weekly_reduce_limit=0.05,
        weekly_halt_limit=0.07,
    )
    assert DataConfig() == DataConfig(market="KRW-BTC", candle_unit_minutes=240, page_size=200, years=7)
    assert ExchangeRulesConfig() == ExchangeRulesConfig(
        fee_rate=0.0005,
        min_order_krw=5_000,
        krw_btc_tick_size=1_000,
        checked_at_utc="2026-09-02T00:00:00Z",
        candle_source_url="https://docs.upbit.com/kr/reference/list-candles-minutes",
        order_policy_source_url="https://docs.upbit.com/kr/docs/krw-market-info",
        fee_source_url="https://docs.upbit.com/kr/docs/upbit-strategy-toolkit-reference",
    )

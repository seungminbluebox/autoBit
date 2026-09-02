from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    ema_period: int = 200
    entry_period: int = 50
    exit_period: int = 20
    atr_period: int = 14
    initial_atr_mult: float = 2.5
    profit_activation_r: float = 2.0
    trailing_atr_mult: float = 3.0
    stagnant_bars: int = 60
    stagnant_min_r: float = 1.0
    max_holding_bars: int = 1095
    warmup_bars: int = 600


@dataclass(frozen=True, slots=True)
class CostConfig:
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0005


@dataclass(frozen=True, slots=True)
class RiskConfig:
    base_risk_rate: float = 0.02
    max_exposure: float = 0.70
    hard_drawdown: float = 0.15
    daily_loss_limit: float = 0.04
    weekly_reduce_limit: float = 0.05
    weekly_halt_limit: float = 0.07


@dataclass(frozen=True, slots=True)
class DataConfig:
    market: str = "KRW-BTC"
    candle_unit_minutes: int = 240
    page_size: int = 200
    years: int = 7


@dataclass(frozen=True, slots=True)
class ExchangeRulesConfig:
    fee_rate: float = 0.0005
    min_order_krw: int = 5_000
    krw_btc_tick_size: int = 1_000
    checked_at_utc: str = "2026-09-02T00:00:00Z"
    candle_source_url: str = "https://docs.upbit.com/kr/reference/list-candles-minutes"
    order_policy_source_url: str = "https://docs.upbit.com/kr/docs/krw-market-info"
    fee_source_url: str = "https://docs.upbit.com/kr/docs/upbit-strategy-toolkit-reference"

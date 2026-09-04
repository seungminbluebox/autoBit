"""Offline, public-data-only research command line interface."""

import argparse
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import combinations
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Protocol
from uuid import uuid4

import httpx
import pandas as pd

from autobit.backtest.analyzers import PerformanceMetrics, calculate_metrics
from autobit.backtest.benchmark import BuyAndHoldResult, run_buy_and_hold
from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.alerts.notifier import Notifier, SafeNotifier, TelegramNotifier, deliver_alert_once
from autobit.config import CostConfig, DataConfig, ExchangeRulesConfig, StrategyConfig
from autobit.data.collector import collect_evidence_range, load_completed_evidence_frame
from autobit.data.quality import QualityReport, canonicalize_ohlcv
from autobit.data import storage as data_storage
from autobit.data.storage import _atomic_write, _canonical_json_bytes
from autobit.data.upbit_public import PublicDataUnavailable, UpbitPublicClient
from autobit.execution.paper_broker import PaperBroker, PaperReconciliation
from autobit.indicators.trend import compute_trend_indicators
from autobit.paper.health import HealthMonitor, HealthStage
from autobit.paper.scheduler import PaperScheduler, latest_completed_end, next_cycle_at
from autobit.paper.service import (
    CycleResult,
    CycleStatus,
    PaperService,
    _ValidatedRiskChain,
    _completed_cycle_ends,
    _has_completed_cycle,
    _validated_cycle_attempts,
    _validate_health_and_risk_chain,
    _validate_operational_alert_chain,
)
from autobit.persistence.sqlite_store import (
    PaperSnapshot,
    SQLiteStore,
    StoreError,
    StoredEvent,
)
from autobit.reporting.reports import SCHEMA_VERSION, write_report_bundle
from autobit.reporting.validation import (
    BenchmarkComparisonRow,
    CostScenarioRow,
    DiagnosticPathEvidence,
    FoldMetricRow,
    FoldTrainingSharpe,
    MetricSnapshot,
    OOSEquityRow,
    RegimeMetricRow,
    RunFailureEvidence,
    TrialMetricRow,
    ValidationDiagnosticEvidence,
    ValidationReportInput,
    write_validation_bundle,
)
from autobit.validation.models import (
    CostScenario,
    FoldWindow,
    StitchedOOSResult,
    WalkForwardConfig,
    WalkForwardResult,
    WalkForwardRun,
)
from autobit.validation.overfit import cpcv_splits
from autobit.validation.runner import run_walk_forward, validate_walk_forward_frame
from autobit.validation.splits import build_rolling_folds
from autobit.validation.trials import registered_cost_scenarios, registered_trials


_FOUR_HOURS = pd.Timedelta(hours=4)
_TRIAL_IDS = tuple(trial.trial_id for trial in registered_trials())
_COST_IDS = tuple(cost.cost_id for cost in registered_cost_scenarios())
_PAPER_HISTORY_BARS = 601
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class _StatusEquity:
    value: float
    as_of_utc: datetime | None
    status: str
    provenance: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autobit", description="KRW-BTC research tools")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("data-download", help="Download public candles")
    download.add_argument("--output", type=Path, required=True, help="Evidence directory")
    download.add_argument("--end-utc", type=_utc_end, required=True, help="UTC range end")
    download.add_argument("--years", type=_seven_years, default=7, help="History span (default: 7)")
    download.set_defaults(handler=_run_data_download)

    quality = commands.add_parser("data-quality", help="Validate public candles")
    quality.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON, CSV, or completed evidence directory",
    )
    quality.add_argument("--output", type=Path, required=True, help="Result directory")
    quality.set_defaults(handler=_run_data_quality)

    simulation = commands.add_parser("backtest", help="Run a historical simulation")
    simulation.add_argument(
        "--input", type=Path, required=True, help="Canonical processed CSV source"
    )
    simulation.add_argument("--output", type=Path, required=True, help="Report directory")
    simulation.add_argument(
        "--slippage",
        type=_slippage,
        default=CostConfig().slippage_rate,
        help="Slippage rate in [0, 1)",
    )
    simulation.set_defaults(handler=_run_backtest)

    validation = commands.add_parser(
        "walk-forward", help="Run offline rolling validation"
    )
    validation.add_argument(
        "--input", type=Path, required=True, help="Canonical processed CSV source"
    )
    validation.add_argument(
        "--output", type=Path, required=True, help="Validation report directory"
    )
    validation.add_argument(
        "--end-utc",
        type=_walk_forward_end,
        default=None,
        help="Exclusive aware UTC 4-hour boundary",
    )
    validation.set_defaults(handler=_run_walk_forward)

    for name, help_text, handler in (
        ("paper-once", "Process one normalized paper candle", _run_paper_once),
        ("paper-run", "Run normalized paper automation", _run_paper_run),
    ):
        paper = commands.add_parser(name, help=help_text)
        paper.add_argument("--db", type=Path, required=True, help="Normalized paper ledger")
        paper.add_argument("--data-dir", type=Path, required=True, help="Public candle evidence")
        paper.add_argument("--telegram-token-env", type=_environment_name, default=None)
        paper.add_argument("--telegram-chat-env", type=_environment_name, default=None)
        paper.set_defaults(handler=handler)

    status = commands.add_parser("paper-status", help="Read normalized paper state")
    status.add_argument("--db", type=Path, required=True, help="Existing normalized paper ledger")
    status.set_defaults(handler=_run_paper_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return int(arguments.handler(arguments))


def _run_data_download(arguments: argparse.Namespace) -> int:
    config = DataConfig(years=arguments.years)
    end = pd.Timestamp(arguments.end_utc)
    start = end - pd.DateOffset(years=arguments.years)
    with httpx.Client() as http_client:
        client = UpbitPublicClient(http_client, config)
        collection = collect_evidence_range(
            client,
            start_utc=_format_utc(start),
            end_utc=_format_utc(end),
            evidence_root=arguments.output,
            config=config,
        )

    frame = collection.frame
    evidence = collection.evidence
    if evidence.collection_snapshot_path is None or evidence.collection_snapshot_sha256 is None:
        raise ValueError("completed collection evidence is missing its snapshot")
    exchange_rules = ExchangeRulesConfig()
    rules_bytes = _canonical_json_bytes(asdict(exchange_rules))
    _atomic_write(arguments.output / "exchange-rules.json", rules_bytes)
    _atomic_write(
        arguments.output / "manifest.json",
        _canonical_json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "market": config.market,
                "years": arguments.years,
                "start_utc": _format_utc(start),
                "end_utc": _format_utc(end),
                "source_url": evidence.source_url,
                "raw_pages": [
                    {
                        "path": page.path.name,
                        "sha256": page.sha256,
                        "request_to_utc": page.request_to_utc,
                        "oldest_timestamp_utc": page.oldest_timestamp_utc,
                        "row_count": page.row_count,
                    }
                    for page in evidence.pages
                ],
                "collection_snapshot": evidence.collection_snapshot_path.name,
                "collection_snapshot_sha256": evidence.collection_snapshot_sha256,
                "exchange_rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
                "config_sha256": evidence.config_sha256,
                "row_count": len(frame),
            }
        ),
    )
    return 0


def _run_data_quality(arguments: argparse.Namespace) -> int:
    raw = _read_ohlcv(arguments.input)
    result = canonicalize_ohlcv(raw, datetime.now(timezone.utc))
    arguments.output.mkdir(parents=True, exist_ok=True)
    processed = result.frame.to_csv(index=True, index_label="timestamp").encode("utf-8")
    _atomic_write(arguments.output / "processed.csv", processed)
    _atomic_write(
        arguments.output / "quality.json",
        _canonical_json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "processed_sha256": hashlib.sha256(processed).hexdigest(),
                "quality": asdict(result.report),
            }
        ),
    )
    return 0


def _run_backtest(arguments: argparse.Namespace) -> int:
    config = BacktestConfig(
        costs=CostConfig(
            fee_rate=CostConfig().fee_rate,
            slippage_rate=arguments.slippage,
        )
    )
    processed = _read_processed_csv(arguments.input)
    quality = _load_quality_provenance(arguments.input, len(processed))
    frame = compute_trend_indicators(processed, config.strategy)
    result = run_backtest(frame, config)
    benchmark = run_buy_and_hold(frame, config.costs)
    metrics = calculate_metrics(
        equity_curve=result.equity_curve,
        trades=result.trades,
        orders=result.orders,
        periods_per_year=2190,
        total_fees=result.total_fees,
        total_slippage=result.total_slippage,
    )
    write_report_bundle(
        arguments.output,
        result=result,
        metrics=metrics,
        benchmark=benchmark,
        quality=quality,
        config=config,
        data_path=arguments.input,
        source_root=Path(__file__).resolve().parent,
    )
    return 0


def _run_walk_forward(arguments: argparse.Namespace) -> int:
    """Run the public-data-only validation pipeline with a process exit code."""
    try:
        frame = _read_walk_forward_csv(arguments.input)
        frame = _apply_walk_forward_end(frame, arguments.end_utc)
        folds = tuple(build_rolling_folds(frame.index, _walk_forward_config()))
        if len(folds) < 2:
            raise ValueError("walk-forward validation requires at least two complete folds")
        _preflight_validation_output(arguments.output)
        result = run_walk_forward(frame, folds)
        report = _validation_report(frame, folds, result)
        write_validation_bundle(arguments.output, report)
    except Exception as error:
        print(f"walk-forward failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


class _SystemClock:
    def now(self) -> datetime:
        return _utc_now()


class _SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _CandleSchemaError(ValueError):
    pass


class _CandleTimestampError(ValueError):
    pass


class _CandleLatestError(ValueError):
    pass


class _PublicPaperCandleSource:
    """Durable public-only evidence bound to one exclusive completed end."""

    def __init__(
        self,
        data_dir: Path,
        *,
        client: UpbitPublicClient | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._client = client

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        end = _paper_end(end_utc)
        if self._data_dir.exists() and (
            not self._data_dir.is_dir() or self._data_dir.is_symlink()
        ):
            raise ValueError("paper data directory must be a real directory")
        start = end - timedelta(hours=4 * _PAPER_HISTORY_BARS)
        identity = hashlib.sha256(_format_utc(end).encode("utf-8")).hexdigest()
        evidence_root = self._data_dir / "KRW-BTC-240" / identity
        if evidence_root.exists() and (
            not evidence_root.is_dir() or evidence_root.is_symlink()
        ):
            raise ValueError("paper candle evidence path is invalid")
        paper_config = DataConfig()
        if self._client is None:
            with httpx.Client() as http_client:
                client = UpbitPublicClient(http_client, paper_config)
                _validate_checkpointless_paper_evidence(
                    evidence_root,
                    source_url=client.source_url,
                    start=_format_utc(start),
                    end=_format_utc(end),
                    config=paper_config,
                )
                collected = collect_evidence_range(
                    client,
                    _format_utc(start),
                    _format_utc(end),
                    evidence_root=evidence_root,
                    config=paper_config,
                )
        else:
            _validate_checkpointless_paper_evidence(
                evidence_root,
                source_url=self._client.source_url,
                start=_format_utc(start),
                end=_format_utc(end),
                config=paper_config,
            )
            collected = collect_evidence_range(
                self._client,
                _format_utc(start),
                _format_utc(end),
                evidence_root=evidence_root,
                config=paper_config,
            )
        raw = collected.frame
        if "market" not in raw or not raw["market"].eq("KRW-BTC").all():
            raise _CandleSchemaError("public candle market evidence is invalid")
        renamed = raw.rename(
            columns={
                "candle_date_time_utc": "timestamp",
                "opening_price": "open",
                "high_price": "high",
                "low_price": "low",
                "trade_price": "close",
                "candle_acc_trade_volume": "volume",
            }
        )
        if "timestamp" not in renamed:
            raise _CandleSchemaError("public candles are missing timestamps")
        frame = renamed.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
        try:
            frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
        except (TypeError, ValueError) as error:
            raise _CandleTimestampError("public candle timestamps are invalid") from error
        return _validated_paper_frame(frame, end)


class _ObservedCandleSource:
    """Persist one health observation before PaperService may decide an entry."""

    def __init__(
        self,
        source: object,
        store: SQLiteStore,
        clock: object,
        notifier: Notifier | None,
    ) -> None:
        self._source = source
        self._store = store
        self._clock = clock
        self._notifier = notifier
        self._prepared: dict[datetime, pd.DataFrame] = {}

    def prepare(self, end_utc: datetime) -> pd.DataFrame:
        end = _paper_end(end_utc)
        observed = _clock_now(self._clock)
        monitor = HealthMonitor.from_store(self._store)
        outcome = "success"
        try:
            loader = getattr(self._source, "load_completed_candles")
            frame = _validated_paper_frame(loader(end), end)
        except Exception as error:
            if isinstance(error, _CandleTimestampError):
                monitor.record_timestamp_check(False, observed)
                outcome = "timestamp"
            elif isinstance(error, (_CandleSchemaError, _CandleLatestError, ValueError, TypeError)):
                if isinstance(error, _CandleLatestError):
                    monitor.record_candle_check(
                        expected_end=end,
                        observed_end=None,
                        checked_at=observed,
                    )
                    outcome = "candle"
                else:
                    monitor.record_schema_check(False, observed)
                    outcome = "schema"
            else:
                monitor.record_api_failure(observed)
                outcome = "failure"
            event = self._persist(monitor, end, observed, outcome)
            self._notify(event)
            raise

        snapshot = monitor.snapshot()
        if not snapshot.schema_valid:
            monitor.record_schema_check(True, observed)
        if not snapshot.timestamps_monotonic:
            monitor.record_timestamp_check(True, observed)
        if not snapshot.latest_candle_valid:
            monitor.record_candle_check(
                expected_end=end,
                observed_end=end,
                checked_at=observed,
            )
        monitor.record_api_success(observed)
        event = self._persist(monitor, end, observed, outcome)
        self._notify(event)
        self._prepared[end] = frame
        return frame.copy(deep=True)

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        end = _paper_end(end_utc)
        frame = self._prepared.pop(end, None)
        if frame is None:
            frame = self.prepare(end)
            self._prepared.pop(end, None)
        return frame.copy(deep=True)

    def _persist(
        self,
        monitor: HealthMonitor,
        end: datetime,
        observed: datetime,
        outcome: str,
    ) -> StoredEvent:
        event_id = f"health:public:{_format_utc(end)}:{_format_utc(observed)}:{outcome}"
        monitor.persist(
            self._store,
            event_id=event_id,
            logical_at=_monotonic_event_time(self._store, end - timedelta(hours=4)),
        )
        return _event_by_id(self._store, event_id)

    def _notify(self, event: StoredEvent) -> None:
        if self._notifier is None:
            return
        try:
            deliver_alert_once(
                self._store,
                self._notifier,
                source_event_id=event.event_id,
                source_event_type=event.event_type,
                source_payload=event.payload,
                logical_at=event.occurred_at_utc,
            )
        except Exception:
            return


class _PaperApplication:
    """Production wiring around the proven one-candle service and scheduler."""

    def __init__(
        self,
        *,
        source: object,
        store: SQLiteStore,
        clock: object,
        sleeper: object,
        notifier: Notifier | None,
        costs: CostConfig = CostConfig(),
        lease_owner: str,
        lease_token: str,
    ) -> None:
        self._store = store
        self._clock = clock
        self._sleeper = sleeper
        self._notifier = notifier
        self._costs = costs
        self._broker = PaperBroker(store, costs)
        self._source = _ObservedCandleSource(source, store, clock, notifier)
        self._retry_delay_applied = False
        service = PaperService(
            source=self._source,
            store=store,
            broker=self._broker,
            clock=clock,
            lease_owner=lease_owner,
            lease_token=lease_token,
            costs=costs,
        )
        self._service = service

    def run_once(self) -> CycleResult:
        self._retry_delay_applied = False
        try:
            self._preflight()
        except Exception:
            self._sleep_failsafe_retry()
            raise
        now = _clock_now(self._clock)
        current_end = latest_completed_end(now)
        action = HealthMonitor.from_store(self._store).current_action()
        if action.stage is HealthStage.HALTED and self._is_completed(current_end):
            if action.retry_delay_seconds > 0:
                self._sleeper.sleep(float(action.retry_delay_seconds))
                self._retry_delay_applied = True
            self._source.prepare(current_end)
            self._record_reconciliation(current_end, ())
            refreshed = HealthMonitor.from_store(self._store).current_action()
            return CycleResult(
                CycleStatus.ALREADY_PROCESSED,
                current_end,
                reasons=refreshed.reasons,
                equity=self._broker.reconcile().equity,
            )

        scheduler = PaperScheduler(
            self._service,
            self._clock,
            self._sleeper,
            retry_delay_provider=self._retry_delay,
        )
        try:
            result = scheduler.run_once()
        except Exception:
            self._retry_delay_applied = True
            raise
        if result.status in {CycleStatus.PROCESSED, CycleStatus.ALREADY_PROCESSED}:
            try:
                self._record_reconciliation(result.end_utc, result.filled_order_ids)
                if result.status is CycleStatus.PROCESSED:
                    self._promote_after_reduced_cycle(result.end_utc)
                self._notify_cycle(result.end_utc)
            except Exception:
                self._sleep_failsafe_retry()
                raise
        return result

    @property
    def retry_delay_applied(self) -> bool:
        return self._retry_delay_applied

    def _preflight(self) -> None:
        self._service.oldest_required_end(
            latest_completed_end(_clock_now(self._clock))
        )

    def _sleep_failsafe_retry(self) -> None:
        try:
            delay = self._retry_delay()
        except Exception:
            delay = 300.0
        self._sleeper.sleep(delay)
        self._retry_delay_applied = True

    def _retry_delay(self) -> float:
        delay = HealthMonitor.from_store(self._store).current_action().retry_delay_seconds
        return float(max(1, delay))

    def _is_completed(self, end: datetime) -> bool:
        snapshot = self._store.replay_state()
        return _has_completed_cycle(
            snapshot,
            f"cycle:{_format_utc(end)}",
            end,
            self._broker,
        )

    def _record_reconciliation(
        self,
        end: datetime,
        filled_order_ids: tuple[str, ...],
    ) -> None:
        health_events = tuple(
            event
            for event in self._store.replay_state().event_evidence
            if event.event_type == "HEALTH_STATE"
        )
        if not health_events:
            raise StoreError("reconciliation requires durable health evidence")
        source_health = health_events[-1]
        binding = hashlib.sha256(source_health.event_id.encode("utf-8")).hexdigest()
        event_id = (
            f"health:reconcile:{_format_utc(end)}:"
            f"{source_health.sequence}:{binding}"
        )
        if _find_event(self._store, event_id) is not None:
            return
        monitor = HealthMonitor.from_store(self._store)
        reconciliation = self._broker.reconcile()
        observed = _clock_now(self._clock)
        if monitor.snapshot().unresolved_orders != 0:
            monitor.set_unresolved_orders(0, observed)
        if not monitor.snapshot().ledger_matches:
            monitor.record_ledger_check(
                stored_cash=reconciliation.cash,
                actual_cash=reconciliation.cash,
                stored_btc=reconciliation.btc_quantity,
                actual_btc=reconciliation.btc_quantity,
                at=observed,
            )
        fills = {fill.order_id: fill for fill in reconciliation.fills}
        for order_id in filled_order_ids:
            fill = fills.get(order_id)
            if fill is None:
                monitor.set_unresolved_orders(1, observed)
                break
            multiplier = 1.0 + self._costs.slippage_rate if fill.side == "BUY" else 1.0 - self._costs.slippage_rate
            monitor.record_fill_check(
                expected_price=fill.reference_price * multiplier,
                actual_price=fill.fill_price,
                at=observed,
            )
        monitor.persist(
            self._store,
            event_id=event_id,
            logical_at=_monotonic_event_time(self._store, end),
        )

    def _promote_after_reduced_cycle(self, end: datetime) -> None:
        monitor = HealthMonitor.from_store(self._store)
        if monitor.current_action().stage is not HealthStage.REDUCED:
            return
        event_id = f"health:recovery:{_format_utc(end)}"
        if _find_event(self._store, event_id) is not None:
            return
        monitor.record_recovery_cycle_success(_clock_now(self._clock))
        monitor.persist(
            self._store,
            event_id=event_id,
            logical_at=_monotonic_event_time(self._store, end),
        )

    def _notify_cycle(self, end: datetime) -> None:
        if self._notifier is None:
            return
        event = _event_by_id(self._store, f"cycle:{_format_utc(end)}")
        try:
            deliver_alert_once(
                self._store,
                self._notifier,
                source_event_id=event.event_id,
                source_event_type=event.event_type,
                source_payload=event.payload,
                logical_at=event.occurred_at_utc,
            )
        except Exception:
            return


def _run_paper_once(arguments: argparse.Namespace) -> int:
    store: SQLiteStore | None = None
    try:
        notifier = _resolve_notifier(arguments)
        store = SQLiteStore(arguments.db)
        store.initialize()
        application = _new_paper_application(arguments, store, notifier)
        result = application.run_once()
        print(_canonical_output(_cycle_output(result)))
        return 0 if result.status in {CycleStatus.PROCESSED, CycleStatus.ALREADY_PROCESSED} else 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("paper-once failed: SAFE_OPERATION_ERROR", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


def _run_paper_run(arguments: argparse.Namespace) -> int:
    store: SQLiteStore | None = None
    try:
        notifier = _resolve_notifier(arguments)
        store = SQLiteStore(arguments.db)
        store.initialize()
        application = _new_paper_application(arguments, store, notifier)
        while True:
            try:
                result = application.run_once()
            except KeyboardInterrupt:
                return 130
            except Exception:
                if getattr(application, "retry_delay_applied", False) is not True:
                    time.sleep(300.0)
                print("paper-run retrying: RECOVERABLE_OPERATION_ERROR", file=sys.stderr)
                continue
            print(_canonical_output(_cycle_output(result)))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("paper-run failed: SAFE_CONFIGURATION_ERROR", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


def _status_equity(
    snapshot: PaperSnapshot,
    chain: _ValidatedRiskChain,
    reconciliation: PaperReconciliation,
    completed: frozenset[datetime],
) -> _StatusEquity:
    if reconciliation.fills:
        fallback = _StatusEquity(
            value=reconciliation.equity,
            as_of_utc=max(fill.fill_time for fill in reconciliation.fills),
            status="STALE",
            provenance="LAST_FILL_BROKER_EQUITY",
        )
    else:
        fallback = _StatusEquity(
            value=reconciliation.equity,
            as_of_utc=None,
            status=("STALE" if snapshot.event_evidence else "UNAVAILABLE"),
            provenance="INITIAL_EQUITY",
        )
    if not completed:
        return fallback

    latest_end = max(completed)
    matches = tuple(
        event
        for event in snapshot.event_evidence
        if event.event_type == "PAPER_CYCLE"
        and event.occurred_at_utc == latest_end
    )
    if len(matches) != 1:
        raise StoreError("latest completed cycle evidence is ambiguous")
    cycle = matches[0]
    expected_risk_at = latest_end - timedelta(hours=4)
    risk = chain.latest_base
    risk_event = chain.latest_base_event
    if risk.last_risk_at != expected_risk_at or risk_event is None:
        return fallback
    if cycle.payload["status"] != CycleStatus.PROCESSED.value:
        raise StoreError("unsafe cycle contradicts a completed-close risk mark")
    if risk_event.sequence >= cycle.sequence:
        raise StoreError("completed-close risk mark is not prior to its terminal cycle")
    reconciled_fill_ids = frozenset(fill.fill_id for fill in reconciliation.fills)
    if any(
        event.event_type == "FILL"
        and event.event_id in reconciled_fill_ids
        and event.sequence > cycle.sequence
        for event in snapshot.event_evidence
    ):
        return fallback

    equity = chain.projection.last_equity
    cash = reconciliation.cash
    quantity = reconciliation.btc_quantity
    if quantity == 0.0:
        if not math.isclose(equity, cash, rel_tol=0.0, abs_tol=1e-12):
            raise StoreError("completed-close equity contradicts flat broker cash")
    else:
        implied_mark = (equity - cash) / quantity
        if not math.isfinite(implied_mark) or implied_mark <= 0.0:
            raise StoreError("completed-close equity contradicts broker inventory")
    return _StatusEquity(
        value=equity,
        as_of_utc=latest_end,
        status="CURRENT",
        provenance="COMPLETED_CLOSE_MTM",
    )


def _run_paper_status(arguments: argparse.Namespace) -> int:
    store: SQLiteStore | None = None
    try:
        store = SQLiteStore.open_read_only(arguments.db)
        snapshot = store.replay_state()
        chain = _validate_health_and_risk_chain(snapshot)
        _validate_operational_alert_chain(snapshot.event_evidence)
        broker = PaperBroker(store)
        reconciliation = broker.reconcile()
        health = HealthMonitor.from_store(store).current_action()
        now = _utc_now()
        completed = _completed_cycle_ends(
            snapshot,
            broker,
            reconciliation,
            latest_completed_end(now),
        )
        _validated_cycle_attempts(
            snapshot,
            completed,
            latest_completed_end(now),
        )
        equity = _status_equity(snapshot, chain, reconciliation, completed)
        stop = reconciliation.active_stop
        combined = tuple(
            dict.fromkeys((*chain.projection.decision.reasons, *health.reasons))
        )
        payload = {
            "active_stop": (
                None
                if stop is None
                else {
                    "active_after_utc": _format_utc(stop.active_after_utc),
                    "reason": stop.reason,
                    "stop_price": stop.stop_price,
                }
            ),
            "breaker_health_reasons": list(combined),
            "btc_quantity": reconciliation.btc_quantity,
            "equity_as_of_utc": (
                _format_utc(equity.as_of_utc)
                if equity.as_of_utc is not None
                else None
            ),
            "equity_provenance": equity.provenance,
            "equity_status": equity.status,
            "health_recovery_progress": {
                "remaining_gates": list(health.progress.remaining_gates),
                "successes_observed": health.progress.successes_observed,
                "successes_required": health.progress.successes_required,
            },
            "health_stage": health.stage.value,
            "last_completed_candle_utc": (
                _format_utc(max(completed)) if completed else None
            ),
            "market": snapshot.market,
            "mode": "normalized-paper",
            "next_scheduled_utc": _format_utc(next_cycle_at(now)),
            "normalized_cash": reconciliation.cash,
            "normalized_equity": equity.value,
            "pending_orders": [order.order_id for order in reconciliation.active_orders],
            "position_state": reconciliation.position_state.value,
        }
        print(_canonical_output(payload))
        return 0
    except Exception:
        print("paper-status failed: INVALID_OR_MISSING_LEDGER", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


def _new_paper_application(
    arguments: argparse.Namespace,
    store: SQLiteStore,
    notifier: Notifier | None,
) -> _PaperApplication:
    owner = f"paper-cli-{os.getpid()}"
    return _PaperApplication(
        source=_paper_source_factory(arguments.data_dir),
        store=store,
        clock=_SystemClock(),
        sleeper=_SystemSleeper(),
        notifier=notifier,
        lease_owner=owner,
        lease_token=f"{owner}-{uuid4().hex}",
    )


def _resolve_notifier(arguments: argparse.Namespace) -> Notifier | None:
    token_name = arguments.telegram_token_env
    chat_name = arguments.telegram_chat_env
    if (token_name is None) != (chat_name is None):
        raise ValueError("both telegram environment names are required")
    if token_name is None:
        return None
    token = os.environ.get(token_name, "")
    chat = os.environ.get(chat_name, "")
    if not token or not chat:
        raise ValueError("telegram environment values must be non-empty")
    return _telegram_notifier_factory(token, chat)


def _paper_source_factory(data_dir: Path) -> object:
    return _PublicPaperCandleSource(data_dir)


def _telegram_notifier_factory(token: str, chat: str) -> Notifier:
    return SafeNotifier(TelegramNotifier(token, chat, http_client=httpx.Client()))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_checkpointless_paper_evidence(
    root: Path,
    *,
    source_url: str,
    start: str,
    end: str,
    config: DataConfig,
) -> None:
    """Reject every invalid artifact before collector recovery can ignore it."""
    if not root.exists() or (root / "checkpoint.json").exists():
        return
    entries = tuple(root.iterdir())
    if not entries:
        return
    identity = data_storage._collection_identity(
        source_url=source_url,
        start_utc=start,
        end_utc=end,
        config=config,
    )
    snapshots = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("paper evidence namespace contains an invalid artifact")
        snapshot_hash = data_storage._snapshot_hash_from_name(entry.name)
        if snapshot_hash is not None:
            snapshot = data_storage._read_hashed_json(
                entry,
                snapshot_hash,
                "paper orphan collection snapshot evidence",
            )
            if not isinstance(snapshot, dict):
                raise ValueError("paper orphan collection snapshot evidence is malformed")
            snapshots.append(
                data_storage._evidence_from_state_payload(
                    root,
                    snapshot,
                    identity,
                    snapshot_path=entry,
                    snapshot_hash=snapshot_hash,
                    label="paper orphan collection snapshot evidence",
                )
            )
            continue
        page_match = re.fullmatch(r"page-([0-9a-f]{64})\.json", entry.name)
        if page_match is None:
            raise ValueError("paper evidence namespace contains unknown evidence")
        page = data_storage._read_hashed_json(
            entry,
            page_match.group(1),
            "paper orphan page evidence",
        )
        if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
            raise ValueError("paper orphan page evidence is malformed")

    if snapshots:
        tip = max(snapshots, key=lambda evidence: len(evidence.pages))
        if any(
            not data_storage._is_snapshot_prefix(candidate, tip)
            for candidate in snapshots
        ):
            raise ValueError("paper collection evidence has ambiguous snapshot chains")


def _validated_paper_frame(frame: object, end: datetime) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise _CandleSchemaError("completed candle source must return a pandas DataFrame")
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise _CandleSchemaError("completed candle source schema is invalid")
    try:
        index = pd.DatetimeIndex(frame.index)
    except (TypeError, ValueError) as error:
        raise _CandleTimestampError("completed candle timestamps are invalid") from error
    if index.tz is None or any(timestamp.utcoffset() != timedelta(0) for timestamp in index):
        raise _CandleTimestampError("completed candle timestamps must use UTC")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise _CandleTimestampError("completed candle timestamps must be unique and increasing")
    if any(timestamp.minute or timestamp.second or timestamp.microsecond or timestamp.hour % 4 for timestamp in index):
        raise _CandleTimestampError("completed candle timestamps must align to four hours")
    if any(timestamp.to_pydatetime() >= end for timestamp in index):
        raise _CandleLatestError("completed candle source crossed its exclusive end")
    expected_latest = end - timedelta(hours=4)
    expected_index = pd.date_range(
        end=pd.Timestamp(expected_latest),
        periods=_PAPER_HISTORY_BARS,
        freq="4h",
        tz="UTC",
    )
    if len(index) != _PAPER_HISTORY_BARS or not index.equals(expected_index):
        raise _CandleLatestError(
            "paper candle history must contain exactly 601 contiguous completed bars"
        )
    try:
        canonical = canonicalize_ohlcv(frame, end).frame
    except ValueError as error:
        raise _CandleSchemaError("completed candle values are invalid") from error
    if canonical.empty or canonical.index[-1].to_pydatetime() != expected_latest:
        raise _CandleLatestError("exact latest completed candle is missing")
    if len(canonical) != _PAPER_HISTORY_BARS or not canonical.index.equals(expected_index):
        raise _CandleLatestError("600-bar warmup plus execution candle is incomplete")
    if canonical.loc[:, ["open", "high", "low", "close", "volume"]].isna().any().any():
        raise _CandleSchemaError("paper candle history contains invalid values")
    return canonical.loc[:, ["open", "high", "low", "close", "volume"]].copy(deep=True)


def _paper_end(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("paper candle end must be timezone-aware UTC")
    result = value.astimezone(timezone.utc)
    if result.minute or result.second or result.microsecond or result.hour % 4:
        raise ValueError("paper candle end must align to four hours")
    return result


def _clock_now(clock: object) -> datetime:
    value = getattr(clock, "now")()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("paper clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _monotonic_event_time(store: SQLiteStore, logical_at: datetime) -> datetime:
    evidence = store.replay_state().event_evidence
    return max(logical_at, evidence[-1].occurred_at_utc) if evidence else logical_at


def _find_event(store: SQLiteStore, event_id: str) -> StoredEvent | None:
    matches = tuple(
        event for event in store.replay_state().event_evidence if event.event_id == event_id
    )
    if len(matches) > 1:
        raise StoreError("event identity is duplicated")
    return matches[0] if matches else None


def _event_by_id(store: SQLiteStore, event_id: str) -> StoredEvent:
    event = _find_event(store, event_id)
    if event is None:
        raise StoreError("required durable event is missing")
    return event


def _cycle_output(result: CycleResult) -> dict[str, object]:
    return {
        "created_order_ids": list(result.created_order_ids),
        "end_utc": _format_utc(result.end_utc),
        "filled_order_ids": list(result.filled_order_ids),
        "normalized_equity": result.equity,
        "reason_codes": list(result.reasons),
        "status": result.status.value,
    }


def _canonical_output(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _environment_name(value: str) -> str:
    if not _ENVIRONMENT_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("environment variable name is invalid")
    return value


def _walk_forward_config() -> WalkForwardConfig:
    # Kept behind one seam so the CLI has a single canonical calendar policy.
    return WalkForwardConfig()


def _validation_report(
    frame: pd.DataFrame,
    folds: tuple[FoldWindow, ...],
    result: WalkForwardResult,
) -> ValidationReportInput:
    fold_ids = tuple(fold.fold_id for fold in folds)
    run_map = {
        (run.phase, run.fold_id, run.trial_id, run.cost_id): run
        for run in result.runs
    }
    stitched_map = {
        (item.trial_id, item.cost_id): item for item in result.stitched_oos
    }

    fold_rows: list[FoldMetricRow] = []
    fold_snapshots: dict[tuple[str, str, str], MetricSnapshot] = {}
    for fold in folds:
        for trial_id in _TRIAL_IDS:
            for cost_id in _COST_IDS:
                run = run_map[("OOS", fold.fold_id, trial_id, cost_id)]
                snapshot = (
                    _metric_snapshot(run.metrics, cost_id=cost_id)
                    if run.status == "COMPLETED" and run.metrics is not None
                    else None
                )
                if snapshot is not None:
                    fold_snapshots[(fold.fold_id, trial_id, cost_id)] = snapshot
                fold_rows.append(
                    FoldMetricRow(
                        fold_id=fold.fold_id,
                        trial_id=trial_id,
                        cost_id=cost_id,
                        status=run.status,
                        error=run.error,
                        metrics=snapshot,
                    )
                )

    trial_rows: list[TrialMetricRow] = []
    trial_snapshots: dict[tuple[str, str], MetricSnapshot] = {}
    for trial_id in _TRIAL_IDS:
        for cost_id in _COST_IDS:
            stitched = stitched_map[(trial_id, cost_id)]
            snapshot = None
            if stitched.status == "COMPLETE" and stitched.metrics is not None:
                fold_gross = tuple(
                    fold_snapshots[(fold.fold_id, trial_id, cost_id)].gross_return
                    for fold in folds
                )
                snapshot = _metric_snapshot(
                    stitched.metrics,
                    cost_id=cost_id,
                    gross_return=_compound(fold_gross),
                )
                trial_snapshots[(trial_id, cost_id)] = snapshot
            trial_rows.append(
                TrialMetricRow(
                    trial_id=trial_id,
                    cost_id=cost_id,
                    status=stitched.status,
                    error=None,
                    metrics=snapshot,
                )
            )

    failures = tuple(
        RunFailureEvidence(
            phase=run.phase,
            fold_id=run.fold_id,
            trial_id=run.trial_id,
            cost_id=run.cost_id,
            error=run.error or "unknown validation execution failure",
        )
        for run in result.runs
        if run.status == "FAILED"
    )
    costs = tuple(
        CostScenarioRow(
            cost_id=cost.cost_id,
            fee_rate=cost.fee_rate,
            slippage_rate=cost.slippage_rate,
            gross_return=trial_snapshots[("baseline", cost.cost_id)].gross_return,
            net_return=trial_snapshots[("baseline", cost.cost_id)].net_return,
            total_fees=trial_snapshots[("baseline", cost.cost_id)].total_fees,
            total_slippage=trial_snapshots[("baseline", cost.cost_id)].total_slippage,
        )
        for cost in registered_cost_scenarios()
    )
    baseline = stitched_map[("baseline", "baseline")]
    return ValidationReportInput(
        diagnostic_evidence=_diagnostic_evidence(folds, result, run_map, stitched_map),
        fold_ids=fold_ids,
        fold_metrics=tuple(fold_rows),
        trial_metrics=tuple(trial_rows),
        cost_scenarios=costs,
        regime_metrics=_regime_metrics(frame, result, baseline),
        benchmark_comparison=_benchmark_rows(frame, folds, trial_snapshots),
        oos_equity=tuple(
            OOSEquityRow(
                timestamp=point.timestamp,
                trial_id="baseline",
                cost_id="baseline",
                equity=point.equity,
            )
            for point in baseline.equity_curve
        ),
        run_failures=failures,
    )


def _metric_snapshot(
    metrics: PerformanceMetrics,
    *,
    cost_id: str,
    gross_return: float | None = None,
) -> MetricSnapshot:
    if gross_return is None:
        gross_return = metrics.total_return
        if cost_id != "zero":
            gross_return = (
                100.0 * (1.0 + metrics.total_return)
                + metrics.total_fees
                + metrics.total_slippage
            ) / 100.0 - 1.0
    return MetricSnapshot(
        gross_return=gross_return,
        net_return=metrics.total_return,
        annualized_return=metrics.annualized_return,
        sharpe=metrics.sharpe_ratio,
        sortino=metrics.sortino_ratio,
        calmar=metrics.calmar_ratio,
        max_drawdown=metrics.max_drawdown,
        max_drawdown_duration_bars=metrics.max_drawdown_duration_bars,
        profit_factor=metrics.profit_factor,
        expectancy=metrics.expectancy,
        win_rate=metrics.win_rate,
        average_win=metrics.average_win,
        average_loss=metrics.average_loss,
        average_win_loss_ratio=metrics.average_win_loss_ratio,
        trade_count=metrics.trade_count,
        mean_holding_bars=metrics.mean_holding_bars,
        median_holding_bars=metrics.median_holding_bars,
        exposure=metrics.exposure,
        turnover=metrics.turnover,
        total_fees=metrics.total_fees,
        total_slippage=metrics.total_slippage,
        cash_ratio=1.0 - metrics.exposure,
    )


def _diagnostic_evidence(
    folds: tuple[FoldWindow, ...],
    result: WalkForwardResult,
    run_map: dict[tuple[str, str, str, str], WalkForwardRun],
    stitched_map: dict[tuple[str, str], StitchedOOSResult],
) -> ValidationDiagnosticEvidence:
    train_sharpes: list[FoldTrainingSharpe] = []
    for fold in folds:
        run = run_map[("TRAIN", fold.fold_id, "baseline", "baseline")]
        if run.status != "COMPLETED" or run.metrics is None:
            raise ValueError("baseline/default TRAIN evidence must be complete")
        train_sharpes.append(
            FoldTrainingSharpe(fold_id=fold.fold_id, sharpe=run.metrics.sharpe_ratio)
        )

    baseline_failures = tuple(
        run for run in result.runs
        if run.cost_id == "baseline" and run.status == "FAILED"
    )
    if baseline_failures:
        return ValidationDiagnosticEvidence(
            status="INCOMPLETE",
            error=None,
            trial_ids=_TRIAL_IDS,
            paths=(),
            train_fold_sharpes=tuple(train_sharpes),
        )

    trial_returns: list[tuple[float, ...]] = []
    timestamps: tuple[datetime, ...] | None = None
    for trial_id in _TRIAL_IDS:
        stitched = stitched_map[(trial_id, "baseline")]
        if stitched.status != "COMPLETE":
            raise ValueError("complete diagnostics require every baseline-cost trial")
        current_times = tuple(point.timestamp for point in stitched.returns)
        if timestamps is None:
            timestamps = current_times
        elif current_times != timestamps:
            raise ValueError("diagnostic OOS returns must share exact timestamps")
        trial_returns.append(tuple(point.value for point in stitched.returns))
    observation_count = len(timestamps or ())
    if observation_count < 10:
        raise ValueError("diagnostics require at least ten aligned OOS returns")

    splits = cpcv_splits(
        n_observations=observation_count,
        n_groups=10,
        n_test_groups=2,
        embargo=30,
    )
    paths = tuple(
        DiagnosticPathEvidence(
            path_id=f"path-{index:03d}",
            test_group_ids=test_groups,
            in_sample_scores=tuple(
                _nonannualized_sharpe(tuple(values[position] for position in train))
                for values in trial_returns
            ),
            out_of_sample_scores=tuple(
                _nonannualized_sharpe(tuple(values[position] for position in test))
                for values in trial_returns
            ),
        )
        for index, ((train, test), test_groups) in enumerate(
            zip(splits, combinations(range(10), 2), strict=True)
        )
    )
    return ValidationDiagnosticEvidence(
        status="COMPLETE",
        error=None,
        trial_ids=_TRIAL_IDS,
        paths=paths,
        train_fold_sharpes=tuple(train_sharpes),
    )


def _nonannualized_sharpe(returns: tuple[float, ...]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return mean / math.sqrt(variance) if variance > 0.0 else 0.0


def _regime_metrics(
    frame: pd.DataFrame,
    result: WalkForwardResult,
    baseline: StitchedOOSResult,
) -> tuple[RegimeMetricRow, ...]:
    enriched = compute_trend_indicators(frame, StrategyConfig())
    ema = enriched["ema_200"]
    slope = ema.diff()
    regimes = pd.Series("sideways", index=enriched.index, dtype="object")
    regimes.loc[(enriched["close"] > ema) & (slope > 0.0)] = "rising"
    regimes.loc[(enriched["close"] < ema) & (slope < 0.0)] = "falling"

    returns_by_regime: dict[str, list[float]] = {
        "rising": [], "falling": [], "sideways": []
    }
    for point in baseline.returns:
        regime = str(regimes.loc[pd.Timestamp(point.timestamp)])
        returns_by_regime[regime].append(point.value)

    trades_by_regime = {"rising": 0, "falling": 0, "sideways": 0}
    for run in result.runs:
        if (
            run.phase == "OOS"
            and run.trial_id == "baseline"
            and run.cost_id == "baseline"
            and run.status == "COMPLETED"
            and run.result is not None
        ):
            for trade in run.result.trades:
                regime = str(regimes.loc[pd.Timestamp(trade.exit_time)])
                trades_by_regime[regime] += 1

    rows: list[RegimeMetricRow] = []
    for regime_id in ("rising", "falling", "sideways"):
        regime_returns = tuple(returns_by_regime[regime_id])
        curve = _equity_from_returns(regime_returns)
        rows.append(
            RegimeMetricRow(
                regime_id=regime_id,
                net_return=curve[-1] / curve[0] - 1.0,
                sharpe=_annualized_sharpe(regime_returns),
                max_drawdown=_maximum_drawdown(curve),
                trade_count=trades_by_regime[regime_id],
            )
        )
    return tuple(rows)


def _benchmark_rows(
    frame: pd.DataFrame,
    folds: tuple[FoldWindow, ...],
    trial_snapshots: dict[tuple[str, str], MetricSnapshot],
) -> tuple[BenchmarkComparisonRow, ...]:
    oos = frame.loc[
        (frame.index >= folds[0].test_start) & (frame.index < folds[-1].test_end)
    ].copy(deep=True)
    rows: list[BenchmarkComparisonRow] = []
    for cost in registered_cost_scenarios():
        benchmark_return, benchmark_drawdown = _continuous_benchmark(oos, cost)
        strategy = trial_snapshots[("baseline", cost.cost_id)]
        rows.append(
            BenchmarkComparisonRow(
                cost_id=cost.cost_id,
                strategy_net_return=strategy.net_return,
                buy_and_hold_net_return=benchmark_return,
                strategy_max_drawdown=strategy.max_drawdown,
                buy_and_hold_max_drawdown=benchmark_drawdown,
            )
        )
    return tuple(rows)


def _continuous_benchmark(
    frame: pd.DataFrame, cost: CostScenario
) -> tuple[float, float]:
    """Hold one position across every unavailable region without retrading."""
    available = frame.loc[:, ("open", "high", "low", "close", "volume")].notna().all(axis=1)
    observed = frame.loc[available].copy(deep=True)
    if observed.empty:
        return 0.0, 0.0
    costs = CostConfig(fee_rate=cost.fee_rate, slippage_rate=cost.slippage_rate)
    benchmark = run_buy_and_hold(
        observed,
        costs,
        enter_at_first_open=True,
    )
    equity = _benchmark_equity(observed, benchmark, cost)
    return equity[-1] / 100.0 - 1.0, _maximum_drawdown(equity)


def _benchmark_equity(
    frame: pd.DataFrame, benchmark: BuyAndHoldResult, cost: CostScenario
) -> tuple[float, ...]:
    if benchmark.entry_time is None or benchmark.exit_time is None:
        return (100.0,) * max(1, len(frame))
    cash = max(
        0.0,
        benchmark.initial_equity
        - benchmark.quantity * float(benchmark.entry_price)
        - benchmark.entry_fee,
    )
    values: list[float] = []
    for timestamp, close in frame["close"].items():
        moment = pd.Timestamp(timestamp).to_pydatetime()
        if moment < benchmark.entry_time:
            values.append(benchmark.initial_equity)
        elif moment >= benchmark.exit_time:
            values.append(benchmark.final_equity)
        else:
            liquidation_price = float(close) * (1.0 - cost.slippage_rate)
            liquidation_notional = benchmark.quantity * liquidation_price
            values.append(cash + liquidation_notional * (1.0 - cost.fee_rate))
    return tuple(values)


def _equity_from_returns(returns: tuple[float, ...]) -> tuple[float, ...]:
    values = [100.0]
    for value in returns:
        values.append(values[-1] * (1.0 + value))
    return tuple(values)


def _annualized_sharpe(returns: tuple[float, ...]) -> float:
    if not returns:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / len(returns)
    return mean / math.sqrt(variance) * math.sqrt(2190) if variance > 0.0 else 0.0


def _maximum_drawdown(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    maximum = 0.0
    for value in values[1:]:
        if value >= peak:
            peak = value
        elif peak > 0.0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _compound(returns: tuple[float, ...]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


def _read_walk_forward_csv(path: Path) -> pd.DataFrame:
    if not isinstance(path, Path) or path.suffix.lower() != ".csv" or not path.is_file():
        raise ValueError("walk-forward input must be an existing processed CSV file")
    frame = pd.read_csv(path)
    if "timestamp" not in frame:
        raise ValueError("processed CSV must contain a timestamp column")
    raw_timestamps = frame.pop("timestamp")
    try:
        parsed = pd.to_datetime(raw_timestamps, errors="raise")
    except (TypeError, ValueError) as error:
        raise ValueError("processed timestamps must be aware UTC values") from error
    index = pd.DatetimeIndex(parsed)
    if index.tz is None or str(index.tz) != "UTC":
        raise ValueError("processed timestamps must use canonical UTC")
    frame.index = index
    return _validated_walk_forward_frame(frame)


def _validated_walk_forward_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return validate_walk_forward_frame(frame)


def _apply_walk_forward_end(
    frame: pd.DataFrame, end_utc: str | None
) -> pd.DataFrame:
    if end_utc is None:
        return frame
    end = pd.Timestamp(end_utc)
    coverage_end = frame.index[-1] + _FOUR_HOURS
    if end > coverage_end:
        raise ValueError("end-utc exceeds completed input coverage")
    filtered = frame.loc[frame.index < end].copy(deep=True)
    if filtered.empty:
        raise ValueError("end-utc excludes all input candles")
    return filtered


def _preflight_validation_output(output_dir: Path) -> None:
    """Create only missing parents after rejecting link and collision hazards."""
    if not isinstance(output_dir, Path):
        raise ValueError("walk-forward output must be a Path")
    output = output_dir.absolute()
    if output.exists() or _is_link_or_junction(output):
        raise ValueError("validation report output already exists or is a link/junction")
    _validate_real_directory_chain(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_real_directory_chain(output.parent)


def _validate_real_directory_chain(path: Path) -> None:
    current = path
    while True:
        if _is_link_or_junction(current):
            raise ValueError("validation report parent must not contain a link or junction")
        if current.exists() and not current.is_dir():
            raise ValueError("validation report parent components must be directories")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


def _read_ohlcv(path: Path) -> pd.DataFrame:
    if path.is_dir():
        frame = load_completed_evidence_frame(path)
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON input must be a list of candle objects")
        frame = pd.DataFrame(payload)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError("input must be JSON or CSV")

    rename = {
        "candle_date_time_utc": "timestamp",
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
    }
    frame = frame.rename(columns=rename)
    if "timestamp" not in frame:
        raise ValueError("input must contain a timestamp column")
    frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
    return frame


def _read_processed_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame:
        raise ValueError("processed CSV must contain a timestamp column")
    frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
    return frame


def _load_quality_provenance(processed_path: Path, row_count: int) -> QualityReport:
    sidecar = processed_path.with_name("quality.json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "quality provenance sidecar is missing or malformed"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("quality provenance schema_version is invalid")
    expected_hash = payload.get("processed_sha256")
    actual_hash = hashlib.sha256(processed_path.read_bytes()).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise ValueError("quality provenance does not match processed data bytes")
    quality = payload.get("quality")
    expected_fields = tuple(field.name for field in fields(QualityReport))
    if not isinstance(quality, dict) or set(quality) != set(expected_fields):
        raise ValueError("quality provenance fields are invalid")
    values: dict[str, int] = {}
    for name in expected_fields:
        value = quality[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("quality provenance counts must be nonnegative integers")
        values[name] = value
    if values["total_bars"] != row_count:
        raise ValueError("quality provenance total_bars does not match processed data")
    return QualityReport(**values)


def _utc_end(value: str) -> str:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("end must be an aware UTC timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0.0:
        raise argparse.ArgumentTypeError("end must be an aware UTC timestamp")
    return _format_utc(timestamp)


def _walk_forward_end(value: str) -> str:
    formatted = _utc_end(value)
    timestamp = pd.Timestamp(formatted)
    if timestamp.value % _FOUR_HOURS.value != 0:
        raise argparse.ArgumentTypeError("end must align to a UTC 4-hour boundary")
    return formatted


def _seven_years(value: str) -> int:
    try:
        years = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("years must be 7") from error
    if years != 7:
        raise argparse.ArgumentTypeError("years must be 7")
    return years


def _slippage(value: str) -> float:
    try:
        rate = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("slippage must be finite and in [0, 1)") from error
    if not math.isfinite(rate) or not 0.0 <= rate < 1.0:
        raise argparse.ArgumentTypeError("slippage must be finite and in [0, 1)")
    return rate


def _format_utc(value: object) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())

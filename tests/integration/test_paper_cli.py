from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.cli import (
    _PaperApplication,
    _PublicPaperCandleSource,
    _validated_paper_frame,
    build_parser,
    main,
)
from autobit.config import CostConfig, DataConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.paper.health import HealthMonitor, HealthStage
from autobit.paper.service import (
    CycleStatus,
    PaperService,
    _RiskState,
    _risk_state_payload,
)
from autobit.persistence.sqlite_store import SQLiteStore


UTC = timezone.utc
END = datetime(2026, 5, 11, 4, tzinfo=UTC)


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Sleeper:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.value += timedelta(seconds=seconds)


class _FrameSource:
    def __init__(self, frames: list[pd.DataFrame | BaseException]) -> None:
        self.frames = iter(frames)
        self.calls: list[datetime] = []

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        self.calls.append(end_utc)
        value = next(self.frames)
        if isinstance(value, BaseException):
            raise value
        return value.copy()


def _history(end: datetime, bars: int = 601) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp(end) - pd.Timedelta(hours=4),
        periods=bars,
        freq="4h",
        tz="UTC",
    )
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )


def _breakout_history(end: datetime) -> pd.DataFrame:
    frame = _history(end)
    frame.iloc[-1, frame.columns.get_loc("high")] = 111.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 110.0
    return frame


def _raw_row(at: datetime) -> dict[str, object]:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": at.isoformat().replace("+00:00", "Z"),
        "opening_price": 100.0,
        "high_price": 101.0,
        "low_price": 99.0,
        "trade_price": 100.0,
        "candle_acc_trade_volume": 1.0,
    }


def _append_cycle(
    store: SQLiteStore,
    end: datetime,
    *,
    status: str = "PROCESSED",
) -> None:
    end_text = end.isoformat().replace("+00:00", "Z")
    store.append_event(
        f"cycle:{end_text}",
        "PAPER_CYCLE",
        end,
        {
            "created_order_ids": [],
            "end_utc": end_text,
            "filled_order_ids": [],
            "reason_codes": [],
            "status": status,
        },
    )


def _process_cycle(
    store: SQLiteStore,
    frame: pd.DataFrame,
    end: datetime = END,
) -> None:
    PaperService(
        source=_FrameSource([frame]),
        store=store,
        broker=PaperBroker(store, CostConfig(0.0, 0.0)),
        clock=_Clock(end + timedelta(minutes=10)),
        lease_owner="status-worker",
        lease_token=f"status-token-{end.isoformat()}",
        costs=CostConfig(0.0, 0.0),
    ).process_completed_candle(end)


class _PublicClient:
    source_url = "https://api.upbit.com/v1/candles/minutes/240"
    collection_config = DataConfig()

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def fetch_page(self, to_utc: str) -> list[dict[str, object]]:
        self.calls.append(to_utc)
        return list(self.rows)


def test_parser_exposes_exact_commands_and_accepts_only_paired_env_names() -> None:
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    assert tuple(action.choices) == (
        "data-download",
        "data-quality",
        "backtest",
        "walk-forward",
        "paper-once",
        "paper-run",
        "paper-status",
    )

    parsed = parser.parse_args(
        [
            "paper-once",
            "--db",
            "paper.sqlite3",
            "--data-dir",
            "evidence",
            "--telegram-token-env",
            "BOT_TOKEN_NAME",
            "--telegram-chat-env",
            "BOT_CHAT_NAME",
        ]
    )
    assert parsed.telegram_token_env == "BOT_TOKEN_NAME"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "paper-once",
                "--db",
                "paper.sqlite3",
                "--data-dir",
                "evidence",
                "--telegram-token",
                "raw-secret",
            ]
        )


def test_paper_status_missing_db_is_nonzero_and_creates_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "missing.sqlite3"

    assert main(["paper-status", "--db", str(path)]) != 0

    assert not path.exists()
    assert tuple(tmp_path.iterdir()) == ()
    assert capsys.readouterr().out == ""


def test_paper_status_is_byte_and_metadata_read_only_with_stable_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    store.close()
    before_files = tuple(sorted(item.name for item in tmp_path.iterdir()))
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    assert main(["paper-status", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == sorted(payload)
    assert payload["mode"] == "normalized-paper"
    assert payload["normalized_cash"] == 100.0
    assert payload["normalized_equity"] == 100.0
    assert payload["equity_as_of_utc"] is None
    assert payload["equity_status"] == "UNAVAILABLE"
    assert payload["equity_provenance"] == "INITIAL_EQUITY"
    assert payload["btc_quantity"] == 0.0
    assert payload["position_state"] == "FLAT"
    assert payload["health_stage"] == "NORMAL"
    assert payload["last_completed_candle_utc"] is None
    assert payload["pending_orders"] == []
    assert payload["next_scheduled_utc"].endswith("Z")
    assert path.read_bytes() == before_bytes
    assert path.stat().st_size == before_stat.st_size
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_files


def test_paper_status_reports_flat_completed_close_mtm_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    store = SQLiteStore(path)
    store.initialize()
    _process_cycle(store, _history(END))

    before = _ledger_file_image(path)
    assert main(["paper-status", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized_equity"] == 100.0
    assert payload["equity_as_of_utc"] == END.isoformat().replace("+00:00", "Z")
    assert payload["equity_status"] == "CURRENT"
    assert payload["equity_provenance"] == "COMPLETED_CLOSE_MTM"
    assert _ledger_file_image(path) == before


def test_paper_status_uses_active_wal_completed_close_mtm_after_no_fill_price_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    order = broker.submit_entry(
        END - timedelta(hours=12),
        quantity=0.2,
        reason="ENTRY_BREAKOUT",
    )
    broker.process_open(order.order_id, END - timedelta(hours=8), open_price=100.0)
    frame = _history(END)
    frame.iloc[-1, frame.columns.get_loc("open")] = 75.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 76.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 69.0
    frame.iloc[-1, frame.columns.get_loc("close")] = 70.0
    _process_cycle(store, frame)
    assert broker.reconcile().equity == 100.0

    before = _ledger_file_image(path)
    assert main(["paper-status", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized_cash"] == 80.0
    assert payload["btc_quantity"] == 0.2
    assert payload["normalized_equity"] == 94.0
    assert payload["equity_as_of_utc"] == END.isoformat().replace("+00:00", "Z")
    assert payload["equity_status"] == "CURRENT"
    assert payload["equity_provenance"] == "COMPLETED_CLOSE_MTM"
    assert _ledger_file_image(path) == before


def test_paper_status_labels_latest_cycle_without_mark_as_stale_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    store = SQLiteStore(path)
    store.initialize()
    _append_cycle(store, END)
    store.close()
    before = _ledger_file_image(path)

    assert main(["paper-status", "--db", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized_equity"] == 100.0
    assert payload["equity_as_of_utc"] is None
    assert payload["equity_status"] == "STALE"
    assert payload["equity_provenance"] == "INITIAL_EQUITY"
    assert _ledger_file_image(path) == before


def test_paper_status_rejects_completed_close_risk_that_contradicts_flat_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    store = SQLiteStore(path)
    store.initialize()
    bar_at = END - timedelta(hours=4)
    forged = _RiskState(
        equity_peak=100.0,
        daily_date=bar_at.date(),
        daily_baseline_equity=100.0,
        equity_history=((bar_at, 99.0),),
        risk_started_at=bar_at,
        last_equity=99.0,
        last_risk_at=bar_at,
    )
    store.append_event(
        f"risk:{bar_at.isoformat().replace('+00:00', 'Z')}",
        "BREAKER_STATE",
        bar_at,
        _risk_state_payload(forged),
    )
    _append_cycle(store, END)
    store.close()
    before = _ledger_file_image(path)

    assert main(["paper-status", "--db", str(path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "paper-status failed: INVALID_OR_MISSING_LEDGER\n"
    assert _ledger_file_image(path) == before


def test_paper_status_corrupt_db_is_nonzero_and_byte_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite ledger")
    before = path.read_bytes()
    before_stat = path.stat()

    assert main(["paper-status", "--db", str(path)]) != 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a sqlite ledger" not in captured.err
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert tuple(item.name for item in tmp_path.iterdir()) == (path.name,)


def test_paper_status_rejects_forged_operational_event_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    HealthMonitor().persist(
        store,
        event_id="health:seed",
        logical_at=END - timedelta(hours=4),
    )
    store.append_event(
        "alert-attempt:forged",
        "ALERT_ATTEMPT",
        END - timedelta(hours=3),
        {
            "source_event_id": "health:seed",
            "source_event_type": "HEALTH_STATE",
            "version": 1,
        },
    )
    store.close()
    before_bytes = path.read_bytes()
    before_stat = path.stat()
    before_files = tuple(sorted(item.name for item in tmp_path.iterdir()))

    assert main(["paper-status", "--db", str(path)]) != 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "paper-status failed: INVALID_OR_MISSING_LEDGER\n"
    assert path.read_bytes() == before_bytes
    assert path.stat().st_size == before_stat.st_size
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_files


def test_paper_status_reports_live_wal_pending_long_stop_and_recovery_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    store = SQLiteStore(path)
    store.initialize()
    broker = PaperBroker(store, CostConfig(0.0, 0.0))
    order = broker.submit_entry(
        END - timedelta(hours=4),
        quantity=0.2,
        reason="ENTRY_BREAKOUT",
    )

    before_pending = _ledger_file_image(path)
    assert main(["paper-status", "--db", str(path)]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending["position_state"] == "ENTRY_PENDING"
    assert pending["pending_orders"] == [order.order_id]
    assert pending["active_stop"] is None
    assert _ledger_file_image(path) == before_pending

    broker.process_open(order.order_id, END, open_price=100.0)
    stop = broker.set_stop(END, 98.0, reason="HARD_STOP")
    monitor = HealthMonitor.from_store(store)
    for offset in (1, 2, 3):
        observed = END + timedelta(minutes=offset)
        monitor.record_api_failure(observed)
        monitor.persist(
            store,
            event_id=f"health:failure:{offset}",
            logical_at=observed,
        )

    before_halted = _ledger_file_image(path)
    assert main(["paper-status", "--db", str(path)]) == 0
    halted = json.loads(capsys.readouterr().out)
    assert halted["position_state"] == "LONG"
    assert halted["btc_quantity"] == 0.2
    assert halted["active_stop"]["stop_price"] == stop.stop_price
    assert halted["health_stage"] == "HALTED"
    assert halted["normalized_equity"] == 100.0
    assert halted["equity_as_of_utc"] == END.isoformat().replace("+00:00", "Z")
    assert halted["equity_status"] == "STALE"
    assert halted["equity_provenance"] == "LAST_FILL_BROKER_EQUITY"
    assert halted["health_recovery_progress"]["successes_observed"] == 0
    assert _ledger_file_image(path) == before_halted

    for offset in (4, 5, 6):
        observed = END + timedelta(minutes=offset)
        monitor.record_api_success(observed)
        monitor.persist(
            store,
            event_id=f"health:success:{offset}",
            logical_at=observed,
        )
    before_reduced = _ledger_file_image(path)
    assert main(["paper-status", "--db", str(path)]) == 0
    reduced = json.loads(capsys.readouterr().out)
    assert reduced["health_stage"] == "REDUCED"
    assert reduced["health_recovery_progress"]["successes_observed"] == 3
    assert reduced["health_recovery_progress"]["successes_required"] == 3
    assert _ledger_file_image(path) == before_reduced


def _ledger_file_image(path: Path) -> tuple[tuple[str, bytes, int, int], ...]:
    return tuple(
        (
            item.name,
            item.read_bytes(),
            item.stat().st_size,
            item.stat().st_mtime_ns,
        )
        for item in sorted(path.parent.iterdir(), key=lambda candidate: candidate.name)
    )


def test_public_source_binds_cache_to_exclusive_end_and_requires_601_bars(
    tmp_path: Path,
) -> None:
    start = END - timedelta(hours=4 * 601)
    rows = [_raw_row(start - timedelta(hours=4))]
    rows.extend(_raw_row(start + timedelta(hours=4 * index)) for index in range(601))
    client = _PublicClient(list(reversed(rows)))
    source = _PublicPaperCandleSource(tmp_path, client=client)

    frame = source.load_completed_candles(END)

    assert len(frame) == 601
    assert frame.index[0].to_pydatetime() == start
    assert frame.index[-1].to_pydatetime() == END - timedelta(hours=4)
    assert all(timestamp.to_pydatetime() < END for timestamp in frame.index)
    assert len(client.calls) == 1


def test_corrupt_public_cache_fails_before_network_and_is_not_replaced(
    tmp_path: Path,
) -> None:
    start = END - timedelta(hours=4 * 601)
    rows = [_raw_row(start - timedelta(hours=4))]
    rows.extend(_raw_row(start + timedelta(hours=4 * index)) for index in range(601))
    first = _PublicClient(list(reversed(rows)))
    _PublicPaperCandleSource(tmp_path, client=first).load_completed_candles(END)
    checkpoint = next(tmp_path.rglob("checkpoint.json"))
    checkpoint.write_text("{corrupt", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    second = _PublicClient([])

    with pytest.raises(ValueError, match="malformed"):
        _PublicPaperCandleSource(tmp_path, client=second).load_completed_candles(END)

    assert second.calls == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_checkpointless_corrupt_collection_snapshot_fails_before_network(
    tmp_path: Path,
) -> None:
    identity = sha256(END.isoformat().replace("+00:00", "Z").encode("utf-8")).hexdigest()
    evidence_root = tmp_path / "KRW-BTC-240" / identity
    evidence_root.mkdir(parents=True)
    artifact = evidence_root / f"collection-{'0' * 64}.json"
    artifact.write_bytes(b"{malformed")
    before = _ledger_file_image(artifact)
    client = _PublicClient([])

    with pytest.raises(ValueError, match="evidence|snapshot|hash|malformed"):
        _PublicPaperCandleSource(tmp_path, client=client).load_completed_candles(END)

    assert client.calls == []
    assert _ledger_file_image(artifact) == before


@pytest.mark.parametrize(
    "defect",
    [
        "hash_mismatch",
        "invalid_json",
        "invalid_schema",
        "range_mismatch",
        "config_mismatch",
        "source_mismatch",
        "unknown_evidence",
    ],
)
def test_checkpointless_invalid_or_unknown_paper_evidence_fails_closed(
    tmp_path: Path,
    defect: str,
) -> None:
    start = END - timedelta(hours=4 * 601)
    rows = [_raw_row(start - timedelta(hours=4))]
    rows.extend(_raw_row(start + timedelta(hours=4 * index)) for index in range(601))
    _PublicPaperCandleSource(
        tmp_path,
        client=_PublicClient(list(reversed(rows))),
    ).load_completed_candles(END)
    evidence_root = next(path.parent for path in tmp_path.rglob("checkpoint.json"))
    (evidence_root / "checkpoint.json").unlink()
    valid_snapshot = sorted(evidence_root.glob("collection-*.json"))[-1]
    state = json.loads(valid_snapshot.read_text(encoding="utf-8"))
    if defect == "hash_mismatch":
        valid_snapshot.write_bytes(b"{}")
    elif defect == "invalid_json":
        contents = b"{invalid-json"
        digest = sha256(contents).hexdigest()
        (evidence_root / f"collection-{digest}.json").write_bytes(contents)
    elif defect == "unknown_evidence":
        (evidence_root / "unknown-evidence.json").write_text("{}", encoding="utf-8")
    else:
        if defect == "invalid_schema":
            state["unexpected"] = True
        elif defect == "range_mismatch":
            state["end_utc"] = "2026-05-11T08:00:00Z"
        elif defect == "config_mismatch":
            state["config"]["years"] = 8
        else:
            state["source_url"] = "https://example.invalid/candles"
        contents = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = sha256(contents).hexdigest()
        (evidence_root / f"collection-{digest}.json").write_bytes(contents)
    before = {
        path: path.read_bytes() for path in evidence_root.iterdir() if path.is_file()
    }
    client = _PublicClient([])

    with pytest.raises(ValueError, match="evidence|snapshot|hash|schema|collection"):
        _PublicPaperCandleSource(tmp_path, client=client).load_completed_candles(END)

    assert client.calls == []
    assert {
        path: path.read_bytes() for path in evidence_root.iterdir() if path.is_file()
    } == before


def test_checkpointless_valid_paper_snapshot_resumes_without_network(
    tmp_path: Path,
) -> None:
    start = END - timedelta(hours=4 * 601)
    rows = [_raw_row(start - timedelta(hours=4))]
    rows.extend(_raw_row(start + timedelta(hours=4 * index)) for index in range(601))
    first = _PublicClient(list(reversed(rows)))
    expected = _PublicPaperCandleSource(tmp_path, client=first).load_completed_candles(END)
    checkpoint = next(tmp_path.rglob("checkpoint.json"))
    checkpoint.unlink()
    second = _PublicClient([])

    resumed = _PublicPaperCandleSource(tmp_path, client=second).load_completed_candles(END)

    assert second.calls == []
    pd.testing.assert_frame_equal(resumed, expected)


@pytest.mark.parametrize(
    "defect",
    ["missing_first", "duplicate", "eight_hour_gap", "end_row", "future_row"],
)
def test_paper_frame_rejects_nonexact_raw_601_bar_evidence(defect: str) -> None:
    frame = _history(END)
    if defect == "missing_first":
        frame = frame.iloc[1:]
    elif defect == "eight_hour_gap":
        frame = frame.drop(frame.index[300])
    else:
        index = frame.index.to_list()
        if defect == "duplicate":
            index[300] = index[299]
        elif defect == "end_row":
            index[-1] = pd.Timestamp(END)
        else:
            index[-1] = pd.Timestamp(END + timedelta(hours=4))
        frame.index = pd.DatetimeIndex(index)

    with pytest.raises(ValueError, match="candle|history|timestamp|exclusive"):
        _validated_paper_frame(frame, END)


def test_application_persists_three_failures_three_successes_and_auto_promotes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    clock = _Clock(END + timedelta(minutes=10))
    sleeper = _Sleeper(clock)
    next_end = END + timedelta(hours=4)
    source = _FrameSource(
        [
            RuntimeError("outage-1"),
            RuntimeError("outage-2"),
            RuntimeError("outage-3"),
            _history(END),
            _history(END),
            _history(END),
            _history(next_end),
        ]
    )
    app = _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=sleeper,
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="test-worker",
        lease_token="test-token",
    )

    for _ in range(3):
        with pytest.raises(RuntimeError, match="outage"):
            app.run_once()
    assert HealthMonitor.from_store(store).current_action().stage is HealthStage.HALTED
    for _ in range(3):
        app.run_once()
    assert HealthMonitor.from_store(store).current_action().stage is HealthStage.REDUCED

    promoted = app.run_once()

    assert promoted.status is CycleStatus.PROCESSED
    assert promoted.end_utc == next_end
    assert HealthMonitor.from_store(store).current_action().stage is HealthStage.NORMAL
    health_events = [
        event.event_id
        for event in store.replay_state().event_evidence
        if event.event_type == "HEALTH_STATE"
    ]
    assert len(health_events) == len(set(health_events))
    assert sum(identity.startswith("health:recovery:") for identity in health_events) == 1
    assert sleeper.delays[:5] == [1.0, 1.0, 1.0, 2.0, 4.0]
    assert sleeper.delays[-1] > 14_000.0
    assert source.calls[:6] == [END] * 6
    assert source.calls[-1] == next_end
    cycles = [
        event
        for event in store.replay_state().event_evidence
        if event.event_type == "PAPER_CYCLE"
    ]
    assert [event.occurred_at_utc for event in cycles] == [END, next_end]
    assert PaperBroker(store, CostConfig(0.0, 0.0)).reconcile().active_orders == ()


def test_first_api_failure_survives_rollover_and_restart_before_newer_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    attempted_end = END + timedelta(hours=4)
    newer_end = attempted_end + timedelta(hours=4)
    first_store = SQLiteStore(path)
    first_store.initialize()
    first_clock = _Clock(attempted_end + timedelta(minutes=10))
    first_source = _FrameSource([RuntimeError("first-cycle outage")])
    first = _PaperApplication(
        source=first_source,
        store=first_store,
        clock=first_clock,
        sleeper=_Sleeper(first_clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="first-attempt-worker",
        lease_token="first-attempt-token",
    )

    with pytest.raises(RuntimeError, match="first-cycle outage"):
        first.run_once()

    attempts_after_failure = tuple(
        event
        for event in first_store.replay_state().event_evidence
        if event.event_type == "PAPER_CYCLE_ATTEMPT"
    )
    assert len(attempts_after_failure) == 1
    assert attempts_after_failure[0].payload == {
        "end_utc": attempted_end.isoformat().replace("+00:00", "Z"),
        "version": 1,
    }
    assert first_source.calls == [attempted_end]
    first_store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    restart_clock = _Clock(newer_end + timedelta(minutes=10))
    restart_source = _FrameSource(
        [_breakout_history(attempted_end), _history(newer_end)]
    )
    restarted = _PaperApplication(
        source=restart_source,
        store=reopened,
        clock=restart_clock,
        sleeper=_Sleeper(restart_clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="restart-attempt-worker",
        lease_token="restart-attempt-token",
    )

    first_result = restarted.run_once()
    second_result = restarted.run_once()

    snapshot = reopened.replay_state()
    reconciliation = PaperBroker(reopened, CostConfig(0.0, 0.0)).reconcile()
    assert [first_result.end_utc, second_result.end_utc] == [attempted_end, newer_end]
    assert restart_source.calls == [attempted_end, newer_end]
    assert [
        event.occurred_at_utc
        for event in snapshot.event_evidence
        if event.event_type == "PAPER_CYCLE"
    ] == [attempted_end, newer_end]
    assert len(
        [
            event
            for event in snapshot.event_evidence
            if event.event_type == "PAPER_CYCLE_ATTEMPT"
        ]
    ) == 2
    assert len(
        [event for event in snapshot.event_evidence if event.event_type == "ORDER_CREATED"]
    ) == 1
    assert len(reconciliation.fills) == 1
    assert reconciliation.fills[0].fill_time == attempted_end


@pytest.mark.parametrize(
    ("event_id", "event_type", "occurred_at", "payload"),
    [
        (
            "paper-cycle-attempt:forged",
            "PAPER_CYCLE_ATTEMPT",
            END - timedelta(hours=4),
            {"end_utc": END.isoformat().replace("+00:00", "Z"), "version": 1},
        ),
        (
            f"paper-cycle-attempt:{END.isoformat().replace('+00:00', 'Z')}",
            "PAPER_CYCLE_ATTEMPT",
            END - timedelta(hours=4),
            {"end_utc": END.isoformat(), "version": 1},
        ),
        (
            f"paper-cycle-attempt:{(END + timedelta(hours=4)).isoformat().replace('+00:00', 'Z')}",
            "PAPER_CYCLE_ATTEMPT",
            END,
            {
                "end_utc": (END + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
                "version": 1,
            },
        ),
        (
            f"paper-cycle-attempt:{END.isoformat().replace('+00:00', 'Z')}",
            "UNKNOWN_EVIDENCE",
            END - timedelta(hours=4),
            {"end_utc": END.isoformat().replace("+00:00", "Z"), "version": 1},
        ),
    ],
)
def test_application_rejects_forged_malformed_or_future_attempt_before_source(
    tmp_path: Path,
    event_id: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, object],
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    store.append_event(event_id, event_type, occurred_at, payload)
    before_sequence = store.replay_state().last_sequence
    clock = _Clock(END + timedelta(minutes=10))
    source = _FrameSource([_history(END)])
    app = _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=_Sleeper(clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="invalid-attempt-worker",
        lease_token="invalid-attempt-token",
    )

    with pytest.raises(RuntimeError, match="attempt"):
        app.run_once()

    assert source.calls == []
    assert store.replay_state().last_sequence == before_sequence


def test_health_only_cursor_compatibility_does_not_accept_arbitrary_events(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    store.append_event(
        "unknown-operational-event",
        "UNKNOWN_EVIDENCE",
        END - timedelta(hours=4),
        {"value": 1},
    )
    clock = _Clock(END + timedelta(minutes=10))
    source = _FrameSource([_history(END)])
    app = _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=_Sleeper(clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="unknown-worker",
        lease_token="unknown-token",
    )

    with pytest.raises(RuntimeError, match="no resolvable cycle cursor"):
        app.run_once()

    assert source.calls == []


@pytest.mark.parametrize(
    ("event_id", "event_type", "payload"),
    [
        (
            "alert-attempt:forged",
            "ALERT_ATTEMPT",
            {
                "source_event_id": "health:seed",
                "source_event_type": "HEALTH_STATE",
                "version": 1,
            },
        ),
        (
            "alert-attempt:orphan",
            "ALERT_ATTEMPT",
            {
                "source_event_id": "health:missing",
                "source_event_type": "HEALTH_STATE",
                "version": 1,
            },
        ),
        (
            "alert-failure:orphan",
            "ALERT_FAILURE",
            {
                "failure_code": "DELIVERY_FAILED",
                "source_event_id": "health:seed",
                "source_event_type": "HEALTH_STATE",
                "version": 1,
            },
        ),
    ],
)
def test_health_cursor_rejects_forged_alert_identity_or_payload_chain(
    tmp_path: Path,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    HealthMonitor().persist(
        store,
        event_id="health:seed",
        logical_at=END - timedelta(hours=4),
    )
    store.append_event(event_id, event_type, END - timedelta(hours=3), payload)
    clock = _Clock(END + timedelta(minutes=10))
    source = _FrameSource([_history(END)])
    app = _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=_Sleeper(clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="forged-worker",
        lease_token="forged-token",
    )

    with pytest.raises(RuntimeError, match="alert evidence"):
        app.run_once()

    assert source.calls == []


def test_halted_same_end_preflight_rejects_forged_alert_before_source_or_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    clock = _Clock(END + timedelta(minutes=10))
    sleeper = _Sleeper(clock)
    source = _FrameSource([_history(END), _history(END)])
    app = _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=sleeper,
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="halted-worker",
        lease_token="halted-token",
    )
    assert app.run_once().status is CycleStatus.PROCESSED
    monitor = HealthMonitor.from_store(store)
    for offset in (11, 12, 13):
        observed = END + timedelta(minutes=offset)
        monitor.record_api_failure(observed)
        monitor.persist(
            store,
            event_id=f"health:halted:{offset}",
            logical_at=observed,
        )
    source_event = store.replay_state().event_evidence[-1]
    store.append_event(
        "alert-attempt:forged",
        "ALERT_ATTEMPT",
        END + timedelta(minutes=14),
        {
            "source_event_id": source_event.event_id,
            "source_event_type": "HEALTH_STATE",
            "version": 1,
        },
    )
    before_sequence = store.replay_state().last_sequence
    before_image = _ledger_file_image(path)
    before_source_calls = tuple(source.calls)

    with pytest.raises(RuntimeError, match="alert evidence"):
        app.run_once()

    assert tuple(source.calls) == before_source_calls
    assert store.replay_state().last_sequence == before_sequence
    assert _ledger_file_image(path) == before_image
    assert sleeper.delays == [1.0]


@pytest.mark.parametrize(
    "broken",
    [
        _history(END).drop(columns=["volume"]),
        _history(END).rename(
            index={_history(END).index[-1]: _history(END).index[-1] + pd.Timedelta(hours=1)}
        ),
        _history(END - timedelta(hours=4)),
    ],
)
def test_malformed_timestamp_or_missing_latest_persists_health_before_no_cycle(
    tmp_path: Path,
    broken: pd.DataFrame,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    clock = _Clock(END + timedelta(minutes=10))
    app = _PaperApplication(
        source=_FrameSource([broken]),
        store=store,
        clock=clock,
        sleeper=_Sleeper(clock),
        notifier=None,
        costs=CostConfig(0.0, 0.0),
        lease_owner="invalid-worker",
        lease_token="invalid-token",
    )

    with pytest.raises(ValueError):
        app.run_once()

    snapshot = store.replay_state()
    assert HealthMonitor.from_store(store).current_action().stage is HealthStage.HALTED
    assert any(event.event_type == "HEALTH_STATE" for event in snapshot.event_evidence)
    assert not any(event.event_type == "PAPER_CYCLE" for event in snapshot.event_evidence)
    assert PaperBroker(store, CostConfig(0.0, 0.0)).reconcile().active_orders == ()


def test_paper_once_prints_one_non_secret_json_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("autobit.cli._utc_now", lambda: END + timedelta(minutes=10))
    monkeypatch.setattr(
        "autobit.cli._paper_source_factory",
        lambda data_dir: _FrameSource([_history(END)]),
    )
    monkeypatch.setenv("BOT_TOKEN_NAME", "NEVER-PRINT-TOKEN")
    monkeypatch.setenv("BOT_CHAT_NAME", "NEVER-PRINT-CHAT")
    monkeypatch.setattr("autobit.cli._telegram_notifier_factory", lambda token, chat: None)

    code = main(
        [
            "paper-once",
            "--db",
            str(tmp_path / "paper.sqlite3"),
            "--data-dir",
            str(tmp_path / "evidence"),
            "--telegram-token-env",
            "BOT_TOKEN_NAME",
            "--telegram-chat-env",
            "BOT_CHAT_NAME",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["status"] == "PROCESSED"
    assert payload["end_utc"] == "2026-05-11T04:00:00Z"
    rendered = captured.out + captured.err + (tmp_path / "paper.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert "NEVER-PRINT-TOKEN" not in rendered
    assert "NEVER-PRINT-CHAT" not in rendered


def test_paper_run_continues_recoverable_error_then_ctrl_c_rolls_back_active_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "paper.sqlite3"

    class InterruptingApplication:
        calls = 0

        def __init__(self, store: SQLiteStore) -> None:
            self.store = store

        def run_once(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("recoverable secret detail")
            with self.store.transaction():
                self.store.append_event(
                    "must-roll-back",
                    "CYCLE_EVIDENCE",
                    END,
                    {"value": 1},
                )
                raise KeyboardInterrupt

    holder: dict[str, InterruptingApplication] = {}
    fallback_delays: list[float] = []

    def factory(arguments, store, notifier):
        del arguments, notifier
        holder["app"] = InterruptingApplication(store)
        return holder["app"]

    monkeypatch.setattr("autobit.cli._new_paper_application", factory)
    monkeypatch.setattr("autobit.cli.time.sleep", fallback_delays.append)

    assert main(
        ["paper-run", "--db", str(path), "--data-dir", str(tmp_path / "evidence")]
    ) == 130

    captured = capsys.readouterr()
    assert "secret detail" not in captured.err
    assert holder["app"].calls == 2
    assert fallback_delays == [300.0]
    reopened = SQLiteStore(path)
    reopened.initialize()
    assert not any(
        event.event_id == "must-roll-back"
        for event in reopened.replay_state().event_evidence
    )


def test_paper_run_does_not_double_sleep_an_application_owned_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedApplication:
        calls = 0
        retry_delay_applied = True

        def run_once(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("already delayed")
            raise KeyboardInterrupt

    application = DelayedApplication()
    fallback_delays: list[float] = []
    monkeypatch.setattr(
        "autobit.cli._new_paper_application",
        lambda arguments, store, notifier: application,
    )
    monkeypatch.setattr("autobit.cli.time.sleep", fallback_delays.append)

    assert main(
        [
            "paper-run",
            "--db",
            str(tmp_path / "paper.sqlite3"),
            "--data-dir",
            str(tmp_path / "evidence"),
        ]
    ) == 130

    assert application.calls == 2
    assert fallback_delays == []

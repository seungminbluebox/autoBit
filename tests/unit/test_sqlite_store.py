from __future__ import annotations

import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from autobit.domain.models import OrderStatus, PositionState
from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    SQLiteStore,
    StoreCorruptionError,
)


UTC_0 = "2026-01-01T00:00:00Z"
UTC_4 = "2026-01-01T04:00:00Z"
UTC_8 = "2026-01-01T08:00:00Z"


def _open_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize(initial_equity=100.0)
    return store


def test_initialize_creates_normalized_flat_state_and_required_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)

    state = store.replay_state()

    assert state.market == "KRW-BTC"
    assert state.initial_equity == 100.0
    assert state.cash == 100.0
    assert state.btc_quantity == 0.0
    assert state.equity == 100.0
    assert state.btc_cost_basis == 0.0
    assert state.average_entry_price is None
    assert state.last_price is None
    assert state.position_state is PositionState.FLAT
    assert state.pending_orders == ()
    assert state.breaker_state == {}
    assert state.health_state == {}
    assert state.last_sequence == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.foreign_keys_enabled


@pytest.mark.parametrize(
    "initial_equity",
    [0.0, 99.999999999, 100.000000001, math.nan, math.inf, -math.inf],
)
def test_initialize_rejects_non_normalized_or_non_finite_equity(
    tmp_path: Path,
    initial_equity: float,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="initial equity must be finite and exactly 100"):
        store.initialize(initial_equity=initial_equity)


def test_initialize_rejects_any_market_other_than_krw_btc(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="market must be KRW-BTC"):
        store.initialize(market="KRW-ETH")


def test_existing_database_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value = 'KRW-ETH' WHERE key = 'market'")
    tables_before = _table_names(path)

    reopened = SQLiteStore(path)
    with pytest.raises(StoreCorruptionError, match="stored market"):
        reopened.initialize()

    assert _table_names(path) == tables_before


def test_unknown_schema_version_fails_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_version VALUES (999)")
    tables_before = _table_names(path)

    store = SQLiteStore(path)
    with pytest.raises(StoreCorruptionError, match="unsupported schema version"):
        store.initialize()

    assert _table_names(path) == tables_before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(999,)]


def test_unreadable_database_fails_closed_without_replacing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    original = b"this is not a sqlite database"
    path.write_bytes(original)

    store = SQLiteStore(path)
    with pytest.raises(StoreCorruptionError, match="database schema is unreadable"):
        store.initialize()

    assert path.read_bytes() == original


def test_order_idempotency_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    first = _open_store(path)
    assert first.record_order_once(
        "KRW-BTC:2026-01-01T00:00:00Z:ENTRY",
        "BUY",
        0.1,
    )
    first.close()

    second = _open_store(path)
    assert not second.record_order_once(
        "KRW-BTC:2026-01-01T00:00:00Z:ENTRY",
        "BUY",
        0.1,
    )
    assert len(second.replay_state().pending_orders) == 1


def test_same_idempotency_key_with_different_payload_is_a_conflict(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    assert store.record_order_once("entry", "BUY", 0.1, occurred_at=UTC_0)
    before = store.replay_state()

    with pytest.raises(IdempotencyConflictError, match="different order payload"):
        store.record_order_once("entry", "BUY", 0.2, occurred_at=UTC_0)

    assert store.replay_state() == before


def test_record_order_once_rejects_invalid_side_status_quantity_and_identifier(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        store.record_order_once("bad-side", "HOLD", 0.1, occurred_at=UTC_0)
    with pytest.raises(ValueError, match="requested quantity must be finite and positive"):
        store.record_order_once("bad-quantity", "BUY", 0.0, occurred_at=UTC_0)
    with pytest.raises(ValueError, match="requested quantity must be finite and positive"):
        store.record_order_once("nan-quantity", "BUY", math.nan, occurred_at=UTC_0)
    with pytest.raises(ValueError, match="status must be a valid OrderStatus"):
        store.record_order_once("bad-status", "BUY", 0.1, occurred_at=UTC_0, status="OPEN")
    with pytest.raises(ValueError, match="order seed status"):
        store.record_order_once(
            "terminal-status",
            "BUY",
            0.1,
            occurred_at=UTC_0,
            status=OrderStatus.COMPLETED,
        )
    with pytest.raises(ValueError, match="idempotency key must be non-empty"):
        store.record_order_once("", "BUY", 0.1, occurred_at=UTC_0)


def test_sell_order_seed_cannot_exceed_replayed_inventory(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="sell order would exceed BTC inventory"):
        store.record_order_once("exit", "SELL", 0.1, occurred_at=UTC_0)

    assert store.replay_state().pending_orders == ()


def test_pending_order_replay_uses_existing_domain_contracts(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    assert store.record_order_once(
        "entry",
        "BUY",
        0.1,
        order_id="entry-1",
        occurred_at=UTC_0,
        status=OrderStatus.CREATED,
    )

    state = store.replay_state()

    assert state.position_state is PositionState.ENTRY_PENDING
    assert len(state.pending_orders) == 1
    assert state.pending_orders[0].order_id == "entry-1"
    assert state.pending_orders[0].status is OrderStatus.CREATED
    assert state.pending_orders[0].filled_quantity == 0.0


def test_duplicate_event_id_is_rejected_without_changing_state(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.append_event("health-1", "HEALTH_STATE", UTC_0, {"api_successes": 1})
    before = store.replay_state()

    with pytest.raises(IdempotencyConflictError, match="event id already exists"):
        store.append_event("health-1", "HEALTH_STATE", UTC_0, {"api_successes": 1})

    assert store.replay_state() == before


def test_event_json_is_canonical_and_unknown_evidence_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event(
        "evidence-1",
        "CYCLE_EVIDENCE",
        UTC_0,
        {"z": [3, 2, 1], "a": {"second": False, "first": True}},
    )

    with sqlite3.connect(path) as connection:
        payload_json = connection.execute(
            "SELECT payload_json FROM events WHERE event_id = 'evidence-1'",
        ).fetchone()[0]
    state = store.replay_state()

    assert payload_json == '{"a":{"first":true,"second":false},"z":[3,2,1]}'
    assert state.event_evidence[-1].event_type == "CYCLE_EVIDENCE"
    assert state.event_evidence[-1].payload == {
        "a": {"first": True, "second": False},
        "z": (3, 2, 1),
    }


def test_breaker_and_health_payloads_survive_reopen_without_losing_unknown_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event(
        "breaker-1",
        "BREAKER_STATE",
        UTC_0,
        {"halt_entries": True, "reasons": ["API_FAILURES"], "future_field": {"level": 2}},
    )
    store.append_event(
        "health-1",
        "HEALTH_STATE",
        UTC_4,
        {"api_successes": 0, "unresolved_orders": 0},
    )
    store.close()

    reopened = _open_store(path)
    state = reopened.replay_state()

    assert state.position_state is PositionState.HALTED
    assert state.breaker_state == {
        "future_field": {"level": 2},
        "halt_entries": True,
        "reasons": ("API_FAILURES",),
    }
    assert state.health_state == {"api_successes": 0, "unresolved_orders": 0}


@pytest.mark.parametrize(
    "occurred_at",
    [
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T09:00:00+09:00",
        "not-a-timestamp",
    ],
)
def test_timestamp_strings_must_be_canonical_utc_z(tmp_path: Path, occurred_at: str) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="timestamp must be strict UTC"):
        store.append_event("event", "CYCLE_EVIDENCE", occurred_at, {})


def test_timestamp_reversal_is_rejected_without_mutation(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.append_event("later", "CYCLE_EVIDENCE", UTC_4, {})
    before = store.replay_state()

    with pytest.raises(ValueError, match="timestamp reversal"):
        store.append_event("earlier", "CYCLE_EVIDENCE", UTC_0, {})

    assert store.replay_state() == before


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_non_finite_json_numbers_are_rejected(tmp_path: Path, bad_value: float) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(ValueError, match="JSON numbers must be finite"):
        store.append_event("bad-json", "CYCLE_EVIDENCE", UTC_0, {"value": bad_value})


def test_replay_restores_partial_buy_and_sell_ledger(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    assert store.append_fill(
        order_id="entry-1",
        side="BUY",
        quantity=0.2,
        price=100.0,
        fee=0.01,
        occurred_at=UTC_4,
    )
    after_buy = store.replay_state()
    assert after_buy.position_state is PositionState.LONG
    assert after_buy.btc_quantity == pytest.approx(0.2)
    assert after_buy.cash == pytest.approx(79.99)
    assert after_buy.btc_cost_basis == pytest.approx(20.01)
    assert after_buy.average_entry_price == pytest.approx(100.05)
    assert after_buy.equity == pytest.approx(99.99)

    assert store.append_fill(
        order_id="exit-1",
        side="SELL",
        quantity=0.05,
        price=110.0,
        fee=0.01,
        occurred_at=UTC_8,
    )
    after_sell = store.replay_state()
    assert after_sell.position_state is PositionState.LONG
    assert after_sell.btc_quantity == pytest.approx(0.15)
    assert after_sell.cash == pytest.approx(85.48)
    assert after_sell.btc_cost_basis == pytest.approx(15.0075)
    assert after_sell.average_entry_price == pytest.approx(100.05)
    assert after_sell.last_price == 110.0
    assert after_sell.equity == pytest.approx(101.98)


def test_exact_fill_retry_is_a_noop(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    fill = dict(
        order_id="entry-1",
        side="BUY",
        quantity=0.2,
        price=100.0,
        fee=0.01,
        occurred_at=UTC_4,
    )
    assert store.append_fill(**fill)
    before = store.replay_state()

    assert not store.append_fill(**fill)

    assert store.replay_state() == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", 0.0, "quantity must be finite and positive"),
        ("quantity", math.nan, "quantity must be finite and positive"),
        ("price", 0.0, "price must be finite and positive"),
        ("price", math.inf, "price must be finite and positive"),
        ("fee", -0.01, "fee must be finite and non-negative"),
        ("fee", math.nan, "fee must be finite and non-negative"),
    ],
)
def test_fill_rejects_invalid_numeric_values(
    tmp_path: Path,
    field: str,
    value: float,
    message: str,
) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    values = {"quantity": 0.1, "price": 100.0, "fee": 0.0}
    values[field] = value

    with pytest.raises(ValueError, match=message):
        store.append_fill(
            order_id="entry",
            side="BUY",
            occurred_at=UTC_0,
            **values,
        )


def test_fill_rejects_insufficient_cash_oversell_and_invalid_side_without_mutation(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    initial = store.replay_state()

    with pytest.raises(ValueError, match="fill would make cash negative"):
        store.append_fill("entry", "BUY", 1.0, 100.0, 0.01, UTC_0)
    with pytest.raises(ValueError, match="fill would oversell BTC"):
        store.append_fill("exit", "SELL", 0.1, 100.0, 0.0, UTC_0)
    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        store.append_fill("bad", "HOLD", 0.1, 100.0, 0.0, UTC_0)

    assert store.replay_state() == initial


def test_sell_fill_fee_cannot_make_cash_negative(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.append_fill("entry", "BUY", 1.0, 50.0, 0.0, UTC_0)
    before = store.replay_state()

    with pytest.raises(ValueError, match="fill would make cash negative"):
        store.append_fill("exit", "SELL", 1.0, 1.0, 60.0, UTC_4)

    assert store.replay_state() == before


def test_tiny_negative_cash_and_inventory_are_not_hidden_by_reconciliation_tolerance(
    tmp_path: Path,
) -> None:
    cash_store = _open_store(tmp_path / "cash.sqlite3")
    with pytest.raises(ValueError, match="fill would make cash negative"):
        cash_store.append_fill("entry", "BUY", 1.0, 100.0, 1e-12, UTC_0)
    assert cash_store.replay_state().cash == 100.0

    inventory_store = _open_store(tmp_path / "inventory.sqlite3")
    inventory_store.append_fill("entry", "BUY", 0.1, 100.0, 0.0, UTC_0)
    with pytest.raises(ValueError, match="fill would oversell BTC"):
        inventory_store.append_fill(
            "exit",
            "SELL",
            0.10000000001,
            100.0,
            0.0,
            UTC_4,
        )
    assert inventory_store.replay_state().btc_quantity == pytest.approx(0.1)


def test_order_and_fill_projection_update_atomically(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.record_order_once(
        "entry",
        "BUY",
        0.2,
        order_id="entry-1",
        occurred_at=UTC_0,
    )

    store.append_fill("entry-1", "BUY", 0.1, 100.0, 0.0, UTC_4)
    partial = store.replay_state().pending_orders[0]
    assert partial.status is OrderStatus.PARTIAL
    assert partial.filled_quantity == pytest.approx(0.1)

    store.append_fill("entry-1", "BUY", 0.1, 101.0, 0.0, UTC_8)
    completed = store.replay_state()
    assert completed.pending_orders == ()
    assert completed.position_state is PositionState.LONG
    assert completed.btc_quantity == pytest.approx(0.2)


def test_public_transaction_rolls_back_nested_mutations_on_failure(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(RuntimeError, match="injected crash"):
        with store.transaction():
            store.record_order_once(
                "entry",
                "BUY",
                0.1,
                order_id="entry-1",
                occurred_at=UTC_0,
            )
            store.append_fill("entry-1", "BUY", 0.1, 100.0, 0.01, UTC_4)
            raise RuntimeError("injected crash")

    state = store.replay_state()
    assert state.last_sequence == 0
    assert state.pending_orders == ()
    assert state.cash == 100.0


def test_public_transaction_rejects_an_incomplete_raw_projection(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with pytest.raises(StoreCorruptionError, match="snapshot sequence history"):
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO events (event_id, event_type, occurred_at_utc, payload_json)
                VALUES ('raw', 'CYCLE_EVIDENCE', ?, '{}')
                """,
                (UTC_0,),
            )

    assert store.replay_state().last_sequence == 0


def test_public_transaction_does_not_expose_commit_or_rollback(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")

    with store.transaction() as transaction:
        assert not hasattr(transaction, "commit")
        assert not hasattr(transaction, "rollback")
        with pytest.raises(AttributeError):
            getattr(transaction, "commit")()
        with pytest.raises(AttributeError):
            getattr(transaction, "rollback")()


@pytest.mark.parametrize(
    "statement",
    [
        " COMMIT",
        "-- leading comment\nRoLlBaCk",
        "/* block comment */ SAVEPOINT escaped",
        "\n\tBEGIN IMMEDIATE",
        "-- first\n/* second */ RELEASE SAVEPOINT escaped",
        "END",
    ],
)
def test_public_transaction_rejects_transaction_control_sql(
    tmp_path: Path,
    statement: str,
) -> None:
    store = _open_store(tmp_path / (sha256_text(statement) + ".sqlite3"))

    with pytest.raises(ValueError, match="transaction-control SQL is not allowed"):
        with store.transaction() as transaction:
            transaction.execute(statement)

    assert store.replay_state().last_sequence == 0


def test_two_store_instances_contend_safely_for_one_idempotency_key(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    first = _open_store(path)
    second = _open_store(path)

    def submit(store: SQLiteStore) -> bool:
        return store.record_order_once("entry", "BUY", 0.1, occurred_at=UTC_0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(submit, (first, second)))

    assert results == [False, True]
    assert first.replay_state() == second.replay_state()
    assert len(first.replay_state().pending_orders) == 1


def test_corrupt_event_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("event", "CYCLE_EVIDENCE", UTC_0, {"ok": True})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET payload_json = '{bad json' WHERE event_id = 'event'")

    with pytest.raises(StoreCorruptionError, match="invalid event JSON"):
        store.replay_state()


def test_snapshot_event_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_fill("entry", "BUY", 0.2, 100.0, 0.01, UTC_0)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT sequence, state_json FROM snapshots ORDER BY sequence DESC LIMIT 1",
        ).fetchone()
        state = json.loads(row[1])
        state["cash"] = 100.0
        connection.execute(
            "UPDATE snapshots SET state_json = ? WHERE sequence = ?",
            (json.dumps(state, sort_keys=True, separators=(",", ":")), row[0]),
        )

    with pytest.raises(StoreCorruptionError, match="snapshot does not match event replay"):
        store.replay_state()


def test_order_projection_corruption_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.record_order_once("entry", "BUY", 0.2, order_id="entry-1", occurred_at=UTC_0)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE orders SET requested_quantity = 0.3 WHERE order_id = 'entry-1'")

    with pytest.raises(StoreCorruptionError, match="order projection does not match event replay"):
        store.replay_state()


def test_sub_tolerance_order_projection_mutation_is_still_corruption(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.record_order_once("entry", "BUY", 0.2, order_id="entry-1", occurred_at=UTC_0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE orders SET filled_quantity = 5e-11 WHERE order_id = 'entry-1'",
        )

    with pytest.raises(StoreCorruptionError, match="order projection does not match event replay"):
        store.replay_state()


def test_deleted_tail_event_is_detected_from_autoincrement_history(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("first", "CYCLE_EVIDENCE", UTC_0, {})
    store.append_event("second", "CYCLE_EVIDENCE", UTC_4, {})
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM snapshots WHERE sequence = 2")
        connection.execute("DELETE FROM events WHERE sequence = 2")

    with pytest.raises(StoreCorruptionError, match="event tail was deleted"):
        store.replay_state()


def test_event_stream_digest_detects_non_state_payload_and_envelope_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("first", "CYCLE_EVIDENCE", UTC_0, {"value": 1})
    store.append_event("second", "CYCLE_EVIDENCE", UTC_4, {"value": 2})
    assert len(store.replay_state().event_digest) == 64

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE events
            SET event_id = 'changed', event_type = 'OTHER_EVIDENCE',
                occurred_at_utc = '2026-01-01T02:00:00Z',
                payload_json = '{"value":99}'
            WHERE sequence = 1
            """,
        )

    with pytest.raises(StoreCorruptionError, match="snapshot.*sequence 1"):
        store.replay_state()


def test_every_historical_snapshot_is_checked_not_only_latest(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("first", "CYCLE_EVIDENCE", UTC_0, {"value": 1})
    store.append_event("second", "CYCLE_EVIDENCE", UTC_4, {"value": 2})
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT state_json FROM snapshots WHERE sequence = 1",
        ).fetchone()
        state = json.loads(row[0])
        state["cash"] = 99.0
        connection.execute(
            "UPDATE snapshots SET state_json = ? WHERE sequence = 1",
            (json.dumps(state, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(StoreCorruptionError, match="snapshot.*sequence 1"):
        store.replay_state()


def test_sub_tolerance_historical_snapshot_mutation_is_still_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("first", "CYCLE_EVIDENCE", UTC_0, {})
    with sqlite3.connect(path) as connection:
        state = json.loads(
            connection.execute(
                "SELECT state_json FROM snapshots WHERE sequence = 1",
            ).fetchone()[0],
        )
        state["cash"] += 1e-11
        connection.execute(
            "UPDATE snapshots SET state_json = ? WHERE sequence = 1",
            (json.dumps(state, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(StoreCorruptionError, match="snapshot.*sequence 1"):
        store.replay_state()


@pytest.mark.parametrize(
    "weakness",
    [
        "missing_unique",
        "wrong_declared_type",
        "missing_autoincrement",
        "comment_only_autoincrement",
        "missing_not_null",
        "hidden_generated_column",
        "autoincrement_string_single",
        "autoincrement_identifier_double",
        "autoincrement_identifier_backtick",
        "autoincrement_identifier_bracket",
        "moved_autoincrement_lookalike",
    ],
)
def test_constraint_light_lookalike_schema_is_rejected(
    tmp_path: Path,
    weakness: str,
) -> None:
    reference_path = tmp_path / f"reference-{weakness}.sqlite3"
    reference = _open_store(reference_path)
    reference.close()
    with sqlite3.connect(reference_path) as connection:
        initial_snapshot = connection.execute(
            "SELECT state_json FROM snapshots WHERE sequence = 0",
        ).fetchone()[0]
    counterfeit_path = tmp_path / f"counterfeit-{weakness}.sqlite3"
    _create_counterfeit_schema(counterfeit_path, initial_snapshot, weakness)

    store = SQLiteStore(counterfeit_path)
    with pytest.raises(StoreCorruptionError, match="schema (definition|constraint)"):
        store.initialize()


def test_positive_btc_dust_is_never_reclassified_or_destroyed(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.append_fill("entry", "BUY", 5e-11, 1.0, 0.0, UTC_0)
    after_buy = store.replay_state()
    assert after_buy.position_state is PositionState.LONG
    assert after_buy.btc_quantity == 5e-11
    assert after_buy.btc_cost_basis == 5e-11

    store.append_fill("exit", "SELL", 1e-11, 1.0, 0.0, UTC_4)
    after_partial_sell = store.replay_state()
    assert after_partial_sell.position_state is PositionState.LONG
    assert after_partial_sell.btc_quantity == 4e-11
    assert after_partial_sell.btc_cost_basis == 4e-11


@pytest.mark.parametrize(
    "invalid_value",
    [
        "0.05",
        Decimal("0.05"),
        True,
        None,
        [0.05],
        {"value": 0.05},
        -1,
        -0.01,
        math.nan,
        math.inf,
    ],
)
def test_fill_deviation_rejects_every_non_exact_numeric_runtime_type(
    tmp_path: Path,
    invalid_value: object,
) -> None:
    store = _open_store(tmp_path / (sha256_text(repr(invalid_value)) + ".sqlite3"))

    with pytest.raises(ValueError, match="fill_deviation.*int or float"):
        store.append_event(
            "health",
            "HEALTH_STATE",
            UTC_0,
            {"fill_deviation": invalid_value},
        )

    assert store.replay_state().last_sequence == 0


@pytest.mark.parametrize("numeric_value", [0, 1, 0.0, 0.05])
def test_fill_deviation_preserves_valid_canonical_numeric_values(
    tmp_path: Path,
    numeric_value: int | float,
) -> None:
    store = _open_store(tmp_path / (sha256_text(repr(numeric_value)) + ".sqlite3"))

    store.append_event(
        "health",
        "HEALTH_STATE",
        UTC_0,
        {"fill_deviation": numeric_value},
    )

    stored = store.replay_state().health_state["fill_deviation"]
    assert type(stored) is type(numeric_value)
    assert stored == numeric_value


@pytest.mark.parametrize(
    "tampered_payload",
    [
        '{"fill_deviation":"0.05"}',
        '{"fill_deviation":true}',
        '{"fill_deviation":[]}',
    ],
)
def test_fill_deviation_type_tampering_fails_replay_on_known_field_type(
    tmp_path: Path,
    tampered_payload: str,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event(
        "health",
        "HEALTH_STATE",
        UTC_0,
        {"fill_deviation": 0.05},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = 'health'",
            (tampered_payload,),
        )

    with pytest.raises(StoreCorruptionError, match="fill_deviation.*int or float"):
        store.replay_state()


def test_malformed_quoted_events_ddl_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.close()
    with sqlite3.connect(path) as connection:
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            """
            UPDATE sqlite_master
            SET sql = replace(
                sql,
                'sequence INTEGER PRIMARY KEY AUTOINCREMENT',
                'sequence INTEGER PRIMARY KEY CHECK (''unterminated)'
            )
            WHERE type = 'table' AND name = 'events'
            """,
        )
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")

    reopened = SQLiteStore(path)
    with pytest.raises(StoreCorruptionError, match="schema|DDL"):
        reopened.initialize()


@pytest.mark.parametrize(
    ("event_type", "payload", "message"),
    [
        ("BREAKER_STATE", {"halt_entries": 1}, "halt_entries must be a bool"),
        ("BREAKER_STATE", {"reasons": "API_FAILURES"}, "reasons must be a list"),
        ("BREAKER_STATE", {"reasons": [""]}, "reason must be a non-empty"),
        ("HEALTH_STATE", {"halt_entries": 0}, "halt_entries must be a bool"),
        ("HEALTH_STATE", {"unresolved_orders": -1}, "unresolved_orders"),
        ("HEALTH_STATE", {"ledger_matches": 1}, "ledger_matches must be a bool"),
    ],
)
def test_known_safety_fields_are_strictly_validated_before_append(
    tmp_path: Path,
    event_type: str,
    payload: dict[str, object],
    message: str,
) -> None:
    store = _open_store(tmp_path / (sha256_text(repr(payload)) + ".sqlite3"))

    with pytest.raises(ValueError, match=message):
        store.append_event("unsafe", event_type, UTC_0, payload)

    assert store.replay_state().last_sequence == 0


def test_corrupt_known_safety_field_fails_replay_instead_of_failing_open(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.append_event("breaker", "BREAKER_STATE", UTC_0, {"halt_entries": True})
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = 'breaker'",
            ('{"halt_entries":1}',),
        )

    with pytest.raises(StoreCorruptionError, match="halt_entries must be a bool"):
        store.replay_state()


def test_transition_order_status_cancels_partial_entry_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = _open_store(path)
    store.record_order_once("entry", "BUY", 0.2, order_id="entry-1", occurred_at=UTC_0)
    assert store.transition_order_status(
        "entry-1",
        "entry-submitted",
        OrderStatus.SUBMITTED,
        "2026-01-01T01:00:00Z",
    )
    assert store.transition_order_status(
        "entry-1",
        "entry-accepted",
        OrderStatus.ACCEPTED,
        "2026-01-01T02:00:00Z",
    )
    store.append_fill("entry-1", "BUY", 0.1, 100.0, 0.0, UTC_4)
    assert store.transition_order_status(
        "entry-1",
        "entry-canceled",
        OrderStatus.CANCELED,
        UTC_8,
        reason="PARTIAL_REMAINDER",
    )
    assert not store.transition_order_status(
        "entry-1",
        "entry-canceled",
        OrderStatus.CANCELED,
        UTC_8,
        reason="PARTIAL_REMAINDER",
    )
    store.close()

    reopened = _open_store(path)
    state = reopened.replay_state()
    assert state.position_state is PositionState.LONG
    assert state.btc_quantity == pytest.approx(0.1)
    assert state.pending_orders == ()


def test_transition_order_status_rejects_conflict_illegal_transition_and_terminal_fill(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.record_order_once("entry", "BUY", 0.2, order_id="entry-1", occurred_at=UTC_0)
    with pytest.raises(ValueError, match="illegal order status transition"):
        store.transition_order_status(
            "entry-1",
            "illegal",
            OrderStatus.ACCEPTED,
            "2026-01-01T01:00:00Z",
        )
    assert store.transition_order_status(
        "entry-1",
        "cancel",
        OrderStatus.CANCELED,
        "2026-01-01T02:00:00Z",
    )
    with pytest.raises(IdempotencyConflictError, match="different transition payload"):
        store.transition_order_status(
            "entry-1",
            "cancel",
            OrderStatus.REJECTED,
            "2026-01-01T02:00:00Z",
        )
    with pytest.raises(ValueError, match="terminal order"):
        store.append_fill("entry-1", "BUY", 0.1, 100.0, 0.0, UTC_4)


def test_created_order_can_end_as_insufficient_cash_atomically(tmp_path: Path) -> None:
    store = _open_store(tmp_path / "paper.sqlite3")
    store.record_order_once("entry", "BUY", 1.0, order_id="entry-1", occurred_at=UTC_0)

    assert store.transition_order_status(
        "entry-1",
        "entry-insufficient",
        OrderStatus.INSUFFICIENT_CASH,
        "2026-01-01T01:00:00Z",
        reason="INSUFFICIENT_CASH",
    )
    state = store.replay_state()
    assert state.pending_orders == ()
    assert state.position_state is PositionState.FLAT


def test_context_manager_and_close_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    with SQLiteStore(path) as store:
        store.initialize()
        assert store.replay_state().cash == 100.0
    store.close()

    with pytest.raises(RuntimeError, match="store is closed"):
        store.replay_state()


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }


def sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_counterfeit_schema(
    path: Path,
    initial_snapshot: str,
    weakness: str,
) -> None:
    event_id = "event_id TEXT NOT NULL UNIQUE"
    requested_quantity = "requested_quantity REAL NOT NULL"
    event_sequence = "sequence INTEGER PRIMARY KEY AUTOINCREMENT"
    event_type = "event_type TEXT NOT NULL"
    idempotency = "idempotency_key TEXT NOT NULL UNIQUE"
    snapshot_extra = ""
    event_table_constraint = ""
    if weakness == "missing_unique":
        event_id = "event_id TEXT NOT NULL"
        idempotency = "idempotency_key TEXT NOT NULL"
    elif weakness == "wrong_declared_type":
        requested_quantity = "requested_quantity TEXT NOT NULL"
    elif weakness == "missing_autoincrement":
        event_sequence = "sequence INTEGER PRIMARY KEY"
    elif weakness == "comment_only_autoincrement":
        event_sequence = (
            "/* sequence INTEGER PRIMARY KEY AUTOINCREMENT */ "
            "sequence INTEGER PRIMARY KEY"
        )
    elif weakness == "missing_not_null":
        event_type = "event_type TEXT"
    elif weakness == "hidden_generated_column":
        snapshot_extra = ", hidden TEXT GENERATED ALWAYS AS ('x') VIRTUAL"
    elif weakness == "autoincrement_string_single":
        event_sequence = (
            "sequence INTEGER PRIMARY KEY "
            "CHECK ('sequence INTEGER PRIMARY KEY AUTOINCREMENT' <> '')"
        )
    elif weakness == "autoincrement_identifier_double":
        event_sequence = "sequence INTEGER PRIMARY KEY"
        event_table_constraint = (
            ', CONSTRAINT "sequence INTEGER PRIMARY KEY AUTOINCREMENT" CHECK (1)'
        )
    elif weakness == "autoincrement_identifier_backtick":
        event_sequence = "sequence INTEGER PRIMARY KEY"
        event_table_constraint = (
            ", CONSTRAINT `sequence INTEGER PRIMARY KEY AUTOINCREMENT` CHECK (1)"
        )
    elif weakness == "autoincrement_identifier_bracket":
        event_sequence = "sequence INTEGER PRIMARY KEY"
        event_table_constraint = (
            ", CONSTRAINT [sequence INTEGER PRIMARY KEY AUTOINCREMENT] CHECK (1)"
        )
    elif weakness == "moved_autoincrement_lookalike":
        event_sequence = "sequence INTEGER PRIMARY KEY"
        event_type = (
            "event_type TEXT NOT NULL "
            "CHECK (event_type <> 'sequence INTEGER PRIMARY KEY AUTOINCREMENT')"
        )

    with sqlite3.connect(path) as connection:
        if weakness in {
            "missing_autoincrement",
            "comment_only_autoincrement",
            "autoincrement_string_single",
            "autoincrement_identifier_double",
            "autoincrement_identifier_backtick",
            "autoincrement_identifier_bracket",
            "moved_autoincrement_lookalike",
        }:
            connection.execute(
                "CREATE TABLE seed_sequence (sequence INTEGER PRIMARY KEY AUTOINCREMENT)",
            )
            connection.execute("DROP TABLE seed_sequence")
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            f"""
            CREATE TABLE events (
                {event_sequence}, {event_id}, {event_type},
                occurred_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
                {event_table_constraint}
            )
            """,
        )
        connection.execute(
            f"""
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY, {idempotency}, side TEXT NOT NULL,
                {requested_quantity}, filled_quantity REAL NOT NULL,
                status TEXT NOT NULL, updated_at_utc TEXT NOT NULL
            )
            """,
        )
        connection.execute(
            """
            CREATE TABLE snapshots (
                sequence INTEGER PRIMARY KEY, state_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL{snapshot_extra}
            )
            """.format(snapshot_extra=snapshot_extra),
        )
        connection.execute("INSERT INTO schema_version VALUES (1)")
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            (("initial_equity", "100.0"), ("market", "KRW-BTC")),
        )
        connection.execute(
            "INSERT INTO snapshots VALUES (0, ?, '1970-01-01T00:00:00Z')",
            (initial_snapshot,),
        )

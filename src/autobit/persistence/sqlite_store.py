"""Transactional SQLite source of truth for normalized paper trading.

The store deliberately has no exchange or network concerns.  Immutable events
are authoritative, while ``orders`` and ``snapshots`` are projections checked
against a full replay before every mutation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from types import MappingProxyType
from typing import Any

from autobit.domain.models import OrderStatus, PositionState


SCHEMA_VERSION = 1
_MARKET = "KRW-BTC"
_INITIAL_EQUITY = 100.0
_TOLERANCE = 1e-10
_EPOCH = "1970-01-01T00:00:00Z"
_UTC_Z_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$",
)
_UTC_Z_SEARCH = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
)
_ACTIVE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIAL,
    },
)
_ORDER_SEED_STATUSES = frozenset(
    {OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED},
)
_REQUIRED_TABLES = frozenset(
    {"schema_version", "metadata", "events", "orders", "snapshots"},
)
_REQUIRED_COLUMNS = {
    "schema_version": ("version",),
    "metadata": ("key", "value"),
    "events": (
        "sequence",
        "event_id",
        "event_type",
        "occurred_at_utc",
        "payload_json",
    ),
    "orders": (
        "order_id",
        "idempotency_key",
        "side",
        "requested_quantity",
        "filled_quantity",
        "status",
        "updated_at_utc",
    ),
    "snapshots": ("sequence", "state_json", "created_at_utc"),
}


class StoreError(RuntimeError):
    """Base error for deterministic persistence failures."""


class StoreCorruptionError(StoreError):
    """Raised when persisted evidence cannot be trusted."""


class IdempotencyConflictError(StoreError):
    """Raised when a durable identity is reused for different evidence."""


@dataclass(frozen=True, slots=True)
class StoredOrder:
    order_id: str
    idempotency_key: str
    side: str
    requested_quantity: float
    filled_quantity: float
    status: OrderStatus
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class StoredEvent:
    sequence: int
    event_id: str
    event_type: str
    occurred_at_utc: datetime
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    market: str
    initial_equity: float
    cash: float
    btc_quantity: float
    equity: float
    btc_cost_basis: float
    average_entry_price: float | None
    last_price: float | None
    position_state: PositionState
    pending_orders: tuple[StoredOrder, ...]
    last_sequence: int
    breaker_state: Mapping[str, object]
    health_state: Mapping[str, object]
    event_evidence: tuple[StoredEvent, ...]


class SQLiteStore:
    """One explicit-transaction SQLite connection for a normalized paper ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._initialized = False
        self._closed = False
        self._lock = threading.RLock()
        self._savepoint_counter = 0

    def __enter__(self) -> SQLiteStore:
        self._ensure_not_closed()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    @property
    def foreign_keys_enabled(self) -> bool:
        self._ensure_ready()
        assert self._connection is not None
        return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def initialize(
        self,
        initial_equity: float = _INITIAL_EQUITY,
        market: str = _MARKET,
    ) -> None:
        """Create or validate schema version 1 without migrating unknown state."""
        self._ensure_not_closed()
        normalized_equity = _finite_number(
            initial_equity,
            "initial equity must be finite and exactly 100",
        )
        if normalized_equity != _INITIAL_EQUITY:
            raise ValueError("initial equity must be finite and exactly 100")
        if market != _MARKET:
            raise ValueError("market must be KRW-BTC")

        with self._lock:
            connection = self._connect()
            if self._initialized:
                self._validate_schema_and_identity(connection)
                return

            tables = self._application_tables(connection)
            if not tables:
                with self._transaction(require_initialized=False):
                    tables = self._application_tables(connection)
                    if not tables:
                        self._create_schema(connection)
                    else:
                        self._validate_schema_and_identity(connection)
            else:
                self._validate_schema_and_identity(connection)

            self._initialized = True
            try:
                self._replay_and_verify(connection)
            except Exception:
                self._initialized = False
                raise

            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                self._initialized = False
                raise StoreError("failed to enable WAL journal mode")

    def close(self) -> None:
        """Close the owned connection; repeated calls are harmless."""
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._initialized = False
            self._closed = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a public mutation boundary using ``BEGIN IMMEDIATE``."""
        self._ensure_ready()
        with self._transaction(require_initialized=True) as connection:
            yield connection
            self._replay_and_verify(connection)

    def append_event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: str | datetime,
        payload: Mapping[str, object],
    ) -> int:
        """Append one immutable event and atomically refresh its snapshot."""
        normalized_event_id = _nonempty_text(event_id, "event id must be non-empty")
        normalized_event_type = _nonempty_text(
            event_type,
            "event type must be non-empty",
        )
        occurred_at_utc, occurred_datetime = _canonical_timestamp(occurred_at)
        payload_json, _ = _canonical_payload(payload)

        with self.transaction() as connection:
            self._replay_and_verify(connection)
            if connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?",
                (normalized_event_id,),
            ).fetchone() is not None:
                raise IdempotencyConflictError(
                    f"event id already exists: {normalized_event_id}",
                )
            self._validate_timestamp_order(connection, occurred_datetime)
            cursor = connection.execute(
                """
                INSERT INTO events (event_id, event_type, occurred_at_utc, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_event_id,
                    normalized_event_type,
                    occurred_at_utc,
                    payload_json,
                ),
            )
            sequence = int(cursor.lastrowid)
            state = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, state, occurred_at_utc)
            return sequence

    def record_order_once(
        self,
        idempotency_key: str,
        side: str,
        requested_quantity: float,
        *,
        order_id: str | None = None,
        occurred_at: str | datetime | None = None,
        status: OrderStatus = OrderStatus.CREATED,
    ) -> bool:
        """Create one durable order seed, returning false for an exact retry."""
        normalized_key = _nonempty_text(
            idempotency_key,
            "idempotency key must be non-empty",
        )
        normalized_order_id = _nonempty_text(
            order_id if order_id is not None else normalized_key,
            "order id must be non-empty",
        )
        normalized_side = _valid_side(side)
        quantity = _positive_number(
            requested_quantity,
            "requested quantity must be finite and positive",
        )
        if not isinstance(status, OrderStatus):
            raise ValueError("status must be a valid OrderStatus")
        if status not in _ORDER_SEED_STATUSES:
            raise ValueError("order seed status must be CREATED, SUBMITTED, or ACCEPTED")
        event_timestamp = occurred_at
        if event_timestamp is None:
            match = _UTC_Z_SEARCH.search(normalized_key)
            event_timestamp = match.group(0) if match is not None else _EPOCH
        occurred_at_utc, occurred_datetime = _canonical_timestamp(event_timestamp)
        event_id = f"order-created:{sha256(normalized_key.encode('utf-8')).hexdigest()}"
        payload = {
            "filled_quantity": 0.0,
            "idempotency_key": normalized_key,
            "order_id": normalized_order_id,
            "requested_quantity": quantity,
            "side": normalized_side,
            "status": status.value,
        }
        payload_json, _ = _canonical_payload(payload)

        with self.transaction() as connection:
            current = self._replay_and_verify(connection)
            existing = connection.execute(
                """
                SELECT order_id, idempotency_key, side, requested_quantity
                FROM orders WHERE idempotency_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                created_event = connection.execute(
                    """
                    SELECT occurred_at_utc, payload_json
                    FROM events WHERE event_id = ? AND event_type = 'ORDER_CREATED'
                    """,
                    (event_id,),
                ).fetchone()
                same_payload = (
                    str(existing["order_id"]) == normalized_order_id
                    and str(existing["side"]) == normalized_side
                    and _numbers_close(float(existing["requested_quantity"]), quantity)
                    and created_event is not None
                    and str(created_event["occurred_at_utc"]) == occurred_at_utc
                    and str(created_event["payload_json"]) == payload_json
                )
                if same_payload:
                    return False
                raise IdempotencyConflictError(
                    f"idempotency key has different order payload: {normalized_key}",
                )
            if connection.execute(
                "SELECT 1 FROM orders WHERE order_id = ?",
                (normalized_order_id,),
            ).fetchone() is not None:
                raise IdempotencyConflictError(
                    f"order id already exists: {normalized_order_id}",
                )
            if (
                normalized_side == "SELL"
                and quantity > current.btc_quantity
            ):
                raise ValueError("sell order would exceed BTC inventory")

            self._validate_timestamp_order(connection, occurred_datetime)
            connection.execute(
                """
                INSERT INTO events (event_id, event_type, occurred_at_utc, payload_json)
                VALUES (?, 'ORDER_CREATED', ?, ?)
                """,
                (event_id, occurred_at_utc, payload_json),
            )
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, idempotency_key, side, requested_quantity,
                    filled_quantity, status, updated_at_utc
                ) VALUES (?, ?, ?, ?, 0.0, ?, ?)
                """,
                (
                    normalized_order_id,
                    normalized_key,
                    normalized_side,
                    quantity,
                    status.value,
                    occurred_at_utc,
                ),
            )
            state = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, state, occurred_at_utc)
            return True

    def append_fill(
        self,
        order_id: str,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        occurred_at: str | datetime,
        *,
        fill_id: str | None = None,
    ) -> bool:
        """Atomically append an idempotent fill, order projection, and ledger."""
        normalized_order_id = _nonempty_text(order_id, "order id must be non-empty")
        normalized_side = _valid_side(side)
        normalized_quantity = _positive_number(
            quantity,
            "quantity must be finite and positive",
        )
        normalized_price = _positive_number(
            price,
            "price must be finite and positive",
        )
        normalized_fee = _nonnegative_number(
            fee,
            "fee must be finite and non-negative",
        )
        occurred_at_utc, occurred_datetime = _canonical_timestamp(occurred_at)
        payload = {
            "fee": normalized_fee,
            "order_id": normalized_order_id,
            "price": normalized_price,
            "quantity": normalized_quantity,
            "side": normalized_side,
        }
        payload_json, _ = _canonical_payload(payload)
        normalized_fill_id = (
            _nonempty_text(fill_id, "fill id must be non-empty")
            if fill_id is not None
            else f"fill:{sha256((occurred_at_utc + payload_json).encode('utf-8')).hexdigest()}"
        )

        with self.transaction() as connection:
            current = self._replay_and_verify(connection)
            existing = connection.execute(
                """
                SELECT event_type, occurred_at_utc, payload_json
                FROM events WHERE event_id = ?
                """,
                (normalized_fill_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["event_type"]) == "FILL"
                    and str(existing["occurred_at_utc"]) == occurred_at_utc
                    and str(existing["payload_json"]) == payload_json
                ):
                    return False
                raise IdempotencyConflictError(
                    f"fill id has different payload: {normalized_fill_id}",
                )

            self._validate_timestamp_order(connection, occurred_datetime)
            if normalized_side == "BUY":
                resulting_cash = current.cash - (
                    normalized_quantity * normalized_price + normalized_fee
                )
            else:
                if normalized_quantity > current.btc_quantity:
                    raise ValueError("fill would oversell BTC")
                resulting_cash = current.cash + (
                    normalized_quantity * normalized_price - normalized_fee
                )
            if resulting_cash < 0.0:
                raise ValueError("fill would make cash negative")

            order = connection.execute(
                """
                SELECT side, requested_quantity, filled_quantity, status
                FROM orders WHERE order_id = ?
                """,
                (normalized_order_id,),
            ).fetchone()
            if order is not None:
                if str(order["side"]) != normalized_side:
                    raise ValueError("fill side does not match order side")
                order_status = _stored_order_status(order["status"])
                if order_status not in _ACTIVE_ORDER_STATUSES:
                    raise ValueError("cannot fill a terminal order")
                requested = _stored_positive_number(
                    order["requested_quantity"],
                    "invalid requested quantity in order projection",
                )
                already_filled = _stored_nonnegative_number(
                    order["filled_quantity"],
                    "invalid filled quantity in order projection",
                )
                total_filled = already_filled + normalized_quantity
                if total_filled > requested:
                    raise ValueError("fill exceeds requested quantity")
                if _numbers_close(total_filled, requested):
                    total_filled = requested
                    next_status = OrderStatus.COMPLETED
                else:
                    next_status = OrderStatus.PARTIAL
                connection.execute(
                    """
                    UPDATE orders
                    SET filled_quantity = ?, status = ?, updated_at_utc = ?
                    WHERE order_id = ?
                    """,
                    (
                        total_filled,
                        next_status.value,
                        occurred_at_utc,
                        normalized_order_id,
                    ),
                )

            connection.execute(
                """
                INSERT INTO events (event_id, event_type, occurred_at_utc, payload_json)
                VALUES (?, 'FILL', ?, ?)
                """,
                (normalized_fill_id, occurred_at_utc, payload_json),
            )
            state = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, state, occurred_at_utc)
            return True

    def load_snapshot(self) -> PaperSnapshot:
        """Load the latest snapshot only after checking it against event replay."""
        self._ensure_ready()
        assert self._connection is not None
        with self._lock:
            return self._replay_and_verify(self._connection)

    def replay_state(self) -> PaperSnapshot:
        """Replay every immutable event and verify both mutable projections."""
        self._ensure_ready()
        assert self._connection is not None
        with self._lock:
            return self._replay_and_verify(self._connection)

    @contextmanager
    def _transaction(
        self,
        *,
        require_initialized: bool,
    ) -> Iterator[sqlite3.Connection]:
        if require_initialized:
            self._ensure_ready()
        else:
            self._ensure_not_closed()
        connection = self._connect()
        with self._lock:
            nested = connection.in_transaction
            savepoint: str | None = None
            began = False
            try:
                if nested:
                    self._savepoint_counter += 1
                    savepoint = f"autobit_sp_{self._savepoint_counter}"
                    connection.execute(f"SAVEPOINT {savepoint}")
                else:
                    connection.execute("BEGIN IMMEDIATE")
                    began = True
                yield connection
                if savepoint is not None:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif began:
                    connection.commit()
            except BaseException:
                if savepoint is not None and connection.in_transaction:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif began and connection.in_transaction:
                    connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
        return self._connection

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)",
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """
            CREATE TABLE events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              occurred_at_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE orders (
              order_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              side TEXT NOT NULL,
              requested_quantity REAL NOT NULL,
              filled_quantity REAL NOT NULL,
              status TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE snapshots (
              sequence INTEGER PRIMARY KEY,
              state_json TEXT NOT NULL,
              created_at_utc TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (("initial_equity", "100.0"), ("market", _MARKET)),
        )
        initial = _initial_snapshot()
        connection.execute(
            """
            INSERT INTO snapshots (sequence, state_json, created_at_utc)
            VALUES (0, ?, ?)
            """,
            (_snapshot_json(initial), _EPOCH),
        )

    def _validate_schema_and_identity(self, connection: sqlite3.Connection) -> None:
        tables = self._application_tables(connection)
        if "schema_version" in tables:
            try:
                rows = connection.execute(
                    "SELECT version FROM schema_version ORDER BY version",
                ).fetchall()
            except sqlite3.DatabaseError as error:
                raise StoreCorruptionError("invalid schema version table") from error
            versions = [int(row["version"]) for row in rows]
            if versions != [SCHEMA_VERSION]:
                values = [row[0] for row in rows]
                raise StoreCorruptionError(
                    f"unsupported schema version: {values}",
                )
        if tables != _REQUIRED_TABLES:
            raise StoreCorruptionError(
                f"schema tables are inconsistent: {sorted(tables)}",
            )
        for table, expected_columns in _REQUIRED_COLUMNS.items():
            columns = tuple(
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if columns != expected_columns:
                raise StoreCorruptionError(
                    f"schema columns are inconsistent for {table}: {columns}",
                )
        metadata_rows = connection.execute(
            "SELECT key, value FROM metadata ORDER BY key",
        ).fetchall()
        metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
        if set(metadata) != {"initial_equity", "market"}:
            raise StoreCorruptionError("metadata keys are inconsistent")
        if metadata["market"] != _MARKET:
            raise StoreCorruptionError(
                f"stored market is not KRW-BTC: {metadata['market']}",
            )
        try:
            stored_equity = float(metadata["initial_equity"])
        except ValueError as error:
            raise StoreCorruptionError("stored initial equity is invalid") from error
        if not math.isfinite(stored_equity) or stored_equity != _INITIAL_EQUITY:
            raise StoreCorruptionError(
                f"stored initial equity is not 100: {metadata['initial_equity']}",
            )

    def _application_tables(self, connection: sqlite3.Connection) -> frozenset[str]:
        try:
            return frozenset(
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """,
                )
            )
        except sqlite3.DatabaseError as error:
            raise StoreCorruptionError("database schema is unreadable") from error

    def _replay_and_verify(self, connection: sqlite3.Connection) -> PaperSnapshot:
        self._validate_schema_and_identity(connection)
        rebuilt = self._rebuild_from_events(connection)
        snapshot_rows = connection.execute(
            "SELECT sequence, state_json, created_at_utc FROM snapshots ORDER BY sequence",
        ).fetchall()
        expected_sequences = [0, *(event.sequence for event in rebuilt.event_evidence)]
        actual_sequences = [int(row["sequence"]) for row in snapshot_rows]
        if actual_sequences != expected_sequences:
            raise StoreCorruptionError("snapshot sequence history is inconsistent")
        latest = snapshot_rows[-1]
        try:
            persisted_mapping = json.loads(str(latest["state_json"]))
        except (json.JSONDecodeError, TypeError) as error:
            raise StoreCorruptionError("invalid snapshot JSON") from error
        if not isinstance(persisted_mapping, dict):
            raise StoreCorruptionError("invalid snapshot JSON")
        try:
            canonical = _canonical_json_value(persisted_mapping)
        except ValueError as error:
            raise StoreCorruptionError("invalid snapshot JSON") from error
        if canonical != str(latest["state_json"]):
            raise StoreCorruptionError("snapshot JSON is not canonical")
        expected_mapping = _snapshot_mapping(rebuilt)
        if not _json_values_match(expected_mapping, persisted_mapping):
            raise StoreCorruptionError("snapshot does not match event replay")
        if int(latest["sequence"]) != rebuilt.last_sequence:
            raise StoreCorruptionError("snapshot does not match event replay")
        stored_created_at, _ = _parse_stored_timestamp(latest["created_at_utc"])
        expected_created_at = (
            _canonical_datetime(rebuilt.event_evidence[-1].occurred_at_utc)
            if rebuilt.event_evidence
            else _EPOCH
        )
        if stored_created_at != expected_created_at:
            raise StoreCorruptionError("snapshot timestamp does not match event replay")
        return rebuilt

    def _rebuild_from_events(self, connection: sqlite3.Connection) -> PaperSnapshot:
        cash = _INITIAL_EQUITY
        btc_quantity = 0.0
        btc_cost_basis = 0.0
        last_price: float | None = None
        breaker_state: Mapping[str, object] = MappingProxyType({})
        health_state: Mapping[str, object] = MappingProxyType({})
        evidence: list[StoredEvent] = []
        orders: dict[str, StoredOrder] = {}
        idempotency_keys: set[str] = set()
        previous_timestamp: datetime | None = None
        expected_sequence = 1

        rows = connection.execute(
            """
            SELECT sequence, event_id, event_type, occurred_at_utc, payload_json
            FROM events ORDER BY sequence
            """,
        ).fetchall()
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise StoreCorruptionError("event sequence history is inconsistent")
            expected_sequence += 1
            event_id = _stored_nonempty_text(row["event_id"], "invalid event id")
            event_type = _stored_nonempty_text(row["event_type"], "invalid event type")
            occurred_at_utc, occurred_datetime = _parse_stored_timestamp(
                row["occurred_at_utc"],
            )
            if previous_timestamp is not None and occurred_datetime < previous_timestamp:
                raise StoreCorruptionError("event timestamp reversal")
            previous_timestamp = occurred_datetime
            payload_json = str(row["payload_json"])
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError) as error:
                raise StoreCorruptionError("invalid event JSON") from error
            if not isinstance(payload, dict):
                raise StoreCorruptionError("invalid event JSON")
            try:
                canonical_payload, normalized_payload = _canonical_payload(payload)
            except ValueError as error:
                raise StoreCorruptionError("invalid event JSON") from error
            if canonical_payload != payload_json:
                raise StoreCorruptionError("event JSON is not canonical")
            frozen_payload = _freeze_json(normalized_payload)
            assert isinstance(frozen_payload, Mapping)
            evidence.append(
                StoredEvent(
                    sequence=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at_utc=occurred_datetime,
                    payload=frozen_payload,
                ),
            )

            if event_type == "ORDER_CREATED":
                order = _order_from_created_payload(
                    normalized_payload,
                    occurred_datetime,
                )
                if order.order_id in orders or order.idempotency_key in idempotency_keys:
                    raise StoreCorruptionError("duplicate order identity in event replay")
                orders[order.order_id] = order
                idempotency_keys.add(order.idempotency_key)
            elif event_type == "FILL":
                fill = _fill_from_payload(normalized_payload)
                quantity = fill["quantity"]
                price = fill["price"]
                fee = fill["fee"]
                side = fill["side"]
                order_id = fill["order_id"]
                if side == "BUY":
                    cash -= quantity * price + fee
                    if cash < 0.0:
                        raise StoreCorruptionError("fill replay makes cash negative")
                    if abs(cash) <= _TOLERANCE:
                        cash = 0.0
                    btc_quantity += quantity
                    btc_cost_basis += quantity * price + fee
                else:
                    if quantity > btc_quantity:
                        raise StoreCorruptionError("fill replay oversells BTC")
                    prior_quantity = btc_quantity
                    cash += quantity * price - fee
                    if cash < 0.0:
                        raise StoreCorruptionError("fill replay makes cash negative")
                    if abs(cash) <= _TOLERANCE:
                        cash = 0.0
                    if prior_quantity > _TOLERANCE:
                        btc_cost_basis -= btc_cost_basis * (quantity / prior_quantity)
                    btc_quantity -= quantity
                    if abs(btc_quantity) <= _TOLERANCE:
                        btc_quantity = 0.0
                        btc_cost_basis = 0.0
                last_price = price
                if order_id in orders:
                    existing = orders[order_id]
                    if existing.side != side:
                        raise StoreCorruptionError("fill side conflicts with order replay")
                    if existing.status not in _ACTIVE_ORDER_STATUSES:
                        raise StoreCorruptionError("fill follows a terminal order")
                    total_filled = existing.filled_quantity + quantity
                    if total_filled > existing.requested_quantity:
                        raise StoreCorruptionError("fill exceeds requested quantity in replay")
                    if _numbers_close(total_filled, existing.requested_quantity):
                        total_filled = existing.requested_quantity
                        next_status = OrderStatus.COMPLETED
                    else:
                        next_status = OrderStatus.PARTIAL
                    orders[order_id] = StoredOrder(
                        order_id=existing.order_id,
                        idempotency_key=existing.idempotency_key,
                        side=existing.side,
                        requested_quantity=existing.requested_quantity,
                        filled_quantity=total_filled,
                        status=next_status,
                        updated_at_utc=occurred_datetime,
                    )
            elif event_type == "BREAKER_STATE":
                breaker_state = _freeze_json(normalized_payload)
                assert isinstance(breaker_state, Mapping)
            elif event_type == "HEALTH_STATE":
                health_state = _freeze_json(normalized_payload)
                assert isinstance(health_state, Mapping)

        autoincrement_row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'events'",
        ).fetchone()
        recorded_tail = int(autoincrement_row["seq"]) if autoincrement_row is not None else 0
        replayed_tail = evidence[-1].sequence if evidence else 0
        if recorded_tail != replayed_tail:
            raise StoreCorruptionError("event tail was deleted from immutable history")

        self._verify_order_projection(connection, orders)
        pending_orders = tuple(
            sorted(
                (
                    order
                    for order in orders.values()
                    if order.status in _ACTIVE_ORDER_STATUSES
                ),
                key=lambda order: (order.updated_at_utc, order.order_id),
            ),
        )
        position_state = _position_state(
            btc_quantity=btc_quantity,
            pending_orders=pending_orders,
            breaker_state=breaker_state,
        )
        average_entry_price = (
            btc_cost_basis / btc_quantity if btc_quantity > _TOLERANCE else None
        )
        equity = cash + btc_quantity * last_price if last_price is not None else cash
        return PaperSnapshot(
            market=_MARKET,
            initial_equity=_INITIAL_EQUITY,
            cash=cash,
            btc_quantity=btc_quantity,
            equity=equity,
            btc_cost_basis=btc_cost_basis,
            average_entry_price=average_entry_price,
            last_price=last_price,
            position_state=position_state,
            pending_orders=pending_orders,
            last_sequence=evidence[-1].sequence if evidence else 0,
            breaker_state=breaker_state,
            health_state=health_state,
            event_evidence=tuple(evidence),
        )

    def _verify_order_projection(
        self,
        connection: sqlite3.Connection,
        replayed: Mapping[str, StoredOrder],
    ) -> None:
        projected: dict[str, StoredOrder] = {}
        idempotency_keys: set[str] = set()
        for row in connection.execute(
            """
            SELECT order_id, idempotency_key, side, requested_quantity,
                   filled_quantity, status, updated_at_utc
            FROM orders ORDER BY order_id
            """,
        ):
            order_id = _stored_nonempty_text(row["order_id"], "invalid projected order id")
            key = _stored_nonempty_text(
                row["idempotency_key"],
                "invalid projected idempotency key",
            )
            if order_id in projected or key in idempotency_keys:
                raise StoreCorruptionError("duplicate identity in order projection")
            side = _stored_side(row["side"])
            requested = _stored_positive_number(
                row["requested_quantity"],
                "invalid requested quantity in order projection",
            )
            filled = _stored_nonnegative_number(
                row["filled_quantity"],
                "invalid filled quantity in order projection",
            )
            if filled > requested:
                raise StoreCorruptionError("filled quantity exceeds requested projection")
            status = _stored_order_status(row["status"])
            _, updated_at = _parse_stored_timestamp(row["updated_at_utc"])
            projected[order_id] = StoredOrder(
                order_id=order_id,
                idempotency_key=key,
                side=side,
                requested_quantity=requested,
                filled_quantity=filled,
                status=status,
                updated_at_utc=updated_at,
            )
            idempotency_keys.add(key)
        if set(projected) != set(replayed):
            raise StoreCorruptionError("order projection does not match event replay")
        for order_id, expected in replayed.items():
            actual = projected[order_id]
            if not _orders_match(expected, actual):
                raise StoreCorruptionError("order projection does not match event replay")

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        state: PaperSnapshot,
        created_at_utc: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO snapshots (sequence, state_json, created_at_utc)
            VALUES (?, ?, ?)
            """,
            (state.last_sequence, _snapshot_json(state), created_at_utc),
        )

    def _validate_timestamp_order(
        self,
        connection: sqlite3.Connection,
        occurred_at: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT occurred_at_utc FROM events ORDER BY sequence DESC LIMIT 1",
        ).fetchone()
        if row is None:
            return
        _, latest = _parse_stored_timestamp(row["occurred_at_utc"])
        if occurred_at < latest:
            raise ValueError("timestamp reversal is not allowed")

    def _ensure_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError("store is closed")

    def _ensure_ready(self) -> None:
        self._ensure_not_closed()
        if not self._initialized or self._connection is None:
            raise RuntimeError("store is not initialized")


def _initial_snapshot() -> PaperSnapshot:
    return PaperSnapshot(
        market=_MARKET,
        initial_equity=_INITIAL_EQUITY,
        cash=_INITIAL_EQUITY,
        btc_quantity=0.0,
        equity=_INITIAL_EQUITY,
        btc_cost_basis=0.0,
        average_entry_price=None,
        last_price=None,
        position_state=PositionState.FLAT,
        pending_orders=(),
        last_sequence=0,
        breaker_state=MappingProxyType({}),
        health_state=MappingProxyType({}),
        event_evidence=(),
    )


def _snapshot_mapping(state: PaperSnapshot) -> dict[str, object]:
    return {
        "average_entry_price": state.average_entry_price,
        "breaker_state": _thaw_json(state.breaker_state),
        "btc_cost_basis": state.btc_cost_basis,
        "btc_quantity": state.btc_quantity,
        "cash": state.cash,
        "equity": state.equity,
        "health_state": _thaw_json(state.health_state),
        "initial_equity": state.initial_equity,
        "last_price": state.last_price,
        "last_sequence": state.last_sequence,
        "market": state.market,
        "pending_orders": [
            {
                "filled_quantity": order.filled_quantity,
                "idempotency_key": order.idempotency_key,
                "order_id": order.order_id,
                "requested_quantity": order.requested_quantity,
                "side": order.side,
                "status": order.status.value,
                "updated_at_utc": _canonical_datetime(order.updated_at_utc),
            }
            for order in state.pending_orders
        ],
        "position_state": state.position_state.value,
    }


def _snapshot_json(state: PaperSnapshot) -> str:
    return _canonical_json_value(_snapshot_mapping(state))


def _position_state(
    *,
    btc_quantity: float,
    pending_orders: tuple[StoredOrder, ...],
    breaker_state: Mapping[str, object],
) -> PositionState:
    if breaker_state.get("halt_entries") is True:
        return PositionState.HALTED
    if any(order.side == "SELL" for order in pending_orders):
        return PositionState.EXIT_PENDING
    if btc_quantity > _TOLERANCE:
        return PositionState.LONG
    if any(order.side == "BUY" for order in pending_orders):
        return PositionState.ENTRY_PENDING
    return PositionState.FLAT


def _order_from_created_payload(
    payload: Mapping[str, object],
    occurred_at: datetime,
) -> StoredOrder:
    expected_keys = {
        "filled_quantity",
        "idempotency_key",
        "order_id",
        "requested_quantity",
        "side",
        "status",
    }
    if set(payload) != expected_keys:
        raise StoreCorruptionError("invalid ORDER_CREATED payload")
    order_id = _stored_nonempty_text(payload["order_id"], "invalid ORDER_CREATED order id")
    key = _stored_nonempty_text(
        payload["idempotency_key"],
        "invalid ORDER_CREATED idempotency key",
    )
    side = _stored_side(payload["side"])
    requested = _stored_positive_number(
        payload["requested_quantity"],
        "invalid ORDER_CREATED requested quantity",
    )
    filled = _stored_nonnegative_number(
        payload["filled_quantity"],
        "invalid ORDER_CREATED filled quantity",
    )
    if filled != 0.0:
        raise StoreCorruptionError("ORDER_CREATED must start unfilled")
    status = _stored_order_status(payload["status"])
    return StoredOrder(
        order_id=order_id,
        idempotency_key=key,
        side=side,
        requested_quantity=requested,
        filled_quantity=filled,
        status=status,
        updated_at_utc=occurred_at,
    )


def _fill_from_payload(payload: Mapping[str, object]) -> dict[str, Any]:
    if set(payload) != {"fee", "order_id", "price", "quantity", "side"}:
        raise StoreCorruptionError("invalid FILL payload")
    return {
        "fee": _stored_nonnegative_number(payload["fee"], "invalid fill fee"),
        "order_id": _stored_nonempty_text(payload["order_id"], "invalid fill order id"),
        "price": _stored_positive_number(payload["price"], "invalid fill price"),
        "quantity": _stored_positive_number(payload["quantity"], "invalid fill quantity"),
        "side": _stored_side(payload["side"]),
    }


def _orders_match(first: StoredOrder, second: StoredOrder) -> bool:
    return (
        first.order_id == second.order_id
        and first.idempotency_key == second.idempotency_key
        and first.side == second.side
        and _numbers_close(first.requested_quantity, second.requested_quantity)
        and _numbers_close(first.filled_quantity, second.filled_quantity)
        and first.status is second.status
        and first.updated_at_utc == second.updated_at_utc
    )


def _canonical_payload(
    payload: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a JSON object")
    normalized = _normalize_json(payload)
    assert isinstance(normalized, dict)
    return _canonical_json_value(normalized), normalized


def _canonical_json_value(value: object) -> str:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            normalized[key] = _normalize_json(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json(item) for item in value]
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_values_match(expected: object, actual: object) -> bool:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        return set(expected) == set(actual) and all(
            _json_values_match(expected[key], actual[key]) for key in expected
        )
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _json_values_match(left, right)
            for left, right in zip(expected, actual, strict=True)
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return _numbers_close(float(expected), float(actual))
    return expected == actual


def _canonical_timestamp(value: str | datetime) -> tuple[str, datetime]:
    if isinstance(value, str):
        if _UTC_Z_PATTERN.fullmatch(value) is None:
            raise ValueError("timestamp must be strict UTC with a Z suffix")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise ValueError("timestamp must be strict UTC with a Z suffix") from error
    elif isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be strict UTC with a Z suffix")
        if value.utcoffset().total_seconds() != 0.0:
            raise ValueError("timestamp must be strict UTC with a Z suffix")
        parsed = value.astimezone(timezone.utc)
    else:
        raise ValueError("timestamp must be strict UTC with a Z suffix")
    canonical = _canonical_datetime(parsed)
    return canonical, parsed.astimezone(timezone.utc)


def _canonical_datetime(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    if utc_value.microsecond:
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_stored_timestamp(value: object) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise StoreCorruptionError("stored timestamp is not text")
    try:
        canonical, parsed = _canonical_timestamp(value)
    except ValueError as error:
        raise StoreCorruptionError("stored timestamp is not strict UTC") from error
    if canonical != value:
        raise StoreCorruptionError("stored timestamp is not canonical")
    return canonical, parsed


def _valid_side(value: object) -> str:
    if not isinstance(value, str) or value not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return value


def _stored_side(value: object) -> str:
    try:
        return _valid_side(value)
    except ValueError as error:
        raise StoreCorruptionError("invalid side in stored evidence") from error


def _finite_number(value: object, message: str) -> float:
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(message) from error
    if not math.isfinite(number):
        raise ValueError(message)
    return number


def _positive_number(value: object, message: str) -> float:
    number = _finite_number(value, message)
    if number <= 0.0:
        raise ValueError(message)
    return number


def _nonnegative_number(value: object, message: str) -> float:
    number = _finite_number(value, message)
    if number < 0.0:
        raise ValueError(message)
    return number


def _stored_positive_number(value: object, message: str) -> float:
    try:
        return _positive_number(value, message)
    except ValueError as error:
        raise StoreCorruptionError(message) from error


def _stored_nonnegative_number(value: object, message: str) -> float:
    try:
        return _nonnegative_number(value, message)
    except ValueError as error:
        raise StoreCorruptionError(message) from error


def _nonempty_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(message)
    return value


def _stored_nonempty_text(value: object, message: str) -> str:
    try:
        return _nonempty_text(value, message)
    except ValueError as error:
        raise StoreCorruptionError(message) from error


def _stored_order_status(value: object) -> OrderStatus:
    if not isinstance(value, str):
        raise StoreCorruptionError("invalid order status in stored evidence")
    try:
        return OrderStatus(value)
    except ValueError as error:
        raise StoreCorruptionError("invalid order status in stored evidence") from error


def _numbers_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=_TOLERANCE)

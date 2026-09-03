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
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from types import MappingProxyType
from typing import Any

from autobit.domain.models import OrderStatus, PositionState


SCHEMA_VERSION = 1
_MARKET = "KRW-BTC"
_INITIAL_EQUITY = 100.0
_TOLERANCE = 1e-10
_EPOCH = "1970-01-01T00:00:00Z"
_EMPTY_EVENT_DIGEST = sha256(b"").hexdigest()
_CYCLE_LEASE_KEY = "paper_cycle_lease"
_FileIdentity = tuple[int, int, int, int, int, int, int]
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
_LEGAL_ORDER_TRANSITIONS = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.CANCELED,
            OrderStatus.INSUFFICIENT_CASH,
            OrderStatus.REJECTED,
        },
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.INSUFFICIENT_CASH,
            OrderStatus.REJECTED,
        },
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.INSUFFICIENT_CASH,
            OrderStatus.REJECTED,
        },
    ),
    OrderStatus.PARTIAL: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.INSUFFICIENT_CASH,
            OrderStatus.REJECTED,
        },
    ),
}
_REQUIRED_TABLES = frozenset(
    {"schema_version", "metadata", "events", "orders", "snapshots"},
)
_EXPECTED_TABLE_INFO = {
    "schema_version": (("version", "INTEGER", 0, 1),),
    "metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
    "events": (
        ("sequence", "INTEGER", 0, 1),
        ("event_id", "TEXT", 1, 0),
        ("event_type", "TEXT", 1, 0),
        ("occurred_at_utc", "TEXT", 1, 0),
        ("payload_json", "TEXT", 1, 0),
    ),
    "orders": (
        ("order_id", "TEXT", 0, 1),
        ("idempotency_key", "TEXT", 1, 0),
        ("side", "TEXT", 1, 0),
        ("requested_quantity", "REAL", 1, 0),
        ("filled_quantity", "REAL", 1, 0),
        ("status", "TEXT", 1, 0),
        ("updated_at_utc", "TEXT", 1, 0),
    ),
    "snapshots": (
        ("sequence", "INTEGER", 0, 1),
        ("state_json", "TEXT", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
    ),
}
_TRANSACTION_CONTROL_KEYWORDS = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"},
)
_REASON_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_KNOWN_BOOLEAN_STATE_FIELDS = frozenset(
    {
        "halt_entries",
        "ledger_matches",
        "timestamps_monotonic",
        "latest_candle_valid",
    },
)
_KNOWN_COUNT_STATE_FIELDS = frozenset(
    {"api_failures", "api_successes", "unresolved_orders"},
)


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
    event_digest: str


@dataclass(frozen=True, slots=True)
class _ReplayResult:
    state: PaperSnapshot
    snapshot_mappings: tuple[dict[str, object], ...]
    snapshot_timestamps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DdlToken:
    kind: str
    value: str


class _RestrictedCursor:
    """Cursor results without a route back to the owned SQLite connection."""

    __slots__ = ("__cursor",)

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.__cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self.__cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self.__cursor.rowcount

    def fetchone(self) -> sqlite3.Row | tuple[Any, ...] | None:
        return self.__cursor.fetchone()

    def fetchall(self) -> list[sqlite3.Row] | list[tuple[Any, ...]]:
        return self.__cursor.fetchall()

    def __iter__(self):
        return iter(self.__cursor)


class _TransactionFacade:
    """Restricted SQL view that cannot end or replace the store-owned transaction."""

    __slots__ = ("__connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.__connection = connection

    def execute(
        self,
        sql: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> _RestrictedCursor:
        _reject_transaction_control_sql(sql)
        return _RestrictedCursor(self.__connection.execute(sql, parameters))

    def executemany(
        self,
        sql: str,
        parameters: Sequence[Sequence[object]],
    ) -> _RestrictedCursor:
        _reject_transaction_control_sql(sql)
        return _RestrictedCursor(self.__connection.executemany(sql, parameters))


class SQLiteStore:
    """One explicit-transaction SQLite connection for a normalized paper ledger."""

    def __init__(
        self,
        path: str | Path,
        *,
        _read_only: bool = False,
        _connection_path: Path | None = None,
        _temporary_snapshot: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._read_only = _read_only
        self._connection_path = _connection_path or self.path
        self._temporary_snapshot = _temporary_snapshot
        self._connection: sqlite3.Connection | None = None
        self._initialized = False
        self._closed = False
        self._lock = threading.RLock()
        self._savepoint_counter = 0

    @classmethod
    def open_read_only(cls, path: str | Path) -> SQLiteStore:
        """Open and fully verify an existing ledger without initialization writes."""
        resolved = Path(os.path.abspath(path))
        main_identity = _literal_regular_file_identity(resolved, required=True)
        if main_identity is None:  # Defensive: ``required=True`` rejects absence.
            raise StoreError("read-only store requires an existing regular database file")
        wal_identity = _literal_regular_file_identity(Path(f"{resolved}-wal"), required=False)
        temporary = tempfile.TemporaryDirectory(prefix="autobit-status-")
        snapshot_path = Path(temporary.name) / resolved.name
        try:
            _copy_stable_sqlite_snapshot(
                resolved,
                snapshot_path,
                expected_main_identity=main_identity,
                expected_wal_identity=wal_identity,
            )
        except BaseException:
            temporary.cleanup()
            raise
        store = cls(
            resolved,
            _read_only=True,
            _connection_path=snapshot_path,
            _temporary_snapshot=temporary,
        )
        try:
            connection = store._connect()
            store._validate_schema_and_identity(connection)
            store._replay_and_verify(connection)
            store._initialized = True
        except BaseException:
            store.close()
            raise
        return store

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
        if self._read_only:
            raise StoreError("read-only store cannot be initialized")
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
                with self._read_transaction() as read_connection:
                    self._validate_schema_and_identity(read_connection)
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
                with self._read_transaction() as read_connection:
                    self._replay_and_verify(read_connection)
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
            if self._temporary_snapshot is not None:
                self._temporary_snapshot.cleanup()
                self._temporary_snapshot = None
            self._initialized = False
            self._closed = True

    @contextmanager
    def transaction(self) -> Iterator[_TransactionFacade]:
        """Run a public mutation boundary using ``BEGIN IMMEDIATE``."""
        self._ensure_ready()
        self._ensure_writable()
        with self._transaction(require_initialized=True) as connection:
            yield _TransactionFacade(connection)
            self._replay_and_verify(connection)

    def acquire_cycle_lease(
        self,
        owner: str,
        token: str,
        now_utc: datetime,
        expires_at_utc: datetime,
    ) -> bool:
        """Atomically acquire or renew the one paper-cycle process lease."""
        normalized_owner = _nonempty_text(owner, "lease owner must be non-empty")
        normalized_token = _nonempty_text(token, "lease token must be non-empty")
        _, now = _canonical_timestamp(now_utc)
        expires_text, expires = _canonical_timestamp(expires_at_utc)
        if expires <= now:
            raise ValueError("lease expiry must be strictly after now")
        value = _canonical_json_value(
            {
                "expires_at_utc": expires_text,
                "owner": normalized_owner,
                "token": normalized_token,
            }
        )

        with self._mutation_transaction() as connection:
            self._replay_and_verify(connection)
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (_CYCLE_LEASE_KEY,),
            ).fetchone()
            if row is not None:
                current_owner, current_token, current_expiry = _cycle_lease_metadata(
                    row["value"],
                    corruption=True,
                )
                same_holder = (
                    current_owner == normalized_owner and current_token == normalized_token
                )
                if not same_holder and current_expiry > now:
                    return False
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = ?",
                    (value, _CYCLE_LEASE_KEY),
                )
            else:
                connection.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (_CYCLE_LEASE_KEY, value),
                )
            return True

    def release_cycle_lease(self, owner: str, token: str) -> bool:
        """Release the paper-cycle lease only for its exact owner and token."""
        normalized_owner = _nonempty_text(owner, "lease owner must be non-empty")
        normalized_token = _nonempty_text(token, "lease token must be non-empty")
        with self._mutation_transaction() as connection:
            self._replay_and_verify(connection)
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (_CYCLE_LEASE_KEY,),
            ).fetchone()
            if row is None:
                return False
            current_owner, current_token, _ = _cycle_lease_metadata(
                row["value"],
                corruption=True,
            )
            if current_owner != normalized_owner or current_token != normalized_token:
                return False
            connection.execute(
                "DELETE FROM metadata WHERE key = ?",
                (_CYCLE_LEASE_KEY,),
            )
            return True

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
        if isinstance(payload, Mapping):
            _validate_known_state_payload(
                normalized_event_type,
                payload,
                corruption=False,
            )
        payload_json, normalized_payload = _canonical_payload(payload)
        _validate_known_state_payload(
            normalized_event_type,
            normalized_payload,
            corruption=False,
        )

        with self._mutation_transaction() as connection:
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
            replay = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, replay.state, occurred_at_utc)
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

        with self._mutation_transaction() as connection:
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
            replay = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, replay.state, occurred_at_utc)
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

        with self._mutation_transaction() as connection:
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
            quantity_decimal = Decimal(str(normalized_quantity))
            price_decimal = Decimal(str(normalized_price))
            fee_decimal = Decimal(str(normalized_fee))
            cash_decimal = Decimal(str(current.cash))
            btc_decimal = Decimal(str(current.btc_quantity))
            if normalized_side == "BUY":
                resulting_cash = cash_decimal - (
                    quantity_decimal * price_decimal + fee_decimal
                )
            else:
                if quantity_decimal > btc_decimal:
                    raise ValueError("fill would oversell BTC")
                resulting_cash = cash_decimal + (
                    quantity_decimal * price_decimal - fee_decimal
                )
            if resulting_cash < 0:
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
                total_filled = Decimal(str(already_filled)) + quantity_decimal
                requested_decimal = Decimal(str(requested))
                if total_filled > requested_decimal:
                    raise ValueError("fill exceeds requested quantity")
                if total_filled == requested_decimal:
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
                        float(total_filled),
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
            replay = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, replay.state, occurred_at_utc)
            return True

    def transition_order_status(
        self,
        order_id: str,
        idempotency_key: str,
        status: OrderStatus,
        occurred_at: str | datetime,
        *,
        reason: str | None = None,
    ) -> bool:
        """Persist one legal, idempotent order lifecycle transition."""
        normalized_order_id = _nonempty_text(order_id, "order id must be non-empty")
        normalized_key = _nonempty_text(
            idempotency_key,
            "idempotency key must be non-empty",
        )
        if not isinstance(status, OrderStatus):
            raise ValueError("status must be a valid OrderStatus")
        if reason is not None:
            normalized_reason = _nonempty_text(reason, "reason must be non-empty")
        else:
            normalized_reason = None
        occurred_at_utc, occurred_datetime = _canonical_timestamp(occurred_at)
        event_id = f"order-status:{sha256(normalized_key.encode('utf-8')).hexdigest()}"
        payload = {
            "idempotency_key": normalized_key,
            "order_id": normalized_order_id,
            "reason": normalized_reason,
            "status": status.value,
        }
        payload_json, _ = _canonical_payload(payload)

        with self._mutation_transaction() as connection:
            self._replay_and_verify(connection)
            existing_event = connection.execute(
                """
                SELECT event_type, occurred_at_utc, payload_json
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if existing_event is not None:
                if (
                    str(existing_event["event_type"]) == "ORDER_STATUS"
                    and str(existing_event["occurred_at_utc"]) == occurred_at_utc
                    and str(existing_event["payload_json"]) == payload_json
                ):
                    return False
                raise IdempotencyConflictError(
                    f"idempotency key has different transition payload: {normalized_key}",
                )

            order = connection.execute(
                """
                SELECT requested_quantity, filled_quantity, status
                FROM orders WHERE order_id = ?
                """,
                (normalized_order_id,),
            ).fetchone()
            if order is None:
                raise ValueError(f"unknown order id: {normalized_order_id}")
            current_status = _stored_order_status(order["status"])
            requested_quantity = _stored_positive_number(
                order["requested_quantity"],
                "invalid requested quantity in order projection",
            )
            filled_quantity = _stored_nonnegative_number(
                order["filled_quantity"],
                "invalid filled quantity in order projection",
            )
            _validate_order_status_transition(
                current_status,
                status,
                requested_quantity=requested_quantity,
                filled_quantity=filled_quantity,
                corruption=False,
            )
            self._validate_timestamp_order(connection, occurred_datetime)
            connection.execute(
                """
                INSERT INTO events (event_id, event_type, occurred_at_utc, payload_json)
                VALUES (?, 'ORDER_STATUS', ?, ?)
                """,
                (event_id, occurred_at_utc, payload_json),
            )
            connection.execute(
                """
                UPDATE orders SET status = ?, updated_at_utc = ? WHERE order_id = ?
                """,
                (status.value, occurred_at_utc, normalized_order_id),
            )
            replay = self._rebuild_from_events(connection)
            self._insert_snapshot(connection, replay.state, occurred_at_utc)
            return True

    def load_snapshot(self) -> PaperSnapshot:
        """Load the latest snapshot only after checking it against event replay."""
        self._ensure_ready()
        with self._read_transaction() as connection:
            return self._replay_and_verify(connection)

    def replay_state(self) -> PaperSnapshot:
        """Replay every immutable event and verify both mutable projections."""
        self._ensure_ready()
        with self._read_transaction() as connection:
            return self._replay_and_verify(connection)

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Pin all replay queries to one SQLite MVCC snapshot."""
        self._ensure_ready()
        connection = self._connect()
        with self._lock:
            owns_transaction = not connection.in_transaction
            if owns_transaction:
                connection.execute("BEGIN")
            try:
                yield connection
            finally:
                if owns_transaction and connection.in_transaction:
                    connection.rollback()

    @contextmanager
    def _mutation_transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose the raw connection only to store-owned mutation methods."""
        self._ensure_writable()
        with self._transaction(require_initialized=True) as connection:
            yield connection
            self._replay_and_verify(connection)

    @contextmanager
    def _transaction(
        self,
        *,
        require_initialized: bool,
    ) -> Iterator[sqlite3.Connection]:
        self._ensure_writable()
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
            if self._read_only:
                uri = f"{self._connection_path.resolve().as_uri()}?mode=ro"
                self._connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=5.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._connection = sqlite3.connect(
                    self.path,
                    timeout=5.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
            self._connection.row_factory = sqlite3.Row
            if self._read_only:
                self._connection.execute("PRAGMA query_only=ON")
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
        try:
            tables = self._application_tables(connection)
            if "schema_version" in tables:
                rows = connection.execute(
                    "SELECT version FROM schema_version ORDER BY version",
                ).fetchall()
                versions = [row["version"] for row in rows]
                if versions != [SCHEMA_VERSION]:
                    values = [row[0] for row in rows]
                    raise StoreCorruptionError(
                        f"unsupported schema version: {values}",
                    )
            if tables != _REQUIRED_TABLES:
                raise StoreCorruptionError(
                    f"schema tables are inconsistent: {sorted(tables)}",
                )
            self._validate_schema_definition(connection)
            metadata_rows = connection.execute(
                "SELECT key, value FROM metadata ORDER BY key",
            ).fetchall()
            metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
            metadata_keys = set(metadata)
            if metadata_keys not in (
                {"initial_equity", "market"},
                {"initial_equity", "market", _CYCLE_LEASE_KEY},
            ):
                raise StoreCorruptionError("metadata keys are inconsistent")
            if metadata["market"] != _MARKET:
                raise StoreCorruptionError(
                    f"stored market is not KRW-BTC: {metadata['market']}",
                )
            stored_equity = float(metadata["initial_equity"])
            if not math.isfinite(stored_equity) or stored_equity != _INITIAL_EQUITY:
                raise StoreCorruptionError(
                    f"stored initial equity is not 100: {metadata['initial_equity']}",
                )
            if _CYCLE_LEASE_KEY in metadata:
                _cycle_lease_metadata(metadata[_CYCLE_LEASE_KEY], corruption=True)
        except StoreCorruptionError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError, KeyError) as error:
            raise StoreCorruptionError("schema introspection or metadata is invalid") from error

    def _validate_schema_definition(self, connection: sqlite3.Connection) -> None:
        for table, expected_info in _EXPECTED_TABLE_INFO.items():
            expected_xinfo = tuple((*column, 0) for column in expected_info)
            actual_info = tuple(
                (
                    str(row["name"]),
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                    int(row["hidden"]),
                )
                for row in connection.execute(f"PRAGMA table_xinfo({table})")
            )
            if actual_info != expected_xinfo:
                raise StoreCorruptionError(
                    f"schema definition is inconsistent for {table}: {actual_info}",
                )

        events_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'",
        ).fetchone()
        events_sql = str(events_sql_row["sql"]) if events_sql_row is not None else ""
        if not _events_sequence_uses_autoincrement(events_sql):
            raise StoreCorruptionError("schema constraint AUTOINCREMENT is missing for events")

        event_unique = self._unique_index_columns(connection, "events")
        if ("event_id",) not in event_unique:
            raise StoreCorruptionError("schema constraint UNIQUE(event_id) is missing")
        order_unique = self._unique_index_columns(connection, "orders")
        if ("idempotency_key",) not in order_unique:
            raise StoreCorruptionError(
                "schema constraint UNIQUE(idempotency_key) is missing",
            )

    def _unique_index_columns(
        self,
        connection: sqlite3.Connection,
        table: str,
    ) -> frozenset[tuple[str, ...]]:
        unique_columns: set[tuple[str, ...]] = set()
        for row in connection.execute(f"PRAGMA index_list({table})"):
            if int(row["unique"]) != 1 or int(row["partial"]) != 0:
                continue
            index_name = str(row["name"])
            columns = tuple(
                str(index_row["name"])
                for index_row in connection.execute(
                    f"PRAGMA index_info('{index_name}')",
                )
            )
            unique_columns.add(columns)
        return frozenset(unique_columns)

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
        replay = self._rebuild_from_events(connection)
        snapshot_rows = connection.execute(
            "SELECT sequence, state_json, created_at_utc FROM snapshots ORDER BY sequence",
        ).fetchall()
        expected_sequences = list(range(len(replay.snapshot_mappings)))
        actual_sequences = [int(row["sequence"]) for row in snapshot_rows]
        if actual_sequences != expected_sequences:
            raise StoreCorruptionError("snapshot sequence history is inconsistent")
        for row, expected_mapping, expected_timestamp in zip(
            snapshot_rows,
            replay.snapshot_mappings,
            replay.snapshot_timestamps,
            strict=True,
        ):
            sequence = int(row["sequence"])
            try:
                persisted_mapping = json.loads(str(row["state_json"]))
            except (json.JSONDecodeError, TypeError) as error:
                raise StoreCorruptionError(
                    f"invalid snapshot JSON at sequence {sequence}",
                ) from error
            if not isinstance(persisted_mapping, dict):
                raise StoreCorruptionError(
                    f"invalid snapshot JSON at sequence {sequence}",
                )
            try:
                canonical = _canonical_json_value(persisted_mapping)
            except ValueError as error:
                raise StoreCorruptionError(
                    f"invalid snapshot JSON at sequence {sequence}",
                ) from error
            if canonical != str(row["state_json"]):
                raise StoreCorruptionError(
                    f"snapshot JSON is not canonical at sequence {sequence}",
                )
            expected_json = _canonical_json_value(expected_mapping)
            if canonical != expected_json:
                raise StoreCorruptionError(
                    f"snapshot does not match event replay at sequence {sequence}",
                )
            stored_created_at, _ = _parse_stored_timestamp(row["created_at_utc"])
            if stored_created_at != expected_timestamp:
                raise StoreCorruptionError(
                    f"snapshot timestamp does not match event replay at sequence {sequence}",
                )
        return replay.state

    def _rebuild_from_events(self, connection: sqlite3.Connection) -> _ReplayResult:
        cash = Decimal("100.0")
        btc_quantity = Decimal("0")
        btc_cost_basis = Decimal("0")
        last_price: Decimal | None = None
        breaker_state: Mapping[str, object] = MappingProxyType({})
        health_state: Mapping[str, object] = MappingProxyType({})
        evidence: list[StoredEvent] = []
        orders: dict[str, StoredOrder] = {}
        idempotency_keys: set[str] = set()
        previous_timestamp: datetime | None = None
        expected_sequence = 1
        event_digest = _EMPTY_EVENT_DIGEST
        snapshot_mappings: list[dict[str, object]] = [
            _snapshot_mapping(_initial_snapshot()),
        ]
        snapshot_timestamps = [_EPOCH]

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
            _validate_known_state_payload(
                event_type,
                normalized_payload,
                corruption=True,
            )
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
            envelope = {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at_utc": occurred_at_utc,
                "payload": normalized_payload,
                "previous_digest": event_digest,
                "sequence": sequence,
            }
            event_digest = sha256(
                _canonical_json_value(envelope).encode("utf-8"),
            ).hexdigest()

            if event_type == "ORDER_CREATED":
                order = _order_from_created_payload(
                    normalized_payload,
                    occurred_datetime,
                )
                expected_event_id = (
                    "order-created:"
                    f"{sha256(order.idempotency_key.encode('utf-8')).hexdigest()}"
                )
                if event_id != expected_event_id:
                    raise StoreCorruptionError("ORDER_CREATED event id is not deterministic")
                if order.order_id in orders or order.idempotency_key in idempotency_keys:
                    raise StoreCorruptionError("duplicate order identity in event replay")
                orders[order.order_id] = order
                idempotency_keys.add(order.idempotency_key)
            elif event_type == "FILL":
                fill = _fill_from_payload(normalized_payload)
                quantity = Decimal(str(fill["quantity"]))
                price = Decimal(str(fill["price"]))
                fee = Decimal(str(fill["fee"]))
                side = fill["side"]
                order_id = fill["order_id"]
                if side == "BUY":
                    cash -= quantity * price + fee
                    if cash < 0:
                        raise StoreCorruptionError("fill replay makes cash negative")
                    btc_quantity += quantity
                    btc_cost_basis += quantity * price + fee
                else:
                    if quantity > btc_quantity:
                        raise StoreCorruptionError("fill replay oversells BTC")
                    prior_quantity = btc_quantity
                    cash += quantity * price - fee
                    if cash < 0:
                        raise StoreCorruptionError("fill replay makes cash negative")
                    if prior_quantity > 0:
                        btc_cost_basis -= btc_cost_basis * (quantity / prior_quantity)
                    btc_quantity -= quantity
                    if btc_quantity == 0:
                        btc_cost_basis = Decimal("0")
                last_price = price
                if order_id in orders:
                    existing = orders[order_id]
                    if existing.side != side:
                        raise StoreCorruptionError("fill side conflicts with order replay")
                    if existing.status not in _ACTIVE_ORDER_STATUSES:
                        raise StoreCorruptionError("fill follows a terminal order")
                    total_filled = Decimal(str(existing.filled_quantity)) + quantity
                    requested_quantity = Decimal(str(existing.requested_quantity))
                    if total_filled > requested_quantity:
                        raise StoreCorruptionError("fill exceeds requested quantity in replay")
                    if total_filled == requested_quantity:
                        next_status = OrderStatus.COMPLETED
                    else:
                        next_status = OrderStatus.PARTIAL
                    orders[order_id] = StoredOrder(
                        order_id=existing.order_id,
                        idempotency_key=existing.idempotency_key,
                        side=existing.side,
                        requested_quantity=existing.requested_quantity,
                        filled_quantity=float(total_filled),
                        status=next_status,
                        updated_at_utc=occurred_datetime,
                    )
            elif event_type == "ORDER_STATUS":
                transition = _order_status_from_payload(normalized_payload)
                expected_event_id = (
                    "order-status:"
                    f"{sha256(transition['idempotency_key'].encode('utf-8')).hexdigest()}"
                )
                if event_id != expected_event_id:
                    raise StoreCorruptionError("ORDER_STATUS event id is not deterministic")
                order_id = transition["order_id"]
                if order_id not in orders:
                    raise StoreCorruptionError("ORDER_STATUS references an unknown order")
                existing = orders[order_id]
                _validate_order_status_transition(
                    existing.status,
                    transition["status"],
                    requested_quantity=existing.requested_quantity,
                    filled_quantity=existing.filled_quantity,
                    corruption=True,
                )
                orders[order_id] = StoredOrder(
                    order_id=existing.order_id,
                    idempotency_key=existing.idempotency_key,
                    side=existing.side,
                    requested_quantity=existing.requested_quantity,
                    filled_quantity=existing.filled_quantity,
                    status=transition["status"],
                    updated_at_utc=occurred_datetime,
                )
            elif event_type == "BREAKER_STATE":
                breaker_state = _freeze_json(normalized_payload)
                assert isinstance(breaker_state, Mapping)
            elif event_type == "HEALTH_STATE":
                health_state = _freeze_json(normalized_payload)
                assert isinstance(health_state, Mapping)

            prefix_state = _snapshot_from_components(
                cash=cash,
                btc_quantity=btc_quantity,
                btc_cost_basis=btc_cost_basis,
                last_price=last_price,
                orders=orders,
                breaker_state=breaker_state,
                health_state=health_state,
                last_sequence=sequence,
                event_evidence=(),
                event_digest=event_digest,
            )
            snapshot_mappings.append(_snapshot_mapping(prefix_state))
            snapshot_timestamps.append(occurred_at_utc)

        autoincrement_row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'events'",
        ).fetchone()
        recorded_tail = int(autoincrement_row["seq"]) if autoincrement_row is not None else 0
        replayed_tail = evidence[-1].sequence if evidence else 0
        if recorded_tail != replayed_tail:
            raise StoreCorruptionError("event tail was deleted from immutable history")

        self._verify_order_projection(connection, orders)
        state = _snapshot_from_components(
            cash=cash,
            btc_quantity=btc_quantity,
            btc_cost_basis=btc_cost_basis,
            last_price=last_price,
            orders=orders,
            breaker_state=breaker_state,
            health_state=health_state,
            last_sequence=replayed_tail,
            event_evidence=tuple(evidence),
            event_digest=event_digest,
        )
        return _ReplayResult(
            state=state,
            snapshot_mappings=tuple(snapshot_mappings),
            snapshot_timestamps=tuple(snapshot_timestamps),
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

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise StoreError("store is read-only")


def _copy_stable_sqlite_snapshot(
    source: Path,
    destination: Path,
    *,
    expected_main_identity: _FileIdentity,
    expected_wal_identity: _FileIdentity | None,
) -> None:
    """Copy a stable main/WAL pair without opening or mutating the source."""
    wal_source = Path(f"{source}-wal")
    def capture() -> tuple[bytes, bytes | None]:
        main_identity = _literal_regular_file_identity(source, required=True)
        if main_identity is None or not _same_file_object(
            expected_main_identity,
            main_identity,
        ):
            raise OSError("SQLite main file changed before snapshot capture")
        wal_identity = _literal_regular_file_identity(wal_source, required=False)
        if expected_wal_identity is None:
            if wal_identity is not None:
                raise OSError("SQLite WAL appeared after snapshot preflight")
        elif wal_identity is None or not _same_file_object(
            expected_wal_identity,
            wal_identity,
        ):
            raise OSError("SQLite WAL changed after snapshot preflight")
        main = _read_preflighted_regular_file(source, main_identity)
        wal = _read_preflighted_regular_file(wal_source, wal_identity)
        if _literal_regular_file_identity(source, required=True) != main_identity:
            raise OSError("SQLite main file changed during snapshot capture")
        if _literal_regular_file_identity(wal_source, required=False) != wal_identity:
            raise OSError("SQLite WAL changed during snapshot capture")
        return main, wal

    stable: tuple[bytes, bytes | None] | None = None
    for _ in range(5):
        try:
            first = capture()
            second = capture()
        except OSError:
            continue
        if first == second:
            stable = second
            break
    if stable is None:
        raise StoreError("could not capture a stable read-only database snapshot")
    main, wal = stable
    destination.write_bytes(main)
    if wal is not None:
        Path(f"{destination}-wal").write_bytes(wal)


def _read_literal_regular_file(path: Path, *, required: bool) -> bytes | None:
    identity = _literal_regular_file_identity(path, required=required)
    return _read_preflighted_regular_file(path, identity)


def _read_preflighted_regular_file(
    path: Path,
    identity: _FileIdentity | None,
) -> bytes | None:
    if identity is None:
        if _literal_regular_file_identity(path, required=False) is not None:
            raise OSError("SQLite source appeared during read-only snapshot capture")
        return None
    with path.open("rb") as handle:
        opened = _regular_file_identity(os.fstat(handle.fileno()))
        if not _same_file_binding(identity, opened):
            raise OSError("SQLite source open handle identity changed")
        contents = handle.read()
        after_read = _regular_file_identity(os.fstat(handle.fileno()))
        if opened != after_read or len(contents) != opened[4]:
            raise OSError("SQLite source changed while reading its open handle")
    after_path = _literal_regular_file_identity(path, required=True)
    if identity != after_path:
        raise OSError("SQLite source changed while taking a read-only snapshot")
    return contents


def _literal_regular_file_identity(
    path: Path,
    *,
    required: bool,
) -> _FileIdentity | None:
    _require_safe_parent_chain(path.parent)
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise StoreError("read-only store requires an existing regular database file") from None
        return None
    return _regular_file_identity(details)


def _regular_file_identity(
    details: os.stat_result,
) -> _FileIdentity:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or _is_reparse_point(details)
    ):
        raise StoreError("read-only store rejects database aliases and non-regular files")
    return (
        details.st_dev,
        details.st_ino,
        stat.S_IFMT(details.st_mode),
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _same_file_binding(left: _FileIdentity, right: _FileIdentity) -> bool:
    comparable = slice(0, 6) if os.name == "nt" else slice(None)
    return left[comparable] == right[comparable]


def _same_file_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return left[:4] == right[:4]


def _require_safe_parent_chain(parent: Path) -> None:
    current = Path(os.path.abspath(parent))
    while True:
        try:
            details = os.lstat(current)
        except FileNotFoundError as error:
            raise StoreError("read-only store parent path is missing") from error
        if not stat.S_ISDIR(details.st_mode) or _is_reparse_point(details):
            raise StoreError("read-only store rejects aliased parent paths")
        if current.parent == current:
            return
        current = current.parent


def _is_reparse_point(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


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
        event_digest=_EMPTY_EVENT_DIGEST,
    )


def _snapshot_from_components(
    *,
    cash: Decimal,
    btc_quantity: Decimal,
    btc_cost_basis: Decimal,
    last_price: Decimal | None,
    orders: Mapping[str, StoredOrder],
    breaker_state: Mapping[str, object],
    health_state: Mapping[str, object],
    last_sequence: int,
    event_evidence: tuple[StoredEvent, ...],
    event_digest: str,
) -> PaperSnapshot:
    pending_orders = tuple(
        sorted(
            (order for order in orders.values() if order.status in _ACTIVE_ORDER_STATUSES),
            key=lambda order: (order.updated_at_utc, order.order_id),
        ),
    )
    average_entry_price = (
        float(btc_cost_basis / btc_quantity) if btc_quantity > 0 else None
    )
    equity = cash + (btc_quantity * last_price if last_price is not None else 0)
    return PaperSnapshot(
        market=_MARKET,
        initial_equity=_INITIAL_EQUITY,
        cash=float(cash),
        btc_quantity=float(btc_quantity),
        equity=float(equity),
        btc_cost_basis=float(btc_cost_basis),
        average_entry_price=average_entry_price,
        last_price=float(last_price) if last_price is not None else None,
        position_state=_position_state(
            btc_quantity=float(btc_quantity),
            pending_orders=pending_orders,
            breaker_state=breaker_state,
        ),
        pending_orders=pending_orders,
        last_sequence=last_sequence,
        breaker_state=breaker_state,
        health_state=health_state,
        event_evidence=event_evidence,
        event_digest=event_digest,
    )


def _snapshot_mapping(state: PaperSnapshot) -> dict[str, object]:
    return {
        "average_entry_price": state.average_entry_price,
        "breaker_state": _thaw_json(state.breaker_state),
        "btc_cost_basis": state.btc_cost_basis,
        "btc_quantity": state.btc_quantity,
        "cash": state.cash,
        "equity": state.equity,
        "event_digest": state.event_digest,
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
    if btc_quantity > 0.0:
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
    if status not in _ORDER_SEED_STATUSES:
        raise StoreCorruptionError("invalid ORDER_CREATED seed status")
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


def _order_status_from_payload(payload: Mapping[str, object]) -> dict[str, Any]:
    if set(payload) != {"idempotency_key", "order_id", "reason", "status"}:
        raise StoreCorruptionError("invalid ORDER_STATUS payload")
    reason = payload["reason"]
    if reason is not None:
        reason = _stored_nonempty_text(reason, "invalid ORDER_STATUS reason")
    return {
        "idempotency_key": _stored_nonempty_text(
            payload["idempotency_key"],
            "invalid ORDER_STATUS idempotency key",
        ),
        "order_id": _stored_nonempty_text(
            payload["order_id"],
            "invalid ORDER_STATUS order id",
        ),
        "reason": reason,
        "status": _stored_order_status(payload["status"]),
    }


def _validate_order_status_transition(
    current: OrderStatus,
    target: OrderStatus,
    *,
    requested_quantity: float,
    filled_quantity: float,
    corruption: bool,
) -> None:
    error_type: type[Exception] = StoreCorruptionError if corruption else ValueError
    requested = Decimal(str(requested_quantity))
    filled = Decimal(str(filled_quantity))
    if filled < 0 or filled > requested:
        raise error_type("order quantity invariants are invalid")
    if target is OrderStatus.CREATED:
        raise error_type("illegal order status transition")
    if target is OrderStatus.PARTIAL:
        if current is not OrderStatus.PARTIAL or not (0 < filled < requested):
            raise error_type("illegal order status transition")
        return
    if target is OrderStatus.COMPLETED:
        if current is not OrderStatus.COMPLETED or filled != requested:
            raise error_type("illegal order status transition")
        return
    if target not in _LEGAL_ORDER_TRANSITIONS.get(current, frozenset()):
        raise error_type("illegal order status transition")


def _validate_known_state_payload(
    event_type: str,
    payload: Mapping[str, object],
    *,
    corruption: bool,
) -> None:
    if event_type not in {"BREAKER_STATE", "HEALTH_STATE"}:
        return
    error_type: type[Exception] = StoreCorruptionError if corruption else ValueError
    for key in _KNOWN_BOOLEAN_STATE_FIELDS:
        if key in payload and type(payload[key]) is not bool:
            raise error_type(f"{key} must be a bool")
    for key in _KNOWN_COUNT_STATE_FIELDS:
        if key in payload:
            value = payload[key]
            if type(value) is not int or value < 0:
                raise error_type(f"{key} must be a non-negative integer")
    if "reasons" in payload:
        reasons = payload["reasons"]
        accepted_sequence_types = (list,) if corruption else (list, tuple)
        if not isinstance(reasons, accepted_sequence_types):
            raise error_type("reasons must be a list or tuple")
        for reason in reasons:
            if not isinstance(reason, str) or _REASON_PATTERN.fullmatch(reason) is None:
                raise error_type("reason must be a non-empty known-format string")
        if len(set(reasons)) != len(reasons):
            raise error_type("reasons must not contain duplicates")
    if "fill_deviation" in payload:
        deviation = payload["fill_deviation"]
        if (
            type(deviation) not in (int, float)
            or (type(deviation) is float and not math.isfinite(deviation))
            or deviation < 0
        ):
            raise error_type(
                "fill_deviation must be an int or float, finite, and non-negative",
            )
    for key in ("last_success_at_utc", "last_failure_at_utc"):
        if key in payload and payload[key] is not None:
            try:
                canonical, _ = _canonical_timestamp(payload[key])
            except ValueError as error:
                raise error_type(f"{key} must be strict UTC") from error
            if canonical != payload[key]:
                raise error_type(f"{key} must be canonical UTC")


def _events_sequence_uses_autoincrement(sql: str) -> bool:
    """Verify AUTOINCREMENT belongs to the actual events.sequence definition."""
    tokens = _tokenize_sqlite_ddl(sql)
    opening = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.kind == "SYMBOL" and token.value == "("
        ),
        None,
    )
    if opening is None or not _valid_events_create_table_header(tokens[:opening]):
        return False

    definitions: list[tuple[_DdlToken, ...]] = []
    current: list[_DdlToken] = []
    depth = 0
    closing: int | None = None
    for index in range(opening + 1, len(tokens)):
        token = tokens[index]
        if token.kind == "SYMBOL" and token.value == "(":
            depth += 1
            current.append(token)
        elif token.kind == "SYMBOL" and token.value == ")":
            if depth == 0:
                if current:
                    definitions.append(tuple(current))
                closing = index
                break
            depth -= 1
            current.append(token)
        elif token.kind == "SYMBOL" and token.value == "," and depth == 0:
            if not current:
                return False
            definitions.append(tuple(current))
            current = []
        else:
            current.append(token)
    if closing is None or depth != 0 or not definitions:
        return False
    if any(
        token.kind != "SYMBOL" or token.value != ";"
        for token in tokens[closing + 1 :]
    ):
        return False

    sequence_definitions = [
        definition
        for definition in definitions
        if definition
        and definition[0].kind in {"WORD", "IDENT"}
        and definition[0].value.casefold() == "sequence"
    ]
    if len(sequence_definitions) != 1:
        return False
    sequence_definition = sequence_definitions[0]
    if len(sequence_definition) != 5:
        return False
    expected_keywords = ("INTEGER", "PRIMARY", "KEY", "AUTOINCREMENT")
    if any(
        token.kind != "WORD" or token.value.upper() != expected
        for token, expected in zip(
            sequence_definition[1:],
            expected_keywords,
            strict=True,
        )
    ):
        return False
    autoincrements = [
        token
        for definition in definitions
        for token in definition
        if token.kind == "WORD" and token.value.upper() == "AUTOINCREMENT"
    ]
    return len(autoincrements) == 1


def _valid_events_create_table_header(tokens: Sequence[_DdlToken]) -> bool:
    if len(tokens) != 3:
        return False
    return (
        tokens[0].kind == "WORD"
        and tokens[0].value.upper() == "CREATE"
        and tokens[1].kind == "WORD"
        and tokens[1].value.upper() == "TABLE"
        and tokens[2].kind in {"WORD", "IDENT"}
        and tokens[2].value.casefold() == "events"
    )


def _tokenize_sqlite_ddl(sql: str) -> tuple[_DdlToken, ...]:
    if not isinstance(sql, str) or not sql.strip():
        raise StoreCorruptionError("events table DDL is missing")
    tokens: list[_DdlToken] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                raise StoreCorruptionError("events table DDL has an unterminated comment")
            index = comment_end + 2
            continue
        if character in {"'", '"', "`", "["}:
            token, index = _consume_quoted_ddl_token(sql, index)
            tokens.append(token)
            continue
        if character in {"(", ")", ",", ";"}:
            tokens.append(_DdlToken("SYMBOL", character))
            index += 1
            continue
        start = index
        while index < len(sql):
            if sql[index].isspace() or sql[index] in "'\"`[](),;":
                break
            if sql.startswith("--", index) or sql.startswith("/*", index):
                break
            index += 1
        if start == index:
            tokens.append(_DdlToken("SYMBOL", character))
            index += 1
            continue
        value = sql[start:index]
        kind = "WORD" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value) else "SYMBOL"
        tokens.append(_DdlToken(kind, value))
    return tuple(tokens)


def _consume_quoted_ddl_token(sql: str, start: int) -> tuple[_DdlToken, int]:
    opening = sql[start]
    closing = "]" if opening == "[" else opening
    kind = "STRING" if opening == "'" else "IDENT"
    value: list[str] = []
    index = start + 1
    while index < len(sql):
        character = sql[index]
        if character == closing:
            if opening != "[" and index + 1 < len(sql) and sql[index + 1] == closing:
                value.append(closing)
                index += 2
                continue
            return _DdlToken(kind, "".join(value)), index + 1
        value.append(character)
        index += 1
    raise StoreCorruptionError("events table DDL has unterminated quoting")


def _reject_transaction_control_sql(sql: object) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a non-empty string")
    remaining = sql.lstrip("\ufeff \t\r\n;")
    while True:
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            if newline < 0:
                raise ValueError("SQL must contain a statement")
            remaining = remaining[newline + 1 :].lstrip(" \t\r\n;")
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                raise ValueError("unterminated SQL comment")
            remaining = remaining[end + 2 :].lstrip(" \t\r\n;")
            continue
        break
    match = re.match(r"[A-Za-z]+", remaining)
    if match is None:
        raise ValueError("SQL must contain a statement")
    if match.group(0).upper() in _TRANSACTION_CONTROL_KEYWORDS:
        raise ValueError("transaction-control SQL is not allowed")


def _orders_match(first: StoredOrder, second: StoredOrder) -> bool:
    return (
        first.order_id == second.order_id
        and first.idempotency_key == second.idempotency_key
        and first.side == second.side
        and first.requested_quantity == second.requested_quantity
        and first.filled_quantity == second.filled_quantity
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


def _cycle_lease_metadata(
    value: object,
    *,
    corruption: bool,
) -> tuple[str, str, datetime]:
    error_type: type[Exception] = StoreCorruptionError if corruption else ValueError
    try:
        if not isinstance(value, str):
            raise ValueError("lease metadata must be text")
        payload = json.loads(value)
        if not isinstance(payload, dict) or set(payload) != {
            "expires_at_utc",
            "owner",
            "token",
        }:
            raise ValueError("lease metadata has invalid fields")
        if _canonical_json_value(payload) != value:
            raise ValueError("lease metadata is not canonical")
        owner = _nonempty_text(payload["owner"], "lease owner must be non-empty")
        token = _nonempty_text(payload["token"], "lease token must be non-empty")
        expires_text, expires = _canonical_timestamp(payload["expires_at_utc"])
        if expires_text != payload["expires_at_utc"]:
            raise ValueError("lease expiry must be canonical UTC")
        return owner, token, expires
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError, KeyError) as error:
        raise error_type("paper cycle lease metadata is invalid") from error


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

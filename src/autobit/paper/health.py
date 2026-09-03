"""Deterministic, persistable paper-system health breakers.

The monitor owns no clock, sleep, network, or database connection.  Callers
provide observation times and may persist the complete immutable snapshot via
the narrow :meth:`HealthMonitor.persist` boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import math
from types import MappingProxyType
from typing import TYPE_CHECKING

from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    SQLiteStore,
    StoreCorruptionError,
)


if TYPE_CHECKING:
    from autobit.persistence.sqlite_store import StoredEvent


HEALTH_REASON_ORDER = (
    "API_FAILURES",
    "STALE_CANDLE",
    "TIMESTAMP_REVERSAL",
    "SCHEMA_ERROR",
    "UNRESOLVED_ORDER",
    "LEDGER_MISMATCH",
    "FILL_DEVIATION",
)

_VERSION = 1
_API_SUCCESSES_REQUIRED = 3
_LEDGER_TOLERANCE = 1e-10
_FILL_DEVIATION_LIMIT = 0.05
_STALE_AFTER = timedelta(minutes=10)
_UTC = timezone.utc
_SNAPSHOT_KEYS = frozenset(
    {
        "version",
        "stage",
        "reasons",
        "halt_entries",
        "resume_reduced",
        "api_failures",
        "api_successes",
        "api_failure_latched",
        "unresolved_orders",
        "ledger_matches",
        "ledger_cash_difference",
        "ledger_btc_difference",
        "timestamps_monotonic",
        "schema_valid",
        "latest_candle_valid",
        "fill_within_limit",
        "fill_deviation",
        "retry_attempts",
        "last_success_at_utc",
        "last_failure_at_utc",
        "last_observation_at_utc",
    },
)


class HealthStateError(ValueError):
    """Raised when health evidence is malformed or contradictory."""


class HealthStage(str, Enum):
    NORMAL = "NORMAL"
    HALTED = "HALTED"
    REDUCED = "REDUCED"


@dataclass(frozen=True, slots=True)
class RecoveryProgress:
    successes_observed: int
    successes_required: int
    remaining_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "remaining_gates", tuple(self.remaining_gates))


@dataclass(frozen=True, slots=True)
class HealthAction:
    halt_entries: bool
    resume_reduced: bool
    reason: str | None
    reasons: tuple[str, ...]
    retry_delay_seconds: int
    stage: HealthStage
    progress: RecoveryProgress

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    version: int = _VERSION
    stage: HealthStage = HealthStage.NORMAL
    reasons: tuple[str, ...] = ()
    api_failures: int = 0
    api_successes: int = 0
    api_failure_latched: bool = False
    unresolved_orders: int = 0
    ledger_matches: bool = True
    ledger_cash_difference: float = 0.0
    ledger_btc_difference: float = 0.0
    timestamps_monotonic: bool = True
    schema_valid: bool = True
    latest_candle_valid: bool = True
    fill_within_limit: bool = True
    fill_deviation: float = 0.0
    retry_attempts: int = 0
    last_success_at_utc: datetime | None = None
    last_failure_at_utc: datetime | None = None
    last_observation_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> HealthSnapshot:
        """Parse only the canonical version-one snapshot shape.

        An empty mapping is the one compatibility exception: Task 3 databases
        predate health events and therefore begin healthy.
        """
        if not isinstance(payload, Mapping):
            raise HealthStateError("health snapshot must be a mapping")
        if not payload:
            return cls()
        if set(payload) != _SNAPSHOT_KEYS:
            raise HealthStateError("health snapshot keys are invalid")
        version = _strict_int(payload["version"], "version")
        if version != _VERSION:
            raise HealthStateError("health snapshot version is unsupported")
        stage = _strict_stage(payload["stage"])
        reasons = _strict_reasons(payload["reasons"])
        api_failures = _strict_int(payload["api_failures"], "api_failures")
        api_successes = _strict_int(payload["api_successes"], "api_successes")
        if api_successes > _API_SUCCESSES_REQUIRED:
            raise HealthStateError("api_successes exceeds its canonical cap")
        api_failure_latched = _strict_bool(
            payload["api_failure_latched"],
            "api_failure_latched",
        )
        unresolved_orders = _strict_int(
            payload["unresolved_orders"],
            "unresolved_orders",
        )
        ledger_matches = _strict_bool(payload["ledger_matches"], "ledger_matches")
        ledger_cash_difference = _nonnegative_finite(
            payload["ledger_cash_difference"],
            "ledger_cash_difference",
        )
        ledger_btc_difference = _nonnegative_finite(
            payload["ledger_btc_difference"],
            "ledger_btc_difference",
        )
        timestamps_monotonic = _strict_bool(
            payload["timestamps_monotonic"],
            "timestamps_monotonic",
        )
        schema_valid = _strict_bool(payload["schema_valid"], "schema_valid")
        latest_candle_valid = _strict_bool(
            payload["latest_candle_valid"],
            "latest_candle_valid",
        )
        fill_within_limit = _strict_bool(
            payload["fill_within_limit"],
            "fill_within_limit",
        )
        fill_deviation = _nonnegative_finite(
            payload["fill_deviation"],
            "fill_deviation",
        )
        retry_attempts = _strict_int(payload["retry_attempts"], "retry_attempts")
        last_success_at = _optional_canonical_datetime(
            payload["last_success_at_utc"],
            "last_success_at_utc",
        )
        last_failure_at = _optional_canonical_datetime(
            payload["last_failure_at_utc"],
            "last_failure_at_utc",
        )
        last_observation_at = _optional_canonical_datetime(
            payload["last_observation_at_utc"],
            "last_observation_at_utc",
        )
        halt_entries = _strict_bool(payload["halt_entries"], "halt_entries")
        resume_reduced = _strict_bool(payload["resume_reduced"], "resume_reduced")

        snapshot = cls(
            version=version,
            stage=stage,
            reasons=reasons,
            api_failures=api_failures,
            api_successes=api_successes,
            api_failure_latched=api_failure_latched,
            unresolved_orders=unresolved_orders,
            ledger_matches=ledger_matches,
            ledger_cash_difference=ledger_cash_difference,
            ledger_btc_difference=ledger_btc_difference,
            timestamps_monotonic=timestamps_monotonic,
            schema_valid=schema_valid,
            latest_candle_valid=latest_candle_valid,
            fill_within_limit=fill_within_limit,
            fill_deviation=fill_deviation,
            retry_attempts=retry_attempts,
            last_success_at_utc=last_success_at,
            last_failure_at_utc=last_failure_at,
            last_observation_at_utc=last_observation_at,
        )
        _validate_snapshot_relationships(snapshot, halt_entries, resume_reduced)
        return snapshot

    def to_mapping(self) -> Mapping[str, object]:
        """Return an immutable canonical JSON-compatible mapping."""
        action = _action(self)
        return MappingProxyType(
            {
                "version": self.version,
                "stage": self.stage.value,
                "reasons": self.reasons,
                "halt_entries": action.halt_entries,
                "resume_reduced": action.resume_reduced,
                "api_failures": self.api_failures,
                "api_successes": self.api_successes,
                "api_failure_latched": self.api_failure_latched,
                "unresolved_orders": self.unresolved_orders,
                "ledger_matches": self.ledger_matches,
                "ledger_cash_difference": self.ledger_cash_difference,
                "ledger_btc_difference": self.ledger_btc_difference,
                "timestamps_monotonic": self.timestamps_monotonic,
                "schema_valid": self.schema_valid,
                "latest_candle_valid": self.latest_candle_valid,
                "fill_within_limit": self.fill_within_limit,
                "fill_deviation": self.fill_deviation,
                "retry_attempts": self.retry_attempts,
                "last_success_at_utc": _optional_datetime(self.last_success_at_utc),
                "last_failure_at_utc": _optional_datetime(self.last_failure_at_utc),
                "last_observation_at_utc": _optional_datetime(
                    self.last_observation_at_utc,
                ),
            },
        )


class HealthMonitor:
    """Apply observations to a complete restart-safe health snapshot."""

    def __init__(self, snapshot: HealthSnapshot | None = None) -> None:
        if snapshot is not None and not isinstance(snapshot, HealthSnapshot):
            raise TypeError("snapshot must be a HealthSnapshot")
        self._snapshot = snapshot or HealthSnapshot()
        # Validate manually constructed snapshots as strictly as persisted ones.
        self._snapshot = HealthSnapshot.from_mapping(self._snapshot.to_mapping())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> HealthMonitor:
        return cls(HealthSnapshot.from_mapping(payload))

    @classmethod
    def from_store(cls, store: SQLiteStore) -> HealthMonitor:
        """Strictly validate every public health event before trusting the tail."""
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be a SQLiteStore")
        state = store.replay_state()
        latest: HealthSnapshot | None = None
        for event in state.event_evidence:
            if event.event_type == "HEALTH_STATE":
                latest = _snapshot_from_health_event(event)
        if latest is None:
            if state.health_state:
                raise StoreCorruptionError("health projection has no event evidence")
            return cls()
        projected = HealthSnapshot.from_mapping(state.health_state)
        if projected != latest:
            raise StoreCorruptionError("health projection contradicts event evidence")
        return cls(latest)

    def snapshot(self) -> HealthSnapshot:
        return self._snapshot

    def current_action(self) -> HealthAction:
        return _action(self._snapshot)

    def record_api_failure(self, at: datetime) -> HealthAction:
        observed = _utc_datetime(at, "API observation time")
        if self._snapshot.last_failure_at_utc == observed:
            return self.current_action()
        self._snapshot = replace(
            self._snapshot,
            api_failures=self._snapshot.api_failures + 1,
            api_successes=0,
            api_failure_latched=(
                self._snapshot.api_failure_latched
                or self._snapshot.api_failures + 1 >= 3
            ),
            last_failure_at_utc=observed,
            last_observation_at_utc=observed,
        )
        return self._finish_observation()

    def record_api_success(self, at: datetime) -> HealthAction:
        observed = _utc_datetime(at, "API observation time")
        if self._snapshot.last_success_at_utc == observed:
            return self.current_action()
        successes = min(_API_SUCCESSES_REQUIRED, self._snapshot.api_successes + 1)
        self._snapshot = replace(
            self._snapshot,
            api_failures=0,
            api_successes=successes,
            api_failure_latched=(
                self._snapshot.api_failure_latched
                and successes < _API_SUCCESSES_REQUIRED
            ),
            last_success_at_utc=observed,
            last_observation_at_utc=observed,
        )
        return self._finish_observation()

    def record_candle_check(
        self,
        *,
        expected_end: datetime,
        observed_end: datetime | None,
        checked_at: datetime,
        structurally_valid: bool = True,
    ) -> HealthAction:
        expected = _utc_datetime(expected_end, "expected candle end")
        checked = _utc_datetime(checked_at, "candle check time")
        if type(structurally_valid) is not bool:
            self._fail_schema(checked)
            raise ValueError("structurally_valid must be a bool")
        observed = (
            None
            if observed_end is None
            else _utc_datetime(observed_end, "observed candle end")
        )
        prior_valid = self._snapshot.latest_candle_valid
        if observed == expected and structurally_valid:
            latest_valid = True
        elif checked >= expected + _STALE_AFTER:
            latest_valid = False
        else:
            latest_valid = prior_valid
        newly_faulted = (
            (prior_valid and not latest_valid)
            or (self._snapshot.schema_valid and not structurally_valid)
        )
        successes = 0 if newly_faulted else self._snapshot.api_successes
        self._snapshot = replace(
            self._snapshot,
            latest_candle_valid=latest_valid,
            schema_valid=self._snapshot.schema_valid and structurally_valid,
            api_successes=successes,
            last_observation_at_utc=checked,
        )
        return self._finish_observation()

    def record_timestamp_check(self, monotonic: bool, at: datetime) -> HealthAction:
        observed = _utc_datetime(at, "timestamp check time")
        if type(monotonic) is not bool:
            self._fail_schema(observed)
            raise ValueError("monotonic must be a bool")
        return self._set_predicate(
            "timestamps_monotonic",
            monotonic,
            observed,
        )

    def record_schema_check(self, valid: bool, at: datetime) -> HealthAction:
        observed = _utc_datetime(at, "schema check time")
        if type(valid) is not bool:
            self._fail_schema(observed)
            raise ValueError("valid must be a bool")
        return self._set_predicate("schema_valid", valid, observed)

    def set_unresolved_orders(
        self,
        count: int,
        at: datetime | None = None,
    ) -> HealthAction:
        observed = _optional_utc_datetime(at, "order check time")
        if type(count) is not int or count < 0:
            self._fail_schema(observed)
            raise ValueError("unresolved order count must be a non-negative integer")
        newly_faulted = self._snapshot.unresolved_orders == 0 and count > 0
        self._snapshot = replace(
            self._snapshot,
            unresolved_orders=count,
            api_successes=0 if newly_faulted else self._snapshot.api_successes,
            last_observation_at_utc=observed or self._snapshot.last_observation_at_utc,
        )
        return self._finish_observation()

    def record_ledger_check(
        self,
        *,
        stored_cash: float,
        actual_cash: float,
        stored_btc: float,
        actual_btc: float,
        at: datetime | None = None,
    ) -> HealthAction:
        observed = _optional_utc_datetime(at, "ledger check time")
        try:
            for value in (stored_cash, actual_cash, stored_btc, actual_btc):
                _nonnegative_number(value, "ledger values")
        except ValueError:
            self._fail_schema(observed)
            raise
        cash_difference = float(
            abs(Decimal(str(stored_cash)) - Decimal(str(actual_cash))),
        )
        btc_difference = float(
            abs(Decimal(str(stored_btc)) - Decimal(str(actual_btc))),
        )
        matches = (
            cash_difference <= _LEDGER_TOLERANCE
            and btc_difference <= _LEDGER_TOLERANCE
        )
        newly_faulted = self._snapshot.ledger_matches and not matches
        self._snapshot = replace(
            self._snapshot,
            ledger_matches=matches,
            ledger_cash_difference=cash_difference,
            ledger_btc_difference=btc_difference,
            api_successes=0 if newly_faulted else self._snapshot.api_successes,
            last_observation_at_utc=observed or self._snapshot.last_observation_at_utc,
        )
        return self._finish_observation()

    def record_fill_check(
        self,
        *,
        expected_price: float,
        actual_price: float,
        at: datetime | None = None,
    ) -> HealthAction:
        observed = _optional_utc_datetime(at, "fill check time")
        try:
            expected = _positive_finite(expected_price, "fill prices")
            actual = _positive_finite(actual_price, "fill prices")
        except ValueError:
            self._fail_schema(observed)
            raise
        deviation = abs(actual - expected) / expected
        within_limit = deviation <= _FILL_DEVIATION_LIMIT
        newly_faulted = self._snapshot.fill_within_limit and not within_limit
        self._snapshot = replace(
            self._snapshot,
            fill_within_limit=within_limit,
            fill_deviation=deviation,
            api_successes=0 if newly_faulted else self._snapshot.api_successes,
            last_observation_at_utc=observed or self._snapshot.last_observation_at_utc,
        )
        return self._finish_observation()

    def record_recovery_cycle_success(self, at: datetime) -> HealthAction:
        observed = _utc_datetime(at, "recovery cycle time")
        self._snapshot = replace(self._snapshot, last_observation_at_utc=observed)
        if self._snapshot.stage is HealthStage.REDUCED and not _derive_reasons(
            self._snapshot,
        ):
            self._snapshot = replace(
                self._snapshot,
                stage=HealthStage.NORMAL,
                retry_attempts=0,
            )
            return self.current_action()
        return self._finish_observation()

    def persist(
        self,
        store: SQLiteStore,
        *,
        event_id: str,
        logical_at: datetime,
    ) -> int:
        """Append one full typed snapshot, treating an exact retry as a no-op."""
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be a SQLiteStore")
        logical = _utc_datetime(logical_at, "logical event time")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be non-empty")
        matching = tuple(
            event
            for event in store.replay_state().event_evidence
            if event.event_id == event_id
        )
        if matching:
            return self._validate_duplicate_event(matching, logical)
        try:
            return store.append_event(
                event_id,
                "HEALTH_STATE",
                logical,
                self._snapshot.to_mapping(),
            )
        except IdempotencyConflictError:
            matching = tuple(
                event
                for event in store.replay_state().event_evidence
                if event.event_id == event_id
            )
            return self._validate_duplicate_event(matching, logical)

    def _validate_duplicate_event(
        self,
        matching: Sequence[StoredEvent],
        logical_at: datetime,
    ) -> int:
        if len(matching) != 1:
            raise IdempotencyConflictError("health event identity is not unique")
        event = matching[0]
        if event.event_type != "HEALTH_STATE" or event.occurred_at_utc != logical_at:
            raise IdempotencyConflictError("health event identity conflicts")
        try:
            stored = HealthSnapshot.from_mapping(event.payload)
        except HealthStateError as error:
            raise StoreCorruptionError("stored health event is invalid") from error
        if stored != self._snapshot:
            raise IdempotencyConflictError("health event identity conflicts")
        return event.sequence

    def _set_predicate(
        self,
        field: str,
        value: bool,
        observed: datetime,
    ) -> HealthAction:
        previous = getattr(self._snapshot, field)
        self._snapshot = replace(
            self._snapshot,
            **{
                field: value,
                "api_successes": (
                    0 if previous and not value else self._snapshot.api_successes
                ),
                "last_observation_at_utc": observed,
            },
        )
        return self._finish_observation()

    def _fail_schema(self, observed: datetime | None) -> None:
        newly_faulted = self._snapshot.schema_valid
        self._snapshot = replace(
            self._snapshot,
            schema_valid=False,
            api_successes=0 if newly_faulted else self._snapshot.api_successes,
            last_observation_at_utc=observed or self._snapshot.last_observation_at_utc,
        )
        self._finish_observation()

    def _finish_observation(self) -> HealthAction:
        reasons = _derive_reasons(self._snapshot)
        stage = self._snapshot.stage
        if reasons:
            stage = HealthStage.HALTED
        elif stage is HealthStage.HALTED:
            if self._snapshot.api_successes >= _API_SUCCESSES_REQUIRED:
                stage = HealthStage.REDUCED
        retry_attempts = self._snapshot.retry_attempts
        if stage is HealthStage.HALTED:
            retry_attempts += 1
        else:
            retry_attempts = 0
        self._snapshot = replace(
            self._snapshot,
            stage=stage,
            reasons=reasons,
            retry_attempts=retry_attempts,
        )
        return self.current_action()


def _snapshot_from_health_event(event: StoredEvent) -> HealthSnapshot:
    return HealthSnapshot.from_mapping(event.payload)


def _derive_reasons(snapshot: HealthSnapshot) -> tuple[str, ...]:
    active = {
        "API_FAILURES": snapshot.api_failure_latched,
        "STALE_CANDLE": not snapshot.latest_candle_valid,
        "TIMESTAMP_REVERSAL": not snapshot.timestamps_monotonic,
        "SCHEMA_ERROR": not snapshot.schema_valid,
        "UNRESOLVED_ORDER": snapshot.unresolved_orders > 0,
        "LEDGER_MISMATCH": not snapshot.ledger_matches,
        "FILL_DEVIATION": not snapshot.fill_within_limit,
    }
    return tuple(reason for reason in HEALTH_REASON_ORDER if active[reason])


def _action(snapshot: HealthSnapshot) -> HealthAction:
    reasons = _derive_reasons(snapshot)
    halt = snapshot.stage is HealthStage.HALTED
    reduced = snapshot.stage is HealthStage.REDUCED
    remaining = list(reasons)
    if halt and snapshot.api_successes < _API_SUCCESSES_REQUIRED:
        remaining.append("API_SUCCESSES")
    retry_delay = _retry_delay(snapshot.retry_attempts) if halt else 0
    return HealthAction(
        halt_entries=halt,
        resume_reduced=reduced,
        reason=reasons[0] if reasons else None,
        reasons=reasons,
        retry_delay_seconds=retry_delay,
        stage=snapshot.stage,
        progress=RecoveryProgress(
            successes_observed=snapshot.api_successes,
            successes_required=_API_SUCCESSES_REQUIRED,
            remaining_gates=tuple(remaining),
        ),
    )


def _retry_delay(attempts: int) -> int:
    if attempts <= 0:
        return 0
    if attempts <= 5:
        return 2 ** (attempts - 1)
    return 300


def _validate_snapshot_relationships(
    snapshot: HealthSnapshot,
    halt_entries: bool,
    resume_reduced: bool,
) -> None:
    derived = _derive_reasons(snapshot)
    if snapshot.reasons != derived:
        raise HealthStateError("health reasons contradict predicates")
    if snapshot.ledger_matches != (
        snapshot.ledger_cash_difference <= _LEDGER_TOLERANCE
        and snapshot.ledger_btc_difference <= _LEDGER_TOLERANCE
    ):
        raise HealthStateError("ledger predicate contradicts differences")
    if snapshot.fill_within_limit != (
        snapshot.fill_deviation <= _FILL_DEVIATION_LIMIT
    ):
        raise HealthStateError("fill predicate contradicts deviation")
    if snapshot.api_failure_latched != ("API_FAILURES" in derived):
        raise HealthStateError("API latch contradicts reasons")
    if snapshot.api_failure_latched and snapshot.api_successes >= 3:
        raise HealthStateError("API latch contradicts recovery successes")
    if snapshot.api_failures >= 3 and not snapshot.api_failure_latched:
        raise HealthStateError("API failures contradict the API latch")
    if snapshot.api_failures > 0 and snapshot.api_successes > 0:
        raise HealthStateError("API failure and success counters contradict")
    if snapshot.api_failures > 0 and snapshot.last_failure_at_utc is None:
        raise HealthStateError("API failure counter lacks observation evidence")
    if snapshot.api_successes > 0 and snapshot.last_success_at_utc is None:
        raise HealthStateError("API success counter lacks observation evidence")
    if snapshot.stage is HealthStage.NORMAL:
        if derived or snapshot.retry_attempts != 0:
            raise HealthStateError("NORMAL health state is contradictory")
    elif snapshot.stage is HealthStage.REDUCED:
        if derived or snapshot.api_successes < 3 or snapshot.retry_attempts != 0:
            raise HealthStateError("REDUCED health state is contradictory")
    else:
        if snapshot.retry_attempts <= 0:
            raise HealthStateError("HALTED health state requires retry evidence")
        if not derived and snapshot.api_successes >= _API_SUCCESSES_REQUIRED:
            raise HealthStateError("HALTED health state has completed every recovery gate")
    action = _action(snapshot)
    if halt_entries != action.halt_entries or resume_reduced != action.resume_reduced:
        raise HealthStateError("health action flags contradict stage")


def _strict_stage(value: object) -> HealthStage:
    if not isinstance(value, str):
        raise HealthStateError("health stage must be a string")
    try:
        return HealthStage(value)
    except ValueError as error:
        raise HealthStateError("health stage is invalid") from error


def _strict_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HealthStateError("health reasons must be a sequence")
    reasons = tuple(value)
    if any(type(reason) is not str for reason in reasons):
        raise HealthStateError("health reason must be a string")
    if reasons != tuple(reason for reason in HEALTH_REASON_ORDER if reason in reasons):
        raise HealthStateError("health reasons are unknown, duplicated, or unordered")
    return reasons


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise HealthStateError(f"{label} must be a non-negative integer")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise HealthStateError(f"{label} must be a bool")
    return value


def _nonnegative_finite(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise HealthStateError(f"{label} must be an int or float")
    try:
        return _nonnegative_number(value, label)
    except ValueError as error:
        raise HealthStateError(str(error)) from error


def _nonnegative_number(value: object, label: str) -> float:
    result = _finite_number(value, label)
    if result < 0.0:
        raise ValueError(f"{label} must be non-negative")
    return result


def _finite_number(value: object, label: str) -> float:
    if type(value) not in (int, float) and not isinstance(value, Decimal):
        raise ValueError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_finite(value: object, label: str) -> float:
    result = _finite_number(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def _utc_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware UTC")
    try:
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"{label} must use UTC")
        return value.astimezone(_UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{label} must use UTC") from error


def _optional_utc_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _utc_datetime(value, label)


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _canonical_datetime(value)


def _optional_canonical_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HealthStateError(f"{label} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise HealthStateError(f"{label} must be canonical UTC") from error
    if _canonical_datetime(parsed) != value:
        raise HealthStateError(f"{label} must be canonical UTC")
    return parsed

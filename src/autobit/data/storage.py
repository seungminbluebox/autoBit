"""Deterministic, content-addressed persistence for raw public snapshots."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Final
from uuid import uuid4

from autobit.config import DataConfig
from autobit.data.upbit_public import PUBLIC_CANDLE_URL


_CHECKPOINT_FILENAME: Final = "checkpoint.json"
_COLLECTION_SNAPSHOT_PREFIX: Final = "collection-"


@dataclass(frozen=True, slots=True)
class RawPageEvidence:
    """One immutable public-response page and the request that produced it."""

    path: Path
    sha256: str
    request_to_utc: str
    oldest_timestamp_utc: str | None
    row_count: int


@dataclass(frozen=True, slots=True)
class CollectionEvidence:
    """Validated durable state for a backward public-candle collection."""

    root: Path
    source_url: str
    market: str
    candle_unit_minutes: int
    page_size: int
    years: int
    start_utc: str
    end_utc: str
    config_sha256: str
    pages: tuple[RawPageEvidence, ...]
    next_to_utc: str
    previous_oldest_utc: str | None
    complete: bool
    collection_snapshot_path: Path | None = None
    collection_snapshot_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    path: Path
    sha256: str
    row_count: int
    created_at_utc: str
    source_url: str
    config_hash: str


def save_snapshot(
    root: Path,
    payload: list[dict[str, object]],
    *,
    source_url: str = PUBLIC_CANDLE_URL,
    config: DataConfig = DataConfig(),
) -> SnapshotManifest:
    """Atomically store a canonical raw snapshot and its evidence checkpoint."""
    root.mkdir(parents=True, exist_ok=True)
    payload_bytes = _canonical_json_bytes(payload)
    sha256 = hashlib.sha256(payload_bytes).hexdigest()
    destination = root / f"snapshot-{sha256}.json"

    _atomic_write(destination, payload_bytes)
    _atomic_write(
        root / _CHECKPOINT_FILENAME,
        _canonical_json_bytes(
            {
                "oldest_timestamp_utc": _oldest_timestamp(payload),
                "raw_snapshot_sha256": sha256,
            }
        ),
    )

    return SnapshotManifest(
        path=destination,
        sha256=sha256,
        row_count=len(payload),
        created_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_url=source_url,
        config_hash=hashlib.sha256(_canonical_json_bytes(asdict(config))).hexdigest(),
    )


def save_json_snapshot(
    root: Path,
    payload: list[dict[str, object]],
    *,
    source_url: str = PUBLIC_CANDLE_URL,
    config: DataConfig = DataConfig(),
) -> SnapshotManifest:
    """Backward-compatible explicit name for saving a JSON raw snapshot."""
    return save_snapshot(root, payload, source_url=source_url, config=config)


def load_collection_evidence(
    root: Path,
    *,
    source_url: str,
    start_utc: str,
    end_utc: str,
    config: DataConfig,
) -> CollectionEvidence:
    """Load a fully validated collection checkpoint, or initialize a new one."""
    root.mkdir(parents=True, exist_ok=True)
    identity = _collection_identity(
        source_url=source_url,
        start_utc=start_utc,
        end_utc=end_utc,
        config=config,
    )
    checkpoint_path = root / _CHECKPOINT_FILENAME
    if not checkpoint_path.exists():
        return _recover_uncheckpointed_evidence(root, identity)

    checkpoint = _read_json(checkpoint_path, "collection evidence checkpoint")
    if not isinstance(checkpoint, dict):
        raise ValueError("collection evidence checkpoint is malformed")
    required = set(identity) | {
        "pages",
        "next_to_utc",
        "previous_oldest_utc",
        "complete",
        "collection_snapshot",
        "collection_snapshot_sha256",
    }
    if set(checkpoint) != required:
        raise ValueError("collection evidence checkpoint has an invalid schema")
    if any(checkpoint[name] != value for name, value in identity.items()):
        raise ValueError("collection evidence checkpoint does not match requested collection")

    snapshot_name = checkpoint["collection_snapshot"]
    snapshot_hash = checkpoint["collection_snapshot_sha256"]
    if not isinstance(snapshot_name, str) or not isinstance(snapshot_hash, str):
        raise ValueError("collection evidence checkpoint has an invalid snapshot reference")
    expected_snapshot_name = f"{_COLLECTION_SNAPSHOT_PREFIX}{snapshot_hash}.json"
    if snapshot_name != expected_snapshot_name:
        raise ValueError("collection evidence checkpoint snapshot name does not match its hash")
    snapshot_path = root / snapshot_name
    snapshot = _read_hashed_json(snapshot_path, snapshot_hash, "collection snapshot evidence")
    if not isinstance(snapshot, dict):
        raise ValueError("collection snapshot evidence is malformed")
    state_payload = _collection_state_payload(checkpoint)
    if snapshot != state_payload:
        raise ValueError("collection snapshot evidence does not match its checkpoint")
    return _evidence_from_state_payload(
        root,
        snapshot,
        identity,
        snapshot_path=snapshot_path,
        snapshot_hash=snapshot_hash,
        label="collection evidence checkpoint",
    )


def load_completed_collection_evidence(root: Path) -> CollectionEvidence:
    """Load a completed evidence chain without needing a live public client."""
    checkpoint_path = root / _CHECKPOINT_FILENAME
    if not root.is_dir() or not checkpoint_path.exists():
        raise ValueError("completed collection evidence requires a checkpoint")
    checkpoint = _read_json(checkpoint_path, "collection evidence checkpoint")
    if not isinstance(checkpoint, dict):
        raise ValueError("collection evidence checkpoint is malformed")
    config = _config_from_evidence(checkpoint.get("config"))
    source_url = checkpoint.get("source_url")
    start_utc = checkpoint.get("start_utc")
    end_utc = checkpoint.get("end_utc")
    if not all(isinstance(value, str) for value in (source_url, start_utc, end_utc)):
        raise ValueError("collection evidence checkpoint has an invalid identity")
    evidence = load_collection_evidence(
        root,
        source_url=source_url,
        start_utc=start_utc,
        end_utc=end_utc,
        config=config,
    )
    if not evidence.complete:
        raise ValueError("incomplete collection evidence cannot be used for data quality")
    return evidence


def persist_raw_page(
    evidence: CollectionEvidence,
    page: list[dict[str, object]],
    *,
    request_to_utc: str,
    oldest_timestamp_utc: str | None,
    next_to_utc: str,
    complete: bool,
) -> CollectionEvidence:
    """Durably record one raw response, then atomically advance its checkpoint."""
    if evidence.complete:
        raise ValueError("cannot add a raw page to completed collection evidence")
    if not isinstance(request_to_utc, str) or not isinstance(next_to_utc, str) or not isinstance(complete, bool):
        raise ValueError("collection evidence pagination fields are invalid")
    if not isinstance(oldest_timestamp_utc, (str, type(None))):
        raise ValueError("collection evidence oldest timestamp is invalid")
    if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
        raise ValueError("raw public page must be a list of objects")

    page_bytes = _canonical_json_bytes(page)
    page_hash = hashlib.sha256(page_bytes).hexdigest()
    page_path = evidence.root / f"page-{page_hash}.json"
    _write_content_addressed(page_path, page_bytes)
    new_page = RawPageEvidence(
        path=page_path,
        sha256=page_hash,
        request_to_utc=request_to_utc,
        oldest_timestamp_utc=oldest_timestamp_utc,
        row_count=len(page),
    )
    advanced = replace(
        evidence,
        pages=(*evidence.pages, new_page),
        next_to_utc=next_to_utc,
        previous_oldest_utc=oldest_timestamp_utc,
        complete=complete,
    )
    snapshot_payload = _collection_state_payload_from_evidence(advanced)
    snapshot_bytes = _canonical_json_bytes(snapshot_payload)
    snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
    snapshot_path = evidence.root / f"{_COLLECTION_SNAPSHOT_PREFIX}{snapshot_hash}.json"
    _write_content_addressed(snapshot_path, snapshot_bytes)
    checkpoint = {
        **snapshot_payload,
        "collection_snapshot": snapshot_path.name,
        "collection_snapshot_sha256": snapshot_hash,
    }
    _atomic_write(evidence.root / _CHECKPOINT_FILENAME, _canonical_json_bytes(checkpoint))
    return replace(
        advanced,
        collection_snapshot_path=snapshot_path,
        collection_snapshot_sha256=snapshot_hash,
    )


def _recover_uncheckpointed_evidence(
    root: Path, identity: dict[str, object]
) -> CollectionEvidence:
    """Recover only the tip of one valid, non-conflicting snapshot chain."""
    candidates: list[CollectionEvidence] = []
    for snapshot_path in root.glob(f"{_COLLECTION_SNAPSHOT_PREFIX}*.json"):
        snapshot_hash = _snapshot_hash_from_name(snapshot_path.name)
        if snapshot_hash is None:
            continue
        try:
            snapshot = _read_hashed_json(
                snapshot_path, snapshot_hash, "orphan collection snapshot evidence"
            )
            if not isinstance(snapshot, dict):
                continue
            candidates.append(
                _evidence_from_state_payload(
                    root,
                    snapshot,
                    identity,
                    snapshot_path=snapshot_path,
                    snapshot_hash=snapshot_hash,
                    label="orphan collection snapshot evidence",
                )
            )
        except ValueError:
            continue

    if not candidates:
        return _new_collection_evidence(root, identity)

    tip = max(candidates, key=lambda evidence: len(evidence.pages))
    for candidate in candidates:
        if not _is_snapshot_prefix(candidate, tip):
            raise ValueError("collection evidence has ambiguous snapshot chains")
    return tip


def _new_collection_evidence(
    root: Path, identity: dict[str, object]
) -> CollectionEvidence:
    return CollectionEvidence(
        root=root,
        pages=(),
        next_to_utc=str(identity["end_utc"]),
        previous_oldest_utc=None,
        complete=False,
        collection_snapshot_path=None,
        collection_snapshot_sha256=None,
        **_evidence_identity(identity),
    )


def _evidence_from_state_payload(
    root: Path,
    state: dict[str, object],
    identity: dict[str, object],
    *,
    snapshot_path: Path,
    snapshot_hash: str,
    label: str,
) -> CollectionEvidence:
    required = set(identity) | {
        "pages",
        "next_to_utc",
        "previous_oldest_utc",
        "complete",
    }
    if set(state) != required:
        raise ValueError(f"{label} has an invalid schema")
    if any(state[name] != value for name, value in identity.items()):
        raise ValueError(f"{label} does not match requested collection")
    pages = _parse_page_evidence(root, state["pages"])
    next_to_utc = state["next_to_utc"]
    previous_oldest_utc = state["previous_oldest_utc"]
    complete = state["complete"]
    if (
        not isinstance(next_to_utc, str)
        or not isinstance(previous_oldest_utc, (str, type(None)))
        or not isinstance(complete, bool)
    ):
        raise ValueError(f"{label} has invalid pagination state")
    _validate_pagination_state(
        pages,
        next_to_utc=next_to_utc,
        previous_oldest_utc=previous_oldest_utc,
        complete=complete,
        start_utc=str(identity["start_utc"]),
        end_utc=str(identity["end_utc"]),
        label=label,
    )
    evidence = CollectionEvidence(
        root=root,
        pages=pages,
        next_to_utc=next_to_utc,
        previous_oldest_utc=previous_oldest_utc,
        complete=complete,
        collection_snapshot_path=snapshot_path,
        collection_snapshot_sha256=snapshot_hash,
        **_evidence_identity(identity),
    )
    load_raw_pages(evidence)
    return evidence


def _snapshot_hash_from_name(name: str) -> str | None:
    if not name.startswith(_COLLECTION_SNAPSHOT_PREFIX) or not name.endswith(".json"):
        return None
    candidate = name[len(_COLLECTION_SNAPSHOT_PREFIX) : -len(".json")]
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        return None
    return candidate


def _is_snapshot_prefix(
    candidate: CollectionEvidence, tip: CollectionEvidence
) -> bool:
    if len(candidate.pages) > len(tip.pages):
        return False
    if candidate.pages != tip.pages[: len(candidate.pages)]:
        return False
    if len(candidate.pages) == len(tip.pages):
        return (
            candidate.next_to_utc == tip.next_to_utc
            and candidate.previous_oldest_utc == tip.previous_oldest_utc
            and candidate.complete == tip.complete
        )
    if candidate.complete:
        return False
    return candidate.next_to_utc == tip.pages[len(candidate.pages)].request_to_utc


def _validate_pagination_state(
    pages: tuple[RawPageEvidence, ...],
    *,
    next_to_utc: str,
    previous_oldest_utc: str | None,
    complete: bool,
    start_utc: str,
    end_utc: str,
    label: str,
) -> None:
    """Ensure page order and pagination boundaries describe one backward walk."""
    expected_request_to = end_utc
    for position, page in enumerate(pages):
        if page.request_to_utc != expected_request_to:
            raise ValueError(f"{label} has invalid pagination boundaries")
        if page.row_count == 0:
            if (
                page.oldest_timestamp_utc is not None
                or position != len(pages) - 1
                or not complete
            ):
                raise ValueError(f"{label} has invalid pagination boundaries")
            expected_request_to = page.request_to_utc
            continue
        if page.oldest_timestamp_utc is None:
            raise ValueError(f"{label} has invalid pagination boundaries")
        expected_request_to = page.oldest_timestamp_utc

    if next_to_utc != expected_request_to:
        raise ValueError(f"{label} has invalid pagination boundaries")
    expected_previous = pages[-1].oldest_timestamp_utc if pages else None
    if previous_oldest_utc != expected_previous:
        raise ValueError(f"{label} has invalid pagination boundaries")
    if complete and pages and pages[-1].row_count > 0:
        oldest = pages[-1].oldest_timestamp_utc
        if oldest is None or oldest >= start_utc:
            raise ValueError(f"{label} claims completion before the requested start")


def load_raw_pages(evidence: CollectionEvidence) -> tuple[list[dict[str, object]], ...]:
    """Return exact stored page lists only after validating every referenced hash."""
    pages: list[list[dict[str, object]]] = []
    for page in evidence.pages:
        payload = _read_hashed_json(page.path, page.sha256, "raw page evidence")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("raw page evidence is not a list of objects")
        if len(payload) != page.row_count or _oldest_timestamp(payload) != page.oldest_timestamp_utc:
            raise ValueError("raw page evidence does not match its checkpoint metadata")
        pages.append(payload)
    return tuple(pages)


def _collection_identity(
    *,
    source_url: str,
    start_utc: str,
    end_utc: str,
    config: DataConfig,
) -> dict[str, object]:
    return {
        "source_url": source_url,
        "market": config.market,
        "candle_unit_minutes": config.candle_unit_minutes,
        "page_size": config.page_size,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "config": asdict(config),
        "config_sha256": hashlib.sha256(_canonical_json_bytes(asdict(config))).hexdigest(),
    }


def _evidence_identity(identity: dict[str, object]) -> dict[str, object]:
    """Keep serialized config immutable in state while exposing scalar fields."""
    config = identity["config"]
    if not isinstance(config, dict) or not isinstance(config.get("years"), int):
        raise ValueError("collection evidence has an invalid configuration")
    return {
        **{name: value for name, value in identity.items() if name != "config"},
        "years": config["years"],
    }


def _collection_state_payload(checkpoint: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in checkpoint.items()
        if key not in {"collection_snapshot", "collection_snapshot_sha256"}
    }


def _collection_state_payload_from_evidence(evidence: CollectionEvidence) -> dict[str, object]:
    return {
        "source_url": evidence.source_url,
        "market": evidence.market,
        "candle_unit_minutes": evidence.candle_unit_minutes,
        "page_size": evidence.page_size,
        "config": {
            "market": evidence.market,
            "candle_unit_minutes": evidence.candle_unit_minutes,
            "page_size": evidence.page_size,
            "years": evidence.years,
        },
        "start_utc": evidence.start_utc,
        "end_utc": evidence.end_utc,
        "config_sha256": evidence.config_sha256,
        "pages": [
            {
                "path": page.path.name,
                "sha256": page.sha256,
                "request_to_utc": page.request_to_utc,
                "oldest_timestamp_utc": page.oldest_timestamp_utc,
                "row_count": page.row_count,
            }
            for page in evidence.pages
        ],
        "next_to_utc": evidence.next_to_utc,
        "previous_oldest_utc": evidence.previous_oldest_utc,
        "complete": evidence.complete,
    }


def _config_from_evidence(value: object) -> DataConfig:
    expected = {"market", "candle_unit_minutes", "page_size", "years"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("collection evidence checkpoint has an invalid configuration")
    market = value["market"]
    candle_unit_minutes = value["candle_unit_minutes"]
    page_size = value["page_size"]
    years = value["years"]
    if (
        not isinstance(market, str)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in (candle_unit_minutes, page_size, years))
        or market != "KRW-BTC"
        or candle_unit_minutes != 240
        or not 1 <= page_size <= 200
        or years < 1
    ):
        raise ValueError("collection evidence checkpoint has an invalid configuration")
    return DataConfig(
        market=market,
        candle_unit_minutes=candle_unit_minutes,
        page_size=page_size,
        years=years,
    )


def _parse_page_evidence(root: Path, value: object) -> tuple[RawPageEvidence, ...]:
    if not isinstance(value, list):
        raise ValueError("collection evidence checkpoint pages are invalid")
    pages: list[RawPageEvidence] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "request_to_utc",
            "oldest_timestamp_utc",
            "row_count",
        }:
            raise ValueError("collection evidence checkpoint page entry is invalid")
        path_name = item["path"]
        sha256 = item["sha256"]
        request_to_utc = item["request_to_utc"]
        oldest_timestamp_utc = item["oldest_timestamp_utc"]
        row_count = item["row_count"]
        if (
            not isinstance(path_name, str)
            or not isinstance(sha256, str)
            or not isinstance(request_to_utc, str)
            or not isinstance(oldest_timestamp_utc, (str, type(None)))
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
            or path_name != f"page-{sha256}.json"
        ):
            raise ValueError("collection evidence checkpoint page metadata is invalid")
        pages.append(
            RawPageEvidence(
                path=root / path_name,
                sha256=sha256,
                request_to_utc=request_to_utc,
                oldest_timestamp_utc=oldest_timestamp_utc,
                row_count=row_count,
            )
        )
    return tuple(pages)


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is missing or malformed") from error


def _read_hashed_json(path: Path, expected_hash: str, label: str) -> object:
    if len(expected_hash) != 64 or any(character not in "0123456789abcdef" for character in expected_hash):
        raise ValueError(f"{label} has an invalid hash")
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} is missing") from error
    actual_hash = hashlib.sha256(contents).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(f"{label} hash does not match its checkpoint")
    try:
        return json.loads(contents.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is malformed") from error


def _write_content_addressed(destination: Path, contents: bytes) -> None:
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as error:
            raise ValueError("content-addressed evidence cannot be read") from error
        if existing != contents:
            raise ValueError("content-addressed evidence path already contains different bytes")
        return
    _atomic_write(destination, contents)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _oldest_timestamp(payload: list[dict[str, object]]) -> str | None:
    timestamps = [row["candle_date_time_utc"] for row in payload if isinstance(row.get("candle_date_time_utc"), str)]
    return min(timestamps) if timestamps else None


def _atomic_write(destination: Path, contents: bytes) -> None:
    temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

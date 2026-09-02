"""Deterministic, content-addressed persistence for raw public snapshots."""

from dataclasses import asdict, dataclass
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

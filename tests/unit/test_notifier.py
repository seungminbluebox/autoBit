from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from autobit.alerts.notifier import (
    NullNotifier,
    SafeNotifier,
    TelegramNotifier,
    deliver_alert_once,
)
from autobit.persistence.sqlite_store import SQLiteStore


UTC = timezone.utc
AT = datetime(2026, 1, 1, tzinfo=UTC)


class _BrokenNotifier:
    def send(self, event: dict[str, object]) -> bool:
        del event
        raise RuntimeError("network failed with secret-token")


def test_safe_notifier_contains_ordinary_failures_without_rendering_them() -> None:
    assert not SafeNotifier(_BrokenNotifier()).send(
        {"type": "HALTED", "reason": "API_FAILURES"}
    )
    assert NullNotifier().send({"type": "HEALTHY"})


def test_safe_notifier_does_not_swallow_process_control() -> None:
    class Interrupted:
        def send(self, event: dict[str, object]) -> bool:
            del event
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SafeNotifier(Interrupted()).send({"type": "HEALTHY"})


def test_telegram_uses_one_public_post_with_bounded_deterministic_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier("TOKEN-123", "CHAT-456", http_client=client)
    event = {"z": "x" * 5_000, "a": 1}

    assert notifier.send(event)
    assert notifier.send(event)

    assert len(requests) == 2
    assert all(request.method == "POST" for request in requests)
    assert all(
        str(request.url) == "https://api.telegram.org/botTOKEN-123/sendMessage"
        for request in requests
    )
    bodies = [request.content.decode("utf-8") for request in requests]
    assert bodies[0] == bodies[1]
    assert "chat_id=CHAT-456" in bodies[0]
    assert len(bodies[0]) < 4_100
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }
    assert "TOKEN-123" not in repr(notifier)
    assert "CHAT-456" not in repr(notifier)


def test_telegram_failure_is_generic_and_does_not_disclose_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.url}", request=request)

    notifier = TelegramNotifier(
        "TOKEN-SECRET",
        "CHAT-SECRET",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError) as caught:
        notifier.send({"type": "HALTED"})

    rendered = f"{caught.value!r} {caught.value} {notifier!r}"
    assert "TOKEN-SECRET" not in rendered
    assert "CHAT-SECRET" not in rendered


def test_safe_telegram_contains_http_and_serialization_failures() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="upstream detail")

    notifier = SafeNotifier(
        TelegramNotifier(
            "TOKEN-SECRET",
            "CHAT-SECRET",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    )

    assert not notifier.send({"type": "HALTED"})
    assert len(requests) == 1
    assert not notifier.send({"not_json": {object()}})
    assert len(requests) == 1


def test_alert_attempt_is_claimed_before_send_and_exact_retry_is_skipped(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    source_payload = {"status": "PROCESSED"}
    store.append_event("cycle:one", "PAPER_CYCLE", AT, source_payload)

    class InspectingNotifier:
        calls = 0

        def send(self, event: dict[str, object]) -> bool:
            self.calls += 1
            assert any(
                item.event_type == "ALERT_ATTEMPT"
                for item in store.replay_state().event_evidence
            )
            return True

    notifier = InspectingNotifier()
    first = deliver_alert_once(
        store,
        notifier,
        source_event_id="cycle:one",
        source_event_type="PAPER_CYCLE",
        source_payload=source_payload,
        logical_at=AT,
    )
    second = deliver_alert_once(
        store,
        notifier,
        source_event_id="cycle:one",
        source_event_type="PAPER_CYCLE",
        source_payload=source_payload,
        logical_at=AT,
    )

    assert first is True
    assert second is None
    assert notifier.calls == 1
    assert [
        event.event_type for event in store.replay_state().event_evidence
    ] == ["PAPER_CYCLE", "ALERT_ATTEMPT"]


def test_failed_alert_records_only_one_generic_failure_across_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    source_payload = {"status": "PROCESSED"}
    store.append_event("cycle:one", "PAPER_CYCLE", AT, source_payload)

    assert deliver_alert_once(
        store,
        SafeNotifier(_BrokenNotifier()),
        source_event_id="cycle:one",
        source_event_type="PAPER_CYCLE",
        source_payload=source_payload,
        logical_at=AT,
    ) is False
    store.close()
    reopened = SQLiteStore(path)
    reopened.initialize()
    assert deliver_alert_once(
        reopened,
        SafeNotifier(_BrokenNotifier()),
        source_event_id="cycle:one",
        source_event_type="PAPER_CYCLE",
        source_payload=source_payload,
        logical_at=AT,
    ) is None

    evidence = reopened.replay_state().event_evidence
    assert [event.event_type for event in evidence] == [
        "PAPER_CYCLE",
        "ALERT_ATTEMPT",
        "ALERT_FAILURE",
    ]
    assert dict(evidence[-1].payload) == {
        "failure_code": "DELIVERY_FAILED",
        "source_event_id": "cycle:one",
        "source_event_type": "PAPER_CYCLE",
        "version": 1,
    }
    assert b"secret-token" not in path.read_bytes()


def test_concurrent_alert_claim_has_one_external_attempt(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    seed = SQLiteStore(path)
    seed.initialize()
    source_payload = {"status": "PROCESSED"}
    seed.append_event("cycle:concurrent", "PAPER_CYCLE", AT, source_payload)
    first = SQLiteStore(path)
    first.initialize()
    second = SQLiteStore(path)
    second.initialize()

    class CountingNotifier:
        calls = 0

        def send(self, event: dict[str, object]) -> bool:
            del event
            self.calls += 1
            return True

    notifier = CountingNotifier()

    def attempt(store: SQLiteStore) -> bool | None:
        return deliver_alert_once(
            store,
            notifier,
            source_event_id="cycle:concurrent",
            source_event_type="PAPER_CYCLE",
            source_payload=source_payload,
            logical_at=AT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, (first, second)))

    assert sorted(results, key=lambda item: item is None) == [True, None]
    assert notifier.calls == 1
    assert len(
        [
            event
            for event in seed.replay_state().event_evidence
            if event.event_type == "ALERT_ATTEMPT"
        ]
    ) == 1


def test_control_flow_crash_after_marker_is_missed_not_duplicated(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()
    source_payload = {"status": "PROCESSED"}
    store.append_event("cycle:crash", "PAPER_CYCLE", AT, source_payload)

    class CrashNotifier:
        def send(self, event: dict[str, object]) -> bool:
            del event
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        deliver_alert_once(
            store,
            CrashNotifier(),
            source_event_id="cycle:crash",
            source_event_type="PAPER_CYCLE",
            source_payload=source_payload,
            logical_at=AT,
        )
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    assert deliver_alert_once(
        reopened,
        NullNotifier(),
        source_event_id="cycle:crash",
        source_event_type="PAPER_CYCLE",
        source_payload=source_payload,
        logical_at=AT,
    ) is None


def test_alert_rejects_missing_or_mismatched_source_before_external_attempt(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()

    class CountingNotifier:
        calls = 0

        def send(self, event: dict[str, object]) -> bool:
            del event
            self.calls += 1
            return True

    notifier = CountingNotifier()
    with pytest.raises(ValueError, match="durable source"):
        deliver_alert_once(
            store,
            notifier,
            source_event_id="cycle:missing",
            source_event_type="PAPER_CYCLE",
            source_payload={"status": "PROCESSED"},
            logical_at=AT,
        )
    store.append_event("cycle:present", "PAPER_CYCLE", AT, {"status": "PROCESSED"})
    with pytest.raises(ValueError, match="durable source"):
        deliver_alert_once(
            store,
            notifier,
            source_event_id="cycle:present",
            source_event_type="PAPER_CYCLE",
            source_payload={"status": "UNSAFE_DATA"},
            logical_at=AT,
        )

    assert notifier.calls == 0
    assert not any(
        event.event_type == "ALERT_ATTEMPT"
        for event in store.replay_state().event_evidence
    )

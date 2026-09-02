from __future__ import annotations

from pathlib import Path

import pytest

from autobit.domain.models import PositionState
from autobit.persistence.sqlite_store import SQLiteStore


def test_replay_restores_cash_position_and_breaker_state(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize(initial_equity=100.0)
    store.append_fill(
        order_id="entry-1",
        side="BUY",
        quantity=0.2,
        price=100.0,
        fee=0.01,
        occurred_at="2026-01-01T04:00:00Z",
    )
    store.append_event(
        event_id="breaker-1",
        event_type="BREAKER_STATE",
        occurred_at="2026-01-01T08:00:00Z",
        payload={"halt_entries": False, "reasons": [], "recovery_stage": "NORMAL"},
    )
    before_restart = store.replay_state()
    persisted_before_restart = store.load_snapshot()
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize(initial_equity=100.0)
    after_restart = reopened.replay_state()

    assert after_restart == before_restart
    assert persisted_before_restart == before_restart
    assert after_restart.position_state is PositionState.LONG
    assert after_restart.btc_quantity == pytest.approx(0.2)
    assert after_restart.cash == pytest.approx(79.99)
    assert after_restart.breaker_state == {
        "halt_entries": False,
        "reasons": (),
        "recovery_stage": "NORMAL",
    }


def test_restart_after_rolled_back_order_has_no_phantom_state(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = SQLiteStore(path)
    store.initialize()

    with pytest.raises(RuntimeError, match="power loss"):
        with store.transaction():
            store.record_order_once(
                "KRW-BTC:2026-01-01T00:00:00Z:ENTRY",
                "BUY",
                0.2,
            )
            raise RuntimeError("power loss")
    store.close()

    reopened = SQLiteStore(path)
    reopened.initialize()
    state = reopened.replay_state()

    assert state.position_state is PositionState.FLAT
    assert state.pending_orders == ()
    assert state.last_sequence == 0
    assert reopened.record_order_once(
        "KRW-BTC:2026-01-01T00:00:00Z:ENTRY",
        "BUY",
        0.2,
    )


def test_reopen_reads_completed_fill_but_does_not_apply_retry_twice(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    fill = dict(
        order_id="entry-1",
        side="BUY",
        quantity=0.25,
        price=80.0,
        fee=0.02,
        occurred_at="2026-01-01T04:00:00Z",
    )
    first = SQLiteStore(path)
    first.initialize()
    assert first.append_fill(**fill)
    first.close()

    second = SQLiteStore(path)
    second.initialize()
    before_retry = second.replay_state()
    assert not second.append_fill(**fill)
    after_retry = second.replay_state()

    assert after_retry == before_retry
    assert after_retry.cash == pytest.approx(79.98)
    assert after_retry.btc_quantity == pytest.approx(0.25)

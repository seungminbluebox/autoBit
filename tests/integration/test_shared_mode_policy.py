"""Real execution adapters consume the shared engine's returned intents."""

from dataclasses import replace
from datetime import timedelta

import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig, StrategyConfig
from autobit.core.engine import StrategyEngine
from autobit.execution.paper_broker import PaperBroker

from test_backtest_risk_time import _entry_signal, _scenario_frame
from test_paper_service import END, _Source, _breakout_history, _history, _service


@pytest.mark.parametrize("mode", ["backtest", "paper"])
@pytest.mark.parametrize("quantity", [.1, .2])
def test_changing_only_shared_result_changes_real_submitted_quantity(monkeypatch, tmp_path, mode, quantity):
    original = StrategyEngine.decide
    recorded = []

    def decorated(self, snapshot):
        decision = original(self, snapshot)
        recorded.append((snapshot, decision))
        return replace(decision, quantity=quantity) if decision.action == "buy" else decision

    monkeypatch.setattr(StrategyEngine, "decide", decorated)
    if mode == "backtest":
        result = run_backtest(_scenario_frame([_entry_signal(), {"open": 100.}, {}]), BacktestConfig(costs=CostConfig(0, 0)))
        entries = [order for order in result.orders if order.side == "BUY" and order.status == "COMPLETED"]
        assert len(entries) == 1
        assert entries[0].requested_quantity == quantity
        assert entries[0].filled_quantity == quantity
        assert entries[0].fill_time > entries[0].signal_time
    else:
        store, service = _service(tmp_path / "shared.sqlite3", _Source(_breakout_history(END)))
        try:
            service.process_completed_candle(END)
            order, = PaperBroker(store, CostConfig(0, 0)).reconcile().active_orders
            assert order.requested_quantity == quantity
        finally:
            store.close()
    assert any(decision.action == "buy" for _, decision in recorded)


@pytest.mark.parametrize("mode", ["backtest", "paper"])
def test_common_sell_intent_is_submitted_by_real_long_processing(monkeypatch, tmp_path, mode):
    original = StrategyEngine.decide
    recorded = []

    def decorated(self, snapshot):
        decision = original(self, snapshot)
        if snapshot.position is not None and decision.action == "hold":
            recorded.append(snapshot)
            return replace(decision, action="sell", reason="CLOSE_EXIT",
                           quantity=snapshot.position.quantity, next_stop=None)
        return decision

    monkeypatch.setattr(StrategyEngine, "decide", decorated)
    if mode == "backtest":
        frame = _scenario_frame([_entry_signal(), {"open": 100.}, {}, {}])
        result = run_backtest(frame, BacktestConfig(costs=CostConfig(0, 0)))
        trade, = result.trades
        assert trade.exit_reason == "CLOSE_EXIT"
        assert trade.quantity == .4
        assert trade.exit_time == frame.index[-2]
    else:
        store, service = _service(tmp_path / "long.sqlite3", _Source(_history(END)))
        try:
            broker = PaperBroker(store, CostConfig(0, 0))
            entry = broker.submit_entry(END - timedelta(hours=12), quantity=.4)
            broker.process_open(entry.order_id, END - timedelta(hours=8), open_price=100.)
            broker.set_stop(END - timedelta(hours=8), 95., active_after=END - timedelta(hours=8), source_id=entry.order_id)
            service.process_completed_candle(END)
            order, = broker.reconcile().active_orders
            assert (order.side, order.reason, order.requested_quantity) == ("SELL", "CLOSE_EXIT", .4)
        finally:
            store.close()
    assert len(recorded) == 1
    assert recorded[0].position.quantity == .4


def test_paper_old_entry_multiplier_survives_restart_with_new_strategy(tmp_path):
    source = _Source(_breakout_history(END))
    path = tmp_path / "historical-entry.sqlite3"
    store, service = _service(path, source, strategy=StrategyConfig(initial_atr_mult=2.))
    service.process_completed_candle(END)
    original_events = store.replay_state().event_evidence
    store.close()
    next_end = END + timedelta(hours=4)
    next_frame = source.frame.copy()
    next_frame.loc[END] = [102., 103., 101., 102., 1.]
    store, service = _service(path, _Source(next_frame), clock_at=next_end + timedelta(minutes=10),
                              strategy=StrategyConfig(initial_atr_mult=5.))
    try:
        service.process_completed_candle(next_end)
        broker = PaperBroker(store, CostConfig(0, 0))
        state = broker.reconcile()
        entry = next(fill for fill in state.fills if fill.side == "BUY")
        # Signal ATR is 30/14. Use historical 2x, never new strategy's 5x.
        assert broker.initial_stop(entry.order_id) == pytest.approx(102. - 2. * (30. / 14.))
        assert tuple(store.replay_state().event_evidence[:len(original_events)]) == original_events
    finally:
        store.close()


def test_identical_completed_indicator_facts_produce_identical_real_mode_entry(monkeypatch, tmp_path):
    original = StrategyEngine.decide
    recorded = []

    def recording(self, snapshot):
        result = original(self, snapshot)
        if result.action == "buy":
            recorded.append((snapshot, result))
        return result

    monkeypatch.setattr(StrategyEngine, "decide", recording)
    store, service = _service(tmp_path / "parity.sqlite3", _Source(_breakout_history(END)))
    try:
        service.process_completed_candle(END)
        paper_order, = PaperBroker(store, CostConfig(0, 0)).reconcile().active_orders
    finally:
        store.close()
    paper_input, paper_intent = recorded.pop()
    frame = _scenario_frame([dict(paper_input.row), {"open": 102., "high": 103., "close": 102.}, {}])
    backtest = run_backtest(frame, BacktestConfig(costs=CostConfig(0, 0)))
    back_input, back_intent = recorded.pop()
    assert back_intent == paper_intent
    assert (back_input.cash, back_input.equity, back_input.position, back_input.has_pending_order, back_input.risk) == (
        paper_input.cash, paper_input.equity, paper_input.position, paper_input.has_pending_order, paper_input.risk)
    entry = next(order for order in backtest.orders if order.side == "BUY" and order.status == "COMPLETED")
    # Hand calculation: ATR=30/14, stop distance=75/14, volatility factor=.952.
    assert paper_order.requested_quantity == pytest.approx(.3554133333333333)
    assert entry.requested_quantity == paper_order.requested_quantity

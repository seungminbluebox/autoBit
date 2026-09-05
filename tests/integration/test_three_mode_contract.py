"""Offline acceptance of real adapters, not equality of exchange/simulated fills.

Hand-authored completed indicator rows enter at each adapter's normalization
boundary. Paper is genuinely funded with 100 normalized units; live has 1,000,000
mock KRW. Only money/PnL/quantity are divided by SCALE for comparison. Prices,
ATR, stops and risk ratios are unchanged. Zero costs isolate policy arithmetic.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig, StrategyConfig
from autobit.core import DecisionInput, RiskObservation, StrategyEngine
from autobit.execution.paper_broker import PaperBroker
from autobit.live import guard
from autobit.live.client import Credentials, LiveClient
from autobit.live.journal import LiveJournal
from autobit.live.service import LiveService
from autobit.paper import service as paper_module
from autobit.paper.service import CycleStatus
from autobit.risk.breakers import RiskDecision

from test_locked_live_service import Venue
from test_paper_service import END, _Source, _history, _service


SCALE = 10000
STEP = timedelta(hours=4)
ZERO_COST = CostConfig(0, 0)
NORMAL = RiskDecision(.02, .70, None, ())


class ZeroCostVenue(Venue):
    """Stateful HTTP venue fixture; acknowledgements never imply a fill."""

    def __call__(self, request):
        response = super().__call__(request)
        if request.url.path == '/v1/orders/chance':
            payload = response.json()
            payload.update(bid_fee='0', ask_fee='0')
            return httpx.Response(200, json=payload)
        if request.method == 'POST':
            payload = response.json()
            payload.update(uuid='venue-' + payload['identifier'], remaining_fee='0',
                           reserved_fee='0', locked=payload['price'] or payload['volume'])
            self.orders[payload['identifier']] = payload
            return httpx.Response(201, json=payload)
        return response

    def fill_at(self, identifier, price):
        value = self.orders[identifier]
        cash, btc = Decimal(self.cash), Decimal(self.btc)
        quantity = (Decimal(value['price']) / Decimal(str(price))
                    if value['side'] == 'bid' else Decimal(value['volume']))
        super().fill(identifier, quantity=str(quantity), price=str(price), fee='0')
        amount = quantity * Decimal(str(price))
        self.cash = str(cash - amount if value['side'] == 'bid' else cash + amount)
        self.btc = str(btc + quantity if value['side'] == 'bid' else btc - quantity)
        value.update(remaining_fee='0', reserved_fee='0', locked='0')


def reconcile_healthy(live, venue):
    # Unresolved orders legitimately latch health. Supply distinct factual API
    # successes; the following completed cycle still has the reduced overlay.
    for _ in range(3):
        venue.now += timedelta(seconds=1)
        live.reconcile()
    assert not live.journal.pending()


def prepared(rows, strategy=StrategyConfig()):
    frame = _history(END, 610)
    defaults = dict(ema_200=90., entry_high=120., previous_close=99.,
                    previous_entry_high=99., baseline_atr_pct=.02,
                    exit_low=40., warmup_complete=False, entry_data_valid=True)
    defaults[f'atr_{strategy.atr_period}'] = 2.
    for key, value in defaults.items():
        frame[key] = value
    for number, changes in enumerate(rows):
        row = dict(open=100., high=101., low=99., close=100., volume=10000.,
                   **defaults)
        row.update(warmup_complete=True)
        row.update(changes)
        frame.loc[END + number * STEP] = row
    return frame


def normal_rows():
    return [dict(entry_high=99.), {},
            dict(open=100., high=112., low=99., close=110.),
            dict(open=110., high=111., low=107., close=108., exit_low=109.),
            dict(open=108., high=109., low=107., close=108.)]


def recovery_rows(independent_halt):
    rows = [dict(entry_high=99.), {},
            dict(open=60., high=61., low=59., close=60., ema_200=50.)]
    rows += [dict(open=60., high=61., low=59., close=60., ema_200=50.)
             for _ in range(17)]
    # Index20 is exactly72h after the gap loss at index2.
    rows.append(dict(entry_high=99., atr_14=8. if independent_halt else 2.))
    rows += [dict(entry_high=99.) for _ in range(3)]
    rows.append({})
    return rows


def run_modes(monkeypatch, tmp_path, rows, *, mutation=False, gap=False,
              strategy=StrategyConfig()):
    """Capture actual common outputs and retain adapter submission/fill facts."""
    frame = prepared(rows, strategy)
    captured = {mode: [] for mode in ('paper', 'backtest', 'live')}
    mode = 'paper'
    original = StrategyEngine.decide

    def observe(self, snapshot):
        decision = original(self, snapshot)
        # One shared current rule: halve permitted new entry quantity. All
        # consumers must use this returned value, not local sizing branches.
        if (mutation and decision.action == 'buy'
                and (mutation is True or 'recovery' in decision.risk.reasons)):
            decision = replace(decision, quantity=decision.quantity / 2)
        if snapshot.row.get('warmup_complete'):
            captured[mode].append((snapshot, decision))
        return decision

    monkeypatch.setattr(StrategyEngine, 'decide', observe)
    # Indicator calculation is a separate tested producer. Here identical
    # normalized evidence is supplied; no risk/health/broker gate is patched.
    monkeypatch.setattr(paper_module, 'compute_trend_indicators',
                        lambda candles, *a, **k: frame.loc[candles.index].copy())
    store = None
    paper_states = []
    try:
        for number in range(len(rows)):
            end = END + (number + 1) * STEP
            source = _Source(frame.loc[frame.index < end])
            store, service = _service(tmp_path / 'paper.sqlite', source, store=store,
                                      clock_at=end + timedelta(minutes=10), strategy=strategy)
            assert service.process_completed_candle(end).status is CycleStatus.PROCESSED
            paper_states.append(PaperBroker(store, ZERO_COST).reconcile())
            before = store.replay_state()
            assert service.process_completed_candle(end).status is CycleStatus.ALREADY_PROCESSED
            assert store.replay_state() == before
    finally:
        if store:
            store.close()

    mode = 'backtest'
    backtest = run_backtest(frame, BacktestConfig(strategy=strategy, costs=ZERO_COST))

    mode = 'live'
    monkeypatch.setattr(guard, 'require_live_authorization', lambda: None)
    venue = ZeroCostVenue()
    venue.cash = str(100 * SCALE)
    client = LiveClient(lambda: Credentials('fake-access', 'fake-secret'),
                        transport=httpx.MockTransport(venue))
    live = LiveService(tmp_path / 'live.sqlite', client,
                       engine=StrategyEngine(strategy, ZERO_COST), clock=lambda: venue.now)
    live_states = []
    try:
        for number in range(len(rows)):
            bar_at = pd.Timestamp(END + number * STEP)
            row = frame.loc[bar_at].to_dict() | {'timestamp': bar_at.to_pydatetime()}
            if gap and number == 2:
                venue.now = bar_at.to_pydatetime()
                live.on_price(60.)
                identifier = json.loads(venue.posts[-1].content)['identifier']
                venue.fill_at(identifier, 60.)
                reconcile_healthy(live, venue)
            cutoff = (bar_at + STEP).to_pydatetime()
            venue.now = cutoff
            observation = RiskObservation(cutoff, 100., (),
                                          (row[f'atr_{strategy.atr_period}'] / row['close']) / row['baseline_atr_pct'], True, True)
            # Caller monetary values are deliberately normalized: LiveService
            # must replace them with its authoritative venue/journal facts.
            snapshot = DecisionInput(row, 100., 100., None, False, NORMAL)
            live.process_completed_candle(snapshot, observation)
            state = live.journal.state()
            live_states.append(state)
            if live.journal.pending():
                identifier = live.journal.pending()[0].identifier
                venue.fill_at(identifier, row['close'])
                reconcile_healthy(live, venue)
            if number == 0:
                # Reopen actual journal facts, not manually seeded money/risk.
                live.close()
                live = LiveService(tmp_path / 'live.sqlite', client,
                                   engine=StrategyEngine(strategy, ZERO_COST), clock=lambda: venue.now)
        final_live = live.journal.state()
    finally:
        live.close()
    return captured, paper_states, backtest, live_states, final_live, venue


def policy_tuple(decision, scale=1, *, live=False):
    risk = decision.risk
    until = risk.halted_until - STEP if live and risk.halted_until else risk.halted_until
    return (decision.action, decision.reason, decision.quantity / scale,
            decision.next_stop, risk.risk_rate, risk.exposure_cap, until, risk.reasons)


def assert_policy_equal(actual, expected):
    assert actual[:2] == expected[:2]
    assert actual[2] == pytest.approx(expected[2], rel=1e-11, abs=1e-12)
    assert actual[3:] == expected[3:]


@pytest.mark.parametrize('mutation', [False, True])
def test_three_real_paths_entry_close_and_delayed_trailing(monkeypatch, tmp_path, mutation):
    records, paper, backtest, live, final_live, venue = run_modes(
        monkeypatch, tmp_path, normal_rows(), mutation=mutation)
    quantity = .2 if mutation else .4  # 100*.02/(2*2.5), optionally halved.
    for mode, scale in [('paper', 1), ('backtest', 1), ('live', SCALE)]:
        values = records[mode]
        assert len(values) == 5
        entry = policy_tuple(values[0][1], scale, live=mode == 'live')
        assert_policy_equal(entry, ('buy', 'ENTRY', quantity, 95., .02, .7, None, ()))
        trailing = values[2][1]
        assert trailing.action == 'hold' and trailing.next_stop == 106.
        assert values[2][0].position.current_stop == 95.  # Not applied retroactively.
        assert values[3][0].position.current_stop == 106.
        assert_policy_equal(policy_tuple(values[3][1], scale),
                            ('sell', 'CLOSE_EXIT', quantity, None, .02, .7, None, ()))
        # Immediate post-fill health overlays are mode-specific inputs, not
        # identical snapshots. Compare only the matched normalized decisions.
        for number in (0, 2, 3):
            assert_policy_equal(policy_tuple(values[number][1], scale, live=mode == 'live'),
                                policy_tuple(records['paper'][number][1]))
            actual = values[number][0]
            expected = records['paper'][number][0]
            assert actual.cash / scale == pytest.approx(expected.cash, rel=1e-11, abs=1e-12)
            assert actual.equity / scale == pytest.approx(expected.equity, rel=1e-11, abs=1e-12)
            assert actual.has_pending_order == expected.has_pending_order
            if actual.position:
                # Backtrader stages the prior high; paper/live stage the folded
                # high. Compare the exact max fold that the common policy uses.
                assert replace(actual.position, quantity=actual.position.quantity / scale,
                               high_water=max(actual.position.high_water, actual.row['high'])) == replace(
                                   expected.position, high_water=max(expected.position.high_water, expected.row['high']))
            else:
                assert expected.position is None
    assert paper[0].cash == 100. and paper[0].btc_quantity == 0.
    assert paper[0].active_orders[0].requested_quantity == quantity
    assert live[0].cash == Decimal('1000000') and live[0].position is None
    assert live[0].risk.initial_equity == 1000000.
    assert Decimal(json.loads(venue.posts[0].content)['price']) == Decimal(str(quantity * 100 * SCALE))
    assert Decimal(json.loads(venue.posts[1].content)['volume']) == Decimal(str(quantity * SCALE))
    completed = [order for order in backtest.orders if order.side == 'BUY' and order.status == 'COMPLETED']
    assert len(completed) == 1 and completed[0].requested_quantity == quantity
    assert completed[0].fill_time > completed[0].signal_time
    assert live[2].position.current_stop == 95. and live[2].position.pending_stop == 106.
    assert live[2].position.pending_stop_at == END + 3 * STEP
    assert records['live'][1][1].risk == RiskDecision(.01, .35, None, ('health_recovery_reduced',))
    assert float(final_live.cash) / SCALE == pytest.approx(paper[-1].cash, rel=1e-11, abs=1e-12)
    assert float(final_live.closed_trades[0].net_pnl) / SCALE == pytest.approx(quantity * 8, rel=1e-11, abs=1e-12)
    assert final_live.closed_trades[0].exit_time != paper[-1].completed_trades[0].exit_time


def test_three_real_paths_share_non_default_atr_period(monkeypatch, tmp_path):
    strategy = StrategyConfig(atr_period=7)

    records, paper, backtest, live, final_live, venue = run_modes(
        monkeypatch,
        tmp_path,
        normal_rows(),
        strategy=strategy,
    )

    for mode, scale in [('paper', 1), ('backtest', 1), ('live', SCALE)]:
        entry = policy_tuple(records[mode][0][1], scale, live=mode == 'live')
        assert_policy_equal(entry, ('buy', 'ENTRY', .4, 95., .02, .7, None, ()))
    assert paper[-1].completed_trades[0].net_pnl == pytest.approx(3.2)
    assert backtest.trades[0].net_pnl == pytest.approx(3.2)
    assert float(final_live.closed_trades[0].net_pnl) / SCALE == pytest.approx(3.2)
    assert len(venue.posts) == 2


@pytest.mark.parametrize('independent_halt', [False, True])
@pytest.mark.parametrize('mutation', [False, 'recovery'])
def test_real_loss_then_72_hour_recovery_respects_independent_halt(monkeypatch, tmp_path, independent_halt, mutation):
    records, paper, backtest, live, final_live, venue = run_modes(
        monkeypatch, tmp_path, recovery_rows(independent_halt), gap=True, mutation=mutation)
    assert paper[2].cash == 84. and paper[2].btc_quantity == 0.
    assert float(live[2].cash) / SCALE == 84.
    assert float(final_live.closed_trades[0].net_pnl) / SCALE == -16.
    for mode, scale in [('paper', 1), ('backtest', 1), ('live', SCALE)]:
        values = records[mode]
        # Gap stop may consume Backtrader's completed callback without decide.
        before_expiry = values[-6][1]
        assert_policy_equal(policy_tuple(before_expiry, scale, live=mode == 'live'),
                            ('hold', None, 0., None, 0., 0., END + 20 * STEP,
                             ('drawdown_halt', 'weekly_loss_reduced')))
        recovery = values[-5][1]
        assert 'recovery' in recovery.risk.reasons
        assert 'drawdown_halt' not in recovery.risk.reasons
        if independent_halt:
            assert recovery.action == 'hold'
            assert recovery.risk.reasons == ('recovery', 'weekly_loss_reduced', 'volatility_halt')
            assert recovery.risk.risk_rate == recovery.risk.exposure_cap == 0.
            recovered = values[-2][1]
        else:
            recovered = recovery
        # Weekly loss is still active: stricter half of .25% /15%, not a bypass.
        assert_policy_equal(policy_tuple(recovered, scale, live=mode == 'live'),
                            ('buy', 'ENTRY', .0105 if mutation else .021, 95., .00125, .075, None,
                             ('recovery', 'weekly_loss_reduced')))
    assert live[20].risk.equity_peak == 1000000.
    assert live[20].risk.recovery_started_at == END + 3 * STEP
    assert live[20].risk.last_risk_at == live[20].risk.recovery_started_at + timedelta(hours=72)
    assert len(venue.posts) == 3  # Entry, protective exit, one reduced entry.
    assert Decimal(json.loads(venue.posts[-1].content)['price']) == Decimal('10500' if mutation else '21000')
    expected_quantity = .0105 if mutation else .021
    assert float(final_live.position.quantity) / SCALE == pytest.approx(expected_quantity, rel=1e-11, abs=1e-12)
    assert paper[-1].btc_quantity == pytest.approx(expected_quantity, rel=1e-11, abs=1e-12)
    entries = [order for order in backtest.orders if order.side == 'BUY' and order.status == 'COMPLETED']
    assert len(entries) == 2
    assert entries[-1].requested_quantity == pytest.approx(expected_quantity, rel=1e-11, abs=1e-12)


def test_cli_help_reports_fixed_live_lock_without_loading_live():
    code = ('import sys; from autobit.cli import build_parser; '
            'print(build_parser().format_help()); '
            'assert not any(n.startswith("autobit.live") for n in sys.modules)')
    result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'Live trading is locked' in result.stdout
    for command in ('data-download', 'data-quality', 'backtest', 'walk-forward',
                    'paper-once', 'paper-run', 'paper-status'):
        assert command in result.stdout


def flat_live(monkeypatch, tmp_path, *, cash='100000'):
    monkeypatch.setattr(guard, 'require_live_authorization', lambda: None)
    venue = ZeroCostVenue()
    venue.cash = cash
    venue.now = END + STEP
    client = LiveClient(lambda: Credentials('fake-access', 'fake-secret'),
                        transport=httpx.MockTransport(venue))
    service = LiveService(tmp_path / 'live.sqlite', client,
                          engine=StrategyEngine(StrategyConfig(), ZERO_COST), clock=lambda: venue.now)
    row = prepared([{}]).loc[END].to_dict() | {'timestamp': END}
    snapshot = DecisionInput(row, 100., 100., None, False, NORMAL)
    observation = RiskObservation(END + STEP, 100., (), 1., True, True)
    return service, venue, snapshot, observation


@pytest.mark.parametrize('case', ['repeated', 'stale', 'unclosed'])
def test_live_open_labels_reject_bad_chronology_without_duplicate_orders(monkeypatch, tmp_path, case):
    service, venue, snapshot, observation = flat_live(monkeypatch, tmp_path)
    try:
        service.process_completed_candle(snapshot, observation)
        original = service.journal.state()
        if case == 'repeated':
            venue.now += timedelta(seconds=1)
        elif case == 'stale':
            venue.now += STEP + timedelta(minutes=11)
        else:
            snapshot = replace(snapshot, row=dict(snapshot.row) | {'timestamp': END + STEP})
        decision = service.process_completed_candle(snapshot, observation)
        after = service.journal.state()
        assert decision.action == 'hold' and not venue.posts
        assert after.risk == original.risk and after.completed_at == END
        if case == 'repeated':
            assert decision.risk == NORMAL
        else:
            assert decision.risk.reasons == ('system_unhealthy',)
            assert decision.risk.risk_rate == 0.
    finally:
        service.close()


def test_real_100_krw_is_valid_but_normalized_provenance_is_not(monkeypatch, tmp_path):
    service, venue, snapshot, observation = flat_live(monkeypatch, tmp_path, cash='100')
    try:
        snapshot = replace(snapshot, row=dict(snapshot.row) | {'entry_high': 99.})
        decision = service.process_completed_candle(snapshot, observation)
        assert_policy_equal(policy_tuple(decision), ('buy', 'ENTRY', .4, 95., .02, .7, None, ()))
        assert service.journal.state().risk.initial_equity == 100.
        assert not venue.posts and not service.journal.pending()  # 40 KRW <5000.
    finally:
        service.close()
    with pytest.raises(ValueError):
        LiveJournal(tmp_path / 'normalized.sqlite', provenance='normalized100')


def test_consumed_live_entry_replay_cannot_submit_another_identifier(monkeypatch, tmp_path):
    service, venue, snapshot, observation = flat_live(monkeypatch, tmp_path)
    try:
        snapshot = replace(snapshot, row=dict(snapshot.row) | {'entry_high': 99.})
        service.process_completed_candle(snapshot, observation)
        identifier = service.journal.pending()[0].identifier
        venue.fill_at(identifier, 100.)
        reconcile_healthy(service, venue)
        before = service.journal.state()
        service.close()
        service = LiveService(tmp_path / 'live.sqlite', service.client,
                              engine=StrategyEngine(StrategyConfig(), ZERO_COST), clock=lambda: venue.now)
        result = service.process_completed_candle(snapshot, observation)
        after = service.journal.state()
        assert result.action == 'hold'
        assert after.risk == before.risk and after.completed_at == before.completed_at
        assert after.position == before.position
        assert len(venue.posts) == 1 and not service.journal.pending()
    finally:
        service.close()


def test_runbook_lock_example_needs_no_keys_or_network(capsys):
    with pytest.raises(guard.LiveTradingLockedError):
        guard.require_live_authorization()
    runbook = Path(__file__).parents[2] / 'docs/runbooks/locked-live.md'
    example = runbook.read_text(encoding='utf-8').split('```python\n', 1)[1].split('```', 1)[0]
    exec(compile(example, str(runbook), 'exec'), {})
    assert capsys.readouterr().out == 'Live trading is locked; paper and backtest remain available.\n'


@pytest.mark.parametrize('age, reason', [(59, None), (60, 'STAGNANT_EXIT'),
                                       (1094, None), (1095, 'MAX_HOLD_EXIT')])
def test_actual_live_holding_boundaries_keep_entry_age_zero_after_restart(monkeypatch, tmp_path, age, reason):
    service, venue, snapshot, observation = flat_live(monkeypatch, tmp_path, cash='1000000')
    seen = []
    engine = service.engine
    original = engine.decide

    def record(snapshot):
        decision = original(snapshot)
        seen.append((snapshot, decision))
        return decision

    engine.decide = record
    try:
        entry_row = dict(snapshot.row) | {'entry_high': 99.}
        service.process_completed_candle(replace(snapshot, row=entry_row), observation)
        identifier = service.journal.pending()[0].identifier
        venue.fill_at(identifier, 100.)
        reconcile_healthy(service, venue)
        actual_entry = service.journal.state().position.entry_at
        assert actual_entry == END + STEP + timedelta(minutes=1)
        for held in (0, 1, age):
            bar_at = END + (held + 1) * STEP
            venue.now = bar_at + STEP
            # +1R but not+2R disables stagnant exit for max-hold cases.
            price = 106. if age >= 1094 and held > 0 else 100.
            row = dict(snapshot.row) | {'timestamp': bar_at, 'open': price,
                                        'high': price + 1., 'low': price - 1., 'close': price}
            result = service.process_completed_candle(replace(snapshot, row=row),
                                                       replace(observation, now=venue.now))
            if held == 1:
                before = service.journal.state()
                service.close()
                service = LiveService(tmp_path / 'live.sqlite', service.client,
                                      engine=engine, clock=lambda: venue.now)
                assert service.journal.state() == before
        # Check action first so RED proves premature exits, not just counters.
        expected_action = ('hold', None) if reason is None else ('sell', reason)
        assert (result.action, result.reason) == expected_action
        assert [item[0].position.held_bars for item in seen[1:]] == [0, 1, age]
        assert service.journal.state().position.entry_at == actual_entry
        assert service.journal.state().position.completed_bar_at == END + (age + 1) * STEP
        assert len(venue.posts) == (1 if reason is None else 2)
    finally:
        service.close()

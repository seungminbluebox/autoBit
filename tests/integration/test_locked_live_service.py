from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import sqlite3

import httpx
import pytest

from autobit.config import CostConfig, StrategyConfig
from autobit.core import DecisionInput, RiskObservation, StrategyEngine
from autobit.risk.breakers import RiskDecision
from tests.unit.test_live_client import order


NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def modules():
    assert importlib.util.find_spec('autobit.live.service') is not None, 'live service missing'
    from autobit.live import client, guard, journal, service
    return client, guard, journal, service


def facts(at=NOW, close=10_000_000):
    row = dict(timestamp=at-timedelta(hours=4), open=10_000_000, high=10_100_000, low=9_900_000, close=close,
               ema_200=9_000_000, entry_high=9_950_000, previous_close=9_900_000,
               previous_entry_high=9_950_000, atr_14=100_000, baseline_atr_pct=.01,
               exit_low=9_000_000, warmup_complete=True, entry_data_valid=True)
    return DecisionInput(row, 100000, 100000, None, False, RiskDecision(.02,.7,None,())), RiskObservation(at,100000,(),1.,True,True)


class Venue:
    def __init__(self):
        self.now = NOW
        self.cash = '100000'
        self.btc = '0'
        self.requests = []
        self.orders = {}
        self.timeout = False
        self.not_found = False
        self.fail = False
        self.minimum = '5000'

    def accounts(self):
        return [dict(currency='KRW', balance=self.cash, locked='0'), dict(currency='BTC', balance=self.btc, locked='0')]

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503, json={'error':{'name':'offline'}})
        if request.url.path == '/v1/accounts':
            return httpx.Response(200,json=self.accounts())
        if request.url.path == '/v1/orders/chance':
            return httpx.Response(200,json=dict(bid_fee='.0005',ask_fee='.0005',
                market=dict(id='KRW-BTC',state='active',order_sides=['bid','ask'],bid_types=['price'],ask_types=['market'],
                            bid=dict(currency='KRW',min_total=self.minimum),ask=dict(currency='BTC',min_total=self.minimum),max_total='1000000000'),
                bid_account=self.accounts()[0],ask_account=self.accounts()[1]))
        if request.method == 'POST':
            data=json.loads(request.content)
            value=order(data['identifier'],data['side'],price=data.get('price'),volume=data.get('volume'),remaining_volume=data.get('volume'),created_at=self.now.replace(microsecond=0).isoformat())
            self.orders[data['identifier']]=value
            if self.timeout:
                raise httpx.ReadTimeout('offline',request=request)
            return httpx.Response(201,json=value)
        identifier=request.url.params['identifier']
        if self.not_found or identifier not in self.orders:
            return httpx.Response(404,json={'error':{'name':'order_not_found'}})
        if request.method == 'DELETE':
            self.orders[identifier]['state']='cancel'
        return httpx.Response(200,json=self.orders[identifier])

    def fill(self, identifier, *, quantity='.001', price='10000000', fee='5', terminal=True):
        value=self.orders[identifier]
        amount=Decimal(quantity)*Decimal(price)
        fill_at=self.now+timedelta(minutes=1)
        self.now=fill_at+timedelta(minutes=1)
        value.update(state='done' if terminal else 'wait',executed_volume=quantity,paid_fee=fee,trades_count=1,
                     trades=[dict(uuid='trade-'+identifier,market='KRW-BTC',side=value['side'],price=price,volume=quantity,funds=str(amount),created_at=fill_at.isoformat())])
        if value['side']=='bid':
            self.cash=str(Decimal('100000')-amount-Decimal(fee)); self.btc=quantity
        else:
            value['remaining_volume']=str(Decimal(value['volume'])-Decimal(quantity))
            self.cash=str(Decimal(self.cash)+amount-Decimal(fee)); self.btc=str(Decimal(self.btc)-Decimal(quantity))

    @property
    def posts(self):
        return [r for r in self.requests if r.method=='POST']


def make(monkeypatch,tmp_path,venue, *, patch=True):
    c,g,j,s=modules()
    if patch:
        monkeypatch.setattr(g,'require_live_authorization',lambda:None)
    engine=StrategyEngine(StrategyConfig(),CostConfig())
    original=engine.decide
    def decide(snapshot):
        value=original(snapshot)
        return replace(value,quantity=.001) if value.action=='buy' else value
    engine.decide=decide
    return s.LiveService(tmp_path/'live.sqlite', c.LiveClient(lambda:c.Credentials('fake-access','fake-secret'),transport=httpx.MockTransport(venue)),
                         engine=engine, clock=lambda:venue.now)


def test_locked_startup_never_creates_journal_or_touches_credentials(monkeypatch,tmp_path):
    c,g,j,s=modules()
    with pytest.raises(g.LiveTradingLockedError):
        make(monkeypatch,tmp_path,Venue(),patch=False)
    assert not list(tmp_path.iterdir())


def test_core_intent_real_fill_restart_and_seeded_money(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    decision=service.process_completed_candle(*facts())
    assert decision.action=='buy' and len(venue.posts)==1
    identifier=json.loads(venue.posts[0].content)['identifier']
    assert json.loads(venue.posts[0].content)['price']=='10000'
    assert service.journal.pending()[0].identifier==identifier
    assert service.journal.state().risk.initial_equity==100000
    venue.fill(identifier)
    service.reconcile()
    state=service.journal.state()
    assert state.cash==Decimal('89995') and state.position.quantity==Decimal('.001')
    assert state.position.entry_atr==100000 and state.position.current_stop==9750000
    service.close()
    resumed=make(monkeypatch,tmp_path,venue)
    resumed.reconcile()
    assert resumed.journal.state().position==state.position
    assert resumed.journal.state().cash==state.cash
    assert resumed.journal.state().risk==state.risk
    resumed.process_completed_candle(*facts())
    assert len(venue.posts)==1
    resumed.close()


@pytest.mark.parametrize('found',[True,False])
def test_timeout_or_not_found_never_resubmits_after_restart(monkeypatch,tmp_path,found):
    venue=Venue(); venue.timeout=True
    service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    assert len(service.journal.pending())==1
    service.close(); venue.not_found=not found
    resumed=make(monkeypatch,tmp_path,venue)
    resumed.process_completed_candle(*facts(NOW+timedelta(hours=4)))
    assert len(venue.posts)==1 and len(resumed.journal.pending())==1
    resumed.close()


def test_partial_buy_cancel_is_reconciled_once_and_never_oversells(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.fill(identifier,quantity='.0006',fee='3',terminal=False)
    service.reconcile(); service.reconcile()
    assert service.journal.state().position.quantity==Decimal('.0006')
    assert not service.journal.pending()
    service.on_price(9_750_000)
    assert Decimal(json.loads(venue.posts[-1].content)['volume'])==Decimal('0.0006')
    service.on_price(9_000_000)
    assert len(venue.posts)==2
    service.close()


def test_two_live_instances_cannot_own_same_journal(monkeypatch,tmp_path):
    venue=Venue(); first=make(monkeypatch,tmp_path,venue)
    *_,j,s=modules()
    with pytest.raises(j.LiveOwnershipError):
        make(monkeypatch,tmp_path,venue)
    first.close()
    second=make(monkeypatch,tmp_path,venue); second.close()


@pytest.mark.parametrize('cash,btc,minimum',[('100','0','5000'),('100000','.1','5000'),('100000','0','20000')])
def test_actual_small_cash_unmanaged_btc_and_venue_minimum_block_entry(monkeypatch,tmp_path,cash,btc,minimum):
    venue=Venue(); venue.cash=cash; venue.btc=btc; venue.minimum=minimum
    service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    assert not venue.posts
    assert service.journal.state().risk.initial_equity==float(Decimal(cash)+Decimal(btc)*10000000)
    if btc!='0':
        assert service.journal.state().position is None
    service.close()


def test_normalized_book_is_rejected_not_numeric_100(monkeypatch,tmp_path):
    c,g,j,s=modules(); monkeypatch.setattr(g,'require_live_authorization',lambda:None)
    with pytest.raises(ValueError):
        j.LiveJournal(tmp_path/'bad.sqlite',provenance='normalized100')


def test_cumulative_regression_blocks_and_preserves_last_fact(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.fill(identifier,terminal=False)
    # A cancel acknowledgement remains pending; regression is rejected on query.
    original=venue.__class__.__call__
    def no_cancel(self,r):
        if r.method=='DELETE':
            return httpx.Response(200,json=self.orders[r.url.params['identifier']])
        return original(self,r)
    monkeypatch.setattr(Venue,'__call__',no_cancel)
    service.reconcile()
    venue.orders[identifier]=order(identifier)
    service.reconcile()
    assert service.journal.state().position.quantity==Decimal('.001')
    assert len(service.journal.pending())==1
    assert not service.journal.state().health['schema_valid']
    service.close()


def test_crash_at_send_boundary_leaves_durable_intent_and_no_blind_retry(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    original=Venue.__call__
    def crash(self,request):
        if request.method=='POST':
            assert len(service.journal.pending())==1
            raise RuntimeError('simulated process death before acknowledgement')
        return original(self,request)
    monkeypatch.setattr(Venue,'__call__',crash)
    with pytest.raises(RuntimeError,match='process death'):
        service.process_completed_candle(*facts())
    service.close()
    monkeypatch.setattr(Venue,'__call__',original)
    resumed=make(monkeypatch,tmp_path,venue)
    resumed.reconcile()
    assert len(resumed.journal.pending())==1 and not venue.posts
    resumed.close()


def test_three_api_successes_do_not_clear_independent_balance_gate(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    snapshot,observation=facts()
    snapshot=replace(snapshot,row={**snapshot.row,'warmup_complete':False})
    service.process_completed_candle(snapshot,observation)
    venue.fail=True
    for minute in (3,4,5):
        service.clock=lambda m=minute:NOW+timedelta(minutes=m)
        service.reconcile()
    assert service.journal.state().health['api_failure_latched']
    service.close()
    venue.fail=False; venue.cash='99999'
    service=make(monkeypatch,tmp_path,venue)
    # First observation establishes the independent mismatch, resetting the
    # recovery count; three subsequent successes clear only the API latch.
    for minute in (6,7,8,9):
        service.clock=lambda m=minute:NOW+timedelta(minutes=m)
        service.reconcile()
    health=service.journal.state().health
    assert not health['api_failure_latched'] and not health['ledger_matches'] and health['halt_entries']
    venue.cash='100000'
    service.clock=lambda:NOW+timedelta(minutes=10)
    service.reconcile()
    assert service.journal.state().health['resume_reduced']
    service.clock=lambda:NOW+timedelta(hours=4,minutes=1)
    next_snapshot,next_observation=facts(NOW+timedelta(hours=4))
    next_snapshot=replace(next_snapshot,row={**next_snapshot.row,'warmup_complete':False})
    decision=service.process_completed_candle(next_snapshot,next_observation)
    assert decision.risk.risk_rate==.01 and decision.risk.exposure_cap==.35
    assert service.journal.state().risk.decision.risk_rate==.02
    service.process_completed_candle(next_snapshot,next_observation)
    assert service.journal.state().risk.decision.risk_rate==.02
    service.close()


def test_completed_low_never_fabricates_stop_fill_and_next_bar_stop_activates(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']; venue.fill(identifier)
    for minute in (3,4,5):
        service.clock=lambda m=minute:NOW+timedelta(minutes=m)
        service.reconcile()
    # Clear reduced progression with a completed healthy cycle first.
    service.clock=lambda:NOW+timedelta(hours=4,minutes=1)
    snapshot,observation=facts(NOW+timedelta(hours=4))
    snapshot=replace(snapshot,row={**snapshot.row,'low':9_000_000})
    original=service.engine.decide
    service.engine.decide=lambda s:replace(original(s),next_stop=9_900_000) if s.position else original(s)
    result=service.process_completed_candle(snapshot,observation)
    assert result.action=='hold' and len(venue.posts)==1
    state=service.journal.state()
    assert state.position.quantity==Decimal('.001')
    assert state.position.current_stop==9750000 and state.position.pending_stop==9900000
    assert state.position.pending_stop_at==NOW+timedelta(hours=4)
    service.on_price(9_800_000)
    assert len(venue.posts)==2
    service.close()


def test_unresolved_sell_freezes_cutoff_then_consumes_actual_trade_on_retry(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    venue.fill(json.loads(venue.posts[0].content)['identifier'])
    service.reconcile()
    service.on_price(9_750_000)
    sell=json.loads(venue.posts[-1].content)['identifier']
    service.clock=lambda:NOW+timedelta(hours=4,minutes=1)
    service.process_completed_candle(*facts(NOW+timedelta(hours=4)))
    assert service.journal.state().risk.last_risk_at==NOW
    # The genuine exit is discovered only after a later candle call.
    venue.fill(sell,price='9750000',fee='4.875')
    venue.orders[sell]['trades'][0]['created_at']='2026-09-05T01:00:00Z'
    service.process_completed_candle(*facts(NOW+timedelta(hours=4)))
    state=service.journal.state()
    assert state.risk.processed_trade_count==1 and state.position is None
    assert state.closed_trades[0].net_pnl==pytest.approx(-259.875)
    assert state.closed_trades[0].exit_time==NOW+timedelta(hours=1)
    service.close()


@pytest.mark.parametrize('change',[{'timestamp':NOW},{'timestamp':NOW-timedelta(hours=5)},{'low':11_000_000},{'close':float('nan')}])
def test_invalid_or_incomplete_candle_blocks_orders(monkeypatch,tmp_path,change):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    snapshot,observation=facts()
    *_,j,s=modules()
    with pytest.raises(j.LiveJournalError):
        service.process_completed_candle(replace(snapshot,row={**snapshot.row,**change}),observation)
    assert not venue.posts
    service.close()


def test_cannot_attribute_order_created_before_durable_intent(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.orders[identifier]['created_at']='2026-09-04T23:00:00Z'
    service.reconcile()
    assert not service.journal.state().health['schema_valid']
    assert len(service.journal.pending())==1
    service.close()


def test_entry_bar_high_before_actual_fill_does_not_raise_trailing_stop(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    venue.fill(json.loads(venue.posts[0].content)['identifier'])
    for minute in (3,4,5):
        service.clock=lambda m=minute:NOW+timedelta(minutes=m)
        service.reconcile()
    service.clock=lambda:NOW+timedelta(hours=4,minutes=1)
    snapshot,observation=facts(NOW+timedelta(hours=4))
    snapshot=replace(snapshot,row={**snapshot.row,'high':11_000_000})
    service.process_completed_candle(snapshot,observation)
    position=service.journal.state().position
    assert position.high_water==10000000
    assert position.pending_stop is None or position.pending_stop==9750000
    service.close()


@pytest.mark.parametrize('field,value',[('last_risk_at','2026-09-05T00:00:00'),('equity_peak',1),('processed_trade_count',-1)])
def test_restart_rejects_corrupt_common_risk_facts(monkeypatch,tmp_path,field,value):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts()); service.close()
    with sqlite3.connect(tmp_path/'live.sqlite') as db:
        payload=json.loads(db.execute('SELECT payload FROM live_state').fetchone()[0])
        payload['risk'][field]=value
        db.execute('UPDATE live_state SET payload=?',(json.dumps(payload),))
    *_,j,s=modules()
    with pytest.raises(j.LiveJournalError):
        make(monkeypatch,tmp_path,venue)


def test_restart_rejects_order_projection_hiding_pending_uncertainty(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts()); service.close()
    with sqlite3.connect(tmp_path/'live.sqlite') as db:
        db.execute('UPDATE live_intents SET terminal=1')
    *_,j,s=modules()
    with pytest.raises(j.LiveJournalError):
        make(monkeypatch,tmp_path,venue)


def test_locked_existing_service_methods_never_mutate_journal(monkeypatch,tmp_path):
    c,g,j,s=modules(); original=g.require_live_authorization
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    before=(tmp_path/'live.sqlite').read_bytes(); count=len(venue.requests)
    monkeypatch.setattr(g,'require_live_authorization',original)
    for call in [service.reconcile,lambda:service.on_price(1.),lambda:service.process_completed_candle(*facts()),
                 lambda:service.journal.save(service.journal.state())]:
        with pytest.raises(g.LiveTradingLockedError):call()
    assert before==(tmp_path/'live.sqlite').read_bytes() and len(venue.requests)==count
    assert b'fake-access' not in before and b'fake-secret' not in before
    service.close()


def test_zero_fill_cancel_keeps_cash_and_creates_no_position(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.orders[identifier]['state']='cancel'
    service.reconcile(); service.reconcile()
    assert service.journal.state().cash==Decimal('100000') and service.journal.state().position is None
    assert not service.journal.pending()
    service.close()


def test_same_candle_updates_health_overlay_without_reapplying_canonical_risk(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    snapshot,observation=facts()
    snapshot=replace(snapshot,row={**snapshot.row,'warmup_complete':False})
    service.process_completed_candle(snapshot,observation)
    risk=service.journal.state().risk
    venue.cash='99999'
    decision=service.process_completed_candle(snapshot,observation)
    assert decision.risk.risk_rate==0 and 'system_unhealthy' in decision.risk.reasons
    assert service.journal.state().risk==risk and not venue.posts
    service.close()


def test_nonfinite_indicator_is_a_durable_schema_gate_not_an_uncaught_json_error(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    snapshot,observation=facts()
    snapshot=replace(snapshot,row={**snapshot.row,'atr_14':float('nan')})
    result=service.process_completed_candle(snapshot,observation)
    assert result.action=='hold' and not venue.posts
    assert not service.journal.state().health['schema_valid']
    service.close()


def test_invalid_current_price_persists_health_and_valid_price_records_actual_high(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts()); venue.fill(json.loads(venue.posts[0].content)['identifier'])
    service.reconcile()
    service.on_price(10_500_000)
    assert service.journal.state().position.high_water==10500000
    service.on_price(float('nan'))
    assert not service.journal.state().health['schema_valid']
    assert len(venue.posts)==1
    service.close()


def test_second_resolution_order_time_accepts_fractional_intent_and_preserves_fill_time(monkeypatch,tmp_path):
    venue=Venue(); venue.now=NOW+timedelta(microseconds=500000)
    service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    assert service.journal.pending()[0].created_at==NOW+timedelta(microseconds=500000)
    # Upbit order creation is second-resolution and may use its +09:00 offset.
    venue.orders[identifier]['created_at']='2026-09-05T09:00:00+09:00'
    service.reconcile()
    assert service.journal.state().health['schema_valid']
    venue.fill(identifier)
    venue.orders[identifier]['trades'][0]['created_at']='2026-09-05T09:00:00.750000+09:00'
    service.reconcile()
    position=service.journal.state().position
    assert position.quantity==Decimal('.001') and position.current_stop==9750000
    assert position.entry_at==NOW+timedelta(microseconds=750000)
    assert not service.journal.pending() and len(venue.posts)==1
    service.close()


def test_second_resolution_order_time_still_rejects_the_previous_second(monkeypatch,tmp_path):
    venue=Venue(); venue.now=NOW+timedelta(microseconds=500000)
    service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.orders[identifier]['created_at']='2026-09-05T08:59:59+09:00'
    service.reconcile()
    assert not service.journal.state().health['schema_valid']
    assert len(service.journal.pending())==1
    service.close()


def test_pending_partial_keeps_bar_facts_across_restart_and_real_trailing_recovery(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    identifier=json.loads(venue.posts[0].content)['identifier']
    venue.fill(identifier,quantity='.0006',fee='3',terminal=False)
    original=Venue.__call__
    def uncertain_cancel(self,request):
        if request.method=='DELETE':
            return httpx.Response(200,json=self.orders[request.url.params['identifier']])
        return original(self,request)
    monkeypatch.setattr(Venue,'__call__',uncertain_cancel)
    service.reconcile()
    frozen_risk=service.journal.state().risk
    frozen_cursor=service.journal.state().completed_at
    service.clock=lambda:NOW+timedelta(hours=4,minutes=1)
    entry_bar=facts(NOW+timedelta(hours=4))
    service.process_completed_candle(*entry_bar)
    service.clock=lambda:NOW+timedelta(hours=8,minutes=1)
    high_snapshot,high_observation=facts(NOW+timedelta(hours=8))
    high_snapshot=replace(high_snapshot,row={**high_snapshot.row,'high':11_000_000})
    service.process_completed_candle(high_snapshot,high_observation)
    saved=service.journal.state()
    assert saved.position.high_water==11000000 and saved.position.held_bars==1
    assert saved.position.completed_bar_at==NOW+timedelta(hours=4)
    assert saved.position.completed_bar_fingerprint
    assert saved.risk==frozen_risk and saved.completed_at==frozen_cursor
    assert len(service.journal.pending())==1
    service.close()
    service=make(monkeypatch,tmp_path,venue)
    service.clock=lambda:NOW+timedelta(hours=8,minutes=2)
    # Older bars cannot move age/protection backward or apply the later high to
    # an earlier policy bar. Changed same-bar OHLC cannot replace saved facts.
    service.process_completed_candle(*entry_bar)
    assert not service.journal.state().health['timestamps_monotonic']
    changed=replace(high_snapshot,row={**high_snapshot.row,'high':12_000_000})
    service.process_completed_candle(changed,high_observation)
    assert not service.journal.state().health['schema_valid']
    assert service.journal.state().position==saved.position
    service.process_completed_candle(high_snapshot,high_observation)
    assert service.journal.state().position==saved.position
    assert service.journal.state().risk==frozen_risk
    assert service.journal.state().completed_at==frozen_cursor

    venue.orders[identifier]['state']='cancel'
    for minute in (3,4,5):
        service.clock=lambda m=minute:NOW+timedelta(hours=8,minutes=m)
        service.reconcile()
    assert not service.journal.pending()
    service.clock=lambda:NOW+timedelta(hours=12,minutes=1)
    later,cutoff=facts(NOW+timedelta(hours=12),close=10_800_000)
    later=replace(later,row={**later.row,'open':10_800_000,'high':10_900_000,'low':10_700_000})
    decision=service.process_completed_candle(later,cutoff)
    # Real shared policy: entry 10m, R=.25m, high 11m, ATR .1m;
    # +2R is attained and high-3*ATR is 10.7m, above entry/old stop.
    assert decision.action=='hold' and decision.next_stop==10700000
    recovered=service.journal.state()
    assert recovered.position.high_water==11000000
    assert recovered.position.pending_stop==10700000
    assert recovered.position.current_stop==9750000
    assert recovered.position.completed_bar_at==NOW+timedelta(hours=8)
    assert recovered.risk.last_risk_at==NOW+timedelta(hours=12)
    assert len(venue.posts)==1
    service.close()


@pytest.mark.parametrize('changes', [
    {'completed_bar_at': '2026-09-05T00:00:00', 'completed_bar_fingerprint': 'a'*64},
    {'completed_bar_at': '2026-09-05T00:00:00+00:00', 'completed_bar_fingerprint': None},
    {'completed_bar_at': '2026-09-05T00:00:00+00:00', 'completed_bar_fingerprint': 'invalid'},
])
def test_restart_rejects_invalid_independent_position_candle_provenance(monkeypatch,tmp_path,changes):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    venue.fill(json.loads(venue.posts[0].content)['identifier'])
    service.reconcile(); service.close()
    with sqlite3.connect(tmp_path/'live.sqlite') as db:
        payload=json.loads(db.execute('SELECT payload FROM live_state').fetchone()[0])
        payload['position'].update(changes)
        db.execute('UPDATE live_state SET payload=?',(json.dumps(payload),))
    *_,j,s=modules()
    with pytest.raises(j.LiveJournalError):
        make(monkeypatch,tmp_path,venue)


def test_older_position_payload_defaults_missing_candle_provenance_without_reset(monkeypatch,tmp_path):
    venue=Venue(); service=make(monkeypatch,tmp_path,venue)
    service.process_completed_candle(*facts())
    venue.fill(json.loads(venue.posts[0].content)['identifier'])
    service.reconcile()
    saved=service.journal.state()
    service.close()
    with sqlite3.connect(tmp_path/'live.sqlite') as db:
        payload=json.loads(db.execute('SELECT payload FROM live_state').fetchone()[0])
        del payload['position']['completed_bar_at']
        del payload['position']['completed_bar_fingerprint']
        db.execute('UPDATE live_state SET payload=?',(json.dumps(payload),))
    service=make(monkeypatch,tmp_path,venue)
    assert service.journal.state()==saved
    assert service.journal.state().position.completed_bar_at is None
    assert service.journal.state().position.completed_bar_fingerprint is None
    service.close()


class CumulativeVenue(Venue):
    """Cancellation acknowledgement leaves cumulative details unresolved."""

    def __call__(self, request):
        if request.method == 'DELETE':
            self.requests.append(request)
            return httpx.Response(200, json=self.orders[request.url.params['identifier']])
        return super().__call__(request)

    def cumulative_buy(self, identifier, times, *, terminal, reverse=False):
        trades = [dict(uuid=f'fill-{n}', market='KRW-BTC', side='bid', price='10000000',
                       volume='.0005', funds='5000', created_at=at.isoformat())
                  for n, at in enumerate(times)]
        if reverse:
            trades.reverse()
        quantity = Decimal('.0005') * len(times)
        fee = Decimal('2.5') * len(times)
        self.orders[identifier].update(state='done' if terminal else 'wait',
            executed_volume=str(quantity), paid_fee=str(fee), trades_count=len(trades), trades=trades)
        self.cash = str(Decimal('100000') - quantity * Decimal('10000000') - fee)
        self.btc = str(quantity)
        self.now = max(times) + timedelta(microseconds=1)


@pytest.mark.parametrize('staged', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('before,after,reason,high', [
    (59, 60, 'STAGNANT_EXIT', 10_100_000),
    (1094, 1095, 'MAX_HOLD_EXIT', 10_300_000),
])
def test_cumulative_first_entry_polling_order_restart_and_holding_boundaries(
        monkeypatch, tmp_path, staged, reverse, before, after, reason, high):
    venue = CumulativeVenue()
    service = make(monkeypatch, tmp_path, venue)
    first = NOW + timedelta(hours=3, minutes=59, seconds=59, microseconds=900000)
    last = NOW + timedelta(hours=4, microseconds=100000)
    try:
        service.process_completed_candle(*facts())
        identifier = service.journal.pending()[0].identifier
        if staged:
            venue.cumulative_buy(identifier, (first,), terminal=False)
            service.reconcile()
            partial = service.journal.state().position
            assert partial.entry_at == first and partial.quantity == Decimal('.0005')
            assert service.journal.pending()
            service.close()
            service = make(monkeypatch, tmp_path, venue)
            assert service.journal.state().position == partial
        venue.cumulative_buy(identifier, (first, last), terminal=True, reverse=reverse)
        for _ in range(3):
            venue.now += timedelta(seconds=1)
            service.reconcile()
        position = service.journal.state().position
        assert position.entry_at == first
        assert (position.entry_atr, position.initial_atr_mult) == (100000., 2.5)
        assert (position.quantity, position.cost_basis) == (Decimal('.001'), Decimal('10005'))
        assert service.journal.state().cash == Decimal('89995')
        assert not service.journal.pending()
        saved = service.journal.state()
        service.close()
        service = make(monkeypatch, tmp_path, venue)
        assert service.journal.state() == saved
        for age in (1, before, after):
            venue.now = NOW + timedelta(hours=4 * (age + 1))
            snapshot, observation = facts(venue.now)
            snapshot = replace(snapshot, row={**snapshot.row, 'high': high})
            decision = service.process_completed_candle(snapshot, observation)
            assert service.journal.state().position.held_bars == age
            assert service.journal.state().position.entry_at == first
            expected = ('sell', reason) if age == after else ('hold', None)
            assert (decision.action, decision.reason) == expected
        assert len(venue.posts) == 2
    finally:
        service.close()


@pytest.mark.parametrize('entry_offset', [timedelta(0), timedelta(minutes=1)])
@pytest.mark.parametrize('close', [11_000_000, 5_000_000])
@pytest.mark.parametrize('reconciled_first', [False, True])
def test_pre_entry_candle_defers_without_owned_price_risk_or_cursor_progress_and_recovers(
        monkeypatch, tmp_path, entry_offset, close, reconciled_first):
    venue = Venue()
    service = make(monkeypatch, tmp_path, venue)
    end = NOW + timedelta(hours=4)
    try:
        service.process_completed_candle(*facts())
        baseline = service.journal.state()
        identifier = service.journal.pending()[0].identifier
        venue.fill(identifier)
        entry = end + entry_offset
        venue.orders[identifier]['trades'][0]['created_at'] = entry.isoformat()
        venue.now = end + timedelta(minutes=2)
        if reconciled_first:
            for _ in range(3):
                service.reconcile()
                venue.now += timedelta(seconds=1)
        snapshot, observation = facts(end, close=close)
        snapshot = replace(snapshot, row={**snapshot.row, 'high': 12_000_000,
                                         'low': 4_000_000, 'exit_low': 6_000_000})
        observation = replace(observation, now=venue.now)
        # False discovers entry inside this call; True exercises healthy policy.
        for attempt in range(3):
            decision = service.process_completed_candle(snapshot, observation)
            assert (decision.action, decision.reason, decision.next_stop) == (
                'hold', 'DEFERRED_PRE_ENTRY_CANDLE', None)
            state = service.journal.state()
            assert state.position.entry_at == entry
            assert state.position.high_water == 10_000_000
            assert state.position.current_stop == 9_750_000
            assert state.position.pending_stop is None and state.position.held_bars == 0
            assert state.position.completed_bar_at is None
            assert state.position.completed_bar_fingerprint is None
            assert state.risk == baseline.risk
            assert (state.completed_at, state.candle_fingerprint, state.decision) == (
                baseline.completed_at, baseline.candle_fingerprint, baseline.decision)
            assert state.cash == Decimal('89995') and state.closed_trades == ()
            assert not service.journal.pending() and len(venue.posts) == 1
            assert state.health['schema_valid'] and state.health['timestamps_monotonic']
            if attempt == 0:
                service.close()
                service = make(monkeypatch, tmp_path, venue)
                assert service.journal.state() == state
            venue.now += timedelta(seconds=1)
            observation = replace(observation, now=venue.now)
        # A genuine observed high survives deferred retries; there is no candle
        # provenance yet and no pending-stop promotion caused by pre-entry OHLC.
        service.on_price(10_200_000)
        observed = service.journal.state().position
        service.process_completed_candle(snapshot, replace(observation, now=venue.now))
        assert service.journal.state().position == observed
        venue.now = end + timedelta(hours=4)
        eligible, cutoff = facts(venue.now)
        # For an interior entry, the high is unknown-before-entry; close is safe.
        eligible = replace(eligible, row={**eligible.row, 'high': 11_000_000, 'close': 10_400_000})
        result = service.process_completed_candle(eligible, cutoff)
        assert result.action == 'hold' and result.reason is None
        progressed = service.journal.state()
        assert progressed.position.high_water == (11_000_000 if entry_offset == timedelta(0) else 10_400_000)
        assert progressed.position.pending_stop == (10_700_000 if entry_offset == timedelta(0) else 9_750_000)
        assert progressed.position.completed_bar_at == end
        assert progressed.risk.last_risk_at == venue.now
        assert progressed.completed_at == end
        assert len(venue.posts) == 1
        # Deferral does not disable independently observed-price protection.
        service.on_price(9_750_000)
        assert len(venue.posts) == 2
        assert json.loads(venue.posts[-1].content)['side'] == 'ask'
    finally:
        service.close()


@pytest.mark.parametrize('first', [None, NOW.replace(tzinfo=None),
                                  NOW - timedelta(seconds=1), NOW + timedelta(hours=1)])
def test_buy_consumer_rejects_missing_or_contradictory_first_fill_without_mutation(monkeypatch, tmp_path, first):
    venue = Venue()
    service = make(monkeypatch, tmp_path, venue)
    try:
        service.process_completed_candle(*facts())
        intent = service.journal.pending()[0]
        venue.fill(intent.identifier)
        actual = service.client.get_order(intent.identifier)
        before = service.journal.state()
        c, *_ = modules()
        with pytest.raises(c.LiveResponseError):
            service._apply_order(intent, replace(actual, first_fill_at=first), venue.now)
        assert service.journal.state() == before
        assert service.journal.pending() == (intent,)
    finally:
        service.close()


@pytest.mark.parametrize('reverse', [False, True])
def test_completed_cumulative_sell_uses_latest_execution_not_first_after_restart(monkeypatch, tmp_path, reverse):
    venue = Venue()
    service = make(monkeypatch, tmp_path, venue)
    try:
        service.process_completed_candle(*facts())
        venue.fill(service.journal.pending()[0].identifier)
        service.reconcile()
        service.on_price(9_750_000)
        intent = service.journal.pending()[0]
        first = NOW + timedelta(hours=1, minutes=59, seconds=59, microseconds=900000)
        last = NOW + timedelta(hours=2, microseconds=100000)
        trades = [dict(uuid=f'sell-{n}', market='KRW-BTC', side='ask', price='9750000',
                       volume='.0005', funds='4875', created_at=at.isoformat())
                  for n, at in enumerate((first, last))]
        if reverse:
            trades.reverse()
        venue.orders[intent.identifier].update(state='done', executed_volume='.001', remaining_volume='0',
            paid_fee='4.875', trades_count=2, trades=trades)
        venue.cash = '99740.125'
        venue.btc = '0'
        venue.now = last + timedelta(seconds=1)
        service.close()
        service = make(monkeypatch, tmp_path, venue)
        service.reconcile()
        state = service.journal.state()
        assert state.position is None and not service.journal.pending()
        assert state.closed_trades[0].exit_time == last
        assert state.closed_trades[0].net_pnl == -259.875
        assert state.cash == Decimal('99740.125')
        service.close()
        service = make(monkeypatch, tmp_path, venue)
        assert service.journal.state() == state
    finally:
        service.close()


def test_pre_entry_deferral_preserves_consumed_candle_fingerprint_rejection(monkeypatch, tmp_path):
    venue = Venue()
    service = make(monkeypatch, tmp_path, venue)
    try:
        snapshot, observation = facts()
        service.process_completed_candle(snapshot, observation)
        baseline = service.journal.state()
        venue.fill(service.journal.pending()[0].identifier)
        changed = replace(snapshot, row={**snapshot.row, 'high': 11_000_000})
        service.process_completed_candle(changed, replace(observation, now=venue.now))
        state = service.journal.state()
        assert not state.health['schema_valid']
        assert state.risk == baseline.risk and state.completed_at == baseline.completed_at
        assert state.candle_fingerprint == baseline.candle_fingerprint
        assert state.position.high_water == 10_000_000
        assert state.position.completed_bar_at is None
        assert len(venue.posts) == 1
    finally:
        service.close()


def test_pre_entry_deferral_preserves_independent_position_provenance_rejection(monkeypatch, tmp_path):
    venue = CumulativeVenue()
    service = make(monkeypatch, tmp_path, venue)
    try:
        service.process_completed_candle(*facts())
        identifier = service.journal.pending()[0].identifier
        venue.cumulative_buy(identifier, (NOW + timedelta(hours=4, minutes=1),), terminal=False)
        venue.now = NOW + timedelta(hours=8, minutes=2)
        service.process_completed_candle(*facts(NOW + timedelta(hours=8)))
        before = service.journal.state()
        assert before.position.completed_bar_at == NOW + timedelta(hours=4)
        assert before.completed_at == NOW - timedelta(hours=4)
        assert service.journal.pending()
        earlier, observation = facts(NOW + timedelta(hours=4))
        service.process_completed_candle(earlier, replace(observation, now=venue.now))
        state = service.journal.state()
        assert not state.health['timestamps_monotonic']
        assert state.position == before.position
        assert state.risk == before.risk and state.completed_at == before.completed_at
        assert len(service.journal.pending()) == 1 and len(venue.posts) == 1
    finally:
        service.close()


def test_pre_entry_deferral_keeps_observed_stop_protection_before_next_eligible_candle(monkeypatch, tmp_path):
    venue = Venue()
    service = make(monkeypatch, tmp_path, venue)
    try:
        service.process_completed_candle(*facts())
        venue.fill(service.journal.pending()[0].identifier)
        snapshot, observation = facts()
        decision = service.process_completed_candle(snapshot, replace(observation, now=venue.now))
        assert decision.reason == 'DEFERRED_PRE_ENTRY_CANDLE'
        before = service.journal.state()
        service.on_price(9_750_000)
        assert len(venue.posts) == 2
        assert json.loads(venue.posts[-1].content)['side'] == 'ask'
        assert service.journal.pending()[0].amount == before.position.quantity
        assert service.journal.state().risk == before.risk
        assert service.journal.state().completed_at == before.completed_at
        service.on_price(9_750_000)
        assert len(venue.posts) == 2
    finally:
        service.close()

"""Common-policy live adapter using only actual returned/reconciled venue facts.

This is not a daemon or a native exchange stop. A caller must supply verified
completed candles and timely observed prices. All entry points remain locked.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.core import (ClosedTradeObservation, Decision, DecisionInput, PositionContext,
                          RiskObservation, RiskState, StrategyEngine, advance_risk_state, apply_health_recovery)
from autobit.live import guard
from autobit.live.client import Account, LiveClient, LiveRequestError, LiveResponseError, VenueOrder
from autobit.live.journal import LiveIntent, LiveJournal, LiveJournalError, LivePosition, LiveState
from autobit.paper.health import HealthMonitor, HealthSnapshot
from autobit.strategy.donchian_trend import initial_stop_price


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _positive(value: object) -> bool:
    return type(value) in (float,int) and math.isfinite(value) and value > 0


def _balances(accounts: tuple[Account, ...]) -> tuple[Account, Account]:
    mapping = {a.currency:a for a in accounts}
    return mapping.get('KRW',Account('KRW',Decimal(0),Decimal(0))), mapping.get('BTC',Account('BTC',Decimal(0),Decimal(0)))


def _context(position: LivePosition | None) -> PositionContext | None:
    if position is None:
        return None
    return PositionContext(position.entry_price,position.initial_stop,position.current_stop,
                           position.high_water,float(position.quantity),position.held_bars)


class LiveService:
    def __init__(self, journal_path: str | Path, client: LiveClient, *, engine: StrategyEngine | None = None,
                 risk_config: RiskConfig = RiskConfig(), clock: Callable[[],datetime] = _now) -> None:
        guard.require_live_authorization()
        self.client = client
        self.engine = engine or StrategyEngine(StrategyConfig(),CostConfig())
        self.risk_config = risk_config
        self.clock = clock
        self.journal = LiveJournal(journal_path)

    def close(self) -> None:
        self.journal.close()

    def _health(self) -> HealthMonitor:
        state = self.journal.state()
        return HealthMonitor.from_mapping(state.health) if state else HealthMonitor()

    def _persist_health(self, health: HealthMonitor) -> None:
        state = self.journal.state()
        if state:
            self.journal.save(replace(state,health=health.snapshot().to_mapping()))

    def _failure(self, error: LiveRequestError, at: datetime) -> None:
        health = self._health()
        health.record_api_failure(at)
        if isinstance(error,LiveResponseError):
            health.record_schema_check(False,at)
        health.set_unresolved_orders(len(self.journal.pending()),at)
        self._persist_health(health)

    def reconcile(self) -> None:
        guard.require_live_authorization()
        self._reconcile(self.clock())

    def _reconcile(self, at: datetime) -> tuple[Account,Account] | None:
        guard.require_live_authorization()
        if not _aware(at):
            raise ValueError('Live clock must be timezone aware')
        try:
            for intent in self.journal.pending():
                actual = self.client.get_order(intent.identifier)
                self._apply_order(intent,actual,at)
                if not actual.terminal and actual.side == 'bid' and actual.executed_volume > 0:
                    updated = self.journal.pending()[0]
                    # Cancellation has its own durable uncertainty. Never assume
                    # its acknowledgement has cancelled or filled the remainder.
                    self.journal.save(self.journal.state(),replace(updated,cancel_requested=True))
                    self.client.cancel_order(intent.identifier)
                    self._apply_order(self.journal.pending()[0],self.client.get_order(intent.identifier),at)
            balances = _balances(self.client.accounts())
            health = self._health()
            health.record_api_success(at)
            health.set_unresolved_orders(len(self.journal.pending()),at)
            state = self.journal.state()
            if state:
                krw,btc = balances
                health.record_ledger_check(stored_cash=float(state.cash),actual_cash=float(krw.balance+krw.locked),
                    stored_btc=float(state.position.quantity) if state.position else 0.,actual_btc=float(btc.balance+btc.locked),at=at)
                self._persist_health(health)
            return balances
        except LiveRequestError as error:
            self._failure(error,at)
            return None

    def _apply_order(self, intent: LiveIntent, actual: VenueOrder, at: datetime) -> None:
        state = self.journal.state()
        if state is None:
            raise LiveJournalError('Intent has no live state')
        if (actual.side != intent.side or actual.identifier != intent.identifier
                # Venue order creation is documented to whole seconds; its
                # represented interval may include a fractional local intent.
                # Trade timestamps remain exact and are never rounded here.
                or actual.created_at < intent.created_at.replace(microsecond=0) or actual.created_at > at
                or (intent.venue_uuid and actual.uuid != intent.venue_uuid)
                or actual.executed_funds is None or actual.executed_volume < intent.quantity
                or actual.executed_funds < intent.funds or actual.paid_fee < intent.fee
                or (actual.side == 'bid' and actual.price != intent.amount)
                or (actual.side == 'ask' and actual.volume != intent.amount)):
            raise LiveResponseError('Contradictory cumulative order facts')
        quantity = actual.executed_volume-intent.quantity
        funds = actual.executed_funds-intent.funds
        fee = actual.paid_fee-intent.fee
        if (quantity == 0 and funds != 0) or (quantity > 0 and (funds <= 0 or actual.last_fill_at is None)):
            raise LiveResponseError('Contradictory incremental fills')
        if actual.last_fill_at and actual.last_fill_at > at:
            raise LiveResponseError('Future execution fact')
        position,cash,trades = state.position,state.cash,state.closed_trades
        health = self._health()
        if quantity > 0:
            health.record_fill_check(expected_price=intent.reference_price,actual_price=float(funds/quantity),at=at)
        if actual.side == 'bid':
            if actual.executed_funds > intent.amount:
                raise LiveResponseError('Buy exceeds durable budget')
            cash -= funds+fee
            if quantity > 0:
                if position is None:
                    entry = float(funds/quantity)
                    stop = initial_stop_price(entry,intent.entry_atr,intent.initial_atr_mult)
                    if not _positive(stop):
                        raise LiveResponseError('Actual entry has invalid protection')
                    position = LivePosition(quantity,entry,intent.entry_atr,intent.initial_atr_mult,actual.last_fill_at,
                                            stop,stop,entry,0,funds+fee)
                else:
                    entry = float(actual.executed_funds/actual.executed_volume)
                    stop = initial_stop_price(entry,position.entry_atr,position.initial_atr_mult)
                    position = replace(position,quantity=position.quantity+quantity,entry_price=entry,
                                       initial_stop=stop,current_stop=max(position.current_stop,stop),
                                       high_water=max(position.high_water,entry),cost_basis=position.cost_basis+funds+fee)
            elif fee and position:
                position = replace(position,cost_basis=position.cost_basis+fee)
        else:
            if quantity > 0:
                if position is None or quantity > position.quantity or actual.executed_volume > intent.amount:
                    raise LiveResponseError('Sell exceeds strategy-owned BTC')
                basis = position.cost_basis*quantity/position.quantity
                pnl = position.realized_pnl+funds-fee-basis
                remaining = position.quantity-quantity
                if remaining == 0:
                    trades += (ClosedTradeObservation(float(pnl),actual.last_fill_at),)
                    position = None
                else:
                    position = replace(position,quantity=remaining,cost_basis=position.cost_basis-basis,realized_pnl=pnl)
            elif fee:
                if position:
                    position = replace(position,realized_pnl=position.realized_pnl-fee)
                elif trades:
                    trades = trades[:-1]+(replace(trades[-1],net_pnl=trades[-1].net_pnl-float(fee)),)
            cash += funds-fee
        if cash < 0:
            raise LiveResponseError('Execution exceeds journal cash')
        updated = replace(intent,quantity=actual.executed_volume,funds=actual.executed_funds,fee=actual.paid_fee,
                          venue_uuid=actual.uuid,terminal=actual.terminal)
        self.journal.save(replace(state,cash=cash,position=position,closed_trades=trades,health=health.snapshot().to_mapping()),updated)

    def process_completed_candle(self, snapshot: DecisionInput, observation: RiskObservation) -> Decision:
        guard.require_live_authorization()
        at = self.clock()
        if not _aware(at) or not _aware(observation.now):
            raise ValueError('Live timestamps must be timezone aware')
        row = snapshot.row
        bar_at = row.get('timestamp')
        end = bar_at + timedelta(hours=4) if _aware(bar_at) else None
        valid = (_aware(bar_at) and bar_at.utcoffset() == timedelta(0)
                 and bar_at.hour % 4 == 0 and bar_at.minute == bar_at.second == bar_at.microsecond == 0
                 and end <= observation.now <= at
                 and all(_positive(row.get(k)) for k in ('open','high','low','close'))
                 and row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high'])
        balances = self._reconcile(at)
        state = self.journal.state()
        if state is None:
            if balances is None or not valid:
                raise LiveJournalError('Cannot initialize without verified balances and candle price')
            krw,btc = balances
            equity = float(krw.balance+krw.locked+(btc.balance+btc.locked)*Decimal(str(row['close'])))
            if equity <= 0:
                raise LiveJournalError('No positive actual equity to initialize')
            risk = RiskState(initial_equity=equity,equity_peak=equity,daily_baseline_equity=equity,last_equity=equity)
            state = LiveState(krw.balance+krw.locked,None,risk,HealthSnapshot().to_mapping())
            self.journal.save(state)
        health = self._health()
        health.record_schema_check(bool(valid),at)
        monotonic = valid and (state.completed_at is None or bar_at >= state.completed_at)
        health.record_timestamp_check(bool(monotonic),at)
        expected_end = at.replace(minute=0,second=0,microsecond=0)-timedelta(hours=at.hour%4)
        health.record_candle_check(expected_end=expected_end,observed_end=end if _aware(end) else None,checked_at=at,structurally_valid=bool(valid))
        if balances:
            krw,btc = balances
            health.record_ledger_check(stored_cash=float(state.cash),actual_cash=float(krw.balance+krw.locked),
                stored_btc=float(state.position.quantity) if state.position else 0.,actual_btc=float(btc.balance+btc.locked),at=at)
        if balances is None:
            # Even before the three-failure API latch, no new exposure can use
            # missing current balance evidence.
            health.record_schema_check(False,at)
        try:
            fingerprint = hashlib.sha256(json.dumps(dict(row),default=str,sort_keys=True,allow_nan=False).encode()).hexdigest() if valid else None
        except (ValueError,TypeError):
            valid = False
            fingerprint = None
            health.record_schema_check(False,at)
        if state.completed_at == bar_at and state.decision:
            if fingerprint != state.candle_fingerprint:
                health.record_schema_check(False,at)
            self._persist_health(health)
            action = health.current_action()
            overlay = apply_health_recovery(state.risk.decision,
                system_healthy=observation.system_healthy and not action.halt_entries,
                resume_reduced=action.resume_reduced)
            # Replay reconciles and reports current health, but cannot reissue
            # the consumed candle's entry/discretionary order or advance risk.
            return self.engine.decide(DecisionInput(row,float(state.cash),state.risk.last_equity,
                                      _context(state.position),True,overlay))
        self._persist_health(health)
        state = self.journal.state()
        if not valid or not monotonic:
            risk = apply_health_recovery(state.risk.decision,system_healthy=False,resume_reduced=False)
            return self.engine.decide(DecisionInput(row,float(state.cash),state.risk.last_equity,_context(state.position),bool(self.journal.pending()),risk))
        if state.position and state.position.completed_bar_at is not None:
            older = bar_at < state.position.completed_bar_at
            changed = (bar_at == state.position.completed_bar_at
                       and fingerprint != state.position.completed_bar_fingerprint)
            if older or changed:
                if older:
                    health.record_timestamp_check(False,at)
                if changed:
                    health.record_schema_check(False,at)
                self._persist_health(health)
                overlay = apply_health_recovery(state.risk.decision,system_healthy=False,resume_reduced=False)
                return self.engine.decide(DecisionInput(row,float(state.cash),state.risk.last_equity,
                                          _context(state.position),True,overlay))
        position = self._activate(state.position,end)
        if position:
            age = max(0,int(end.timestamp()//14400-position.entry_at.timestamp()//14400))
            # Candle extremes have no intra-bar timestamps. In the entry bar,
            # only its post-entry close is evidence of an attained price.
            if position.entry_at > bar_at:
                row = {**row,'high':max(position.high_water,float(row['close']))}
            position = replace(position,held_bars=max(position.held_bars,age),
                               high_water=max(position.high_water,float(row['high'])),
                               completed_bar_at=bar_at,completed_bar_fingerprint=fingerprint)
            # Completed position facts/protection progress are independently
            # durable even while an uncertain exit freezes canonical risk.
            # The separate provenance prevents replay against an older bar.
            state = replace(state,position=position)
            self.journal.save(state)
        equity = float(state.cash+(position.quantity if position else Decimal(0))*Decimal(str(row['close'])))
        if balances:
            equity = float(balances[0].balance+balances[0].locked+(balances[1].balance+balances[1].locked)*Decimal(str(row['close'])))
        action = health.current_action()
        healthy = observation.system_healthy and not action.halt_entries and balances is not None
        cash_available = min(state.cash,balances[0].balance) if balances else Decimal(0)
        # Canonical risk is accumulated once, then health reduction is applied
        # only to that base, never to yesterday's already-reduced decision.
        if self.journal.pending():
            # Do not advance the trade-history cutoff past an uncertain exit.
            # Otherwise a later discovered factual fill could precede the last
            # risk cutoff and could never be honestly consumed by the reducer.
            overlay = apply_health_recovery(state.risk.decision,system_healthy=False,resume_reduced=False)
            return self.engine.decide(DecisionInput(row,float(cash_available) if balances else 0.,equity,_context(position),True,overlay))
        try:
            risk = advance_risk_state(state.risk,replace(observation,equity=equity,closed_trades=state.closed_trades,
                                     system_healthy=healthy),config=self.risk_config)
        except ValueError:
            health.record_schema_check(False,at)
            self._persist_health(health)
            overlay = apply_health_recovery(state.risk.decision,system_healthy=False,resume_reduced=False)
            return self.engine.decide(DecisionInput(row,0.,equity,_context(position),False,overlay))
        overlay = apply_health_recovery(risk.decision,system_healthy=healthy,resume_reduced=action.resume_reduced)
        cash_available = min(state.cash,balances[0].balance) if balances else Decimal(0)
        decision = self.engine.decide(DecisionInput(row,float(cash_available),equity,_context(position),bool(self.journal.pending()),overlay))
        if position and decision.next_stop is not None:
            position = replace(position,pending_stop=decision.next_stop,pending_stop_at=end)
        state = replace(state,position=position,risk=risk,completed_at=bar_at,decision=decision,candle_fingerprint=fingerprint)
        self.journal.save(state)
        if balances and decision.action in {'buy','sell'}:
            self._submit(decision,float(row['close']),float(row.get(f'atr_{self.engine.strategy.atr_period}',0)),at,balances)
        if action.resume_reduced and healthy:
            health = self._health()
            health.record_recovery_cycle_success(at)
            self._persist_health(health)
        return decision

    def _activate(self, position: LivePosition | None, at: datetime) -> LivePosition | None:
        if position and position.pending_stop_at and at >= position.pending_stop_at:
            return replace(position,current_stop=max(position.current_stop,position.pending_stop),pending_stop=None,pending_stop_at=None)
        return position

    def _submit(self, decision: Decision, price: float, entry_atr: float, at: datetime, balances: tuple[Account,Account]) -> None:
        guard.require_live_authorization()
        if self.journal.pending() or not _positive(price) or not _positive(decision.quantity):
            return
        state = self.journal.state()
        try:
            chance = self.client.order_chance('KRW-BTC')
            krw,btc = balances
            if (chance.bid_account != krw or chance.ask_account != btc):
                raise LiveResponseError('Order chance balance changed; reconcile before submission')
            reference = Decimal(str(price))
            if decision.action == 'buy':
                if state.position or self._health().current_action().halt_entries:
                    return
                amount = min(Decimal(str(decision.quantity))*reference,krw.balance/(1+chance.bid_fee),chance.max_total).quantize(Decimal('1'),rounding=ROUND_DOWN)
                if amount < chance.bid_min_total or not _positive(entry_atr):
                    return
                side = 'bid'
            else:
                if state.position is None:
                    return
                amount = min(Decimal(str(decision.quantity)),state.position.quantity,btc.balance).quantize(Decimal('.00000001'),rounding=ROUND_DOWN)
                if amount*reference < chance.ask_min_total or amount*reference > chance.max_total:
                    return
                side = 'ask'
            intent = LiveIntent(str(uuid4()),side,amount,price,entry_atr,self.engine.strategy.initial_atr_mult,at)
            health = self._health(); health.set_unresolved_orders(1,at)
            # This committed durable intent is the sole authority to send once.
            self.journal.save(replace(state,health=health.snapshot().to_mapping()),intent,new_intent=True)
            acknowledgement = (self.client.place_market_buy(intent.identifier,amount) if side == 'bid'
                               else self.client.place_market_sell(intent.identifier,amount))
            # Acknowledgement is identity evidence, not fabricated fill evidence.
            self.journal.save(self.journal.state(),replace(intent,venue_uuid=acknowledgement.uuid))
        except LiveRequestError as error:
            self._failure(error,at)

    def on_price(self, observed_price: float) -> None:
        guard.require_live_authorization()
        at = self.clock()
        balances = self._reconcile(at)
        state = self.journal.state()
        if state is None:
            return
        if not _positive(observed_price):
            health = self._health()
            health.record_schema_check(False,at)
            self._persist_health(health)
            return
        position = self._activate(state.position,at)
        if position:
            position = replace(position,high_water=max(position.high_water,observed_price))
        if position != state.position:
            self.journal.save(replace(state,position=position))
        if position and balances and self.engine.protect(_context(position),observed_price):
            self._submit(Decision('sell','STOP_EXIT',float(position.quantity),None,state.risk.decision),observed_price,position.entry_atr,at,balances)

"""Live-only durable facts and process-lifetime, crash-released ownership.

The sidecar holds an SQLite write transaction while the facts database commits
intent-before-send. No lease timer can expire underneath a slow network call.
"""

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from decimal import Decimal
import json
import math
import re
from pathlib import Path
import sqlite3
from types import MappingProxyType
from collections.abc import Mapping

from autobit.core import ClosedTradeObservation, Decision, RiskState
from autobit.live import guard
from autobit.paper.health import HealthSnapshot
from autobit.risk.breakers import RiskDecision


class LiveOwnershipError(RuntimeError):
    """Another writer owns this live journal."""


class LiveJournalError(ValueError):
    """Wrong provenance, malformed state or contradictory durable facts."""


@dataclass(frozen=True, slots=True)
class LivePosition:
    quantity: Decimal
    entry_price: float
    entry_atr: float
    initial_atr_mult: float
    entry_at: datetime
    initial_stop: float
    current_stop: float
    high_water: float
    held_bars: int
    cost_basis: Decimal
    realized_pnl: Decimal = Decimal(0)
    pending_stop: float | None = None
    pending_stop_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LiveIntent:
    identifier: str
    side: str
    amount: Decimal
    reference_price: float
    entry_atr: float
    initial_atr_mult: float
    created_at: datetime
    quantity: Decimal = Decimal(0)
    funds: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    venue_uuid: str | None = None
    terminal: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True, slots=True)
class LiveState:
    cash: Decimal
    position: LivePosition | None
    risk: RiskState
    health: Mapping[str, object]
    closed_trades: tuple[ClosedTradeObservation, ...] = ()
    completed_at: datetime | None = None
    decision: Decision | None = None
    candle_fingerprint: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'health', MappingProxyType(dict(self.health)))
        object.__setattr__(self, 'closed_trades', tuple(self.closed_trades))


def _encode(value):
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    raise TypeError('Unsupported journal value')


def _risk_decision(value):
    data = dict(value)
    data['halted_until'] = datetime.fromisoformat(data['halted_until']) if data['halted_until'] else None
    data['reasons'] = tuple(data['reasons'])
    return RiskDecision(**data)


def _risk(value):
    data = dict(value)
    if set(data) != {f.name for f in fields(RiskState)}:
        raise LiveJournalError('Invalid risk schema')
    for name in ('risk_started_at','last_risk_at','recovery_started_at','daily_halt_started_at','weekly_halt_started_at','streak_halt_started_at'):
        data[name] = datetime.fromisoformat(data[name]) if data[name] else None
    data['daily_date'] = date.fromisoformat(data['daily_date']) if data['daily_date'] else None
    data['equity_history'] = tuple((datetime.fromisoformat(at), equity) for at,equity in data['equity_history'])
    data['decision'] = _risk_decision(data['decision'])
    return RiskState(**data)


def _position(value):
    if value is None:
        return None
    data = dict(value)
    for name in ('quantity','cost_basis','realized_pnl'):
        data[name] = Decimal(data[name])
    for name in ('entry_at','pending_stop_at'):
        data[name] = datetime.fromisoformat(data[name]) if data[name] else None
    return LivePosition(**data)


def _intent(value):
    data = dict(value)
    for name in ('amount','quantity','funds','fee'):
        data[name] = Decimal(data[name])
    data['created_at'] = datetime.fromisoformat(data['created_at'])
    intent = LiveIntent(**data)
    if (not isinstance(intent.identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',intent.identifier)
            or intent.side not in {'bid','ask'} or type(intent.terminal) is not bool
            or type(intent.cancel_requested) is not bool or intent.created_at.tzinfo is None
            or not all(v.is_finite() and v >= 0 for v in (intent.amount,intent.quantity,intent.funds,intent.fee))
            or intent.amount == 0 or not math.isfinite(intent.reference_price) or intent.reference_price <= 0
            or (intent.side == 'ask' and intent.quantity > intent.amount)
            or (intent.side == 'bid' and intent.funds > intent.amount)):
        raise LiveJournalError('Invalid intent facts')
    return intent


def _state_mapping(state):
    # asdict cannot deepcopy MappingProxyType; health is explicitly canonical.
    return dict(cash=state.cash, position=asdict(state.position) if state.position else None,
                risk=asdict(state.risk), health=dict(state.health),
                closed_trades=[asdict(trade) for trade in state.closed_trades],
                completed_at=state.completed_at, decision=asdict(state.decision) if state.decision else None,
                candle_fingerprint=state.candle_fingerprint)


def _state(value):
    data = dict(value)
    data['cash'] = Decimal(data['cash'])
    data['position'] = _position(data['position'])
    data['risk'] = _risk(data['risk'])
    data['health'] = HealthSnapshot.from_event_mapping(data['health']).to_mapping()
    data['closed_trades'] = tuple(ClosedTradeObservation(t['net_pnl'],datetime.fromisoformat(t['exit_time'])) for t in data['closed_trades'])
    data['completed_at'] = datetime.fromisoformat(data['completed_at']) if data['completed_at'] else None
    if data['decision']:
        d = dict(data['decision']); d['risk'] = _risk_decision(d['risk'])
        data['decision'] = Decision(**d)
    state = LiveState(**data)
    if not state.cash.is_finite() or state.cash < 0:
        raise LiveJournalError('Invalid live cash')
    for name in ('initial_equity','equity_peak','daily_baseline_equity','last_equity'):
        number = getattr(state.risk,name)
        if not isinstance(number,(float,int)) or not math.isfinite(number) or number < 0:
            raise LiveJournalError('Invalid risk baseline')
    risk = state.risk
    if risk.equity_peak < max(risk.initial_equity,risk.last_equity):
        raise LiveJournalError('Risk peak contradicts equity facts')
    for name in ('consecutive_losses','processed_trade_count','profitable_trades_since_streak_halt','volatility_stable_bars'):
        number = getattr(risk,name)
        if type(number) is not int or number < 0:
            raise LiveJournalError('Invalid risk counter')
    if risk.processed_trade_count > len(state.closed_trades):
        raise LiveJournalError('Risk cursor exceeds factual trades')
    dates = [state.completed_at, risk.risk_started_at,risk.last_risk_at,risk.recovery_started_at,
             risk.daily_halt_started_at,risk.weekly_halt_started_at,risk.streak_halt_started_at,risk.decision.halted_until]
    dates.extend(at for at,_ in risk.equity_history)
    dates.extend(trade.exit_time for trade in state.closed_trades)
    if any(at is not None and (at.tzinfo is None or at.utcoffset() is None) for at in dates):
        raise LiveJournalError('Naive journal timestamp')
    if any(not math.isfinite(trade.net_pnl) for trade in state.closed_trades):
        raise LiveJournalError('Invalid factual trade PnL')
    if any(a.exit_time > b.exit_time for a,b in zip(state.closed_trades,state.closed_trades[1:])):
        raise LiveJournalError('Reversed factual trades')
    history = risk.equity_history
    if any(not math.isfinite(equity) or equity < 0 for _,equity in history):
        raise LiveJournalError('Invalid equity history')
    if any(a[0] >= b[0] for a,b in zip(history,history[1:])):
        raise LiveJournalError('Reversed equity history')
    if history and (history[-1][0] != risk.last_risk_at or history[-1][1] != risk.last_equity):
        raise LiveJournalError('Contradictory last risk observation')
    position = state.position
    if position:
        if (not position.quantity.is_finite() or position.quantity <= 0 or position.entry_at.tzinfo is None
                or not position.cost_basis.is_finite() or position.cost_basis < 0 or not position.realized_pnl.is_finite()
                or type(position.held_bars) is not int or position.held_bars < 0
                or not all(math.isfinite(v) and v > 0 for v in (position.entry_price,position.entry_atr,
                    position.initial_atr_mult,position.initial_stop,position.current_stop,position.high_water))
                or position.initial_stop >= position.entry_price or position.current_stop < position.initial_stop
                or position.high_water < position.entry_price
                or (position.pending_stop is None) != (position.pending_stop_at is None)
                or (position.pending_stop is not None and (not math.isfinite(position.pending_stop)
                    or position.pending_stop < position.current_stop or position.pending_stop_at.tzinfo is None))):
            raise LiveJournalError('Invalid position facts')
    return state


class LiveJournal:
    def __init__(self, path: str | Path, *, provenance: str = 'upbit-actual-krw', market: str = 'KRW-BTC') -> None:
        guard.require_live_authorization()
        if provenance != 'upbit-actual-krw' or market != 'KRW-BTC':
            raise LiveJournalError('Only actual KRW live provenance is accepted')
        path = Path(path).resolve()
        self._owner = sqlite3.connect(str(path)+'.owner.sqlite',timeout=0)
        try:
            self._owner.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            self._owner.close()
            raise LiveOwnershipError('Live journal already has a writer') from None
        try:
            self._db = sqlite3.connect(path,timeout=0)
            existing = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and existing != {'live_metadata','live_state','live_intents'}:
                raise LiveJournalError('Not a live-only journal')
            with self._db:
                self._db.execute('CREATE TABLE IF NOT EXISTS live_metadata (id INTEGER PRIMARY KEY CHECK(id=1), provenance TEXT, market TEXT, version INTEGER)')
                self._db.execute('CREATE TABLE IF NOT EXISTS live_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
                self._db.execute('CREATE TABLE IF NOT EXISTS live_intents (identifier TEXT PRIMARY KEY, payload TEXT NOT NULL, terminal INTEGER NOT NULL)')
                metadata = self._db.execute('SELECT provenance,market,version FROM live_metadata WHERE id=1').fetchone()
                if metadata is None and existing:
                    raise LiveJournalError('Missing live provenance')
                if metadata is None:
                    self._db.execute('INSERT INTO live_metadata VALUES (1,?,?,1)',(provenance,market))
                elif metadata != (provenance,market,1):
                    raise LiveJournalError('Wrong live journal provenance/version')
            self.state()
            self.pending()
        except Exception:
            if hasattr(self,'_db'):
                self._db.close()
            self._owner.close()
            raise

    def state(self) -> LiveState | None:
        row = self._db.execute('SELECT payload FROM live_state WHERE id=1').fetchone()
        try:
            return _state(json.loads(row[0])) if row else None
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise LiveJournalError('Invalid persisted live state') from None

    def pending(self) -> tuple[LiveIntent, ...]:
        try:
            pending = []
            for identifier,payload,terminal in self._db.execute('SELECT identifier,payload,terminal FROM live_intents ORDER BY rowid'):
                intent = _intent(json.loads(payload))
                if intent.identifier != identifier or terminal != int(intent.terminal):
                    raise LiveJournalError('Contradictory intent projection')
                if not intent.terminal:
                    pending.append(intent)
            if len(pending) > 1:
                raise LiveJournalError('Multiple unresolved intents')
            return tuple(pending)
        except (ValueError,TypeError,KeyError,ArithmeticError):
            raise LiveJournalError('Invalid durable intent') from None

    def save(self, state: LiveState, intent: LiveIntent | None = None, *, new_intent: bool = False) -> None:
        guard.require_live_authorization()
        payload = json.dumps(_state_mapping(state),default=_encode,allow_nan=False,sort_keys=True)
        _state(json.loads(payload))
        with self._db:
            if intent:
                encoded = json.dumps(asdict(intent),default=_encode,allow_nan=False,sort_keys=True)
                _intent(json.loads(encoded))
                if new_intent:
                    if self.pending():
                        raise LiveJournalError('An unresolved intent already exists')
                    self._db.execute('INSERT INTO live_intents VALUES (?,?,?)',(intent.identifier,encoded,int(intent.terminal)))
                else:
                    cursor = self._db.execute('UPDATE live_intents SET payload=?,terminal=? WHERE identifier=?',(encoded,int(intent.terminal),intent.identifier))
                    if cursor.rowcount != 1:
                        raise LiveJournalError('Missing durable intent')
            self._db.execute('INSERT INTO live_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(payload,))

    def close(self) -> None:
        self._db.close()
        self._owner.close()

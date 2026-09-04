"""Narrow authenticated Upbit boundary, reachable only after the fixed guard.

The only injection is an HTTP transport, never a destination, client with
mutable defaults, TLS policy, proxy, redirect policy or authorization switch.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import base64
import hashlib
import hmac
import json
import re
from urllib.parse import unquote, urlencode
from uuid import uuid4

import httpx

from autobit.live import guard


class LiveRequestError(RuntimeError):
    """Sanitized network/venue rejection; never contains response bodies."""


class OrderNotFound(LiveRequestError):
    """Absence is not proof that a previously submitted intent is safe to retry."""


class LiveResponseError(LiveRequestError):
    """Invalid or contradictory venue facts."""


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Account:
    currency: str
    balance: Decimal
    locked: Decimal


@dataclass(frozen=True, slots=True)
class OrderChance:
    market: str
    bid_fee: Decimal
    ask_fee: Decimal
    bid_min_total: Decimal
    ask_min_total: Decimal
    max_total: Decimal
    bid_account: Account
    ask_account: Account


@dataclass(frozen=True, slots=True)
class VenueOrder:
    identifier: str
    uuid: str
    market: str
    side: str
    ord_type: str
    state: str
    executed_volume: Decimal
    executed_funds: Decimal | None
    paid_fee: Decimal
    volume: Decimal | None
    remaining_volume: Decimal | None
    price: Decimal | None
    created_at: datetime
    last_fill_at: datetime | None
    first_fill_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.state in {'done', 'cancel'}


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise LiveResponseError('Invalid numeric venue field')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise LiveResponseError('Invalid numeric venue field') from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise LiveResponseError('Invalid numeric venue field')
    return result


def _time(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        result = None
    if result is None or result.tzinfo is None or result.utcoffset() is None:
        raise LiveResponseError('Invalid venue timestamp')
    return result


def _account(data: object, expected: str | None = None) -> Account:
    if not isinstance(data, dict) or not isinstance(data.get('currency'), str):
        raise LiveResponseError('Invalid account schema')
    currency = data['currency']
    if not re.fullmatch(r'[A-Z0-9]{1,20}', currency) or (expected and currency != expected):
        raise LiveResponseError('Invalid account currency')
    return Account(currency, _decimal(data.get('balance')), _decimal(data.get('locked')))


def _order(data: object, identifier: str, side: str | None, *, detail: bool) -> VenueOrder:
    if not isinstance(data, dict):
        raise LiveResponseError('Invalid order schema')
    if (data.get('identifier') != identifier or data.get('market') != 'KRW-BTC'
            or data.get('side') not in {'bid', 'ask'} or (side and data['side'] != side)
            or data.get('state') not in {'wait', 'watch', 'done', 'cancel'}
            or not isinstance(data.get('uuid'), str) or not data['uuid']):
        raise LiveResponseError('Mismatched order identity or state')
    if data.get('ord_type') != ('price' if data['side'] == 'bid' else 'market'):
        raise LiveResponseError('Unexpected order type')
    volume = _decimal(data['volume']) if data.get('volume') is not None else None
    remaining = _decimal(data['remaining_volume']) if data.get('remaining_volume') is not None else None
    price = _decimal(data['price'], positive=True) if data.get('price') is not None else None
    executed, fee = _decimal(data.get('executed_volume')), _decimal(data.get('paid_fee'))
    for name in ('locked', 'reserved_fee', 'remaining_fee'):
        _decimal(data.get(name))
    if volume is not None and (executed > volume or remaining is None or executed + remaining > volume):
        raise LiveResponseError('Contradictory order volumes')
    created = _time(data.get('created_at'))
    funds, first_fill, last_fill = None, None, None
    trades = data.get('trades')
    if trades is not None:
        if not isinstance(trades, list) or type(data.get('trades_count')) is not int or len(trades) != data['trades_count']:
            raise LiveResponseError('Incomplete trade facts')
        total, funds, trade_ids = Decimal(0), Decimal(0), set()
        for trade in trades:
            if (not isinstance(trade, dict) or trade.get('market') != 'KRW-BTC'
                    or trade.get('side') != data['side'] or not isinstance(trade.get('uuid'), str)
                    or not trade['uuid'] or trade['uuid'] in trade_ids):
                raise LiveResponseError('Invalid trade identity')
            trade_ids.add(trade['uuid'])
            quantity = _decimal(trade.get('volume'), positive=True)
            trade_price = _decimal(trade.get('price'), positive=True)
            trade_funds = _decimal(trade.get('funds'), positive=True)
            if abs(quantity * trade_price - trade_funds) > Decimal('0.00000001'):
                raise LiveResponseError('Contradictory trade funds')
            at = _time(trade.get('created_at'))
            if at < created:
                raise LiveResponseError('Trade precedes order')
            first_fill = min(first_fill, at) if first_fill else at
            last_fill = max(last_fill, at) if last_fill else at
            total += quantity
            funds += trade_funds
        if total != executed:
            raise LiveResponseError('Incomplete cumulative fills')
    if detail and executed > 0 and funds is None:
        raise LiveResponseError('Missing actual fill facts')
    if executed == 0 and fee != 0:
        raise LiveResponseError('Fee without a fill')
    return VenueOrder(identifier, data['uuid'], 'KRW-BTC', data['side'], data['ord_type'], data['state'],
                      executed, funds, fee, volume, remaining, price, created, last_fill, first_fill)


class LiveClient:
    def __init__(self, credentials_supplier: Callable[[], Credentials], *, transport: httpx.BaseTransport | None = None) -> None:
        self._credentials_supplier = credentials_supplier
        self._transport = transport

    def _request(self, method: str, path: str, fields: dict[str, str]) -> object:
        guard.require_live_authorization()
        if (method, path) not in {('GET', '/v1/accounts'), ('GET', '/v1/orders/chance'),
                                  ('POST', '/v1/orders'), ('GET', '/v1/order'), ('DELETE', '/v1/order')}:
            raise ValueError('Unapproved private endpoint')
        try:
            credentials = self._credentials_supplier()
        except Exception:
            raise LiveRequestError('Credential supplier failed') from None
        if (not isinstance(credentials, Credentials) or not isinstance(credentials.access_key,str)
                or not isinstance(credentials.secret_key,str) or not credentials.access_key or not credentials.secret_key):
            raise LiveRequestError('Invalid supplied credentials')
        payload = {'access_key': credentials.access_key, 'nonce': str(uuid4())}
        query = unquote(urlencode(fields))
        if query:
            payload.update(query_hash=hashlib.sha512(query.encode()).hexdigest(), query_hash_alg='SHA512')
        def encode(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')
        head = encode(b'{"alg":"HS512","typ":"JWT"}')
        body = encode(json.dumps(payload, separators=(',', ':')).encode())
        signature = encode(hmac.new(credentials.secret_key.encode(), f'{head}.{body}'.encode(), hashlib.sha512).digest())
        headers = {'Authorization': f'Bearer {head}.{body}.{signature}'}
        try:
            with httpx.Client(transport=self._transport, verify=True, follow_redirects=False, trust_env=False, timeout=10.0) as client:
                response = client.request(method, 'https://api.upbit.com' + path, headers=headers,
                                          json=fields if method == 'POST' else None,
                                          params=fields if method != 'POST' else None)
        except (httpx.HTTPError, OSError):
            raise LiveRequestError('Private request outcome uncertain') from None
        try:
            data = response.json()
        except ValueError:
            raise LiveResponseError('Invalid venue JSON') from None
        if not 200 <= response.status_code < 300:
            if (response.status_code == 404 and isinstance(data, dict)
                    and isinstance(data.get('error'), dict) and data['error'].get('name') == 'order_not_found'):
                raise OrderNotFound('Order not found; intent remains uncertain')
            raise LiveRequestError(f'Private request rejected (HTTP {response.status_code})')
        return data

    def accounts(self) -> tuple[Account, ...]:
        guard.require_live_authorization()
        data = self._request('GET', '/v1/accounts', {})
        if not isinstance(data, list):
            raise LiveResponseError('Invalid accounts schema')
        accounts = tuple(_account(item) for item in data)
        if len({a.currency for a in accounts}) != len(accounts):
            raise LiveResponseError('Duplicate account currency')
        return accounts

    def order_chance(self, market: str) -> OrderChance:
        guard.require_live_authorization()
        if market != 'KRW-BTC':
            raise ValueError('Only KRW-BTC is supported')
        data = self._request('GET', '/v1/orders/chance', {'market': market})
        try:
            m = data['market']
            if (m['id'] != market or m['state'] != 'active' or not {'bid','ask'} <= set(m['order_sides'])
                    or 'price' not in m['bid_types'] or 'market' not in m['ask_types']
                    or m['bid']['currency'] != 'KRW' or m['ask']['currency'] != 'BTC'):
                raise LiveResponseError('Unsupported market constraints')
            result = OrderChance(market, _decimal(data['bid_fee']), _decimal(data['ask_fee']),
                                 _decimal(m['bid']['min_total'], positive=True), _decimal(m['ask']['min_total'], positive=True),
                                 _decimal(m['max_total'], positive=True), _account(data['bid_account'], 'KRW'), _account(data['ask_account'], 'BTC'))
            if result.bid_fee >= 1 or result.ask_fee >= 1 or result.max_total < max(result.bid_min_total, result.ask_min_total):
                raise LiveResponseError('Invalid market constraints')
            return result
        except (KeyError, TypeError):
            raise LiveResponseError('Invalid order chance schema') from None

    def place_market_buy(self, identifier: str, amount_krw: Decimal) -> VenueOrder:
        guard.require_live_authorization()
        _identifier(identifier)
        amount = _amount(amount_krw)
        data = self._request('POST', '/v1/orders', {'market':'KRW-BTC', 'side':'bid', 'price':amount, 'ord_type':'price', 'identifier':identifier})
        result = _order(data, identifier, 'bid', detail=False)
        if result.price != amount_krw:
            raise LiveResponseError('Mismatched buy amount')
        return result

    def place_market_sell(self, identifier: str, volume_btc: Decimal) -> VenueOrder:
        guard.require_live_authorization()
        _identifier(identifier)
        volume = _amount(volume_btc)
        result = _order(self._request('POST', '/v1/orders', {'market':'KRW-BTC', 'side':'ask', 'volume':volume, 'ord_type':'market', 'identifier':identifier}), identifier, 'ask', detail=False)
        if result.volume != volume_btc:
            raise LiveResponseError('Mismatched sell volume')
        return result

    def get_order(self, identifier: str) -> VenueOrder:
        guard.require_live_authorization()
        _identifier(identifier)
        return _order(self._request('GET', '/v1/order', {'identifier':identifier}), identifier, None, detail=True)

    def cancel_order(self, identifier: str) -> VenueOrder:
        guard.require_live_authorization()
        _identifier(identifier)
        return _order(self._request('DELETE', '/v1/order', {'identifier':identifier}), identifier, None, detail=False)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('Invalid client identifier')


def _amount(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError('Order amount must be a positive finite Decimal')
    return format(value, 'f')

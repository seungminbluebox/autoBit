import base64
import hashlib
import hmac
import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest


def api():
    assert importlib.util.find_spec('autobit.live') is not None, 'locked live package missing'
    from autobit.live import client, guard
    return client, guard


def order(identifier='offline-1', side='bid', **changes):
    result = dict(uuid='venue-1', identifier=identifier, market='KRW-BTC', side=side,
                  ord_type='price' if side == 'bid' else 'market', state='wait',
                  price='10000' if side == 'bid' else None,
                  volume=None if side == 'bid' else '0.001', remaining_volume=None if side == 'bid' else '0.001',
                  executed_volume='0', paid_fee='0', remaining_fee='5', reserved_fee='5', locked='10005',
                  created_at='2026-09-05T00:00:00Z', trades_count=0, trades=[])
    result.update(changes)
    return result


def test_release_lock_precedes_credentials_transport_and_all_private_methods(monkeypatch):
    c, g = api()
    monkeypatch.setenv('AUTOBIT_LIVE_ENABLED', 'true')
    touched = []
    live = c.LiveClient(lambda: touched.append('credentials'), transport=httpx.MockTransport(lambda r: touched.append('HTTP')))
    monkeypatch.setattr(httpx,'Client',lambda **kw:pytest.fail('HTTP client constructed while locked'))
    monkeypatch.setattr(c.hmac,'new',lambda *a,**kw:pytest.fail('signing reached while locked'))
    for call in [live.accounts, lambda: live.order_chance('KRW-BTC'),
                 lambda: live.place_market_buy('offline-1', Decimal('10000')),
                 lambda: live.place_market_sell('offline-1', Decimal('.001')),
                 lambda: live.get_order('offline-1'), lambda: live.cancel_order('offline-1')]:
        with pytest.raises(g.LiveTradingLockedError):
            call()
    assert touched == []


def unlocked(monkeypatch, handler):
    c, g = api()
    monkeypatch.setattr(g, 'require_live_authorization', lambda: None)
    return c.LiveClient(lambda: c.Credentials('fake-access', 'fake-secret'), transport=httpx.MockTransport(handler))


def test_signed_requests_have_exact_market_fields_hash_signature_and_unique_nonce(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        data = json.loads(request.content) if request.method == 'POST' else dict(request.url.params)
        return httpx.Response(200, json=order(data['identifier'], data.get('side', 'bid')))
    live = unlocked(monkeypatch, handler)
    live.place_market_buy('offline-1', Decimal('10000'))
    live.place_market_sell('offline-2', Decimal('0.001'))
    live.get_order('offline-1')
    live.cancel_order('offline-1')
    assert json.loads(seen[0].content) == {'market':'KRW-BTC','side':'bid','price':'10000','ord_type':'price','identifier':'offline-1'}
    assert json.loads(seen[1].content) == {'market':'KRW-BTC','side':'ask','volume':'0.001','ord_type':'market','identifier':'offline-2'}
    assert [(r.method, r.url.path) for r in seen] == [('POST','/v1/orders'),('POST','/v1/orders'),('GET','/v1/order'),('DELETE','/v1/order')]
    nonces = set()
    for r in seen:
        assert r.url.host == 'api.upbit.com' and r.url.scheme == 'https'
        head, payload, signature = r.headers['Authorization'].split()[1].split('.')
        decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
        assert json.loads(decode(head)) == {'alg':'HS512','typ':'JWT'}
        claims = json.loads(decode(payload))
        nonces.add(claims['nonce'])
        expected = hmac.new(b'fake-secret', f'{head}.{payload}'.encode(), hashlib.sha512).digest()
        assert decode(signature) == expected
        fields = json.loads(r.content) if r.method == 'POST' else dict(r.url.params)
        query = '&'.join(f'{k}={v}' for k, v in fields.items())
        assert claims['query_hash'] == hashlib.sha512(query.encode()).hexdigest()
    assert len(nonces) == 4


@pytest.mark.parametrize('changes', [{'market':'KRW-ETH'}, {'identifier':'wrong'}, {'side':'other'}, {'paid_fee':'-1'}, {'executed_volume':'NaN'}, {'state':'mystery'}, {'created_at':'invalid'}, {'trades_count':1}])
def test_rejects_malformed_or_mismatched_order(monkeypatch, changes):
    live = unlocked(monkeypatch, lambda r: httpx.Response(200, json=order(**changes)))
    c, _ = api()
    with pytest.raises(c.LiveResponseError):
        live.get_order('offline-1')


@pytest.mark.parametrize('amount', [Decimal('0'), Decimal('-1'), Decimal('NaN'), Decimal('Infinity'), 10000.0])
def test_invalid_amount_never_sent(monkeypatch, amount):
    seen = []
    live = unlocked(monkeypatch, lambda r: seen.append(r))
    with pytest.raises(ValueError):
        live.place_market_buy('offline-1', amount)
    assert not seen


@pytest.mark.parametrize('status', [302, 401, 429, 500])
def test_http_failures_do_not_leak_body_or_follow_redirect(monkeypatch, status):
    seen = []
    def handler(r):
        seen.append(r)
        return httpx.Response(status, headers={'location':'https://evil.example'}, json={'error':{'name':'bad','message':'fake-secret'}})
    live = unlocked(monkeypatch, handler)
    c, _ = api()
    with pytest.raises(c.LiveRequestError) as caught:
        live.accounts()
    assert len(seen) == 1 and 'fake-secret' not in str(caught.value)


def test_timeout_is_sanitized_and_not_found_is_distinct(monkeypatch):
    c, _ = api()
    def timeout(r):
        raise httpx.ReadTimeout('fake-secret', request=r)
    live = unlocked(monkeypatch, timeout)
    with pytest.raises(c.LiveRequestError, match='uncertain'):
        live.get_order('offline-1')
    live = unlocked(monkeypatch, lambda r: httpx.Response(404, json={'error':{'name':'order_not_found'}}))
    with pytest.raises(c.OrderNotFound):
        live.get_order('offline-1')


def test_supplier_exception_is_sanitized(monkeypatch):
    c,g=api(); monkeypatch.setattr(g,'require_live_authorization',lambda:None)
    def supplier():
        raise ValueError('fake-secret')
    live=c.LiveClient(supplier,transport=httpx.MockTransport(lambda r:pytest.fail('HTTP reached')))
    with pytest.raises(c.LiveRequestError) as error:
        live.accounts()
    assert 'fake-secret' not in str(error.value)


@pytest.mark.parametrize('payload', [[dict(currency='KRW',balance='-1',locked='0')],
    [dict(currency='BTC',balance='NaN',locked='0')], [dict(currency='KRW',balance='1',locked='0')]*2,
    {'balance':'100'}, [dict(currency='KRW',balance='100')]])
def test_accounts_reject_bad_balances_and_duplicate_currency(monkeypatch,payload):
    c,_=api()
    live=unlocked(monkeypatch,lambda r:httpx.Response(200,json=payload))
    with pytest.raises(c.LiveResponseError):
        live.accounts()


def test_fixed_destination_rejects_unsafe_private_path_before_credentials(monkeypatch):
    c,g=api(); monkeypatch.setattr(g,'require_live_authorization',lambda:None)
    live=c.LiveClient(lambda:pytest.fail('credentials reached'),transport=httpx.MockTransport(lambda r:pytest.fail('HTTP reached')))
    for path in ['https://evil.example/v1/orders','/v1/withdraws','/v1/orders/test','/v1/../withdraws']:
        with pytest.raises(ValueError):
            live._request('POST',path,{})


def test_empty_accounts_jwt_omits_query_hash_and_uses_secure_client(monkeypatch):
    c,_=api(); captured=[]
    original=httpx.Client
    def client(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(httpx,'Client',client)
    def handler(r):
        payload=r.headers['Authorization'].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        assert set(claims)=={'access_key','nonce'}
        return httpx.Response(200,json=[])
    live=unlocked(monkeypatch,handler)
    assert live.accounts()==()
    assert captured[0]['verify'] is True and captured[0]['follow_redirects'] is False and captured[0]['trust_env'] is False


@pytest.mark.parametrize('field,value',[('bid_fee','NaN'),('ask_fee','-1'),('market',{}),('bid_account',{'currency':'BTC','balance':'1','locked':'0'})])
def test_invalid_order_constraints_fail_closed(monkeypatch,field,value):
    c,_=api()
    data=dict(bid_fee='.0005',ask_fee='.0005',market=dict(id='KRW-BTC',state='active',order_sides=['bid','ask'],
        bid_types=['price'],ask_types=['market'],bid=dict(currency='KRW',min_total='5000'),
        ask=dict(currency='BTC',min_total='5000'),max_total='1000000000'),
        bid_account=dict(currency='KRW',balance='100000',locked='0'),ask_account=dict(currency='BTC',balance='0',locked='0'))
    data[field]=value
    live=unlocked(monkeypatch,lambda r:httpx.Response(200,json=data))
    with pytest.raises(c.LiveResponseError):live.order_chance('KRW-BTC')


@pytest.mark.parametrize('reverse', [False, True])
def test_cumulative_trades_preserve_first_and_latest_execution_independent_of_order(monkeypatch, reverse):
    first = datetime(2026, 9, 5, 3, 59, 59, 900000, tzinfo=timezone.utc)
    last = datetime(2026, 9, 5, 4, 0, 0, 100000, tzinfo=timezone.utc)
    trades = [dict(uuid=str(n), market='KRW-BTC', side='bid', price='10000000',
                   volume='.0005', funds='5000', created_at=at.isoformat())
              for n, at in enumerate((first, last))]
    if reverse:
        trades.reverse()
    payload = order(state='done', executed_volume='.001', paid_fee='5', trades_count=2, trades=trades)
    live = unlocked(monkeypatch, lambda r: httpx.Response(200, json=payload))
    actual = live.get_order('offline-1')
    assert getattr(actual, 'first_fill_at', None) == first
    assert actual.last_fill_at == last
    assert actual.executed_funds == Decimal('10000')


@pytest.mark.parametrize('timestamp', [None, '', 'invalid', '2026-09-05T03:59:59.9',
                                      '2026-09-04T23:59:59Z'])
def test_missing_or_invalid_actual_first_execution_time_is_not_order_time(monkeypatch, timestamp):
    trades = [dict(uuid='first', market='KRW-BTC', side='bid', price='10000000',
                   volume='.0005', funds='5000', created_at=timestamp),
              dict(uuid='last', market='KRW-BTC', side='bid', price='10000000',
                   volume='.0005', funds='5000', created_at='2026-09-05T04:00:00.1Z')]
    if timestamp is None:
        del trades[0]['created_at']
    live = unlocked(monkeypatch, lambda r: httpx.Response(200, json=order(
        state='done', executed_volume='.001', paid_fee='5', trades_count=2, trades=trades)))
    c, _ = api()
    with pytest.raises(c.LiveResponseError):
        live.get_order('offline-1')


def test_zero_fill_acknowledgement_has_no_invented_execution_times(monkeypatch):
    live = unlocked(monkeypatch, lambda r: httpx.Response(200, json=order()))
    actual = live.place_market_buy('offline-1', Decimal('10000'))
    assert actual.last_fill_at is None
    assert getattr(actual, 'first_fill_at', None) is None

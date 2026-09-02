from collections.abc import Callable

import httpx
import pandas as pd
import pytest

from autobit.config import DataConfig
from autobit.data.collector import collect_range
from autobit.data.upbit_public import PublicDataUnavailable, RemainingRequestLimit, UpbitPublicClient


def _candle(timestamp: str, price: float = 100.0) -> dict[str, object]:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": timestamp,
        "opening_price": price,
        "high_price": price + 1,
        "low_price": price - 1,
        "trade_price": price,
        "candle_acc_trade_volume": 1.0,
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    waits: list[float] | None = None,
) -> UpbitPublicClient:
    return UpbitPublicClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        DataConfig(),
        sleep=(waits.append if waits is not None else lambda _: None),
    )


def test_client_uses_only_public_candle_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = UpbitPublicClient(httpx.Client(transport=httpx.MockTransport(handler)), DataConfig())

    assert client.fetch_page("2026-01-01T00:00:00Z") == []
    assert seen[0].method == "GET"
    assert str(seen[0].url) == (
        "https://api.upbit.com/v1/candles/minutes/240?market=KRW-BTC"
        "&to=2026-01-01T00%3A00%3A00Z&count=200"
    )
    assert "Authorization" not in seen[0].headers


def test_client_refuses_any_market_other_than_krw_btc() -> None:
    with pytest.raises(ValueError, match="KRW-BTC"):
        UpbitPublicClient(
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))),
            DataConfig(market="BTC-ETH"),
        )


def test_remaining_request_header_throttles_next_request_without_sleeping_tests() -> None:
    waits: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Remaining-Req": "group=candle; min=599; sec=0"}, json=[])

    client = _client(handler, waits)

    client.fetch_page("2026-01-01T00:00:00Z")
    client.fetch_page("2025-12-31T20:00:00Z")

    assert client.remaining_request_limit == RemainingRequestLimit(group="candle", min_remaining=599, sec_remaining=0)
    assert waits == [1.0]


def test_client_retries_retryable_failures_four_times_then_raises() -> None:
    waits: list[float] = []
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"error": "temporary"})

    client = _client(handler, waits)

    with pytest.raises(PublicDataUnavailable):
        client.fetch_page("2026-01-01T00:00:00Z")

    assert attempts == 4
    assert waits == [1.0, 2.0, 4.0]


def test_collect_range_moves_backward_deduplicates_and_stops_before_start() -> None:
    seen_to: list[str] = []
    pages = {
        "2026-01-01T12:00:00Z": [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)],
        "2026-01-01T04:00:00Z": [
            _candle("2026-01-01T04:00:00Z", 104),
            _candle("2026-01-01T00:00:00Z", 100),
            _candle("2025-12-31T20:00:00Z", 96),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        to_utc = request.url.params["to"]
        seen_to.append(to_utc)
        return httpx.Response(200, json=pages[to_utc])

    frame = collect_range(
        _client(handler),
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
    )

    assert seen_to == ["2026-01-01T12:00:00Z", "2026-01-01T04:00:00Z"]
    assert frame["candle_date_time_utc"].tolist() == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T04:00:00Z",
        "2026-01-01T08:00:00Z",
    ]
    assert frame["candle_date_time_utc"].is_unique
    assert frame["candle_date_time_utc"].is_monotonic_increasing


def test_collect_range_stops_when_a_repeated_page_cannot_move_backward() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_candle("2026-01-01T04:00:00Z")])

    frame = collect_range(
        _client(handler),
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
    )

    assert calls == 2
    assert frame["candle_date_time_utc"].tolist() == ["2026-01-01T04:00:00Z"]
    assert isinstance(frame, pd.DataFrame)

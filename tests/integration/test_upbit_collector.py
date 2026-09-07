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


def test_client_does_not_inherit_client_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=[])

    client = UpbitPublicClient(
        httpx.Client(
            auth=httpx.BasicAuth("public-collector", "must-not-be-sent"),
            transport=httpx.MockTransport(handler),
        ),
        DataConfig(),
    )

    assert client.fetch_page("2026-01-01T00:00:00Z") == []


def test_client_does_not_follow_redirects_to_another_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.upbit.com":
            return httpx.Response(302, headers={"Location": "https://different.example/candles"})
        return httpx.Response(200, json=[])

    client = UpbitPublicClient(
        httpx.Client(follow_redirects=True, transport=httpx.MockTransport(handler)),
        DataConfig(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_page("2026-01-01T00:00:00Z")

    assert [(request.url.host, request.url.path) for request in seen] == [
        ("api.upbit.com", "/v1/candles/minutes/240"),
    ]


def test_client_refuses_any_market_other_than_krw_btc() -> None:
    with pytest.raises(ValueError, match="KRW-BTC"):
        UpbitPublicClient(
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))),
            DataConfig(market="BTC-ETH"),
        )


@pytest.mark.parametrize("candle_unit_minutes", [1, 60, 200])
def test_client_refuses_any_candle_unit_other_than_four_hours_before_network(
    candle_unit_minutes: int,
) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ValueError, match="candle_unit_minutes"):
        UpbitPublicClient(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: seen.append(request) or httpx.Response(200, json=[])
                )
            ),
            DataConfig(candle_unit_minutes=candle_unit_minutes),
        )

    assert seen == []


@pytest.mark.parametrize("page_size", [0, 201])
def test_client_refuses_out_of_range_page_sizes_before_sending_a_request(page_size: int) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(ValueError, match="page_size"):
        UpbitPublicClient(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: seen.append(request) or httpx.Response(200, json=[])
                )
            ),
            DataConfig(page_size=page_size),
        )

    assert seen == []


def test_client_exposes_the_immutable_validated_request_config() -> None:
    config = DataConfig(page_size=17)
    client = UpbitPublicClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))),
        config,
    )

    assert client.collection_config is config
    assert client.collection_config.market == "KRW-BTC"
    assert client.collection_config.candle_unit_minutes == 240
    assert client.collection_config.page_size == 17


def test_remaining_request_header_throttles_next_request_without_sleeping_tests() -> None:
    waits: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Remaining-Req": "group=candle; min=599; sec=0"}, json=[])

    client = _client(handler, waits)

    client.fetch_page("2026-01-01T00:00:00Z")
    client.fetch_page("2025-12-31T20:00:00Z")

    assert client.remaining_request_limit == RemainingRequestLimit(group="candle", min_remaining=599, sec_remaining=0)
    assert waits == [1.0]


def test_retry_failures_with_zero_remaining_seconds_do_not_add_throttle_waits() -> None:
    waits: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, headers={"Remaining-Req": "group=candle; min=599; sec=0"}, json=[])

    with pytest.raises(PublicDataUnavailable):
        _client(handler, waits).fetch_page("2026-01-01T00:00:00Z")

    assert waits == [1.0, 2.0, 4.0]


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


def test_collect_range_accepts_upbit_documented_timezone_less_utc_candles() -> None:
    """Upbit labels this exact timezone-less response field as UTC."""
    client = _client(
        lambda _: httpx.Response(
            200,
            json=[
                _candle("2026-01-01T08:00:00", 108),
                _candle("2025-12-31T20:00:00", 96),
            ],
        )
    )

    frame = collect_range(
        client,
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
    )

    assert frame["candle_date_time_utc"].tolist() == ["2026-01-01T08:00:00"]


def test_collect_range_rejects_non_utc_offset_in_utc_candle_field() -> None:
    client = _client(
        lambda _: httpx.Response(
            200,
            json=[_candle("2026-01-01T08:00:00+09:00")],
        )
    )

    with pytest.raises(ValueError, match="UTC"):
        collect_range(
            client,
            start_utc="2026-01-01T00:00:00Z",
            end_utc="2026-01-01T12:00:00Z",
        )


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

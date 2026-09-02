"""Keyless client for Upbit's public four-hour candle endpoint."""

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Final

import httpx

from autobit.config import DataConfig


PUBLIC_CANDLE_URL: Final = "https://api.upbit.com/v1/candles/minutes/240"
_RETRY_DELAYS_SECONDS: Final = (1.0, 2.0, 4.0)


class PublicDataUnavailable(RuntimeError):
    """Raised after all retry attempts for public candle data fail."""


@dataclass(frozen=True, slots=True)
class RemainingRequestLimit:
    group: str
    min_remaining: int
    sec_remaining: int


def parse_remaining_request_limit(value: str | None) -> RemainingRequestLimit | None:
    """Parse Upbit's ``Remaining-Req`` response header when it is complete."""
    if value is None:
        return None

    parts: dict[str, str] = {}
    for item in value.split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if not separator:
            return None
        parts[key.strip()] = raw_value.strip()

    try:
        return RemainingRequestLimit(
            group=parts["group"],
            min_remaining=int(parts["min"]),
            sec_remaining=int(parts["sec"]),
        )
    except (KeyError, ValueError):
        return None


class UpbitPublicClient:
    """Fetch only public KRW-BTC four-hour candle pages without credentials."""

    def __init__(
        self,
        http_client: httpx.Client,
        config: DataConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.market != "KRW-BTC":
            raise ValueError("public collector supports only the KRW-BTC market")
        if config.candle_unit_minutes != 240:
            raise ValueError("public collector requires candle_unit_minutes=240")
        if isinstance(config.page_size, bool) or not isinstance(config.page_size, int) or not 1 <= config.page_size <= 200:
            raise ValueError("page_size must be an integer from 1 through 200")
        self._http_client = http_client
        self._config = config
        self._sleep = sleep
        self.remaining_request_limit: RemainingRequestLimit | None = None

    @property
    def source_url(self) -> str:
        return PUBLIC_CANDLE_URL

    @property
    def collection_config(self) -> DataConfig:
        """The frozen, validated configuration that determines every request."""
        return self._config

    def fetch_page(self, to_utc: str) -> list[dict[str, object]]:
        """Fetch one page, retrying only transient public-endpoint failures."""
        last_failure: BaseException | None = None
        for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
            self._throttle_if_needed()
            try:
                response = self._send_candle_request(to_utc)
            except httpx.TransportError as error:
                last_failure = error
            else:
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_failure = httpx.HTTPStatusError(
                        f"public candle request returned {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                else:
                    response.raise_for_status()
                    self.remaining_request_limit = parse_remaining_request_limit(response.headers.get("Remaining-Req"))
                    payload = response.json()
                    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
                        raise PublicDataUnavailable("public candle response was not a list of objects")
                    return [dict(row) for row in payload]

            if attempt == len(_RETRY_DELAYS_SECONDS):
                raise PublicDataUnavailable("public Upbit candle data is unavailable") from last_failure
            self._sleep(_RETRY_DELAYS_SECONDS[attempt])

        raise AssertionError("unreachable")

    def _send_candle_request(self, to_utc: str) -> httpx.Response:
        request = httpx.Request(
            "GET",
            PUBLIC_CANDLE_URL,
            params={
                "market": self._config.market,
                "to": to_utc,
                "count": str(self._config.page_size),
            },
        )
        return self._http_client.send(request, auth=None, follow_redirects=False)

    def _throttle_if_needed(self) -> None:
        if self.remaining_request_limit is not None and self.remaining_request_limit.sec_remaining <= 0:
            self._sleep(1.0)

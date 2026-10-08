import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from config import settings
from ingest.throttle import TokenBucket, backoff_delay

log = logging.getLogger(__name__)

PHASES = ("calendar", "symbols", "bars")

BARS_HOST = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"

# self-inflicted and self-clearing: the one status exempt from the attempt budget
THROTTLED_STATUS = 429
RETRYABLE_STATUSES = (500, 502, 503, 504)
# retrying cannot help these; one root cause would log once per unit across the run
FATAL_STATUSES = (400, 401, 403)


class FatalVendorError(RuntimeError):
    """A status no retry can clear: the run stops."""


class UnitFetchError(RuntimeError):
    """A failure confined to one (ticker, month) unit: the run logs it and continues."""


# a ticker-month is one page on iex, two on sip at limit=10000; this bounds only a stuck cursor
MAX_PAGES = 64


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int
    vwap: Decimal


class AlpacaClient:
    """Authenticated transport for the Alpaca REST endpoints."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        sleep=time.sleep,
    ) -> None:
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._headers = {
            "APCA-API-KEY-ID": settings.ALPACA_KEY_ID,
            "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
        }
        self._sleep = sleep
        # one bucket for the three phases: the vendor's limit spans endpoints, not one each
        self._bucket = bucket if bucket is not None else TokenBucket(settings.RATE_LIMIT_RPM, sleep=sleep)
        self.request_counts = dict.fromkeys(PHASES, 0)
        self._logged: set[tuple[str, str]] = set()

    def __enter__(self) -> "AlpacaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def get_json(self, base_url: str, path: str, params: dict, phase: str) -> Any:
        warned = False
        attempt = 0
        while True:
            attempt += 1
            request = self._http.build_request(
                "GET", base_url + path, params=params, headers=self._headers
            )
            if (base_url, path) not in self._logged:
                log.info("request: %s", request.url)
                self._logged.add((base_url, path))
            # both inside the loop: a retry is real traffic on the budget the real ingest shares
            self._bucket.acquire()
            # per phase: calendar and symbols run once per run while bars narrows, so one total
            # overstates the per-unit request rate
            self.request_counts[phase] += 1
            try:
                response = self._http.send(request)
            except httpx.TransportError as exc:
                status, why = None, f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        # Decimal keeps prices off the float boundary numeric storage avoids
                        return json.loads(response.content, parse_float=Decimal)
                    except ValueError as exc:
                        # a 200 with a proxy error page or truncation is as transient as a reset
                        status, why = None, f"unparseable body, {exc}"
                elif status in FATAL_STATUSES:
                    raise FatalVendorError(f"{base_url}{path}: vendor returned {status}, the run cannot continue")
                elif status != THROTTLED_STATUS and status not in RETRYABLE_STATUSES:
                    raise UnitFetchError(f"{base_url}{path}: vendor returned {status}")
                else:
                    why = f"status {status}"

            # HTTP_MAX_ATTEMPTS counts total requests, not retries
            if status != THROTTLED_STATUS and attempt >= settings.HTTP_MAX_ATTEMPTS:
                raise UnitFetchError(
                    f"{base_url}{path}: {why} after {attempt} attempts"
                )

            delay = backoff_delay(attempt, random.random())
            if not warned:
                # 429 retries forever, so without this a stalled unattended run reads as progress
                log.warning(
                    "retrying %s%s: %s, attempt %d, waiting %.1fs", base_url, path, why, attempt, delay
                )
                warned = True
            self._sleep(delay)


def next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def month_window(month: date) -> tuple[str, str]:
    last = next_month(month) - timedelta(days=1)
    # both bounds are inclusive: the next month's first instant drags in its 20:00 ET print
    return f"{month:%Y-%m}-01T00:00:00Z", f"{last:%Y-%m-%d}T23:59:59Z"


def fetch_bars(client, symbol: str, month: date) -> list[Bar]:
    start, end = month_window(month)
    params = {
        "symbols": symbol,
        "timeframe": "1Min",
        "start": start,
        "end": end,
        "feed": settings.ALPACA_FEED,
        "adjustment": settings.ALPACA_ADJUSTMENT,
        "limit": settings.ALPACA_LIMIT,
    }

    bars: list[Bar] = []
    token: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        page_params = params if token is None else params | {"page_token": token}
        body = client.get_json(BARS_HOST, BARS_PATH, page_params, "bars")
        if not isinstance(body, dict):
            # calendar and assets answer lists, so the mapping check belongs here, not in get_json
            raise UnitFetchError(f"{symbol} {month:%Y-%m}: bars endpoint returned {type(body).__name__}, not an object")
        # a holiday answers {"bars":{}}: indexing it raises KeyError on closed days in seven years
        bars.extend(_parse(symbol, (body.get("bars") or {}).get(symbol) or []))
        token = body.get("next_page_token")
        if not token:
            return bars
        if token in seen:
            # a non-advancing token would loop an unattended run forever rather than fail
            raise UnitFetchError(f"{symbol} {month:%Y-%m}: page token repeated, pagination is not advancing")
        seen.add(token)

    # an endlessly advancing cursor clears the repeat check, so page count bounds the request budget
    raise UnitFetchError(
        f"{symbol} {month:%Y-%m}: still paginating after {MAX_PAGES} pages, the cursor is not terminating"
    )


def _parse(symbol: str, rows: list[dict]) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=datetime.fromisoformat(row["t"]),
            open=Decimal(row["o"]),
            high=Decimal(row["h"]),
            low=Decimal(row["l"]),
            close=Decimal(row["c"]),
            volume=row["v"],
            trade_count=row["n"],
            vwap=Decimal(row["vw"]),
        )
        for row in rows
    ]


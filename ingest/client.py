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

# the limit is self-inflicted and clears on its own, so it is the one status exempt from the attempt budget
THROTTLED_STATUS = 429
RETRYABLE_STATUSES = (500, 502, 503, 504)
# retrying cannot help any of these, and one root cause would otherwise be logged once per unit across the whole run
FATAL_STATUSES = (400, 401, 403)


class FatalVendorError(RuntimeError):
    """A status no retry can clear: the run stops."""


class UnitFetchError(RuntimeError):
    """A failure confined to one (ticker, month) unit: the run logs it and continues."""


# a ticker-month is one page on iex and two on sip at limit=10000, so this bounds only a cursor that will not terminate
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
        # one bucket shared across the three phases, because the vendor grants its limit across endpoints rather than one per endpoint
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
            # the acquire and the count both sit inside the loop: a retry is real vendor traffic against a budget shared with the real ingest
            self._bucket.acquire()
            # counted per phase: the calendar and symbols phases run once per invocation however the bars phase is narrowed, so one combined total overstates the per-unit request rate.
            self.request_counts[phase] += 1
            try:
                response = self._http.send(request)
            except httpx.TransportError as exc:
                status, why = None, f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        # Decimal keeps prices off the float boundary that numeric storage exists to avoid.
                        return json.loads(response.content, parse_float=Decimal)
                    except ValueError as exc:
                        # a 200 carrying a proxy error page or a truncated payload is as transient as a reset, so it retries on the same budget rather than leaving the policy unclassified
                        status, why = None, f"unparseable body, {exc}"
                elif status in FATAL_STATUSES:
                    raise FatalVendorError(f"{base_url}{path}: vendor returned {status}, the run cannot continue")
                elif status != THROTTLED_STATUS and status not in RETRYABLE_STATUSES:
                    raise UnitFetchError(f"{base_url}{path}: vendor returned {status}")
                else:
                    why = f"status {status}"

            # HTTP_MAX_ATTEMPTS counts total requests rather than retries after a first try
            if status != THROTTLED_STATUS and attempt >= settings.HTTP_MAX_ATTEMPTS:
                raise UnitFetchError(
                    f"{base_url}{path}: {why} after {attempt} attempts"
                )

            delay = backoff_delay(attempt, random.random())
            if not warned:
                # 429 retries forever by design, so without this the last line of an unattended run stays an ordinary progress line while nothing advances
                log.warning(
                    "retrying %s%s: %s, attempt %d, waiting %.1fs", base_url, path, why, attempt, delay
                )
                warned = True
            self._sleep(delay)


def next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def month_window(month: date) -> tuple[str, str]:
    last = next_month(month) - timedelta(days=1)
    # both bounds are inclusive, so an end at the next month's first instant would drag its 20:00 ET print into this unit.
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
            # the calendar and assets endpoints answer with a list, so get_json cannot demand a mapping and this is the only place that can
            raise UnitFetchError(f"{symbol} {month:%Y-%m}: bars endpoint returned {type(body).__name__}, not an object")
        # a holiday answers {"bars":{}}, so indexing straight into the symbol raises KeyError on every closed day of a seven-year run.
        bars.extend(_parse(symbol, (body.get("bars") or {}).get(symbol) or []))
        token = body.get("next_page_token")
        if not token:
            return bars
        if token in seen:
            # a token that stops advancing would loop forever against an unattended background run rather than fail
            raise UnitFetchError(f"{symbol} {month:%Y-%m}: page token repeated, pagination is not advancing")
        seen.add(token)

    # a cursor that keeps advancing without ending clears the repeat check, so the page count is what bounds the request budget
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


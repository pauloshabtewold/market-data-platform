import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

PHASES = ("calendar", "symbols", "bars")

BARS_HOST = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"


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

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._headers = {
            "APCA-API-KEY-ID": settings.ALPACA_KEY_ID,
            "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
        }
        self.request_counts = dict.fromkeys(PHASES, 0)
        self._logged: set[tuple[str, str]] = set()

    def __enter__(self) -> "AlpacaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def get_json(self, base_url: str, path: str, params: dict, phase: str) -> Any:
        # counted per phase: the calendar and symbols phases run once per invocation however the bars phase is narrowed, so one combined total overstates the per-unit request rate.
        self.request_counts[phase] += 1
        request = self._http.build_request(
            "GET", base_url + path, params=params, headers=self._headers
        )
        if (base_url, path) not in self._logged:
            log.info("request: %s", request.url)
            self._logged.add((base_url, path))
        response = self._http.send(request)
        response.raise_for_status()
        # Decimal keeps prices off the float boundary that numeric storage exists to avoid.
        return json.loads(response.content, parse_float=Decimal)


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
    while True:
        page_params = params if token is None else params | {"page_token": token}
        body = client.get_json(BARS_HOST, BARS_PATH, page_params, "bars")
        # a holiday answers {"bars":{}}, so indexing straight into the symbol raises KeyError on every closed day of a seven-year run.
        bars.extend(_parse(symbol, (body.get("bars") or {}).get(symbol) or []))
        token = body.get("next_page_token")
        if not token:
            return bars


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


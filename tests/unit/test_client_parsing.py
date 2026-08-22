import json
import logging
from datetime import date
from decimal import Decimal

import httpx

from ingest.client import BARS_HOST, BARS_PATH, AlpacaClient, fetch_bars, month_window


class StubClient:
    """Replays recorded pages so the suite never spends the shared request budget."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.params = []

    def get_json(self, base_url, path, params, phase):
        self.params.append(params)
        assert phase == "bars"
        return self._pages.pop(0)


def _page(fixtures_dir, name):
    # parsed the way get_json parses, so the stub cannot hand the fetcher a float the transport would never produce
    return json.loads((fixtures_dir / name).read_text(), parse_float=Decimal)


def test_a_page_token_is_followed_and_never_sent_empty(fixtures_dir):
    client = StubClient(
        [_page(fixtures_dir, "bars_page1.json"), _page(fixtures_dir, "bars_empty.json")]
    )
    bars = fetch_bars(client, "AAPL", date(2026, 6, 1))

    assert len(bars) == 3
    # an empty page_token= has never been exercised against this vendor, so the first request omits the parameter entirely
    assert "page_token" not in client.params[0]
    assert client.params[1]["page_token"] == "QUFQTHxNfDE3ODAzMjAxMjAwMDAwMDAwMDA="


def test_pages_concatenate_in_order_and_stop_on_a_null_token(fixtures_dir):
    client = StubClient(
        [_page(fixtures_dir, "bars_page1.json"), _page(fixtures_dir, "bars_page2.json")]
    )
    bars = fetch_bars(client, "AAPL", date(2026, 6, 1))

    assert len(client.params) == 2
    assert [b.ts.isoformat() for b in bars] == [
        "2026-06-01T12:14:00+00:00",
        "2026-06-01T12:24:00+00:00",
        "2026-06-01T12:57:00+00:00",
        "2026-06-01T13:22:00+00:00",
        "2026-06-01T13:23:00+00:00",
        "2026-06-01T13:30:00+00:00",
    ]
    assert all(b.ts.tzinfo is not None for b in bars)
    assert all(isinstance(b.open, Decimal) for b in bars)
    assert bars[0].close == Decimal("310.13")
    assert bars[-1].vwap == Decimal("309.407967")
    # the recorded high arrives as a JSON integer, which parse_float never sees
    assert bars[-1].high == Decimal("310")
    assert (bars[-1].volume, bars[-1].trade_count) == (16799, 399)


def test_a_holiday_payload_yields_no_bars_rather_than_raising(fixtures_dir):
    client = StubClient([_page(fixtures_dir, "bars_empty.json")])
    assert fetch_bars(client, "AAPL", date(2025, 11, 1)) == []


def test_the_month_window_ends_inside_the_month():
    assert month_window(date(2026, 6, 1)) == (
        "2026-06-01T00:00:00Z",
        "2026-06-30T23:59:59Z",
    )
    assert month_window(date(2026, 12, 1))[1] == "2026-12-31T23:59:59Z"
    assert month_window(date(2024, 2, 1))[1] == "2024-02-29T23:59:59Z"


def test_the_transport_parses_prices_without_crossing_a_float(fixtures_dir, caplog):
    pages = [(fixtures_dir / n).read_bytes() for n in ("bars_page1.json", "bars_page2.json")]
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=pages.pop(0)))
    )
    client = AlpacaClient(http=http)

    with caplog.at_level(logging.INFO, logger="ingest.client"):
        bars = fetch_bars(client, "AAPL", date(2026, 6, 1))[:3]

    assert [b.close for b in bars] == [Decimal("310.13"), Decimal("310.44"), Decimal("310.86")]
    assert client.request_counts["bars"] == 2

    # this one logged line is the only record that the whole adjustment list reached the vendor
    logged = [m for m in caplog.messages if m.startswith("request: " + BARS_HOST + BARS_PATH + "?")]
    assert len(logged) == 1
    assert "feed=iex" in logged[0]
    assert "limit=10000" in logged[0]
    assert "adjustment=split%2Cspin-off" in logged[0]
    assert "APCA-API" not in logged[0]

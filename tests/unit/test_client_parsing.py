import json
import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest
from urllib.parse import quote

from config import settings
from ingest.client import (
    BARS_HOST,
    BARS_PATH,
    MAX_PAGES,
    AlpacaClient,
    FatalVendorError,
    UnitFetchError,
    fetch_bars,
    month_window,
)


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


def test_a_403_raises_fatal_vendor_error_rather_than_reaching_the_parser():
    # an error body carries no "bars" key, so one that reaches the parser reads as a month with no data and gets checkpointed as a completed unit
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, content=b'{"message":"no."}'))
    )
    client = AlpacaClient(http=http, sleep=lambda delay: None)

    with pytest.raises(FatalVendorError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")


def test_a_500_raises_unit_fetch_error_rather_than_reaching_the_parser():
    # an error body carries no "bars" key, so one that reaches the parser reads as a month with no data and gets checkpointed as a completed unit
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, content=b'{"message":"no."}'))
    )
    client = AlpacaClient(http=http, sleep=lambda delay: None)

    with pytest.raises(UnitFetchError):
        client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars")


def test_a_429_is_retried_past_rather_than_raised_and_reaches_the_parser_once_it_clears():
    # 429 retries indefinitely by design, so a transport that never stopped refusing would hang the suite rather than fail it
    statuses = [429, 429]

    def send(request):
        if statuses:
            return httpx.Response(statuses.pop(0), content=b'{"message":"no."}')
        return httpx.Response(200, content=b'{"bars": {}}')

    client = AlpacaClient(http=httpx.Client(transport=httpx.MockTransport(send)), sleep=lambda delay: None)

    assert client.get_json(BARS_HOST, BARS_PATH, {"symbols": "AAPL"}, "bars") == {"bars": {}}


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
    # the payload nests bars under the symbol rather than repeating it per row, so the parser supplies it and nothing else would notice it going missing until the NOT NULL primary key
    assert all(b.symbol == "AAPL" for b in bars)
    assert bars[0].close == Decimal("310.13")
    assert bars[-1].vwap == Decimal("309.407967")
    # the only recorded bar whose four prices differ, so this is what distinguishes a correct mapping from a transposed one
    assert (bars[-1].open, bars[-1].high, bars[-1].low, bars[-1].close) == (
        Decimal("309.6"),
        Decimal("310"),
        Decimal("308.55"),
        Decimal("309.71"),
    )
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


def test_fetch_bars_requests_the_symbol_the_1min_timeframe_and_the_month_window(fixtures_dir):
    client = StubClient([_page(fixtures_dir, "bars_empty.json")])
    start, end = month_window(date(2026, 6, 1))

    fetch_bars(client, "AAPL", date(2026, 6, 1))

    params = client.params[0]
    assert params["symbols"] == "AAPL"
    assert params["timeframe"] == "1Min"
    assert params["start"] == start
    assert params["end"] == end


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
    assert f"feed={settings.ALPACA_FEED}" in logged[0]
    assert f"limit={settings.ALPACA_LIMIT}" in logged[0]
    assert f"adjustment={quote(settings.ALPACA_ADJUSTMENT, safe='')}" in logged[0]
    assert "APCA-API" not in logged[0]


def test_a_token_that_stops_advancing_raises_rather_than_looping(fixtures_dir):
    stuck = _page(fixtures_dir, "bars_page1.json")
    client = StubClient([stuck, stuck, stuck])

    with pytest.raises(RuntimeError, match="not advancing"):
        fetch_bars(client, "AAPL", date(2026, 6, 1))


def test_the_page_cap_leaves_room_above_the_widest_correct_response():
    # the test below asserts pages == MAX_PAGES, so it holds under any cap; a ticker-month is one page on iex and two on sip at limit=10000, and the feed comparison followed sip to its second page
    assert MAX_PAGES > 2


def test_a_cursor_that_keeps_advancing_is_bounded_by_the_page_cap(fixtures_dir):
    page = _page(fixtures_dir, "bars_page1.json")

    class EverAdvancing:
        """A cursor that moves on every page and never returns null clears the repeat check."""

        def __init__(self):
            self.pages = 0

        def get_json(self, base_url, path, params, phase):
            self.pages += 1
            if self.pages > MAX_PAGES:
                # an unbounded fetcher would hang this test rather than fail it, and a hung suite reports nothing
                raise AssertionError(f"fetch_bars asked for page {self.pages}, past the {MAX_PAGES} cap")
            return page | {"next_page_token": f"tok-{self.pages}"}

    client = EverAdvancing()
    with pytest.raises(RuntimeError, match="not terminating"):
        fetch_bars(client, "AAPL", date(2026, 6, 1))

    assert client.pages == MAX_PAGES


def test_a_bars_payload_that_is_not_an_object_fails_the_unit_rather_than_raising_attribute_error():
    # the calendar and assets endpoints answer with a list, so get_json cannot demand a mapping and this caller is the only place that can
    client = StubClient([[{"t": "2026-06-01T14:30:00Z"}]])

    with pytest.raises(UnitFetchError):
        fetch_bars(client, "AAPL", date(2026, 6, 1))

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.errors import INVALID_RANGE_MESSAGE
from api.main import create_app
from api.pagination import BARS_CURSOR, decode_cursor, encode_cursor
from config import settings
from db.session import connect
from tests.market_fixture import load

# load(extended_hours=True) writes 7 bars per session over 4 TRADING_DAYS -- the five in-session
# bars plus one 30 minutes before the open and one exactly on close_ts
ALL_BARS = 28
WITHOUT_THE_FIRST_SESSION = 21


def _seed_symbol_with_no_bars(dsn: str, symbol: str) -> None:
    # a per-file helper: market_fixture's load_symbol always writes bars, and a symbol that is
    # ingested but has none is the only thing that separates an existence check against symbols
    # from one against bars
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES (%s, %s, 'X', true, NULL)",
            (symbol, symbol),
        )
        conn.commit()


@pytest.fixture
def client(migrated_dsn):
    load(migrated_dsn, extended_hours=True)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def test_a_window_returns_the_bars_in_it_and_nothing_outside_it(client):
    # TWO windows, because neither bound is killable at the other's window. The fixture's earliest
    # bar is 14:00 on 2026-03-06 and the window's low bound is that day's UTC midnight, so at the
    # first window every bar is above the lower bound and deleting it changes nothing
    wide = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-06", "end": "2026-03-12", "limit": 50}
    ).json()
    # the upper bound as a bare date would resolve to 2026-03-12's UTC midnight and drop that
    # session entirely: 28 becomes 21
    assert len(wide["data"]) == ALL_BARS
    assert "next_cursor" in wide and wide["next_cursor"] is None

    narrow = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-09", "end": "2026-03-12", "limit": 50}
    ).json()
    # this window is the only one at which removing `ts >= lo` changes an answer: 21 becomes 28
    assert len(narrow["data"]) == WITHOUT_THE_FIRST_SESSION
    assert "next_cursor" in narrow and narrow["next_cursor"] is None
    assert all(row["ts"] >= "2026-03-09" for row in narrow["data"])


def test_the_second_page_starts_after_the_first_pages_last_bar_and_the_cursor_names_it(client):
    # limit=10 and not the default: paginate returns early whenever the rows fit inside the limit,
    # so at BARS_PAGE_DEFAULT=1000 against 28 bars encode_cursor is never reached at all and the
    # tuple-row defect this test exists for is invisible
    first = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-06", "end": "2026-03-12", "limit": 10}
    ).json()
    assert len(first["data"]) == 10
    assert isinstance(first["next_cursor"], str) and first["next_cursor"]
    # the cursor names the LAST RETURNED row, never the probe row that proved a page 2 exists
    last_ts = first["data"][-1]["ts"]
    assert decode_cursor(BARS_CURSOR, first["next_cursor"])["ts"] == datetime.fromisoformat(last_ts)

    second = client.get(
        "/symbols/AAA/bars",
        params={
            "start": "2026-03-06",
            "end": "2026-03-12",
            "limit": 10,
            "cursor": first["next_cursor"],
        },
    ).json()
    assert len(second["data"]) == 10
    assert all(row["ts"] > last_ts for row in second["data"])


def test_start_and_end_are_required_and_omitting_both_names_both(client):
    response = client.get("/symbols/AAA/bars")
    assert response.status_code == 400
    detail = response.json()["error"]["detail"]
    # both, which is what the additive errors list bought: the three first-error keys can only
    # ever name one of the two
    assert detail["errors"] == [
        {"parameter": "start", "location": "query", "type": "missing"},
        {"parameter": "end", "location": "query", "type": "missing"},
    ]


def test_a_cursor_outside_the_requested_window_is_a_four_hundred(client):
    outside = encode_cursor(BARS_CURSOR, {"ts": datetime.fromisoformat("2024-01-02T14:30:00+00:00")})
    response = client.get(
        "/symbols/AAA/bars",
        params={"start": "2026-03-06", "end": "2026-03-12", "cursor": outside},
    )
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["reason"] == "cursor_outside_window"

    # 400 BEATS 404: NOSUCH is not seeded, and the answer is still the cursor's 400 because the
    # existence check needs a connection and none is acquired until validation has returned
    beats_404 = client.get(
        "/symbols/NOSUCH/bars",
        params={"start": "2026-03-06", "end": "2026-03-12", "cursor": "!!!!"},
    )
    assert beats_404.status_code == 400
    assert beats_404.json()["error"]["code"] == "invalid_cursor"
    assert beats_404.json()["error"]["detail"]["reason"] == "not_base64"

    # 400 BEATS 422, and this looks wrong on purpose: a cursor is decoded before the window is
    # range-validated, so an inverted window with a cursor answers cursor_outside_window rather
    # than start_after_end. Spec line 507 puts params ahead of range semantics -- do not "fix" it.
    # The same window WITHOUT a cursor is the 422 the sibling test below asserts
    inverted = encode_cursor(BARS_CURSOR, {"ts": datetime.fromisoformat("2026-03-09T14:00:00+00:00")})
    beats_422 = client.get(
        "/symbols/AAA/bars",
        params={"start": "2026-03-12", "end": "2026-03-06", "cursor": inverted},
    )
    assert beats_422.status_code == 400
    assert beats_422.json()["error"]["detail"]["reason"] == "cursor_outside_window"

    # THE OTHER BOUND. Every case above sits below `start`, so all three pass with `end`
    # dropped from the decode_cursor call -- and a cursor past the window would then be paged
    # from instead of refused. decode_cursor resolves each bound from its own date alone, so
    # this is the only assertion that reaches the second one from an endpoint
    later = encode_cursor(
        BARS_CURSOR, {"ts": datetime.fromisoformat("2026-04-01T14:30:00+00:00")}
    )
    beyond = client.get(
        "/symbols/AAA/bars",
        params={"start": "2026-03-06", "end": "2026-03-12", "cursor": later},
    )
    assert beyond.status_code == 400
    assert beyond.json()["error"]["detail"]["reason"] == "cursor_outside_window"


def test_an_inverted_window_is_a_four_twenty_two_naming_start_after_end(client):
    # no cursor, deliberately: with one the request is a 400 by design and cannot see this rule
    response = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-12", "end": "2026-03-06"}
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_range"
    # the whole dict and not a subset: an extra key is a wire change this catches and a subset
    # comparison would not. With no rule at all the answer is 200 {"data": [], ...}
    assert body["detail"] == {"reason": "start_after_end"}
    # the message and not only the code: ApiError carries it to error.message on the wire, and a
    # raise site that passed None would publish a null message against a green suite
    assert body["message"] == INVALID_RANGE_MESSAGE
    # and a single-day window stays legal, which is what stops the rule being written as >=
    assert (
        client.get(
            "/symbols/AAA/bars", params={"start": "2026-03-06", "end": "2026-03-06"}
        ).status_code
        == 200
    )
    # the LOW edge of the limit rule, which is the half paginate's own floor cannot reach: the
    # resolved limit is checked as 1 <= limit, and both ways of writing that one higher make a
    # legal ?limit=1 a 400 while every other limit in this suite is 10 or 50
    one = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-06", "end": "2026-03-12", "limit": 1}
    )
    assert one.status_code == 200
    assert len(one.json()["data"]) == 1


def test_a_window_outside_the_ingested_range_is_refused_with_the_bounds(client):
    response = client.get(
        "/symbols/AAA/bars", params={"start": "2019-01-01", "end": "2019-03-01"}
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_range"
    # ISO STRINGS and not date objects: settings holds these as `date`, and a date in a detail is
    # a TypeError inside the error handler, so the client would get 500 internal instead of this
    assert body["detail"] == {
        "reason": "outside_ingested_range",
        "min": "2020-08-01",
        "max": "2026-06-30",
    }
    assert body["message"] == INVALID_RANGE_MESSAGE
    # BOTH edges are legal, which is what stops either bound being written with an equals: the
    # rule is start < INGEST_START or end > INGEST_END, and this test drove only the outside of
    # the low end. Read off settings rather than retyped -- the literals above already pin the
    # values, so this pins the COMPARISON without a second copy to keep in step
    edges = client.get(
        "/symbols/AAA/bars",
        params={
            "start": settings.INGEST_START.isoformat(),
            "end": settings.INGEST_END.isoformat(),
            "limit": 50,
        },
    )
    assert edges.status_code == 200


def test_a_window_wider_than_the_aggregation_cap_is_served_here(client):
    # 180 days, twice AGG_MAX_WINDOW_DAYS: that cap bounds the aggregating endpoints alone, and
    # wiring it to /bars would make the deep-pagination measurement unrunnable at Feature 7
    response = client.get(
        "/symbols/AAA/bars", params={"start": "2026-01-01", "end": "2026-06-30", "limit": 50}
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == ALL_BARS


def test_the_wire_types_are_numbers_for_prices_and_iso_strings_for_instants(client):
    data = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-09", "end": "2026-03-09", "limit": 50}
    ).json()["data"]
    session = [row for row in data if row["ts"] == "2026-03-09T13:30:00+00:00"]
    assert len(session) == 1
    row = session[0]

    # JSON numbers and never strings, which is the decision -- pinned here so a later switch to
    # string-serialised Decimals is deliberate rather than a drift.
    # The stored numeric scale is lost, and it is lost in BOTH directions: bars.open is an
    # unqualified numeric, so a whole-dollar price is stored as Decimal("110") and reaches the
    # wire as the JSON integer 110, while a fractional one becomes the float 110.5. A client
    # must read these as numbers, not as floats
    for field in ("open", "high", "low", "close", "vwap"):
        assert type(row[field]) in (int, float), (field, row[field])
    # `is int` and not `== 110`, which 110.0 also satisfies: the integer is the whole subject of
    # this assertion, and an encoder that started emitting 110.0 would leave `== 110` green while
    # breaking every client written against the type. The two prices are asserted as a PAIR
    # because it is the disagreement between them, inside one response, that a client trips on
    assert type(row["open"]) is int
    assert row["open"] == 110
    assert row["close"] == 110.5
    assert type(row["close"]) is float
    for field in ("volume", "trade_count"):
        assert type(row[field]) is int, field

    # and the instant is the same string form the cursor carries, so a row's ts and a decoded
    # next_cursor agree byte for byte
    assert isinstance(row["ts"], str)
    assert row["ts"] == "2026-03-09T13:30:00+00:00"


def test_an_unknown_symbol_is_a_four_oh_four_and_a_known_symbol_with_no_rows_is_two_hundred(
    client, migrated_dsn
):
    # inside the fixture's window but containing none of its four trading days
    empty = client.get(
        "/symbols/AAA/bars", params={"start": "2026-03-02", "end": "2026-03-04"}
    )
    assert empty.status_code == 200
    assert empty.json()["data"] == []
    assert empty.json()["next_cursor"] is None

    # and the case that separates an existence check against symbols from one against bars: AAA
    # has rows outside this window, so a bars-based check still finds one and answers 200 here.
    # NIL is ingested and has no bars at all, where a bars-based check answers 404 and collapses
    # the spec's own distinction between an unknown symbol and an empty window
    _seed_symbol_with_no_bars(migrated_dsn, "NIL")
    barren = client.get(
        "/symbols/NIL/bars", params={"start": "2026-03-06", "end": "2026-03-12"}
    )
    assert barren.status_code == 200
    assert barren.json()["data"] == []
    assert barren.json()["next_cursor"] is None

    # lower case, and the detail echoes the string the client sent: symbols.symbol is a
    # case-sensitive text primary key and the path is never normalised on the way in
    missing = client.get(
        "/symbols/aapl/bars", params={"start": "2026-03-06", "end": "2026-03-12"}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "unknown_symbol"
    assert missing.json()["error"]["detail"] == {"reason": "unknown_symbol", "symbol": "aapl"}

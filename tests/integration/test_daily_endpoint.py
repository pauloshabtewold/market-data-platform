import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.pagination import DAILY_CURSOR, decode_cursor
from db.session import connect
from tests.market_fixture import HOLIDAY, TRADING_DAYS, expected_rollup, load

TRADING_DAY_STRINGS = [day.isoformat() for day in TRADING_DAYS]


def _insert_bar_on(dsn: str, symbol: str, stamp) -> None:
    # a per-file helper: market_fixture writes bars only on the four trading days, so without this
    # the holiday has no bar either and the test could not tell "absent from market_days" from
    # "no data at all"
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES (%s, %s, 500, 500, 500, 500, 900, 1, 500)",
            (symbol, stamp),
        )
        conn.commit()


@pytest.fixture
def client(migrated_dsn):
    load(migrated_dsn, extended_hours=True)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def test_a_page_rolls_up_the_same_values_the_committed_query_produces(client):
    body = client.get(
        "/symbols/AAA/daily", params={"start": "2026-03-01", "end": "2026-03-31", "limit": 50}
    ).json()
    assert [row["day"] for row in body["data"]] == TRADING_DAY_STRINGS
    # present and explicitly null on the last page, never omitted
    assert "next_cursor" in body and body["next_cursor"] is None

    # the wrapper's outer SELECT drops the rollup's own symbol column and keeps the rest in order
    assert list(body["data"][0]) == ["day", "open", "high", "low", "close", "volume", "bars"]

    for day, row in zip(TRADING_DAYS, body["data"]):
        # against the hand-computed fixture rollup, never against the query's own output
        want = expected_rollup(day)
        for field in ("open", "high", "low", "close", "volume", "bars"):
            assert float(row[field]) == float(want[field]), (day, field)
        # the two extended-hours bars sit outside the half-open session, so a session that
        # counted them would read 7 rather than 5
        assert row["bars"] == 5


def test_the_second_page_starts_at_the_day_after_the_cursor(client):
    # limit=2 and not the default: a legal window yields at most 65 trading days against
    # AGG_PAGE_DEFAULT=100, so a paging test written at the default limit cannot fail
    first = client.get(
        "/symbols/AAA/daily", params={"start": "2026-03-01", "end": "2026-03-31", "limit": 2}
    ).json()
    assert [row["day"] for row in first["data"]] == TRADING_DAY_STRINGS[:2]
    assert decode_cursor(DAILY_CURSOR, first["next_cursor"]) == {"day": TRADING_DAYS[1]}

    second = client.get(
        "/symbols/AAA/daily",
        params={
            "start": "2026-03-01",
            "end": "2026-03-31",
            "limit": 2,
            "cursor": first["next_cursor"],
        },
    ).json()
    # the page narrows the committed query's own :start to cursor_day + 1 day; narrowing to
    # cursor_day itself would repeat the boundary day on every page.
    # NOTE this test does not discriminate the narrowing from an outer WHERE day > cursor -- both
    # return these rows. The block-count comparison is what separates those two
    assert [row["day"] for row in second["data"]] == TRADING_DAY_STRINGS[2:]
    assert second["next_cursor"] is None


def test_the_wrapper_fetches_one_more_row_than_the_limit(client):
    # three of the four trading days: a fetch of limit rather than limit + 1 returns exactly three
    # rows, paginate sees them fit inside the limit, and next_cursor goes null on a page that has
    # a successor -- which no row count in this suite would notice
    body = client.get(
        "/symbols/AAA/daily", params={"start": "2026-03-01", "end": "2026-03-31", "limit": 3}
    ).json()
    assert len(body["data"]) == 3
    assert body["next_cursor"] is not None


def test_the_holiday_absent_from_market_days_produces_no_row(client, migrated_dsn):
    # a real bar on the holiday, so what is being tested is the join to market_days rather than
    # the absence of data. 2026-03-11 is deliberately not in TRADING_DAYS and so has no session
    _insert_bar_on(migrated_dsn, "AAA", f"{HOLIDAY.isoformat()} 14:30:00+00")

    body = client.get(
        "/symbols/AAA/daily", params={"start": "2026-03-01", "end": "2026-03-31", "limit": 50}
    ).json()
    assert HOLIDAY.isoformat() not in [row["day"] for row in body["data"]]
    assert [row["day"] for row in body["data"]] == TRADING_DAY_STRINGS


def test_a_window_of_exactly_the_cap_is_legal(client):
    # exactly AGG_MAX_WINDOW_DAYS, and it has to stay legal: it is the length of the window the
    # published Class A measurement is taken at. These dates rather than the published ones
    # because they contain all four of the fixture's trading days
    response = client.get(
        "/symbols/AAA/daily", params={"start": "2026-03-01", "end": "2026-05-30"}
    )
    assert response.status_code == 200
    assert [row["day"] for row in response.json()["data"]] == TRADING_DAY_STRINGS


def test_a_window_one_day_over_the_cap_is_refused_with_the_bound_and_the_value(client):
    response = client.get(
        "/symbols/AAA/daily", params={"start": "2026-02-28", "end": "2026-05-30"}
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_range"
    # the bound AND the value, so the client is shown the number that failed the check
    assert body["detail"] == {"reason": "window_too_long", "max_days": 90, "requested_days": 91}


def test_an_unknown_symbol_on_daily_is_a_four_oh_four(client):
    # a window legal in every rule, so the 404 is what is being measured: 30 days, inside the
    # ingested range, start before end. Every other /daily precedence case refuses inside the
    # validation tier before a connection exists, so an existence check written on /bars alone
    # would pass all of them and answer 200 with an empty list here
    response = client.get(
        "/symbols/NOSUCH/daily", params={"start": "2026-03-01", "end": "2026-03-31"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_symbol"
    assert response.json()["error"]["detail"] == {"reason": "unknown_symbol", "symbol": "NOSUCH"}


def test_a_limit_above_the_aggregating_cap_names_the_aggregating_cap(client):
    response = client.get(
        "/symbols/AAA/daily",
        params={"start": "2026-03-01", "end": "2026-03-31", "limit": 5000},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_params"
    # 1000 is the whole point: under the raw-row pair 5000 is below BARS_PAGE_MAX=10000 and this
    # same request is a 200, so a constant read correctly and handed to the wrong call site is
    # visible nowhere else in this feature
    assert body["detail"] == {"reason": "limit_out_of_range", "limit": 5000, "max": 1000}
    assert (
        client.get(
            "/symbols/AAA/daily",
            params={"start": "2026-03-01", "end": "2026-03-31", "limit": 1000},
        ).status_code
        == 200
    )

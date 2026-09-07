import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.pagination import SYMBOLS_CURSOR, decode_cursor
from db.session import connect
from tests.market_fixture import load

SEEDED = ("AAA", "BBB", "CCC")
# written out of alphabetical order on purpose: heap order is insert order on a fresh table,
# and a missing ORDER BY is unobservable while the two agree
_INSERT_ORDER = ("CCC", "AAA", "BBB")


def _set_active(dsn: str, symbol: str, flag: bool) -> None:
    # a per-file helper rather than a change to market_fixture: load_symbol hardcodes active=true
    # with no ON CONFLICT, and that module is imported by twenty test files
    with connect(dsn) as conn:
        conn.execute("UPDATE symbols SET active = %s WHERE symbol = %s", (flag, symbol))
        conn.commit()


@pytest.fixture
def client(migrated_dsn):
    load(migrated_dsn, symbols=_INSERT_ORDER)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def test_the_first_page_starts_at_the_alphabetically_first_symbol(client):
    body = client.get("/symbols", params={"limit": 2}).json()
    assert [row["symbol"] for row in body["data"]] == ["AAA", "BBB"]
    # every column, because active is a documented filter and a filter on a field the response
    # hides is not usable
    assert set(body["data"][0]) == {"symbol", "name", "exchange", "active", "first_bar_ts"}


def test_the_second_page_starts_after_the_first_pages_last_symbol(client):
    first = client.get("/symbols", params={"limit": 2}).json()
    assert decode_cursor(SYMBOLS_CURSOR, first["next_cursor"]) == {"symbol": "BBB"}

    second = client.get("/symbols", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    # strictly after, never including: a >= bound would repeat BBB on every page
    assert [row["symbol"] for row in second["data"]] == ["CCC"]
    assert second["next_cursor"] is None


def test_the_last_page_carries_next_cursor_explicitly_null(client):
    body = client.get("/symbols", params={"limit": 10}).json()
    assert len(body["data"]) == 3
    # present and explicitly null, never omitted: .get() would pass on a missing key, which is the
    # whole failure mode a response model with exclude_none would produce
    assert "next_cursor" in body
    assert body["next_cursor"] is None


def test_the_active_filter_selects_and_excludes(client, migrated_dsn):
    _set_active(migrated_dsn, "BBB", False)

    assert [r["symbol"] for r in client.get("/symbols", params={"active": True}).json()["data"]] == [
        "AAA",
        "CCC",
    ]
    assert [
        r["symbol"] for r in client.get("/symbols", params={"active": False}).json()["data"]
    ] == ["BBB"]
    # and with no filter at all every row comes back. This is the assertion that kills dropping
    # the IS NULL branch, which turns an omitted filter into `active = NULL` and returns nothing,
    # and dropping the ::boolean cast, which makes the unfiltered bind a 42P08 and a 500
    assert [r["symbol"] for r in client.get("/symbols").json()["data"]] == list(SEEDED)


def test_a_limit_above_the_cap_names_the_cap_and_the_value(client):
    response = client.get("/symbols", params={"limit": 10001})
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_params"
    # 10000 and not 1000: spec line 508 classes /symbols with /bars as a raw-row endpoint, so a
    # swap to the aggregating pair fails here by value
    assert body["detail"] == {"reason": "limit_out_of_range", "limit": 10001, "max": 10000}
    # and the cap itself is legal, which is what stops the check being written as >=
    assert client.get("/symbols", params={"limit": 10000}).status_code == 200

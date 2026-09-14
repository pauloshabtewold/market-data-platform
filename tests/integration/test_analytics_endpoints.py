import re
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api.routes
from api.main import create_app
from api.pagination import BARS_CURSOR, encode_cursor
from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    DST_FRIDAY_EST,
    TRADING_DAYS,
    bar_ts,
    ensure_partition,
    load,
    load_flat_symbol,
    load_run_calendar,
    load_run_symbol,
    load_sparse_symbol,
    run_days,
)

# six consecutive months so the bound query has partitions on both sides of after_ts to prune
MONTHS = [date(2026, month, 1) for month in range(1, 7)]


def _six_months_of_partitions(dsn: str) -> str:
    with connect(dsn) as conn:
        for month in MONTHS:
            ensure_partition(conn, month)
        conn.commit()
    return dsn


def _walk_largest_moves(client, base_params, *, max_iterations=100):
    """Follow next_cursor at limit=1 until it is null, bounded so a paging defect fails fast."""
    collected = []
    params = dict(base_params, limit=1)
    for _ in range(max_iterations):
        body = client.get("/analytics/largest-moves", params=params).json()
        collected.extend(body["data"])
        if body["next_cursor"] is None:
            return collected
        params = dict(base_params, limit=1, cursor=body["next_cursor"])
    raise RuntimeError(f"largest-moves paging did not terminate within {max_iterations} iterations")


@pytest.fixture
def volatility_client(migrated_dsn):
    load(migrated_dsn)
    load_flat_symbol(migrated_dsn, "FLAT")
    load_sparse_symbol(migrated_dsn, "SPARSE", TRADING_DAYS, (0, 5, 35, 40, 65))
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def gaps_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def moves_zero_client(migrated_dsn):
    load(migrated_dsn)
    load_flat_symbol(migrated_dsn, "FLAT")
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def moves_tie_client(migrated_dsn):
    load(migrated_dsn, symbols=("AAA", "BBB"))
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def moves_run_client(migrated_dsn):
    days = load_run_calendar(migrated_dsn)
    load_run_symbol(migrated_dsn, "RUN", [100 + i for i in range(len(days))], days)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def window_cap_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def unknown_symbol_client(migrated_dsn):
    load(migrated_dsn)
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


@pytest.fixture
def empty_client(migrated_dsn):
    with TestClient(create_app(migrated_dsn)) as c:
        yield c


def test_volatility_projects_the_five_columns_and_a_flat_series_has_zero_stddev(volatility_client):
    response = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "AAA", "start": "2026-03-01", "end": "2026-03-31"},
    )
    body = response.json()
    assert list(body["data"][0]) == [
        "bucket_minute",
        "returns",
        "avg_minutes_per_return",
        "stddev_pct",
        "annualized_pct",
    ]
    # present and explicitly null: this endpoint takes no cursor and has no page 2
    assert "next_cursor" in body and body["next_cursor"] is None

    flat = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "FLAT", "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    assert flat["data"]
    for row in flat["data"]:
        assert float(row["stddev_pct"]) == 0
        assert float(row["annualized_pct"]) == 0


def test_volatility_buckets_minutes_since_open_rather_than_a_utc_hour(volatility_client):
    response = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "SPARSE", "start": "2026-03-01", "end": "2026-03-31"},
    )
    data = response.json()["data"]

    # hand-computed from the fixture's own prices (100 + minute) at minutes 0, 5, 35, 40, 65: a
    # return's bucket is the LATER bar's minute floored to 30, so minute 5 -> bucket 0, minutes 35
    # and 40 -> bucket 30, minute 65 -> bucket 60. All 4 TRADING_DAYS repeat this identically, which
    # is what proves the bucketing is minutes-since-open: a UTC-hour bucketing would split the
    # DST_FRIDAY_EST session (which opens an hour later in UTC) from the other three
    assert [row["bucket_minute"] for row in data] == [0, 30, 60]
    assert [row["returns"] for row in data] == [4, 8, 4]
    assert [float(row["avg_minutes_per_return"]) for row in data] == [5.00, 17.50, 25.00]
    assert float(data[0]["stddev_pct"]) == 0
    assert float(data[2]["stddev_pct"]) == 0
    # minutes 35 and 40 put two different returns in this bucket in every session, so it is the
    # one bucket whose returns are not all equal
    assert float(data[1]["stddev_pct"]) > 0


def test_gaps_returns_one_row_with_bars_and_one_row_of_zeros_on_an_empty_window(gaps_client):
    response = gaps_client.get(
        "/analytics/gaps", params={"symbol": "AAA", "start": "2026-03-01", "end": "2026-03-31"}
    )
    body = response.json()
    assert len(body["data"]) == 1
    assert list(body["data"][0]) == [
        "gaps",
        "gaps_spanning_a_skipped_session",
        "mean_pct",
        "stddev_pct",
        "min_pct",
        "p25_pct",
        "median_pct",
        "p75_pct",
        "max_pct",
        "gaps_up",
        "gaps_down",
        "gaps_flat",
    ]
    assert "next_cursor" in body and body["next_cursor"] is None

    # a legal window inside the ingested range holding no AAA bars: 03_gaps.sql has no GROUP BY,
    # so it still answers exactly one row -- of zeros and nulls -- rather than an empty list
    empty = gaps_client.get(
        "/analytics/gaps", params={"symbol": "AAA", "start": "2026-04-01", "end": "2026-04-30"}
    ).json()
    assert len(empty["data"]) == 1
    assert empty["data"][0]["gaps"] == 0


def test_largest_moves_at_zero_threshold_includes_every_flat_bar_inclusively(moves_zero_client):
    response = moves_zero_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-03-01", "end": "2026-03-31", "min_move_pct": 0, "limit": 1000},
    )
    body = response.json()
    assert list(body["data"][0]) == ["ts", "symbol", "open", "close", "move_pct"]
    # 20 regular-session AAA bars plus 20 FLAT bars: this is what kills >= rewritten to > -- under
    # > every flat (move_pct == 0) bar disappears and the count would fall by exactly 20
    expected_count = BARS_PER_SESSION * len(TRADING_DAYS) * 2
    assert len(body["data"]) == expected_count
    flat_rows = [row for row in body["data"] if row["symbol"] == "FLAT"]
    assert flat_rows
    assert all(float(row["move_pct"]) == 0 for row in flat_rows)


def test_largest_moves_above_a_threshold_excludes_flat_bars_and_keeps_the_negative_move(
    moves_zero_client,
):
    response = moves_zero_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-03-01", "end": "2026-03-31", "min_move_pct": 3, "limit": 1000},
    )
    body = response.json()
    assert all(row["symbol"] != "FLAT" for row in body["data"])
    assert any(float(row["move_pct"]) < 0 for row in body["data"])

    # pins the PROJECTED value itself rather than only its sign: on the unshifted DST_FRIDAY_EST
    # session _BAR_SHAPE's third bar is open 104, close 96, and round(100 * (96 - 104) / 104, 4)
    # is -7.6923. A mutation dropping the "100 *" factor from the projection alone -- leaving the
    # filter's own "100 *" untouched -- would leave the row count, the sign check and the flat
    # exclusion above all unchanged, and only a value pin like this one catches it
    target_ts = bar_ts(DST_FRIDAY_EST, 2).isoformat()
    matches = [row for row in body["data"] if row["symbol"] == "AAA" and row["ts"] == target_ts]
    assert len(matches) == 1
    assert float(matches[0]["move_pct"]) == -7.6923


def test_largest_moves_paging_across_a_tied_timestamp_matches_the_unpaged_result(moves_tie_client):
    # AAA and BBB share every ts value -- bar_ts depends only on day and minute, never on symbol --
    # which is the (ts, symbol) tie the ORDER BY and the row-value cursor comparison exist for
    base_params = {"start": "2026-03-01", "end": "2026-03-31", "min_move_pct": 0}
    unpaged = moves_tie_client.get(
        "/analytics/largest-moves", params={**base_params, "limit": 1000}
    ).json()["data"]
    paged = _walk_largest_moves(moves_tie_client, base_params)
    # this is what kills "LIMIT %(fetch)s" rewritten to the resolved limit, which makes
    # next_cursor null on a full page. It does NOT kill dropping ", b.symbol" from the ORDER BY;
    # test_the_order_by_names_the_tie_key_even_though_no_fixture_can_show_its_absence records why
    # and guards it
    assert [(row["ts"], row["symbol"]) for row in paged] == [
        (row["ts"], row["symbol"]) for row in unpaged
    ]


def test_largest_moves_paging_crosses_a_partition_boundary_and_matches_the_unpaged_result(
    moves_run_client,
):
    days = run_days()
    base_params = {"start": days[0].isoformat(), "end": days[-1].isoformat(), "min_move_pct": 0}
    unpaged = moves_run_client.get(
        "/analytics/largest-moves", params={**base_params, "limit": 1000}
    ).json()["data"]
    paged = _walk_largest_moves(moves_run_client, base_params)
    assert [(row["ts"], row["symbol"]) for row in paged] == [
        (row["ts"], row["symbol"]) for row in unpaged
    ]
    months = {row["ts"][:7] for row in paged}
    assert months == {"2026-04", "2026-05"}


def test_pruning_the_universe_query_removes_partitions_outside_the_after_ts_bound(migrated_dsn):
    dsn = _six_months_of_partitions(migrated_dsn)
    _lo, hi = api.routes._instant_bounds(date(2026, 1, 1), date(2026, 3, 31))
    params = {
        "start": date(2026, 1, 1),
        "end": date(2026, 3, 31),
        "after_ts": datetime(2026, 2, 15, tzinfo=timezone.utc),
        "after_symbol": "",
        "hi": hi,
        "min_move_pct": 0,
        "fetch": 101,
    }

    # raise rather than assert: python -O strips an assert, and a harness in this project has
    # already measured one rewriting a file unchanged and exiting 0 because its own exactly-once
    # check was one
    anchor = "\n      AND b.ts >= %(after_ts)s"
    found = api.routes._MOVES_SQL.count(anchor)
    if found != 1:
        raise RuntimeError(f"pruning anchor found {found} times in _MOVES_SQL, expected exactly 1")
    without_bound = api.routes._MOVES_SQL.replace(anchor, "")

    with connect(dsn) as conn:
        with_bound_plan = "\n".join(
            row[0] for row in conn.execute("EXPLAIN " + api.routes._MOVES_SQL, params).fetchall()
        )
        without_bound_plan = "\n".join(
            row[0] for row in conn.execute("EXPLAIN " + without_bound, params).fetchall()
        )

    # no "Subplans Removed" line to look for here, and its absence is not a failure: the bound is
    # a timestamptz that folds to a Const, so pruning happens at PLAN time and the excluded
    # children are removed from the plan text entirely -- stronger than the executor-init pruning
    # that line counts
    children_with_bound = sorted(set(re.findall(r"\bbars_\d{4}_\d{2}\b", with_bound_plan)))
    children_without_bound = sorted(set(re.findall(r"\bbars_\d{4}_\d{2}\b", without_bound_plan)))

    # first, because it is the property itself: lost pruning fails here, while a shift in which
    # children survive with pruning intact fails on the exact lists below
    assert len(children_with_bound) < len(children_without_bound), (
        children_with_bound,
        children_without_bound,
    )
    assert children_with_bound == ["bars_2026_02", "bars_2026_03"]
    assert children_without_bound == ["bars_2026_01", "bars_2026_02", "bars_2026_03"]
    assert "Sort" not in with_bound_plan


def test_window_too_long_is_refused_on_all_three_analytics_endpoints(window_cap_client):
    # largest-moves takes no symbol parameter, so its case carries no extra query param
    cases = [
        ("/analytics/volatility", {"symbol": "AAA"}),
        ("/analytics/gaps", {"symbol": "AAA"}),
        ("/analytics/largest-moves", {}),
    ]
    for path, extra in cases:
        too_long = window_cap_client.get(
            path, params={**extra, "start": "2026-03-31", "end": "2026-06-30"}
        )
        assert too_long.status_code == 422, path
        assert too_long.json()["error"]["detail"] == {
            "reason": "window_too_long",
            "max_days": 90,
            "requested_days": 91,
        }, path

        at_cap = window_cap_client.get(
            path, params={**extra, "start": "2026-04-01", "end": "2026-06-30"}
        )
        assert at_cap.status_code == 200, path


def test_unknown_symbol_is_a_four_oh_four_on_volatility_and_gaps(unknown_symbol_client):
    expected = {
        "error": {
            "code": "unknown_symbol",
            "message": "no symbol by that name has been ingested",
            "detail": {"reason": "unknown_symbol", "symbol": "NOPE"},
        }
    }
    for path in ("/analytics/volatility", "/analytics/gaps"):
        response = unknown_symbol_client.get(
            path, params={"symbol": "NOPE", "start": "2026-03-01", "end": "2026-03-31"}
        )
        assert response.status_code == 404, path
        assert response.json() == expected, path

    # largest-moves takes no symbol parameter, so it has no unknown_symbol case of its own to reach


def test_a_negative_min_move_pct_is_a_four_hundred(empty_client):
    response = empty_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-04-01", "end": "2026-06-30", "min_move_pct": -1},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_params"
    # pydantic's own slug, written as a literal so a rename fails here by name
    assert body["detail"]["errors"] == [
        {"parameter": "min_move_pct", "location": "query", "type": "greater_than_equal"}
    ]


def test_a_cursor_from_a_different_shape_is_a_four_hundred_invalid_cursor(empty_client):
    # a bars cursor carries one field where the universe shape carries two, so decode_cursor's
    # field-set check refuses it regardless of the value inside
    wrong_shape_cursor = encode_cursor(
        BARS_CURSOR, {"ts": datetime(2026, 4, 15, 13, 30, tzinfo=timezone.utc)}
    )
    response = empty_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-04-01", "end": "2026-06-30", "cursor": wrong_shape_cursor},
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_cursor"
    assert body["detail"]["reason"] == "wrong_fields"


def test_the_order_by_names_the_tie_key_even_though_no_fixture_can_show_its_absence():
    # deleting ", b.symbol" is unobservable through this endpoint, and that is a property of the
    # index rather than of the clause: the cursor predicate is a ROW comparison on (ts, symbol),
    # which the planner serves from migration 005's (ts, symbol) index as an Index Cond, and an
    # index scan already emits (ts, symbol) order. Measured on a scratch database seeded with two
    # symbols on identical timestamps -- with the index present the two plans are byte-identical
    # and the paged walk equals the unpaged result either way; with it dropped the plan falls back
    # to a Sort whose key is b.ts alone, and the walk stops matching the unpaged order. So the
    # clause is load-bearing for any plan that is not index-ordered, and no fixture this suite can
    # build makes the planner choose one
    assert "\nORDER BY b.ts, b.symbol\n" in api.routes._MOVES_SQL

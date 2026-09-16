import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from math import isclose, sqrt

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
    WINDOW_END,
    WINDOW_START,
    bar_ts,
    close_ts,
    ensure_partition,
    load,
    load_flat_symbol,
    load_run_calendar,
    load_run_symbol,
    load_sparse_symbol,
    load_spread_symbol,
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


def _regular_session_bars(dsn, start, end):
    """(ts, symbol, open, close) for every bar inside a regular session of [start, end], read with
    plain SELECTs against bars and market_days rather than through _MOVES_SQL's own join."""
    with connect(dsn) as conn:
        sessions = conn.execute(
            "SELECT open_ts, close_ts FROM market_days WHERE day >= %s AND day <= %s", (start, end)
        ).fetchall()
        bars = conn.execute("SELECT ts, symbol, open, close FROM bars").fetchall()
    return sorted(
        (ts, symbol, open_, close_)
        for ts, symbol, open_, close_ in bars
        if any(lo <= ts < hi for lo, hi in sessions)
    )


def _sql_without_comments(sql: str) -> str:
    # a `--` comment can span exactly the text a plan-shape or text-presence assertion reads
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


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
def moves_extended_hours_client(migrated_dsn):
    load(migrated_dsn, extended_hours=True)
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
        error = too_long.json()["error"]
        assert error["code"] == "invalid_range", path
        assert error["message"] == "the requested date range is not one this endpoint serves", path
        assert error["detail"] == {
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


def test_largest_moves_keeps_only_bars_inside_the_half_open_session(moves_extended_hours_client):
    # _MOVES_SQL is its own text, so test_largest_moves_query.py's boundary cases never reach it;
    # both extended-hours bars fall on the session's own New York date, so no day equality could
    # exclude them, and the pair's operators alone keep them out and keep the bar on the open
    response = moves_extended_hours_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-03-01", "end": "2026-03-31", "min_move_pct": 0, "limit": 1000},
    )
    rows = response.json()["data"]
    assert len(rows) == BARS_PER_SESSION * len(TRADING_DAYS)
    stamps = {row["ts"] for row in rows}
    for day in TRADING_DAYS:
        assert bar_ts(day, -30).isoformat() not in stamps, day
        assert close_ts(day).isoformat() not in stamps, day
        assert bar_ts(day, 0).isoformat() in stamps, day


def test_the_session_join_cannot_be_hashed(migrated_dsn):
    # on the loaded database the planner underestimates this join by hundreds of times, and past a
    # threshold a hashable join trades the ordered index scan for a hash join over every remaining
    # row plus a sort -- at the page cap from page 1, or at the default page late in a window. The
    # rollup's day equality is what makes it hashable, and the half-open pair alone implies that
    # equality. On this fixture, never analyzed, the ordered loop and the hashed alternative sit
    # within the planner's 1% cost fuzz of each other, so which one it keeps is a tie-break rather
    # than a margin this plan check alone can be trusted for --
    # test_the_session_join_pins_the_half_open_pair_and_nothing_that_could_be_hashed guards the
    # join's text directly for that reason
    days = load_run_calendar(migrated_dsn)
    for offset, symbol in enumerate(("RUNA", "RUNB", "RUNC", "RUND")):
        load_run_symbol(migrated_dsn, symbol, [100 + offset + i for i in range(len(days))], days)
    lo, hi = api.routes._instant_bounds(days[0], days[-1])
    params = {
        "start": days[0],
        "end": days[-1],
        "after_ts": lo,
        "after_symbol": "",
        "hi": hi,
        "min_move_pct": 0,
    }
    with connect(migrated_dsn) as conn:
        for fetch in (101, 1001):
            plan = "\n".join(
                row[0]
                for row in conn.execute(
                    "EXPLAIN " + api.routes._MOVES_SQL, {**params, "fetch": fetch}
                ).fetchall()
            )
            assert "Hash Join" not in plan, (fetch, plan)


def test_the_session_join_pins_the_half_open_pair_and_nothing_that_could_be_hashed():
    # a planner-choice assertion on a small, un-analysed fixture lets a hashable respelling of this
    # join through -- a UTC date cast reproduces the shipped plan on this fixture and still
    # reproduces D-430 at production shape, because the check above rests on a 1% cost margin. This
    # pins the join's own text instead, which no plan choice can satisfy by accident.
    #
    # a positive pin rather than a denylist of hashable respellings (::date, date_trunc, EXISTS,
    # BETWEEN, IN), because a denylist passes any spelling it does not name -- `AND m.day =
    # CAST(b.ts AS date)` in the WHERE clause, for one. Pinning every reference to market_days
    # leaves a respelling nowhere to hide, named or not
    normalized = _sql_without_comments(api.routes._MOVES_SQL)
    assert normalized.count("market_days") == 1
    assert "JOIN market_days m ON b.ts >= m.open_ts AND b.ts < m.close_ts" in normalized
    assert normalized.count("m.open_ts") == 1
    assert normalized.count("m.close_ts") == 1
    assert normalized.count("m.day") == 2
    assert "m.day >= %(start)s" in normalized
    assert "m.day <= %(end)s" in normalized
    # open_ts once, close_ts once, day twice: four total, so a fifth m. reference anywhere --
    # a smuggled equality, a second predicate on m.day -- is refused even if it reuses a name above
    assert len(re.findall(r"\bm\.\w+", normalized)) == 4


def _load_moves_identity_fixture(dsn):
    # bars on the window's first and last session (TRADING_DAYS' ends), extended-hours bars on
    # both sides of every session, two symbols sharing every timestamp (AAA/BBB, WIDEA/WIDEB), and
    # bars at many minutes of a session rather than only _BAR_SHAPE's first five
    load(dsn, symbols=("AAA", "BBB"), extended_hours=True)
    load_spread_symbol(dsn, "WIDEA", extended_hours=True)
    load_spread_symbol(dsn, "WIDEB", extended_hours=True)


def test_largest_moves_at_zero_threshold_matches_bars_and_sessions_computed_independently(
    migrated_dsn,
):
    # spec line 484: at min_move_pct=0 the full walk equals the regular-session bars of the
    # window, counted from bars and market_days directly rather than from _MOVES_SQL's own text.
    #
    # bounded on the fixture's own first and last loaded session, not on WINDOW_START/END: the
    # loaded calendar has no session within 19 days of WINDOW_END, so a mutated `m.day <= %(end)s`
    # (-> `<`) would still agree with this test's independently-computed count, since no session
    # ever sits exactly on WINDOW_END for the boundary to drop
    start, end = TRADING_DAYS[0], TRADING_DAYS[-1]
    _load_moves_identity_fixture(migrated_dsn)
    expected = _regular_session_bars(migrated_dsn, start, end)

    with TestClient(create_app(migrated_dsn)) as c:
        walked = _walk_largest_moves(
            c,
            {"start": start.isoformat(), "end": end.isoformat(), "min_move_pct": 0},
            max_iterations=len(expected) + 10,
        )
    assert [(row["ts"], row["symbol"]) for row in walked] == [
        (ts.isoformat(), symbol) for ts, symbol, _, _ in expected
    ]
    assert len(walked) == len(expected)


def test_largest_moves_at_a_positive_threshold_matches_bars_computed_independently(migrated_dsn):
    _load_moves_identity_fixture(migrated_dsn)
    quantum = Decimal("0.0001")
    # 3, not a round number close to any bar's move: AAA/BBB's DST_MONDAY_EDT bar 5 (open 108.00,
    # close 111.25) clears it by the open denominator (3.0093%) and misses it by the close
    # denominator (2.9213%), so this threshold is what a denominator swap changes the row set at
    threshold = Decimal("3")
    expected = []
    for ts, symbol, open_, close_ in _regular_session_bars(migrated_dsn, WINDOW_START, WINDOW_END):
        move = 100 * (close_ - open_) / open_
        if abs(move) >= threshold:
            expected.append((ts, symbol, open_, close_, move))

    with TestClient(create_app(migrated_dsn)) as c:
        walked = _walk_largest_moves(
            c,
            {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
                "min_move_pct": str(threshold),
            },
            max_iterations=150,
        )
    assert [(row["ts"], row["symbol"]) for row in walked] == [
        (ts.isoformat(), symbol) for ts, symbol, _, _, _ in expected
    ]
    assert len(walked) == len(expected)
    assert expected  # the threshold must actually exclude some and keep some of this fixture's bars
    for row, (_, _, open_, close_, move) in zip(walked, expected):
        assert Decimal(str(row["open"])) == open_.quantize(quantum, rounding=ROUND_HALF_UP)
        assert Decimal(str(row["close"])) == close_.quantize(quantum, rounding=ROUND_HALF_UP)
        assert Decimal(str(row["move_pct"])) == move.quantize(quantum, rounding=ROUND_HALF_UP)


def test_largest_moves_without_min_move_pct_matches_min_move_pct_zero(moves_zero_client):
    # kills a default silently moved off 0: the request omitting the parameter must clear exactly
    # the same bar (including every FLAT, zero-move one) as the request naming the threshold
    params = {"start": "2026-03-01", "end": "2026-03-31", "limit": 1000}
    default = moves_zero_client.get("/analytics/largest-moves", params=params).json()
    zero = moves_zero_client.get(
        "/analytics/largest-moves", params={**params, "min_move_pct": 0}
    ).json()
    assert default == zero
    assert default["data"]


def test_largest_moves_a_single_page_above_the_cap_is_not_silently_truncated(migrated_dsn):
    # the cap-500 defect only shows on a request whose OWN fetch exceeds it; walking at limit=1
    # never does, so this asks for every row of a >500-row, <=1000-row window in one page
    days = load_run_calendar(migrated_dsn, run_days(10))
    minutes = range(0, 390, 5)
    load_sparse_symbol(migrated_dsn, "DENSE", days, minutes)
    total = len(minutes) * len(days)
    assert 500 < total <= 1000

    with TestClient(create_app(migrated_dsn)) as c:
        response = c.get(
            "/analytics/largest-moves",
            params={
                "start": days[0].isoformat(),
                "end": days[-1].isoformat(),
                "min_move_pct": 0,
                "limit": total,
            },
        )
    body = response.json()
    assert len(body["data"]) == total
    assert body["next_cursor"] is None


def test_gaps_matches_hand_computed_counts_on_a_populated_window_and_a_narrower_one(migrated_dsn):
    days = load_run_calendar(migrated_dsn, run_days(5))
    # day1/day0 +10% up, day2/day1 0% flat, day3/day2 -9.0909% down, day4/day3 +5% up
    load_run_symbol(migrated_dsn, "MOVES", [100, 110, 110, 100, 105], days)
    with TestClient(create_app(migrated_dsn)) as c:
        full = c.get(
            "/analytics/gaps",
            params={"symbol": "MOVES", "start": days[0].isoformat(), "end": days[-1].isoformat()},
        ).json()["data"][0]
        narrower = c.get(
            "/analytics/gaps",
            params={"symbol": "MOVES", "start": days[1].isoformat(), "end": days[-1].isoformat()},
        ).json()["data"][0]
    assert (full["gaps"], full["gaps_up"], full["gaps_down"], full["gaps_flat"]) == (4, 2, 1, 1)
    # excluding day0 drops its (up) gap, leaving the flat/down/up trio
    assert (
        narrower["gaps"],
        narrower["gaps_up"],
        narrower["gaps_down"],
        narrower["gaps_flat"],
    ) == (
        3,
        1,
        1,
        1,
    )


def test_gaps_counts_a_gap_that_spans_a_skipped_session(migrated_dsn):
    days = load_run_calendar(migrated_dsn, run_days(4))
    # the symbol trades on days 0, 1 and 3 -- day 2 is a session in the calendar it has no bar for
    load_run_symbol(migrated_dsn, "SKIPRUN", [100, 105, 110], [days[0], days[1], days[3]])
    with TestClient(create_app(migrated_dsn)) as c:
        body = c.get(
            "/analytics/gaps",
            params={"symbol": "SKIPRUN", "start": days[0].isoformat(), "end": days[-1].isoformat()},
        ).json()["data"][0]
    assert body["gaps"] == 2
    assert body["gaps_spanning_a_skipped_session"] == 1


def test_volatility_narrowing_the_window_changes_the_return_count(volatility_client):
    # SPARSE bars at minutes 0, 5, 35, 40, 65 give one return per day in buckets 0 and 60 and two
    # in bucket 30; dropping the first of the four TRADING_DAYS drops exactly one of each
    full = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "SPARSE", "start": "2026-03-01", "end": "2026-03-31"},
    ).json()["data"]
    narrower = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "SPARSE", "start": "2026-03-09", "end": "2026-03-31"},
    ).json()["data"]
    assert [row["returns"] for row in full] == [4, 8, 4]
    assert [row["returns"] for row in narrower] == [3, 6, 3]


def test_volatility_annualizes_by_the_square_root_of_minutes_in_a_year(volatility_client):
    data = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "SPARSE", "start": "2026-03-01", "end": "2026-03-31"},
    ).json()["data"]
    bucket = data[1]  # bucket 30, the one bucket the bucketing test pins as stddev > 0
    assert float(bucket["stddev_pct"]) > 0
    ratio = float(bucket["annualized_pct"]) / float(bucket["stddev_pct"])
    assert isclose(ratio, sqrt(98280), rel_tol=1e-3)


def test_volatility_on_a_window_with_no_bars_answers_an_empty_list(volatility_client):
    response = volatility_client.get(
        "/analytics/volatility",
        params={"symbol": "AAA", "start": "2026-04-01", "end": "2026-04-30"},
    )
    assert response.json() == {"data": [], "next_cursor": None}


def test_largest_moves_on_a_window_with_no_bars_answers_an_empty_list(moves_zero_client):
    response = moves_zero_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-04-01", "end": "2026-04-30", "min_move_pct": 0},
    )
    assert response.json() == {"data": [], "next_cursor": None}


def test_missing_required_parameters_are_a_four_hundred_on_all_three_analytics_endpoints(
    empty_client,
):
    cases = [
        ("/analytics/volatility", {"start": "2026-03-01", "end": "2026-03-31"}, "symbol"),
        ("/analytics/gaps", {"start": "2026-03-01", "end": "2026-03-31"}, "symbol"),
        ("/analytics/largest-moves", {"end": "2026-03-31"}, "start"),
    ]
    for path, params, missing in cases:
        response = empty_client.get(path, params=params)
        assert response.status_code == 400, path
        body = response.json()["error"]
        assert body["code"] == "invalid_params", path
        assert body["message"] == "one or more parameters are not valid", path
        assert body["detail"]["parameter"] == missing, path
        assert body["detail"]["errors"][0]["type"] == "missing", path


def test_malformed_parameters_are_a_four_hundred_on_all_three_analytics_endpoints(empty_client):
    cases = [
        (
            "/analytics/volatility",
            {"symbol": "AAA", "start": "not-a-date", "end": "2026-03-31"},
            "start",
        ),
        ("/analytics/gaps", {"symbol": "AAA", "start": "2026-03-01", "end": "not-a-date"}, "end"),
        ("/analytics/largest-moves", {"start": "not-a-date", "end": "2026-03-31"}, "start"),
    ]
    for path, params, malformed in cases:
        response = empty_client.get(path, params=params)
        assert response.status_code == 400, path
        body = response.json()["error"]
        assert body["code"] == "invalid_params", path
        assert body["message"] == "one or more parameters are not valid", path
        assert body["detail"]["parameter"] == malformed, path
        assert body["detail"]["errors"][0]["type"] == "date_from_datetime_parsing", path


def test_a_limit_over_the_cap_and_a_garbage_cursor_are_four_hundreds_on_largest_moves(
    empty_client,
):
    over_cap = empty_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-03-01", "end": "2026-03-31", "limit": 1001},
    )
    assert over_cap.status_code == 400
    body = over_cap.json()["error"]
    assert body["code"] == "invalid_params"
    assert body["message"] == "one or more parameters are not valid"
    assert body["detail"] == {"reason": "limit_out_of_range", "limit": 1001, "max": 1000}

    garbage = empty_client.get(
        "/analytics/largest-moves",
        params={"start": "2026-03-01", "end": "2026-03-31", "cursor": "not-a-cursor"},
    )
    assert garbage.status_code == 400
    body = garbage.json()["error"]
    assert body["code"] == "invalid_cursor"
    assert body["message"] == "the cursor is not one this endpoint issued"
    assert body["detail"] == {"reason": "not_base64"}

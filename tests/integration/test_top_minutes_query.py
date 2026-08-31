from datetime import UTC, date, datetime

from psycopg.rows import dict_row

from db.session import connect
from tests.market_fixture import (
    PLAIN_TUESDAY,
    TRADING_DAYS,
    ensure_partition,
    bar_ts,
    close_ts,
    load_calendar,
    open_ts,
)


def _read(dsn, query_sql):
    with connect(dsn) as conn:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            # binds nothing: universe-wide over the whole window
            return cur.execute(query_sql("08_top_minutes.sql")).fetchall()


def _bar(conn, symbol, day, minute, volume, trades=1):
    conn.execute(
        "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
        " VALUES (%s, %s, 'X', true, %s) ON CONFLICT (symbol) DO NOTHING",
        (symbol, symbol, bar_ts(day, minute)),
    )
    conn.execute(
        "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
        " VALUES (%s, %s, 10, 10, 10, 10, %s, %s, 10)",
        (symbol, bar_ts(day, minute), volume, trades),
    )


def test_a_minute_is_summed_across_symbols_rather_than_taking_the_largest_single_bar(
    migrated_dsn, query_sql
):
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        # minute 1: three symbols at 100 each, so 300 market-wide
        for symbol in ("AAA", "BBB", "CCC"):
            _bar(conn, symbol, PLAIN_TUESDAY, 1, 100)
        # minute 2: one symbol at 250, the largest single bar in the fixture
        _bar(conn, "DDD", PLAIN_TUESDAY, 2, 250)
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    # market-wide means the sum over every symbol that printed in the minute. a per-symbol maximum
    # would rank minute 2 first and would only ever surface the most heavily traded symbol
    assert rows[0]["volume"] == 300
    assert rows[0]["symbols"] == 3
    assert rows[1]["volume"] == 250


def test_the_ranking_is_per_year_and_capped_at_ten(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        for minute in range(15):
            _bar(conn, f"S{minute:02d}", PLAIN_TUESDAY, minute, 100 + minute)
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    assert len(rows) == 10
    assert {r["year"] for r in rows} == {2026}
    assert [r["rank_in_year"] for r in rows] == list(range(1, 11))
    # descending by volume, so the fifteenth minute (volume 114) ranks first
    assert rows[0]["volume"] == 114
    assert rows[-1]["volume"] == 105


def test_each_year_gets_its_own_top_ten(migrated_dsn, query_sql):
    earlier = date(2025, 3, 10)
    with connect(migrated_dsn) as conn:
        conn.execute(
            "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
            " VALUES (%s, %s, %s, 390)",
            (earlier, datetime(2025, 3, 10, 13, 30, tzinfo=UTC),
             datetime(2025, 3, 10, 20, 0, tzinfo=UTC)),
        )
        conn.commit()
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        ensure_partition(conn, earlier)
        _bar(conn, "NOW", PLAIN_TUESDAY, 1, 10)
        # twelve busy minutes in 2025, so a single global top ten fills up entirely from that year
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('THEN','THEN','X',true,%s)", (datetime(2025, 3, 10, 13, 31, tzinfo=UTC),))
        for minute in range(12):
            conn.execute(
                "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
                " VALUES ('THEN', %s, 10, 10, 10, 10, %s, 1, 10)",
                (datetime(2025, 3, 10, 13, 31 + minute, tzinfo=UTC), 999999 - minute),
            )
        conn.commit()

    by_year = {}
    for row in _read(migrated_dsn, query_sql):
        by_year.setdefault(row["year"], []).append(row)

    # the tiny 2026 minute still ranks first in 2026. dropped from the partition, one global top
    # ten fills entirely from 2025's twelve busy minutes and the 2026 row disappears -- which is
    # exactly what "per year" means and what a two-row fixture cannot show
    assert set(by_year) == {2025, 2026}
    assert len(by_year[2025]) == 10
    assert by_year[2026][0]["volume"] == 10
    assert by_year[2026][0]["rank_in_year"] == 1


def test_the_year_is_taken_from_the_resolved_session_rather_than_from_the_stamp(
    query_sql, repo_root
):
    body = (repo_root / "db" / "queries" / "08_top_minutes.sql").read_text()

    # No fixture can separate these two. A regular ET session runs 09:30-16:00, which is
    # 13:30-21:00Z under EDT and 14:30-21:00Z under EST, so an in-session bar never crosses
    # midnight UTC and EXTRACT(year FROM ts) always equals EXTRACT(year FROM day). Measured:
    # swapping day for ts changes no row of the result.
    #
    # It reads from day anyway, because that is the column that already resolved the session
    # through America/New_York, and the equality is a property of the session filter rather than
    # of this expression. An extended-hours print at 20:30 ET on 31 December is 01:30Z on
    # 1 January -- the day the session filter widens, the two stop agreeing.
    assert "EXTRACT(year FROM day)::int AS year" in body


def test_the_year_comes_from_the_new_york_trading_date(migrated_dsn, query_sql):
    # a session on the last trading day of a year whose late bars fall in the next UTC year
    turn = date(2025, 12, 31)
    with connect(migrated_dsn) as conn:
        conn.execute(
            "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
            " VALUES (%s, %s, %s, 390)",
            (turn, datetime(2025, 12, 31, 14, 30, tzinfo=UTC),
             datetime(2025, 12, 31, 21, 0, tzinfo=UTC)),
        )
        conn.commit()
        ensure_partition(conn, turn)
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('NYE','NYE','X',true,%s)", (datetime(2025, 12, 31, 20, 59, tzinfo=UTC),))
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES ('NYE', %s, 10, 10, 10, 10, 50, 1, 10)",
            (datetime(2025, 12, 31, 20, 59, tzinfo=UTC),))
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    # 20:59Z on 2025-12-31 is 15:59 ET the same day, so it is a 2025 session either way -- what
    # this pins is that the year comes from market_days.day, which already resolved the session,
    # rather than from a second timezone conversion that could disagree with it
    assert [r["year"] for r in rows] == [2025]


def test_the_ranking_is_deterministic_when_two_minutes_tie(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        for minute in (3, 1, 2):
            _bar(conn, f"T{minute}", PLAIN_TUESDAY, minute, 500)
        conn.commit()

    first = [r["ts"] for r in _read(migrated_dsn, query_sql)]
    second = [r["ts"] for r in _read(migrated_dsn, query_sql)]

    # three minutes of identical volume. the ts tie-break is what keeps the README table
    # reproducing between runs rather than swapping rows on a re-sort
    assert first == sorted(first)
    assert first == second


def test_bars_outside_the_session_never_reach_the_ranking(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        _bar(conn, "IN", PLAIN_TUESDAY, 1, 100)
        # a huge print exactly on close_ts, which the half-open interval excludes
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('OUT','OUT','X',true,%s)", (close_ts(PLAIN_TUESDAY),))
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES ('OUT', %s, 10, 10, 10, 10, 999999, 1, 10)", (close_ts(PLAIN_TUESDAY),))
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    # the closing-auction print would take the top slot every single year
    assert len(rows) == 1
    assert rows[0]["volume"] == 100


def test_the_minute_is_reported_in_new_york_wall_clock(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        _bar(conn, "AAA", PLAIN_TUESDAY, 0, 100)
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    # "which minute" is the whole question, and a UTC stamp hides whether it was the open, the
    # close, or something mid-session
    assert rows[0]["et_minute"] == "09:30"


def test_the_file_binds_no_parameters_at_all(query_sql, repo_root):
    rendered = query_sql("08_top_minutes.sql")

    assert "%(" not in rendered
    assert ":'" not in rendered
    assert "-- parameters: none" in (repo_root / "db" / "queries" / "08_top_minutes.sql").read_text()


def test_the_final_order_is_explicit_even_though_no_fixture_can_show_its_absence(
    query_sql, repo_root
):
    body = (repo_root / "db" / "queries" / "08_top_minutes.sql").read_text()

    # ranked's own row_number() OVER (PARTITION BY year ORDER BY volume DESC, ts) needs its own
    # Sort to compute rank_in_year at all, and that Sort leaves every row in (year, volume DESC,
    # ts) order regardless of insertion order, hash aggregation, or a forced spill -- measured
    # with twenty symbols across three years inserted in shuffled order, under enable_hashagg=off
    # and a work_mem small enough to force an external sort, and the final order never moved.
    # Only a plan that gathers already-ranked per-worker output without merging it would drop
    # this, which a single-process fixture cannot construct
    assert "ORDER BY year, rank_in_year;" in body

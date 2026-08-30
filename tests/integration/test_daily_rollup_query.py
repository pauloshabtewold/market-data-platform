from decimal import Decimal

import pytest

from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    DST_FRIDAY_EST,
    DST_MONDAY_EDT,
    HALF_DAY,
    HOLIDAY,
    PLAIN_TUESDAY,
    TRADING_DAYS,
    WINDOW_END,
    WINDOW_START,
    bar_ts,
    close_ts,
    expected_rollup,
    load,
    open_ts,
)


def _read(dsn: str, query_sql, symbol="AAA", start=WINDOW_START, end=WINDOW_END) -> list[tuple]:
    with connect(dsn) as conn:
        return conn.execute(
            query_sql("06_daily_rollup.sql"),
            {"symbol": symbol, "start": start, "end": end},
        ).fetchall()


@pytest.fixture
def loaded(migrated_dsn):
    return load(migrated_dsn, extended_hours=True)


def _by_day(rows):
    return {r[1]: r for r in rows}


def test_every_trading_day_in_the_window_gets_one_row_and_the_holiday_gets_none(loaded, query_sql):
    rows = _read(loaded, query_sql)

    assert [r[1] for r in rows] == list(TRADING_DAYS)
    # the holiday is absent from market_days rather than flagged, so it cannot produce a row
    assert HOLIDAY not in [r[1] for r in rows]


def test_the_open_is_the_first_bars_open_and_not_the_last_bars(loaded, query_sql):
    row = _by_day(_read(loaded, query_sql))[PLAIN_TUESDAY]

    # the fixture's last bar opens at 98 + offset, so a rollup reading the wrong end reads 118 here
    assert row[2] == Decimal(str(expected_rollup(PLAIN_TUESDAY)["open"]))
    assert row[2] != Decimal("118.00")


def test_the_close_is_the_last_bars_close_and_not_the_first_bars(loaded, query_sql):
    row = _by_day(_read(loaded, query_sql))[PLAIN_TUESDAY]

    # this is the trap the spec calls the one everyone gets wrong: last_value on the default
    # frame, and DISTINCT ON (day) ORDER BY day, ts, both return the opening minute's close --
    # 120.50 here -- which is a 60-second return published as the day
    assert row[5] == Decimal(str(expected_rollup(PLAIN_TUESDAY)["close"]))
    assert row[5] != Decimal("120.50")


def test_the_high_and_low_come_from_the_middle_of_the_session(loaded, query_sql):
    row = _by_day(_read(loaded, query_sql))[PLAIN_TUESDAY]
    expected = expected_rollup(PLAIN_TUESDAY)

    # both extremes sit in bars 1 and 2, so a query that only inspects the first and last bar
    # reads 121.00 and 117.00 instead
    assert row[3] == Decimal(str(expected["high"]))
    assert row[4] == Decimal(str(expected["low"]))


def test_the_volume_is_summed_over_the_session(loaded, query_sql):
    row = _by_day(_read(loaded, query_sql))[PLAIN_TUESDAY]

    assert row[6] == expected_rollup(PLAIN_TUESDAY)["volume"]


def test_every_session_rolls_up_to_its_own_hand_computed_values(loaded, query_sql):
    rows = _by_day(_read(loaded, query_sql))

    for day in TRADING_DAYS:
        expected = expected_rollup(day)
        assert rows[day][2] == Decimal(str(expected["open"])), day
        assert rows[day][3] == Decimal(str(expected["high"])), day
        assert rows[day][4] == Decimal(str(expected["low"])), day
        assert rows[day][5] == Decimal(str(expected["close"])), day
        assert rows[day][6] == expected["volume"], day


def test_the_day_boundary_follows_new_york_across_a_dst_transition(loaded, query_sql):
    rows = _by_day(_read(loaded, query_sql))

    # both sessions open at 09:30 ET, but the Friday is EST at 14:30Z and the Monday is EDT at
    # 13:30Z. a day boundary taken from date_trunc('day', ts) in UTC still separates these two,
    # so what this actually pins is that each 09:30 ET open lands in its own day and neither
    # session is split or merged by the hour shift
    assert open_ts(DST_FRIDAY_EST).hour == 14
    assert open_ts(DST_MONDAY_EDT).hour == 13
    assert rows[DST_FRIDAY_EST][7] == BARS_PER_SESSION
    assert rows[DST_MONDAY_EDT][7] == BARS_PER_SESSION


def test_bars_outside_the_session_never_reach_the_rollup(loaded, query_sql):
    rows = _by_day(_read(loaded, query_sql))

    # the fixture writes a pre-open bar and one exactly on close_ts for every day, both priced
    # at 1. either leaking in would move the low to 1 and the bar count off five
    for day in TRADING_DAYS:
        assert rows[day][7] == BARS_PER_SESSION, day
        assert rows[day][4] != Decimal("1"), day


def test_the_bar_exactly_on_close_ts_is_excluded_because_membership_is_half_open(loaded, query_sql):
    with connect(loaded) as conn:
        on_close = conn.execute(
            "SELECT count(*) FROM bars WHERE symbol='AAA' AND ts = %s", (close_ts(PLAIN_TUESDAY),)
        ).fetchone()[0]

    # the bar is really there; it is the half-open interval that keeps it out, and a <= anywhere
    # would make coverage exceed 100% for some symbols with nobody finding it
    assert on_close == 1
    assert _by_day(_read(loaded, query_sql))[PLAIN_TUESDAY][7] == BARS_PER_SESSION


def test_a_half_day_rolls_up_from_its_own_shorter_session(loaded, query_sql):
    rows = _by_day(_read(loaded, query_sql))

    # the half day closes at 13:00 ET. its bars sit in the first five minutes either way, so what
    # this pins is that the shorter close_ts still bounds the session rather than a hardcoded 16:00
    assert rows[HALF_DAY][7] == BARS_PER_SESSION
    assert close_ts(HALF_DAY).hour == 17
    assert rows[HALF_DAY][5] == Decimal(str(expected_rollup(HALF_DAY)["close"]))


def test_the_window_is_bounded_at_both_ends(loaded, query_sql):
    rows = _read(loaded, query_sql, start=DST_MONDAY_EDT, end=PLAIN_TUESDAY)

    assert [r[1] for r in rows] == [DST_MONDAY_EDT, PLAIN_TUESDAY]


def test_the_final_session_survives_the_end_bound(loaded, query_sql):
    rows = _read(loaded, query_sql, start=WINDOW_START, end=HALF_DAY)

    # :end is a date, so a bare b.ts < :end resolves to midnight UTC and drops the whole final
    # session while market_days still counts its minutes. the + 1 day in the file is what keeps it
    assert [r[1] for r in rows][-1] == HALF_DAY
    assert rows[-1][7] == BARS_PER_SESSION


def test_a_symbol_with_no_bars_in_the_window_returns_nothing_rather_than_a_zero_row(loaded, query_sql):
    assert _read(loaded, query_sql, symbol="MISSING") == []


def test_one_symbols_bars_never_reach_another_symbols_rollup(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, symbols=("AAA", "BBB"))

    aaa = _by_day(_read(dsn, query_sql, symbol="AAA"))
    bbb = _by_day(_read(dsn, query_sql, symbol="BBB"))

    # both symbols carry identical prices, so a missing symbol filter doubles the bar count
    # rather than changing any price -- which is why this asserts the count and not the close
    assert aaa[PLAIN_TUESDAY][7] == BARS_PER_SESSION
    assert bbb[PLAIN_TUESDAY][7] == BARS_PER_SESSION


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("06_daily_rollup.sql")

    assert ":'" not in rendered
    for name in ("symbol", "start", "end"):
        assert f"%({name})s" in rendered


def test_the_ordering_is_by_day_so_the_consumers_can_lag_over_it(loaded, query_sql, repo_root):
    days = [r[1] for r in _read(loaded, query_sql)]

    # queries 2 and 3 consume this rollup and both lag over consecutive rows, so an unordered
    # result is a wrong answer in the consumer rather than a cosmetic difference here
    assert days == sorted(days)

    # the returned order alone cannot hold this and asserting it would be a test that passes by
    # coincidence: with the ORDER BY deleted the planner still returns these four rows sorted,
    # because a small group set comes back from a sorted aggregate. ordering in SQL is guaranteed
    # by the clause and by nothing else, so the clause is what gets asserted
    assert "ORDER BY day" in (repo_root / "db" / "queries" / "06_daily_rollup.sql").read_text()


def test_a_session_with_a_single_bar_reports_that_bar_as_both_open_and_close(migrated_dsn, query_sql):
    from tests.market_fixture import load_calendar

    load_calendar(migrated_dsn)
    with connect(migrated_dsn) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute("CREATE TABLE bars_2026_03 (LIKE bars INCLUDING ALL)")
        conn.execute(
            "ALTER TABLE bars ATTACH PARTITION bars_2026_03"
            " FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')"
        )
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('ONE','ONE','X',true,%s)", (open_ts(PLAIN_TUESDAY),)
        )
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES ('ONE', %s, 5, 6, 4, 5.5, 9, 1, 5)", (bar_ts(PLAIN_TUESDAY, 3),)
        )
        conn.commit()

    rows = _read(migrated_dsn, query_sql, symbol="ONE")

    # the degenerate group is where an ordered aggregate and a windowed one diverge most quietly
    assert rows == [("ONE", PLAIN_TUESDAY, Decimal("5"), Decimal("6"), Decimal("4"),
                     Decimal("5.5"), 9, 1)]

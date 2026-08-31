from datetime import date
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    HALF_DAY,
    PLAIN_TUESDAY,
    SESSION_MINUTES,
    TRADING_DAYS,
    WINDOW_END,
    WINDOW_START,
    ensure_partition,
    bar_ts,
    close_ts,
    load,
    load_calendar,
    load_symbol,
    open_ts,
)

MONTH = date(2026, 3, 1)
TOTAL_SESSION_MINUTES = sum(SESSION_MINUTES[d] for d in TRADING_DAYS)


def _read(dsn, query_sql, start=WINDOW_START, end=WINDOW_END):
    with connect(dsn) as conn:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            return cur.execute(
                query_sql("10_missing_minutes.sql"), {"start": start, "end": end}
            ).fetchall()


def _coverage(dsn, query_sql, start=WINDOW_START, end=WINDOW_END):
    with connect(dsn) as conn:
        return dict(
            conn.execute(query_sql("09_coverage.sql"), {"start": start, "end": end}).fetchall()
        )


def test_there_is_one_row_per_symbol_per_month(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, symbols=("AAA", "BBB"))

    rows = _read(dsn, query_sql)

    # per month, not one number per symbol -- that is query 9's role, and keeping them separate is
    # what makes seven years of drift visible here
    assert len(rows) == 2
    assert {r["month"] for r in rows} == {MONTH}
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]


def test_the_share_is_the_minutes_that_produced_no_bar(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql)[0]

    # four sessions, 390 + 390 + 390 + 210 = 1,380 expected minutes, of which the fixture fills 20
    expected_missing = TOTAL_SESSION_MINUTES - len(TRADING_DAYS) * BARS_PER_SESSION
    assert row["expected"] == TOTAL_SESSION_MINUTES
    assert row["actual"] == len(TRADING_DAYS) * BARS_PER_SESSION
    assert row["missing"] == expected_missing
    assert row["missing_pct"] == round(
        Decimal(100) * expected_missing / TOTAL_SESSION_MINUTES, 2
    )


def test_the_metric_is_absence_and_not_a_zero_volume_share(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_symbol(migrated_dsn, "AAA")

    row = _read(migrated_dsn, query_sql)[0]

    # the feed emits no bar at all when a minute has no print, and documents that a bar is only
    # emitted if none of its fields is 0 -- so a stored bar with volume = 0 essentially cannot
    # exist and the zero-volume form of this query returns 0.00% for every symbol in every month.
    # that query looks implemented, runs fast, and measures nothing
    with connect(migrated_dsn) as conn:
        zero_volume_bars = conn.execute(
            "SELECT count(*) FROM bars WHERE volume = 0"
        ).fetchone()[0]

    assert zero_volume_bars == 0
    assert row["missing_pct"] > 0


def test_a_half_day_contributes_its_own_shorter_denominator(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_symbol(migrated_dsn, "HALF", days=(HALF_DAY,))

    row = _read(migrated_dsn, query_sql)[0]

    # the denominator is the sum over every session in the month regardless of which the symbol
    # traded, so it is still 1,380 -- but a hardcoded 390 a day would make it 1,560
    assert row["expected"] == TOTAL_SESSION_MINUTES
    assert row["sessions"] == len(TRADING_DAYS)


def test_a_symbol_that_was_never_ingested_gets_no_row(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('NEVER','NEVER','X',true,NULL)")
        conn.commit()

    rows = _read(dsn, query_sql)

    # reporting an uningested symbol as 100% missing would put an ingest failure on the same line
    # as a liquidity finding, and the two need different responses
    assert "NEVER" not in [r["symbol"] for r in rows]


def test_the_denominator_is_not_floored_at_the_first_bar(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # LATE's first bar is on the half day, so the three earlier sessions precede its listing
    load_symbol(migrated_dsn, "LATE", days=(HALF_DAY,))

    row = _read(migrated_dsn, query_sql)[0]

    # deliberately unfloored, and this is where it differs from query 9: a month a symbol had not
    # listed for really did have session minutes it produced no bar in, and this query reports
    # absence rather than completeness
    assert row["expected"] == TOTAL_SESSION_MINUTES
    assert row["actual"] == BARS_PER_SESSION


def test_missing_share_and_coverage_sum_to_one_hundred(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # AAA prints from the first session's open, which is the precondition below
    load_symbol(migrated_dsn, "AAA")

    missing = _read(migrated_dsn, query_sql)[0]
    coverage = _coverage(migrated_dsn, query_sql)

    # the identity the spec asks to be written down: over the same slice this query is
    # 100 - query 9. it holds ONLY where first_bar_ts is set and is at or before the first
    # session's OPEN -- compared against the open and not against min(day), which is a date
    # coerced to midnight UTC and therefore earlier than any 13:30 or 14:30 open, which would
    # make the guard false for every hand-built fixture and this assertion dead code that passes
    with connect(migrated_dsn) as conn:
        first_bar_ts, first_open = conn.execute(
            "SELECT s.first_bar_ts, (SELECT min(open_ts) FROM market_days)"
            " FROM symbols s WHERE s.symbol = 'AAA'"
        ).fetchone()
    assert first_bar_ts is not None and first_bar_ts <= first_open

    assert missing["missing_pct"] + coverage["AAA"] == Decimal("100.00")


def test_the_identity_breaks_on_a_symbols_first_partial_month_and_that_is_correct(
    migrated_dsn, query_sql
):
    load_calendar(migrated_dsn)
    # LATE lists mid-window, so query 9 floors its denominator and this query does not
    load_symbol(migrated_dsn, "LATE", days=(HALF_DAY,))

    missing = _read(migrated_dsn, query_sql)[0]
    coverage = _coverage(migrated_dsn, query_sql)

    # 9 reads against the half day alone and 10 against all four sessions, so the two do not
    # complement. asserting the identity here without the precondition above would fail on two
    # correct queries, which is why the precondition is stated rather than assumed
    assert missing["missing_pct"] + coverage["LATE"] != Decimal("100.00")


def test_the_window_is_bounded_at_both_ends(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql, start=WINDOW_START, end=PLAIN_TUESDAY)[0]

    # the half day drops out of the denominator with the window narrowed
    assert row["sessions"] == 3
    assert row["expected"] == sum(SESSION_MINUTES[d] for d in TRADING_DAYS[:3])


def test_bars_outside_the_session_are_not_counted_as_covered(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    row = _read(dsn, query_sql)[0]

    # eight extended-hours bars are present. counting them would understate the missing share, and
    # the one exactly on close_ts is the half-open boundary
    assert row["actual"] == len(TRADING_DAYS) * BARS_PER_SESSION


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("10_missing_minutes.sql")

    assert ":'" not in rendered
    for name in ("start", "end"):
        assert f"%({name})s" in rendered


def test_the_bounded_cte_is_inlined_rather_than_materialised(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)
    with connect(dsn) as conn:
        plan = "\n".join(
            row[0] for row in conn.execute(
                "EXPLAIN " + query_sql("10_missing_minutes.sql"),
                {"start": WINDOW_START, "end": WINDOW_END},
            ).fetchall()
        )

    # same directive and same reason as 09_coverage.sql's. measured on the full universe:
    # 131.1 s serial with a 1.2 GB spill, against 42.6 s with 71 parallel scans and no spill
    assert "CTE Scan on bounded" not in plan
    assert "CTE bounded" not in plan

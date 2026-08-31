from decimal import Decimal

import pytest

from db.session import connect
from tests.market_fixture import (
    DST_FRIDAY_EST,
    DST_MONDAY_EDT,
    HALF_DAY,
    HOLIDAY,
    PLAIN_TUESDAY,
    TRADING_DAYS,
    WINDOW_END,
    WINDOW_START,
    expected_rollup,
    load,
    load_calendar,
    load_symbol,
)


def _read(dsn, query_sql, symbol="AAA", start=WINDOW_START, end=WINDOW_END, *, heap_order=False):
    with connect(dsn) as conn:
        if heap_order:
            # see bugs.md D-122: on the fixture the planner index-scans the PK and delivers ts
            # order for free, so a missing window ORDER BY is invisible. production bitmap-scans
            conn.execute("SET enable_indexscan = off")
        return conn.execute(
            query_sql("03_gaps.sql"),
            {"symbol": symbol, "start": start, "end": end},
        ).fetchone()


def _gap_pct(prev_day, next_day) -> Decimal:
    """The hand-computed gap, from the fixture's own bar shape rather than from the query."""
    prev_close = Decimal(str(expected_rollup(prev_day)["close"]))
    next_open = Decimal(str(expected_rollup(next_day)["open"]))
    return 100 * (next_open - prev_close) / prev_close


def test_there_is_one_gap_for_every_session_after_the_first(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    row = _read(dsn, query_sql)

    # four sessions produce three gaps: the first session has no prior close to pair with
    assert row[0] == len(TRADING_DAYS) - 1


def test_the_gap_pairs_a_prior_close_with_the_next_open(migrated_dsn, query_sql):
    # extended-hours bars are loaded deliberately: they are priced at 1 against a session near
    # 100, so a session filter that admitted either end would move every gap in the set
    dsn = load(migrated_dsn, extended_hours=True)

    row = _read(dsn, query_sql)
    expected = [_gap_pct(a, b) for a, b in zip(TRADING_DAYS, TRADING_DAYS[1:])]

    # min and max pin both ends of the hand-computed set, so a query pairing the wrong two
    # figures -- open against open, or close against close -- misses on at least one
    assert row[4] == round(min(expected), 4)
    assert row[8] == round(max(expected), 4)


def test_the_next_session_is_the_next_row_in_market_days_and_not_the_next_calendar_day(
    migrated_dsn, query_sql
):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql)

    # the holiday sits between Tuesday and the half day and is absent from market_days rather
    # than flagged. so Tuesday's close pairs with the half day's open across two calendar days,
    # and none of the three gaps is reported as spanning a skipped session
    assert (HALF_DAY - PLAIN_TUESDAY).days == 2
    assert HOLIDAY not in TRADING_DAYS
    assert row[1] == 0
    assert row[0] == 3


def test_a_weekend_and_a_dst_transition_are_one_gap_rather_than_three(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_symbol(migrated_dsn, "PAIR", days=(DST_FRIDAY_EST, DST_MONDAY_EDT))

    row = _read(migrated_dsn, query_sql, symbol="PAIR")

    # Friday to Monday crosses two non-trading days and the 2026-03-08 transition. counted on
    # market_days rows it is a single overnight gap, which is the whole point of the ordinal
    assert row[0] == 1
    assert row[1] == 0
    assert row[6] == round(_gap_pct(DST_FRIDAY_EST, DST_MONDAY_EDT), 4)


def test_a_session_the_symbol_did_not_trade_is_reported_as_a_spanned_gap(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # SKIP has no bars on the Tuesday, which IS a trading day -- unlike the holiday, which is not
    load_symbol(migrated_dsn, "SKIP", days=(DST_FRIDAY_EST, DST_MONDAY_EDT, HALF_DAY))

    row = _read(migrated_dsn, query_sql, symbol="SKIP")

    # the Monday-to-half-day gap crosses a session the symbol sat out. reporting it silently as
    # an overnight gap would understate it, so the count is surfaced instead
    assert row[0] == 2
    assert row[1] == 1


def test_the_direction_counts_partition_the_gaps_exactly(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql)

    assert row[9] + row[10] + row[11] == row[0]


def test_the_quartiles_stay_numeric_rather_than_crossing_into_double_precision(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql)

    # percentile_cont has no numeric variant and returns double precision, whose last digits will
    # not match a hand-computed expectation. percentile_disc preserves numeric and returns a gap
    # that was actually observed rather than an interpolation between two
    for index in (5, 6, 7):
        assert isinstance(row[index], Decimal)


def test_the_median_is_an_observed_gap_rather_than_an_interpolation(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql)
    observed = [round(_gap_pct(a, b), 4) for a, b in zip(TRADING_DAYS, TRADING_DAYS[1:])]

    assert row[6] in observed


def test_the_window_is_bounded_at_both_ends(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql, start=DST_MONDAY_EDT, end=PLAIN_TUESDAY)

    assert row[0] == 1


def test_a_symbol_with_no_bars_reports_no_gaps_rather_than_failing(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _read(dsn, query_sql, symbol="MISSING")

    assert row[0] == 0


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("03_gaps.sql")

    assert ":'" not in rendered
    for name in ("symbol", "start", "end"):
        assert f"%({name})s" in rendered


def test_the_ordinal_and_the_lag_order_explicitly_rather_than_trusting_physical_order(
    migrated_dsn, query_sql
):
    from tests.market_fixture import load_scrambled_symbol

    # market_days written out of day order, so row_number() OVER () numbers the sessions wrongly
    with connect(migrated_dsn) as conn:
        for day in (HALF_DAY, DST_FRIDAY_EST, PLAIN_TUESDAY, DST_MONDAY_EDT):
            from tests.market_fixture import SESSION_MINUTES, close_ts, open_ts

            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%s, %s, %s, %s)",
                (day, open_ts(day), close_ts(day), SESSION_MINUTES[day]),
            )
        conn.commit()
    # and the bars written 4, 1, 3, 0, 2 inside each session, so the rollup's own order is not free
    load_scrambled_symbol(migrated_dsn, "SCRAM")

    row = _read(migrated_dsn, query_sql, symbol="SCRAM", heap_order=True)
    expected = [_gap_pct(a, b) for a, b in zip(TRADING_DAYS, TRADING_DAYS[1:])]

    # a mis-numbered ordinal pairs the wrong two sessions and reports phantom spanned gaps
    assert row[0] == 3
    assert row[1] == 0
    assert row[4] == round(min(expected), 4)
    assert row[8] == round(max(expected), 4)


def test_the_lag_orders_by_the_session_ordinal_even_though_no_result_can_hold_it(
    query_sql, repo_root
):
    body = (repo_root / "db" / "queries" / "03_gaps.sql").read_text()

    # Same shape as query 2's, and same reason: the lag reads a join whose rollup side is a
    # GROUP BY day, so its input already arrives in day order and session_no is monotonic in day.
    # Measured rather than assumed -- with market_days and every bar written out of order, and
    # under a forced bitmap plan, deleting the ORDER BY changed no value. The ordinal's own
    # ORDER BY day is a different matter and IS caught, by the test above this one.
    assert "lag(r.close)     OVER (ORDER BY c.session_no)" in body

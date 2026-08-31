from decimal import Decimal

import pytest

from db.session import connect
from tests.market_fixture import (
    RUN_LENGTH,
    load_run_calendar,
    load_run_symbol,
    run_days,
)

# A deterministic price path. The exact values do not matter; what matters is that the two
# series are not identical, so a correlation of 1 would be a finding rather than a coincidence.
_PATH_A = [100 + (i * 7) % 23 for i in range(RUN_LENGTH)]
_PATH_B = [100 + (i * 11) % 17 for i in range(RUN_LENGTH)]


def _read(dsn, query_sql, a="AAA", b="BBB", start=None, end=None, *, heap_order=False):
    days = run_days()
    with connect(dsn) as conn:
        if heap_order:
            # the fixture is small enough that the planner index-scans the PK and hands every
            # window ts order for free, which hides a missing ORDER BY entirely (bugs.md D-122).
            # production plans this as a bitmap scan, which returns heap order
            conn.execute("SET enable_indexscan = off")
        return conn.execute(
            query_sql("02_correlation.sql"),
            {"symbol_a": a, "symbol_b": b,
             "start": start or days[0], "end": end or days[-1]},
        ).fetchall()


@pytest.fixture
def paired(migrated_dsn):
    load_run_calendar(migrated_dsn)
    load_run_symbol(migrated_dsn, "AAA", _PATH_A)
    load_run_symbol(migrated_dsn, "BBB", _PATH_B)
    return migrated_dsn


def test_a_series_correlated_with_itself_is_exactly_one(migrated_dsn, query_sql):
    load_run_calendar(migrated_dsn)
    load_run_symbol(migrated_dsn, "AAA", _PATH_A)
    load_run_symbol(migrated_dsn, "SAME", _PATH_A)

    rows = _read(migrated_dsn, query_sql, a="AAA", b="SAME")

    # the property assertion the spec names. it holds under the wrong frame too, which is why
    # the next test exists and why this one is not sufficient on its own
    settled = [r for r in rows if r[2] >= 2]
    assert settled
    assert all(r[1] == Decimal("1.0000") for r in settled)


def test_the_frame_is_a_rolling_thirty_rows_rather_than_the_expanding_default(paired, query_sql):
    rows = _read(paired, query_sql)

    # this is the trap. the default frame is RANGE UNBOUNDED PRECEDING AND CURRENT ROW, which
    # computes an EXPANDING-window correlation and returns a plausible number that is not a
    # 30-day correlation. under it rows_in_window climbs to the full series length instead of
    # settling at 30, and corr(v,v) = 1 holds either way so no property assertion catches it
    assert max(r[2] for r in rows) == 30
    assert len(rows) > 30


def test_the_window_counts_trading_days_because_the_series_is_pivoted_to_one_row_a_day(paired, query_sql):
    rows = _read(paired, query_sql)

    # the frame counts rows, so on an unpivoted two-symbol series 30 rows would be 15 days. one
    # row per day is what makes 30 rows mean 30 trading days
    assert len(rows) == len({r[0] for r in rows})
    assert [r[2] for r in rows[:5]] == [1, 2, 3, 4, 5]


def test_the_first_session_carries_no_return_and_so_no_row(paired, query_sql):
    days = run_days()
    rows = _read(paired, query_sql)

    # the first session has no prior close, so its return is NULL and it is filtered out
    assert rows[0][0] == days[1]
    assert len(rows) == RUN_LENGTH - 1


def test_a_day_only_one_symbol_traded_is_dropped_rather_than_correlated_against_a_gap(migrated_dsn, query_sql):
    days = run_days()
    load_run_calendar(migrated_dsn)
    load_run_symbol(migrated_dsn, "AAA", _PATH_A)
    # BBB is missing its last five sessions
    load_run_symbol(migrated_dsn, "BBB", _PATH_B[:-5], days=days[:-5])

    rows = _read(migrated_dsn, query_sql)

    # a day with only one side has no pair to correlate, and carrying it forward would silently
    # reuse a stale return. the HAVING count = 2 is what drops it
    assert max(r[0] for r in rows) == days[-6]


def test_the_correlation_is_rounded_through_numeric_rather_than_erroring_on_a_float(paired, query_sql):
    rows = _read(paired, query_sql)

    # corr returns double precision no matter what it is fed, and round(double precision, integer)
    # does not exist -- the natural round(corr(...), 4) is a hard 42883 error, not a rounding.
    # the ::numeric cast at the call site is what makes this a Decimal
    settled = [r for r in rows if r[1] is not None]
    assert settled
    assert all(isinstance(r[1], Decimal) for r in settled)


def test_a_correlation_of_two_points_is_reported_with_its_row_count(paired, query_sql):
    rows = _read(paired, query_sql)

    # two points always correlate to exactly +/-1, which is an artifact of the sample size rather
    # than a finding. rows_in_window is what lets a reader see that
    assert rows[0][2] == 1
    assert rows[1][2] == 2


def test_the_window_is_bounded_at_both_ends(paired, query_sql):
    days = run_days()

    rows = _read(paired, query_sql, start=days[0], end=days[9])

    assert max(r[0] for r in rows) == days[9]


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("02_correlation.sql")

    assert ":'" not in rendered
    for name in ("symbol_a", "symbol_b", "start", "end"):
        assert f"%({name})s" in rendered


def test_a_perfectly_opposed_pair_correlates_to_exactly_minus_one(migrated_dsn, query_sql):
    # B's return is the exact negative of A's on every session, so the two series correlate to
    # -1 by construction. the returns have to VARY: a constant +1% against a constant -1% has zero
    # variance on both sides and corr is then undefined rather than -1
    steps = [Decimal(s) for s in ("0.01", "-0.02", "0.03", "-0.015", "0.005")]
    up, down, a, b = [Decimal(100)], [Decimal(100)], Decimal(100), Decimal(100)
    for i in range(RUN_LENGTH - 1):
        step = steps[i % len(steps)]
        a *= 1 + step
        b *= 1 - step
        up.append(a)
        down.append(b)

    load_run_calendar(migrated_dsn)
    load_run_symbol(migrated_dsn, "UP", up)
    load_run_symbol(migrated_dsn, "DOWN", down)

    rows = _read(migrated_dsn, query_sql, a="UP", b="DOWN")

    # the value, not just the shape. without this the pivot could take the max over both symbols
    # rather than each symbol's own return and every structural assertion here would still hold
    settled = [r for r in rows if r[2] >= 3]
    assert settled
    assert all(r[1] == Decimal("-1.0000") for r in settled)


def test_the_pivot_takes_each_symbols_own_return_rather_than_the_larger_of_the_two(
    paired, query_sql
):
    rows = {r[0]: r for r in _read(paired, query_sql)}

    # read beside the -1 case above: a pivot that took max(ret) over both symbols would correlate
    # a series against a running maximum that includes itself, which biases every value upward.
    # the two hand-computed paths here are constructed to correlate weakly, so a positive bias shows
    settled = [r for r in rows.values() if r[2] == 30]
    assert settled
    assert all(abs(r[1]) < Decimal("0.9") for r in settled)


def test_the_windows_order_by_day_rather_than_trusting_physical_order(migrated_dsn, query_sql):
    steps = [Decimal(s) for s in ("0.01", "-0.02", "0.03", "-0.015", "0.005")]
    up, down, a, b = [Decimal(100)], [Decimal(100)], Decimal(100), Decimal(100)
    for i in range(RUN_LENGTH - 1):
        step = steps[i % len(steps)]
        a *= 1 + step
        b *= 1 - step
        up.append(a)
        down.append(b)

    # every table written out of order, so nothing but an explicit ORDER BY can recover the sequence
    load_run_calendar(migrated_dsn, scrambled=True)
    load_run_symbol(migrated_dsn, "UP", up, scrambled=True)
    load_run_symbol(migrated_dsn, "DOWN", down, scrambled=True)

    rows = _read(migrated_dsn, query_sql, a="UP", b="DOWN", heap_order=True)

    # the daily-return lag, the rolling frame and the output ordering all depend on ORDER BY day.
    # lose any one and the returns pair the wrong sessions, so the exact -1 collapses
    settled = [r for r in rows if r[2] >= 3]
    assert settled
    assert all(r[1] == Decimal("-1.0000") for r in settled)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert [r[2] for r in rows[:5]] == [1, 2, 3, 4, 5]
    assert max(r[2] for r in rows) == 30


def test_the_window_order_by_clauses_are_present_even_though_no_result_can_hold_them(
    query_sql, repo_root
):
    body = (repo_root / "db" / "queries" / "02_correlation.sql").read_text()

    # Unlike query 1, these two clauses cannot be caught by any fixture, and the reason is not
    # fixture size. Both windows read the output of a GROUP BY on the very columns they order by
    # -- the rollup groups (symbol, day) and the pivot groups (day) -- so their input is already
    # in the required order whichever aggregate the planner picks. Verified by measurement, not
    # assumed: with the calendar, both symbols and every bar written out of order, and under
    # enable_indexscan=off, enable_sort=off and enable_hashagg=off in turn, deleting either
    # clause changed no value in the result. GroupAggregate emits sorted output and a sort on a
    # prefix of an already-sorted stream is a no-op, so Postgres will not construct the input
    # that would expose them.
    #
    # They stay in the file because they are what makes the query's meaning independent of the
    # plan: the day this rollup stops being a GROUP BY -- a view, a lateral, a union -- the
    # guarantee vanishes silently and every return pairs the wrong sessions. Asserting the text
    # is weak, and it is the strongest instrument that exists for a clause nothing can observe.
    assert "lag(close) OVER (PARTITION BY symbol ORDER BY day)" in body
    assert "WINDOW w AS (ORDER BY day ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)" in body

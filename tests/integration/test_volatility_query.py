from decimal import Decimal

from db.session import connect
from tests.market_fixture import (
    DST_FRIDAY_EST,
    DST_MONDAY_EDT,
    PLAIN_TUESDAY,
    TRADING_DAYS,
    WINDOW_END,
    WINDOW_START,
    load,
    load_flat_symbol,
    load_sparse_symbol,
    load_calendar,
    load_symbol as load_symbol_in_ts_order,
    open_ts,
)


def _read(dsn, query_sql, symbol="AAA", start=WINDOW_START, end=WINDOW_END, *, heap_order=False):
    with connect(dsn) as conn:
        if heap_order:
            # on twenty rows the planner picks an ordered Index Scan on the PK, which hands the
            # window ts order for free and hides a missing ORDER BY entirely. on the real 41.7M-row
            # database the same query plans as a Bitmap Heap Scan, which returns HEAP order -- so
            # the fixture has to be pushed onto production's plan shape for the clause to be under
            # test at all. verified: bitmap on bars_2026_04/05 with Sort Key m.day, b.ts above it
            conn.execute("SET enable_indexscan = off")
        return conn.execute(
            query_sql("01_volatility.sql"),
            {"symbol": symbol, "start": start, "end": end},
        ).fetchall()


def test_a_flat_series_has_exactly_zero_volatility(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_flat_symbol(migrated_dsn, "FLAT")

    rows = _read(migrated_dsn, query_sql, symbol="FLAT")

    # the property assertion the spec names for the statistical queries. numeric arithmetic makes
    # this exactly 0 rather than a float epsilon, which is what the numeric storage decision buys
    assert rows
    assert all(r[3] == Decimal("0.000000") for r in rows)
    assert all(r[4] == Decimal("0.0000") for r in rows)


def test_the_opening_bar_of_each_session_carries_no_return(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    rows = {r[0]: r for r in _read(dsn, query_sql)}

    # five bars a session over four sessions is twenty bars, but each session's first bar has no
    # prior bar inside its own session. computing its return against the previous session's close
    # would be the overnight gap, which query 3 measures separately
    assert sum(r[1] for r in rows.values()) == 4 * (5 - 1)


def test_returns_are_never_computed_across_a_session_boundary(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    rows = {r[0]: r for r in _read(dsn, query_sql)}

    # counted rather than measured on the stddev: a cross-session return is one extra observation
    # per session boundary, which is exact, where its effect on a stddev over sixteen returns is
    # a threshold someone has to guess at
    assert sum(r[1] for r in rows.values()) == 4 * (5 - 1)


def test_a_bar_outside_the_session_is_neither_a_return_nor_the_other_end_of_one(
    migrated_dsn, query_sql
):
    dsn = load(migrated_dsn, extended_hours=True)

    rows = {r[0]: r for r in _read(dsn, query_sql)}

    # the fixture writes a bar 30 minutes before each open and one exactly on close_ts, both
    # priced at 1 against a session near 100. either leaking in adds returns of roughly -99% and
    # +9900%, so both the count and the volatility move
    assert sum(r[1] for r in rows.values()) == 4 * (5 - 1)
    assert all(r[3] < Decimal("50") for r in rows.values())
    # the pre-open bar would also open a bucket of its own, at -30 minutes since the open
    assert min(rows) == 0


def test_the_bucket_is_minutes_since_that_days_open_and_not_a_utc_hour(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    rows = {r[0]: r for r in _read(dsn, query_sql)}

    # this is the DST trap. the Friday opens 14:30Z under EST and the Monday 13:30Z under EDT,
    # both 09:30 ET. bucketed on ts - open_ts both open into bucket 0; bucketed on a UTC hour
    # they split into two buckets and every bucket is contaminated by its neighbour
    assert open_ts(DST_FRIDAY_EST).hour != open_ts(DST_MONDAY_EDT).hour
    # all four sessions put their bars in the first five minutes, so one bucket holds them all
    assert list(rows) == [0]
    assert rows[0][1] == 4 * (5 - 1)


def test_every_session_contributes_to_the_same_bucket_despite_the_hour_shift(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # only the two sessions either side of the transition, so a UTC-hour bucketing splits them
    from tests.market_fixture import load_symbol

    load_symbol(migrated_dsn, "PAIR", days=(DST_FRIDAY_EST, DST_MONDAY_EDT))

    rows = _read(migrated_dsn, query_sql, symbol="PAIR")

    # one bucket, not two. under date_trunc('hour', ts) this is the assertion that fails
    assert [r[0] for r in rows] == [0]
    assert rows[0][1] == 2 * (5 - 1)


def test_a_return_spanning_more_than_a_minute_is_kept_and_its_span_is_reported(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # bars at minutes 0, 5 and 10 only, which is normal on iex where a minute with no print
    # produces no row at all
    # written newest-first inside each session, so the span lag cannot lean on physical order
    load_sparse_symbol(migrated_dsn, "THIN", TRADING_DAYS, (0, 5, 10), scrambled=True)

    rows = _read(migrated_dsn, query_sql, symbol="THIN", heap_order=True)

    # the returns are kept rather than dropped: two per session over four sessions
    assert sum(r[1] for r in rows) == 4 * 2
    # and the span is reported rather than scaled away, so the sparsity is visible in the output
    assert all(r[2] == Decimal("5.00") for r in rows)


def test_the_annualisation_stays_numeric_rather_than_crossing_into_double_precision(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    rows = _read(dsn, query_sql)

    # bare sqrt(98280) resolves to double precision and makes round(..., 4) a hard 42883 error
    # rather than a rounding. the ::numeric anchor in the file is what keeps this a Decimal
    assert isinstance(rows[0][4], Decimal)
    assert isinstance(rows[0][3], Decimal)


def test_the_window_is_bounded_at_both_ends(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    narrowed = _read(dsn, query_sql, start=DST_MONDAY_EDT, end=DST_MONDAY_EDT)

    assert sum(r[1] for r in narrowed) == 5 - 1


def test_one_symbols_bars_never_reach_another_symbols_volatility(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, symbols=("AAA", "BBB"))

    assert sum(r[1] for r in _read(dsn, query_sql, symbol="AAA")) == 4 * (5 - 1)


def test_a_symbol_with_no_bars_returns_nothing(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    assert _read(dsn, query_sql, symbol="MISSING") == []


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("01_volatility.sql")

    assert ":'" not in rendered
    for name in ("symbol", "start", "end"):
        assert f"%({name})s" in rendered


def _expected_stddev() -> Decimal:
    """The pooled return stddev, derived from the fixture's bar shape rather than from the query."""
    from decimal import getcontext

    from tests.market_fixture import _BAR_SHAPE, _DAY_OFFSET

    getcontext().prec = 40
    returns = []
    for day in TRADING_DAYS:
        off = Decimal(str(_DAY_OFFSET[day]))
        closes = [Decimal(str(bar[3])) + off for bar in _BAR_SHAPE]
        returns += [100 * (cur - prev) / prev for prev, cur in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(variance.sqrt(), 6)


def test_the_window_orders_by_ts_rather_than_trusting_physical_order(migrated_dsn, query_sql):
    from tests.market_fixture import load_scrambled_symbol

    load_calendar(migrated_dsn)
    # written 4, 1, 3, 0, 2 within each session, so heap order is not ts order
    load_scrambled_symbol(migrated_dsn, "SCRAM")

    rows = _read(migrated_dsn, query_sql, symbol="SCRAM", heap_order=True)

    # the ORDER BY is what makes this hold. without it the lag pairs whatever rows the heap
    # handed it, and every return is garbage that still produces a plausible stddev -- so the
    # assertion has to be against a value derived from the fixture, not against "not empty".
    # a fixture written in ts order cannot catch this at all, which is the point of scrambling
    assert [r[0] for r in rows] == [0]
    assert rows[0][1] == 4 * (5 - 1)
    assert rows[0][3] == _expected_stddev()


def test_the_scrambled_and_ordered_fixtures_agree(migrated_dsn, fresh_dsn, query_sql):
    from tests.market_fixture import load_scrambled_symbol

    load_calendar(migrated_dsn)
    load_scrambled_symbol(migrated_dsn, "SCRAM")
    scrambled = _read(migrated_dsn, query_sql, symbol="SCRAM", heap_order=True)

    ordered_dsn = fresh_dsn()
    from db.migrate import apply

    with connect(ordered_dsn) as conn:
        apply(conn)
    load_calendar(ordered_dsn)
    load_symbol_in_ts_order(ordered_dsn, "TIDY")
    ordered = _read(ordered_dsn, query_sql, symbol="TIDY", heap_order=True)

    # the same five bars, written two different ways, must roll up identically. this is the
    # property the ORDER BY exists to guarantee and the one a single-fixture test cannot state
    assert [tuple(r[1:]) for r in scrambled] == [tuple(r[1:]) for r in ordered]


def test_the_span_lag_shares_the_named_window_rather_than_running_unpartitioned(
    query_sql, repo_root
):
    body = (repo_root / "db" / "queries" / "01_volatility.sql").read_text()

    # span_minutes is the one clause here that no result can hold, and the reason is precise:
    # the rows where an unpartitioned lag would differ are exactly each session's first bar, and
    # those are already dropped by the prev_close IS NOT NULL filter -- which uses the correctly
    # partitioned lag. So the two forms agree on every surviving row, by construction.
    # It shares the named window anyway, because that is what stops the two lags disagreeing
    # about which row is previous if the filter above them ever changes.
    assert "EXTRACT(epoch FROM ts - lag(ts) OVER w) / 60" in body


def test_the_annualisation_scales_by_the_minutes_in_a_year_rather_than_the_days(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    rows = _read(dsn, query_sql)

    # the rows being aggregated are per-MINUTE returns, so the factor is sqrt(252 x 390) -- the
    # session minutes a year holds. the textbook sqrt(252) annualises a daily series and understates
    # this one by sqrt(390), a factor of 19.75, so the constant is pinned here rather than left to
    # read like a typo someone corrects
    scaled = [r[4] / r[3] for r in rows if r[3] >= Decimal("0.01")]
    assert scaled
    # sqrt(98280) is 313.4964114627..., and on this fixture the two roundings (annualized_pct to
    # 4 places, stddev_pct to 6) leave the ratio within a few parts in a million of it. +-0.0005
    # stays two orders of magnitude above that noise while still catching the constant drifting
    # by even 1 -- its nearest integer neighbours move the ratio by ~0.0016, over three times as
    # far as the tolerance allows
    assert all(Decimal("313.4959") < f < Decimal("313.4969") for f in scaled), scaled


def test_the_final_order_by_holds_even_under_a_forced_hash_aggregate(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # eleven distinct 30-minute buckets from twelve bars: the first two share bucket 0, and every
    # bar after that opens a fresh bucket, so each of the eleven holds exactly one return
    minutes = (0, 1, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300)
    load_sparse_symbol(migrated_dsn, "MANY", (PLAIN_TUESDAY,), minutes)

    with connect(migrated_dsn) as conn:
        # the free-choice plan is a GroupAggregate fed by a Sort keyed on bucket_minute itself,
        # which hands the final ORDER BY its order for free and hides a missing clause entirely.
        # forcing a HashAggregate removes that incidental ordering -- a hash aggregate's output
        # order tracks its hash table, not its input, regardless of how the rows arrived
        conn.execute("SET enable_hashagg = on")
        conn.execute("SET enable_sort = off")
        rows = conn.execute(
            query_sql("01_volatility.sql"),
            {"symbol": "MANY", "start": WINDOW_START, "end": WINDOW_END},
        ).fetchall()

    assert [r[0] for r in rows] == sorted(r[0] for r in rows)

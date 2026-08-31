from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from db.session import connect

START = date(2026, 6, 1)
END = date(2026, 6, 30)

# two full sessions and one half day; the 4th is a holiday and is simply absent, because market_days carries no is_open flag
FULL_DAYS = (date(2026, 6, 1), date(2026, 6, 2))
HALF_DAY = date(2026, 6, 3)
HOLIDAY = date(2026, 6, 4)

# 09:30 ET open and 16:00 ET close in EDT, and a 13:00 ET early close on the half day
OPEN_UTC = (13, 30)
CLOSE_UTC = (20, 0)
HALF_CLOSE_UTC = (17, 0)


def _ts(day: date, hm: tuple[int, int], minute_offset: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1] + minute_offset, tzinfo=UTC)


def _seed(dsn: str) -> None:
    with connect(dsn) as conn:
        for day in FULL_DAYS:
            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%s, %s, %s, 390)",
                (day, _ts(day, OPEN_UTC), _ts(day, CLOSE_UTC)),
            )
        conn.execute(
            "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
            " VALUES (%s, %s, %s, 210)",
            (HALF_DAY, _ts(HALF_DAY, OPEN_UTC), _ts(HALF_DAY, HALF_CLOSE_UTC)),
        )
        conn.commit()


def _add_symbol(dsn: str, symbol: str, first_bar_ts: datetime | None) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES (%s, %s, 'X', true, %s)",
            (symbol, symbol, first_bar_ts),
        )
        conn.commit()


def _add_bars(dsn: str, symbol: str, stamps: list[datetime]) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bars_2026_06 (LIKE bars INCLUDING ALL)"
        )
        if not conn.execute(
            "SELECT relispartition FROM pg_class WHERE oid = to_regclass('public.bars_2026_06')"
        ).fetchone()[0]:
            conn.execute(
                "ALTER TABLE bars ATTACH PARTITION bars_2026_06"
                " FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')"
            )
        for stamp in stamps:
            conn.execute(
                "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
                " VALUES (%s, %s, 10, 11, 9, 10, 100, 4, 10)",
                (symbol, stamp),
            )
        conn.commit()


def _progress(dsn: str, symbol: str, month: date, rows: int) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)"
            " VALUES (%s, %s, now(), %s, 0)",
            (symbol, month, rows),
        )
        conn.commit()


def _read(dsn: str, query_sql, start: date = START, end: date = END) -> list[tuple]:
    with connect(dsn) as conn:
        return conn.execute(
            query_sql("09_coverage.sql"), {"start": start, "end": end}
        ).fetchall()


@pytest.fixture
def loaded(migrated_dsn, query_sql):
    """One symbol covering the whole window, one listed mid-window, one never ingested."""
    _seed(migrated_dsn)
    # EARLY prints from the first session's open, so every session in the window is expected of it
    _add_symbol(migrated_dsn, "EARLY", _ts(FULL_DAYS[0], OPEN_UTC))
    # LATE's first bar is on the half day, so the two full sessions above it are floored out of its denominator
    _add_symbol(migrated_dsn, "LATE", _ts(HALF_DAY, OPEN_UTC))
    # NEVER has no first_bar_ts at all and belongs only on the uningested line
    _add_symbol(migrated_dsn, "NEVER", None)

    _add_bars(migrated_dsn, "EARLY", [
        _ts(FULL_DAYS[0], OPEN_UTC),
        _ts(FULL_DAYS[0], OPEN_UTC, 1),
        _ts(FULL_DAYS[1], OPEN_UTC),
        _ts(HALF_DAY, OPEN_UTC),
        # before the open and exactly on the close: neither may reach the numerator, and the second is the half-open boundary
        _ts(FULL_DAYS[0], OPEN_UTC, -30),
        _ts(FULL_DAYS[0], CLOSE_UTC),
    ])
    _add_bars(migrated_dsn, "LATE", [_ts(HALF_DAY, OPEN_UTC), _ts(HALF_DAY, OPEN_UTC, 1)])

    _progress(migrated_dsn, "EARLY", START, 4)
    _progress(migrated_dsn, "LATE", START, 2)
    return migrated_dsn


def test_the_three_summary_rows_are_last_and_keep_their_fixed_order(loaded, query_sql):
    rows = _read(loaded, query_sql)

    assert [r[0] for r in rows[-3:]] == ["missing_units", "coverage_pct", "uningested_symbols"]
    # the detail band sits above, which is what lets both gates read the summary through tail -3
    assert [r[0] for r in rows[:-3]] == ["EARLY", "LATE"]


def test_the_detail_rows_stay_alphabetical_even_when_loaded_out_of_order(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    # inserted neither alphabetically nor as a palindrome of it, so nothing but the trailing
    # ORDER BY could be what puts them back in label order
    for symbol in ("ZEB", "ALP", "MID"):
        _add_symbol(migrated_dsn, symbol, _ts(FULL_DAYS[0], OPEN_UTC))
        _add_bars(migrated_dsn, symbol, [_ts(FULL_DAYS[0], OPEN_UTC)])

    detail = [r[0] for r in _read(migrated_dsn, query_sql)[:-3]]

    assert detail == ["ALP", "MID", "ZEB"]


def test_the_detail_band_carries_one_row_per_ingested_symbol_and_none_for_the_uningested_one(loaded, query_sql):
    detail = _read(loaded, query_sql)[:-3]

    # one number per symbol over the whole window, never one row per (symbol, month)
    assert [r[0] for r in detail] == ["EARLY", "LATE"]
    assert "NEVER" not in [r[0] for r in detail]


def test_a_symbol_listed_mid_window_is_floored_onto_the_sessions_after_its_first_bar(loaded, query_sql):
    detail = dict(_read(loaded, query_sql)[:-3])

    # LATE's floored denominator is the half day alone, so 100 * 2 bars / 210 minutes = 0.95; unfloored it would be 100 * 2 / 990 = 0.20
    assert detail["LATE"] == Decimal("0.95")


def test_bars_outside_the_session_never_reach_the_numerator(loaded, query_sql):
    detail = dict(_read(loaded, query_sql)[:-3])

    # EARLY holds 6 bars but only 4 are inside a session; the pre-open one and the one exactly on close_ts are excluded, the latter because membership is half-open
    # 400 / (390 + 390 + 210) = 0.4040... -> 0.40
    assert detail["EARLY"] == Decimal("0.40")


def test_coverage_never_exceeds_one_hundred_percent(loaded, query_sql):
    rows = _read(loaded, query_sql)

    # a <= on close_ts, or a denominator floored on open_ts, would put a symbol above 100 and the spec calls that a bug rather than good news
    assert all(value is None or value <= 100 for _, value in rows)


def test_a_half_day_contributes_its_own_minutes_and_an_absent_holiday_contributes_none(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    _add_symbol(migrated_dsn, "HALF", _ts(HALF_DAY, OPEN_UTC))
    _add_bars(migrated_dsn, "HALF", [_ts(HALF_DAY, OPEN_UTC, i) for i in range(21)])
    _progress(migrated_dsn, "HALF", START, 21)

    detail = dict(_read(migrated_dsn, query_sql)[:-3])

    # 21 bars over the half day's 210 minutes is exactly 10.00; a hardcoded 390, or a holiday row invented for the 4th, would both move this
    assert detail["HALF"] == Decimal("10.00")
    assert HOLIDAY not in FULL_DAYS


def test_uningested_symbols_counts_exactly_the_symbols_with_no_first_bar_ts(loaded, query_sql):
    summary = dict(_read(loaded, query_sql)[-3:])

    assert summary["uningested_symbols"] == 1


def test_a_ticker_month_with_no_progress_row_is_counted_missing(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    # both symbols are expected to cover 2026-06, and neither is given a progress row for it
    _add_symbol(migrated_dsn, "AAA", _ts(FULL_DAYS[0], OPEN_UTC))
    _add_symbol(migrated_dsn, "BBB", _ts(FULL_DAYS[0], OPEN_UTC))
    _add_bars(migrated_dsn, "AAA", [_ts(FULL_DAYS[0], OPEN_UTC)])
    _add_bars(migrated_dsn, "BBB", [_ts(FULL_DAYS[0], OPEN_UTC)])

    summary = dict(_read(migrated_dsn, query_sql)[-3:])

    # the window holds one month and two symbols expect it, so two (symbol, month) pairs have no progress row
    assert summary["missing_units"] == 2


def test_a_symbol_with_a_progress_row_is_not_counted_missing(loaded, query_sql):
    summary = dict(_read(loaded, query_sql)[-3:])

    # read beside the nonzero case above: on its own a join returning the empty set would satisfy it
    assert summary["missing_units"] == 0


def test_the_overall_percentage_is_the_pooled_ratio_rather_than_the_mean_of_the_two_symbols(loaded, query_sql):
    summary = dict(_read(loaded, query_sql)[-3:])

    # (4 + 2) bars over (990 + 210) minutes is 0.50; the mean of 0.40 and 0.95 would read 0.68
    assert summary["coverage_pct"] == Decimal("0.50")


def test_nothing_ingested_reports_zero_missing_an_empty_percentage_and_the_whole_universe(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    for symbol in ("AAA", "BBB", "CCC"):
        _add_symbol(migrated_dsn, symbol, None)

    rows = _read(migrated_dsn, query_sql)

    # the detail band is empty and the percentage is NULL, which is the honest answer to nothing ingested rather than a zero that reads like a measurement
    assert rows == [("missing_units", 0), ("coverage_pct", None), ("uningested_symbols", 3)]


def test_a_count_prints_without_a_scale_while_a_percentage_keeps_two_places(loaded, query_sql):
    summary = dict(_read(loaded, query_sql)[-3:])

    # the gate criteria assert exact strings, so a uniform numeric(10, 2) would print missing_units as 0.00 and fail them
    assert str(summary["missing_units"]) == "0"
    assert str(summary["coverage_pct"]) == "0.50"


def test_the_render_leaves_no_psql_placeholder_and_covers_the_declared_parameters(query_sql):
    rendered = query_sql("09_coverage.sql")

    assert ":'" not in rendered
    assert "%(start)s" in rendered and "%(end)s" in rendered


def test_a_parameter_bound_without_being_declared_fails_the_render(tmp_path, monkeypatch, repo_root, query_sql):
    # the guard is asserted rather than trusted: a parameter added to the body and left out of the header would otherwise be bound silently and never checked
    original = (repo_root / "db" / "queries" / "09_coverage.sql").read_text()
    undeclared = original.replace("m.day <= :'end'", "m.day <= :'end' AND m.day <> :'sneaked'")
    (repo_root / "db" / "queries" / "_render_probe.sql").write_text(undeclared)
    try:
        with pytest.raises(AssertionError, match="header declares"):
            query_sql("_render_probe.sql")
    finally:
        (repo_root / "db" / "queries" / "_render_probe.sql").unlink()


def test_a_percent_sign_in_the_file_survives_the_render(repo_root, query_sql, migrated_dsn):
    # psycopg reads a bare % as its own placeholder, so an unescaped "100%" in a comment fails the bind with an error naming neither comments nor the file -- and Feature 4 adds nine more query files through this fixture
    assert "%" in (repo_root / "db" / "queries" / "09_coverage.sql").read_text()

    with connect(migrated_dsn) as conn:
        conn.execute(query_sql("09_coverage.sql"), {"start": START, "end": END})


def test_a_query_file_declaring_no_parameters_renders_rather_than_raising(repo_root, query_sql):
    # several of the ten analytical queries bind no parameters at all, and a fixture that indexed into a missing header would fail them on a correct file
    probe = repo_root / "db" / "queries" / "_no_params_probe.sql"
    probe.write_text("-- a query with nothing to bind\nSELECT count(*) FROM symbols;\n")
    try:
        assert "SELECT count(*)" in query_sql("_no_params_probe.sql")
    finally:
        probe.unlink()


def test_a_symbol_whose_leading_months_failed_is_reported_missing_rather_than_floored_away(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    # LOST printed nothing until the half day: its two earlier months are the ones a first_bar_ts floor would silently drop
    _add_symbol(migrated_dsn, "LOST", _ts(HALF_DAY, OPEN_UTC))
    _add_bars(migrated_dsn, "LOST", [_ts(HALF_DAY, OPEN_UTC)])
    _progress(migrated_dsn, "LOST", START, 1)

    # a window whose earlier months hold trading days the symbol has no progress row for
    with connect(migrated_dsn) as conn:
        for day in (date(2026, 4, 15), date(2026, 5, 15)):
            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%s, %s, %s, 390)",
                (day, _ts(day, OPEN_UTC), _ts(day, CLOSE_UTC)),
            )
        conn.commit()

    summary = dict(_read(migrated_dsn, query_sql, start=date(2026, 4, 1), end=END)[-3:])

    # April and May were in the window and left no progress row, so they are two lost units and not two months the symbol was never expected to cover
    assert summary["missing_units"] == 2


def test_the_bounded_cte_is_inlined_rather_than_materialised(loaded, query_sql):
    with connect(loaded) as conn:
        plan = "\n".join(
            row[0] for row in conn.execute(
                "EXPLAIN " + query_sql("09_coverage.sql"), {"start": START, "end": END}
            ).fetchall()
        )

    # NOT MATERIALIZED is a directive rather than a cost decision, so it holds at any data size
    # and this assertion is plan-shape independent. bounded is referenced three times, so without
    # it Postgres materialises the CTE, the planner loses market_days' statistics, and it cannot
    # cost a hash join -- it falls back to a merge join and sorts every bar in the database.
    # Measured on the full universe: 173.9 s serial with a spill, against 32.7 s with 71 parallel
    # scans, 2 workers launched and no spill at all
    assert "CTE Scan on bounded" not in plan
    assert "CTE bounded" not in plan


def test_the_numerator_does_not_depend_on_the_first_bar_floor(migrated_dsn, query_sql):
    _seed(migrated_dsn)
    # first_bar_ts is the second session's open while the symbol printed in the first session too.
    # this is a database that violates the pipeline's own contract, where recompute_first_bar_ts
    # sets first_bar_ts to MIN(ts) -- checked on the live universe, where all 100 symbols hold it
    # exactly. The counting join is bounded by market_days alone rather than by the floored
    # session set, so a bar earlier than its own symbol's floor is still counted; the floor
    # applies to the denominator, which is what it is for
    _add_symbol(migrated_dsn, "EARLYBIRD", _ts(FULL_DAYS[1], OPEN_UTC))
    _add_bars(migrated_dsn, "EARLYBIRD", [
        _ts(FULL_DAYS[0], OPEN_UTC),
        _ts(FULL_DAYS[1], OPEN_UTC),
    ])
    _progress(migrated_dsn, "EARLYBIRD", START, 2)

    detail = dict(_read(migrated_dsn, query_sql)[:-3])

    # both bars reach the numerator; the denominator is floored to the second and third sessions,
    # 390 + 210 = 600 minutes, so 100 * 2 / 600 = 0.33
    assert detail["EARLYBIRD"] == Decimal("0.33")

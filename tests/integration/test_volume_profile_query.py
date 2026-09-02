from decimal import Decimal

from psycopg.rows import dict_row

from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    DST_FRIDAY_EST,
    DST_MONDAY_EDT,
    HALF_DAY,
    TRADING_DAYS,
    load,
    load_calendar,
    load_sparse_symbol,
    load_symbol,
    open_ts,
)


def _read(dsn, query_sql):
    with connect(dsn) as conn:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            # binds nothing: universe-wide over the whole window
            return cur.execute(query_sql("04_volume_profile.sql")).fetchall()


def test_every_bar_lands_in_the_hour_measured_from_that_days_open(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # minutes 0, 65 and 125 after the open, so one bar in each of the first three session hours
    load_sparse_symbol(migrated_dsn, "SPREAD", TRADING_DAYS, (0, 65, 125))

    rows = {r["session_hour"]: r for r in _read(migrated_dsn, query_sql)}

    assert sorted(rows) == [0, 1, 2]
    assert all(rows[h]["bars"] == len(TRADING_DAYS) for h in (0, 1, 2))


def test_the_bucket_is_minutes_since_open_and_not_a_utc_hour(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # the two sessions either side of the DST transition and nothing else
    load_symbol(migrated_dsn, "PAIR", days=(DST_FRIDAY_EST, DST_MONDAY_EDT))

    rows = _read(migrated_dsn, query_sql)

    # both sessions open at 09:30 ET, at 14:30Z under EST and 13:30Z under EDT. bucketed on
    # ts - open_ts both land in hour 0; on a UTC hour they split into hours 14 and 13, and a
    # whole-history profile would then smear every hour into its neighbour for a third of the sample
    assert open_ts(DST_FRIDAY_EST).hour != open_ts(DST_MONDAY_EDT).hour
    assert [r["session_hour"] for r in rows] == [0]
    assert rows[0]["bars"] == 2 * BARS_PER_SESSION


def test_the_shares_sum_to_one_hundred_percent(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_sparse_symbol(migrated_dsn, "SPREAD", TRADING_DAYS, (0, 65, 125))

    rows = _read(migrated_dsn, query_sql)

    # the window aggregate divides by the total over the whole result rather than by a
    # per-group total, which is the difference between a profile and a column of 100s.
    # each share is rounded to 4 places independently, so the sum need not be exactly 100 --
    # three equal buckets give 33.3333 each and total 99.9999 -- and the tolerance is the
    # per-row rounding, not a fudge factor
    total = sum(r["pct_of_volume"] for r in rows)
    assert abs(total - Decimal("100")) <= Decimal("0.0001") * len(rows)
    assert all(r["pct_of_volume"] > 0 for r in rows)


def test_the_label_names_the_start_of_the_bucket_in_new_york_wall_clock(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    load_sparse_symbol(migrated_dsn, "SPREAD", TRADING_DAYS, (0, 65, 125))

    rows = {r["session_hour"]: r for r in _read(migrated_dsn, query_sql)}

    # every session opens 09:30 ET, so the mapping is fixed and DST cannot move it. it is a start
    # rather than a range because the last bucket is truncated by the close -- 30 minutes on a
    # normal day, nothing at all on a half day -- so a range label would be wrong wherever it showed
    assert rows[0]["et_from"] == "09:30"
    assert rows[1]["et_from"] == "10:30"
    assert rows[2]["et_from"] == "11:30"


def test_a_half_day_contributes_no_hours_past_its_own_close(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # minute 250 sits inside a 390-minute session and past the half day's 210th minute, so the
    # half day's close_ts is the only thing that can exclude it
    load_sparse_symbol(migrated_dsn, "LATE", TRADING_DAYS, (0, 250))

    rows = {r["session_hour"]: r for r in _read(migrated_dsn, query_sql)}

    # hour 4 holds the three full sessions and not the half day. a hardcoded 390-minute session,
    # or a close taken from anywhere but market_days, would put four bars here
    assert rows[4]["bars"] == len(TRADING_DAYS) - 1
    assert rows[0]["bars"] == len(TRADING_DAYS)


def test_bars_outside_the_session_never_reach_the_profile(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    rows = _read(dsn, query_sql)

    # the fixture writes a pre-open bar and one exactly on close_ts for every day. the pre-open one
    # would compute a negative session hour and open a bucket of its own
    assert min(r["session_hour"] for r in rows) == 0
    assert sum(r["bars"] for r in rows) == len(TRADING_DAYS) * BARS_PER_SESSION


def test_the_profile_pools_the_whole_universe_rather_than_one_symbol(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, symbols=("AAA", "BBB", "CCC"))

    rows = _read(dsn, query_sql)

    assert rows[0]["symbols"] == 3
    assert rows[0]["bars"] == 3 * len(TRADING_DAYS) * BARS_PER_SESSION


def test_the_volume_trade_and_mean_columns_are_pinned_to_the_fixture(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    # every bar this fixture writes carries volume=100 and trade_count=1, so the sum and the
    # mean of the same column diverge sharply and a swap between sum and avg cannot pass by
    # coincidence the way it would if the totals happened to equal the averages
    load_sparse_symbol(migrated_dsn, "SPREAD", TRADING_DAYS, (0, 65, 125))

    rows = {r["session_hour"]: r for r in _read(migrated_dsn, query_sql)}

    for hour in (0, 1, 2):
        assert rows[hour]["volume"] == 100 * len(TRADING_DAYS)
        assert rows[hour]["trades"] == len(TRADING_DAYS)
        assert rows[hour]["mean_volume_per_bar"] == Decimal("100.0")


def test_the_file_binds_no_parameters_at_all(query_sql, repo_root):
    rendered = query_sql("04_volume_profile.sql")

    assert "%(" not in rendered
    assert ":'" not in rendered
    assert "-- parameters: none" in (repo_root / "db" / "queries" / "04_volume_profile.sql").read_text()

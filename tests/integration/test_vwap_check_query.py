from decimal import Decimal

from psycopg.rows import dict_row

from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    PLAIN_TUESDAY,
    TRADING_DAYS,
    load,
    load_calendar,
    load_symbol,
)


def _read(dsn, query_sql):
    with connect(dsn) as conn:
        # nine columns, read by name: the two bp columns sit beside three vwap columns and a
        # positional index into them is a silent off-by-one rather than a failure
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            # the file binds no parameters at all: it is universe-wide over the whole window
            return cur.execute(query_sql("07_vwap_check.sql")).fetchall()


def _by_key(rows):
    return {(r["symbol"], r["day"]): r for r in rows}


def _expected_session_vwap(day) -> Decimal:
    """Hand-computed from the fixture's bar shape: sum(vwap * volume) / sum(volume)."""
    from tests.market_fixture import _BAR_SHAPE, _DAY_OFFSET

    off = Decimal(str(_DAY_OFFSET[day]))
    num = sum((Decimal(str((h + low) / 2)) + off) * vol for _, h, low, _, vol in _BAR_SHAPE)
    den = sum(Decimal(vol) for *_, vol in _BAR_SHAPE)
    return num / den


def test_there_is_one_row_per_symbol_per_day(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, symbols=("AAA", "BBB"))

    rows = _read(dsn, query_sql)

    assert len(rows) == 2 * len(TRADING_DAYS)
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}


def test_the_session_vwap_is_the_volume_weighted_mean_of_the_feeds_own_vwap_column(
    migrated_dsn, query_sql
):
    dsn = load(migrated_dsn)

    row = _by_key(_read(dsn, query_sql))[("AAA", PLAIN_TUESDAY)]

    # hand-computed, and volume-weighted rather than a plain mean: the fixture's volumes run
    # 10..50, so an unweighted average of the same vwaps lands somewhere else entirely
    assert row["vwap_session"] == round(_expected_session_vwap(PLAIN_TUESDAY), 6)


def test_the_all_hours_figure_differs_from_the_session_one_when_extended_bars_exist(
    migrated_dsn, query_sql
):
    dsn = load(migrated_dsn, extended_hours=True)

    row = _by_key(_read(dsn, query_sql))[("AAA", PLAIN_TUESDAY)]

    # the delta this query exists to explain. the fixture's extended bars are priced at 1 against
    # a session vwap near 120, so the all-hours figure is dragged down and the bp column says so
    assert row["extended_bars"] == 2
    assert row["vwap_all_hours"] < row["vwap_session"]
    assert row["extended_hours_bp"] < Decimal("0")


def test_the_two_figures_agree_exactly_when_there_are_no_extended_bars(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _by_key(_read(dsn, query_sql))[("AAA", PLAIN_TUESDAY)]

    # read beside the test above: without this pair, a session filter that did nothing at all
    # would still satisfy one of them
    assert row["extended_bars"] == 0
    assert row["vwap_session"] == row["vwap_all_hours"]
    assert row["extended_hours_bp"] == Decimal("0.0000")


def test_the_session_bar_count_excludes_the_bar_exactly_on_close_ts(migrated_dsn, query_sql):
    dsn = load(migrated_dsn, extended_hours=True)

    row = _by_key(_read(dsn, query_sql))[("AAA", PLAIN_TUESDAY)]

    # membership is half-open everywhere, so the close_ts bar is one of the two extended ones
    assert row["session_bars"] == BARS_PER_SESSION
    assert row["extended_bars"] == 2


def test_the_query_reads_only_the_columns_the_covering_index_carries(query_sql, repo_root):
    body = (repo_root / "db" / "queries" / "07_vwap_check.sql").read_text()

    # This file's whole Class C evidence rests on the covering index being able to serve it
    # index-only, and that index is (symbol, ts) INCLUDE (vwap, volume). Reading any other bars
    # column makes an index-only scan impossible by construction -- no plan, no tuning and no
    # VACUUM recovers it. An earlier draft reconstructed a typical price from high, low and
    # close; measured, the forced scan then reported 0 index-only scans and 682,813 root blocks
    # against the seq scan's 514,260 -- 0.75x, worse than what it replaced -- and the byte-ratio
    # prediction missed by 60%. Asserting the file text is weak, and it is the only instrument
    # that catches the column being added back.
    select_body = body[body.index("WITH stamped"):]
    for forbidden in ("b.high", "b.low", "b.close", "s.high", "s.low", "s.close",
                      "high +", "+ low", "+ close"):
        assert forbidden not in select_body, forbidden
    for needed in ("b.vwap", "b.volume", "b.symbol", "b.ts"):
        assert needed in select_body, needed


def test_every_figure_stays_numeric_and_exact(migrated_dsn, query_sql):
    dsn = load(migrated_dsn)

    row = _by_key(_read(dsn, query_sql))[("AAA", PLAIN_TUESDAY)]

    # this query crosses no float boundary: there is no corr, sqrt, ln or percentile_cont in it,
    # so unlike queries 1, 2 and 3 it is exact end to end
    for column in ("vwap_session", "vwap_all_hours", "extended_hours_bp"):
        assert isinstance(row[column], Decimal), column


def test_bars_on_a_day_that_is_not_a_trading_day_produce_no_row(migrated_dsn, query_sql):
    from datetime import UTC, datetime

    from tests.market_fixture import HOLIDAY

    load_calendar(migrated_dsn)
    load_symbol(migrated_dsn, "AAA")
    with connect(migrated_dsn) as conn:
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES ('AAA', %s, 1, 1, 1, 1, 5, 1, 1)",
            (datetime(HOLIDAY.year, HOLIDAY.month, HOLIDAY.day, 15, 0, tzinfo=UTC),),
        )
        conn.commit()

    rows = _by_key(_read(migrated_dsn, query_sql))

    # the holiday has no market_days row, so there is no session to check the bar against and the
    # inner join drops it rather than inventing a day
    assert ("AAA", HOLIDAY) not in rows
    assert len(rows) == len(TRADING_DAYS)


def test_the_file_binds_no_parameters_at_all(query_sql, repo_root):
    rendered = query_sql("07_vwap_check.sql")

    # the parameter table makes this one universe-wide over the whole window. a range predicate
    # true of every row is still evaluated on every row, and pushing one into an Index Cond makes
    # the covering-index path cheaper -- which flips the planner's free choice and corrupts the
    # ratio HEAP_INDEX_COVERING_RATIO is measured against
    assert "%(" not in rendered
    assert ":'" not in rendered
    body = (repo_root / "db" / "queries" / "07_vwap_check.sql").read_text()
    assert "-- parameters: none" in body

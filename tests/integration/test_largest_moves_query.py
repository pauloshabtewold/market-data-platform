from datetime import UTC, datetime
from decimal import Decimal

import pytest
from psycopg.rows import dict_row

from db.session import connect
from tests.market_fixture import (
    HALF_DAY,
    PLAIN_TUESDAY,
    TRADING_DAYS,
    WINDOW_END,
    WINDOW_START,
    bar_ts,
    close_ts,
    load,
    load_calendar,
    ensure_partition,
)


def _read(dsn, query_sql, start=WINDOW_START, end=WINDOW_END, min_move_pct=0, limit=50):
    with connect(dsn) as conn:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            return cur.execute(
                query_sql("05_largest_moves.sql"),
                {"start": start, "end": end, "min_move_pct": min_move_pct, "limit": limit},
            ).fetchall()


def _plant(dsn, symbol, day, minute, open_, close_):
    """One bar with a chosen open and close, so its move is exactly known."""
    with connect(dsn) as conn:
        ensure_partition(conn, day)
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES (%s, %s, 'X', true, %s) ON CONFLICT (symbol) DO NOTHING",
            (symbol, symbol, bar_ts(day, minute)),
        )
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES (%s, %s, %s, %s, %s, %s, 100, 1, %s)",
            (symbol, bar_ts(day, minute), open_, max(open_, close_), min(open_, close_),
             close_, (open_ + close_) / 2),
        )
        conn.commit()


@pytest.fixture
def planted(migrated_dsn):
    load_calendar(migrated_dsn)
    # three known moves: +10%, -20%, +5%
    _plant(migrated_dsn, "UP", PLAIN_TUESDAY, 1, Decimal(100), Decimal(110))
    _plant(migrated_dsn, "DOWN", PLAIN_TUESDAY, 2, Decimal(100), Decimal(80))
    _plant(migrated_dsn, "SMALL", PLAIN_TUESDAY, 3, Decimal(100), Decimal(105))
    return migrated_dsn


def test_the_moves_are_ranked_by_absolute_size_so_a_fall_outranks_a_smaller_rise(planted, query_sql):
    rows = _read(planted, query_sql)

    # -20% is the largest move even though it is the smallest signed number. ranking on the signed
    # move would put the +10% first and quietly turn this into a "largest gains" query
    assert [r["symbol"] for r in rows] == ["DOWN", "UP", "SMALL"]
    assert rows[0]["move_pct"] == Decimal("-20.0000")
    assert rows[1]["move_pct"] == Decimal("10.0000")


def test_the_move_is_computed_from_the_bars_own_open_and_close(planted, query_sql):
    rows = {r["symbol"]: r for r in _read(planted, query_sql)}

    assert rows["UP"]["open"] == Decimal("100.0000")
    assert rows["UP"]["close"] == Decimal("110.0000")
    assert rows["UP"]["move_pct"] == Decimal("10.0000")


def test_the_threshold_filters_on_the_absolute_move(planted, query_sql):
    rows = _read(planted, query_sql, min_move_pct=6)

    # 6% keeps the -20 and the +10 and drops the +5. a threshold applied to the signed move would
    # drop the largest move in the set
    assert sorted(r["symbol"] for r in rows) == ["DOWN", "UP"]


def test_a_threshold_of_zero_filters_nothing(planted, query_sql):
    # min_move_pct = 0 is the load-bearing case: the deep-pagination gate runs at it, and a
    # nonzero default would silently shrink the set that gate pages through
    assert len(_read(planted, query_sql, min_move_pct=0)) == 3


def test_the_threshold_is_compared_as_numeric_rather_than_through_a_float(planted, query_sql):
    # a threshold no float can hold exactly. numeric >= float8 has no operator, so an uncast
    # comparison casts the exact left side to double precision -- and 5.0000000000000001 as a
    # float is 5.0, which would keep the +5% move this excludes
    rows = _read(planted, query_sql, min_move_pct=Decimal("5.0000000000000001"))

    assert sorted(r["symbol"] for r in rows) == ["DOWN", "UP"]


def test_the_limit_caps_the_result(planted, query_sql):
    rows = _read(planted, query_sql, limit=2)

    assert len(rows) == 2
    assert [r["symbol"] for r in rows] == ["DOWN", "UP"]


def test_the_ranking_is_deterministic_when_two_moves_tie(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    for symbol in ("ZZZ", "AAA", "MMM"):
        _plant(migrated_dsn, symbol, PLAIN_TUESDAY, 1, Decimal(100), Decimal(110))

    first = [r["symbol"] for r in _read(migrated_dsn, query_sql)]
    second = [r["symbol"] for r in _read(migrated_dsn, query_sql)]

    # three identical moves. without the (symbol, ts) tie-break the order is whatever the sort
    # happened to produce and the README table stops reproducing between runs
    assert first == ["AAA", "MMM", "ZZZ"]
    assert first == second


def test_a_bar_outside_the_session_is_never_a_largest_move(migrated_dsn, query_sql):
    load_calendar(migrated_dsn)
    _plant(migrated_dsn, "IN", PLAIN_TUESDAY, 1, Decimal(100), Decimal(101))
    # a 90% move placed exactly on close_ts, which is outside the half-open session
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES ('OUT','OUT','X',true,%s)", (close_ts(PLAIN_TUESDAY),))
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES ('OUT', %s, 100, 190, 100, 190, 1, 1, 145)", (close_ts(PLAIN_TUESDAY),))
        conn.commit()

    rows = _read(migrated_dsn, query_sql)

    # the extended-hours print would top the ranking on any day it existed, which is exactly the
    # nonsense the session filter exists to prevent: a list of thin after-hours prints
    assert [r["symbol"] for r in rows] == ["IN"]


def test_the_window_is_bounded_at_both_ends(planted, query_sql):
    _plant(planted, "LATER", HALF_DAY, 1, Decimal(100), Decimal(150))

    inside = _read(planted, query_sql, start=WINDOW_START, end=PLAIN_TUESDAY)

    assert "LATER" not in [r["symbol"] for r in inside]


def test_the_final_session_survives_the_end_bound(planted, query_sql):
    _plant(planted, "LATER", HALF_DAY, 1, Decimal(100), Decimal(150))

    rows = _read(planted, query_sql, start=WINDOW_START, end=HALF_DAY)

    # :end is a date, so a bare b.ts < :end resolves to midnight UTC and drops the whole final
    # session. the + 1 day in the file is what keeps it, and this is the largest move in the set
    assert rows[0]["symbol"] == "LATER"


def test_the_rendered_file_declares_exactly_the_parameters_it_binds(query_sql):
    rendered = query_sql("05_largest_moves.sql")

    assert ":'" not in rendered
    for name in ("start", "end", "min_move_pct", "limit"):
        assert f"%({name})s" in rendered

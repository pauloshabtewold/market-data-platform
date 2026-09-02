"""The fixture's own guarantees, which no query test can observe.

Four loaders write out of timestamp order on purpose, because heap order is insert order
on a fresh table and that is the only condition under which a window function missing its
ORDER BY gives a different answer. Nothing asserted that they still do: an identity
permutation leaves every query test green while silently disarming the fixture.

Scrambling the order is only half of it. An order clause is observable only where the
values differ across the rows it reorders, so the bar shape's own distinctness is load
bearing too -- flatten it and the heap stays scrambled while every pick becomes the same
answer. Both halves are asserted here.
"""

import inspect

from db.session import connect
from tests import market_fixture
from tests.market_fixture import (
    BARS_PER_SESSION,
    TRADING_DAYS,
    _BAR_SHAPE,
    _SCRAMBLE,
    load_run_calendar,
    load_run_symbol,
    load_scrambled_symbol,
    load_sparse_symbol,
)


def _heap_order(dsn: str, symbol: str) -> list:
    # tableoid first because ctid is per partition and a run spans two months
    with connect(dsn) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT ts FROM bars WHERE symbol = %s ORDER BY tableoid, ctid", (symbol,)
            ).fetchall()
        ]


def test_the_bar_scramble_is_a_permutation_that_is_not_the_identity():
    assert sorted(_SCRAMBLE) == list(range(BARS_PER_SESSION)), "a bar is dropped or repeated"
    assert list(_SCRAMBLE) != sorted(_SCRAMBLE), "the scramble no longer scrambles"


def test_the_scrambled_symbol_lands_in_the_heap_out_of_timestamp_order(migrated_dsn):
    load_scrambled_symbol(migrated_dsn, "SCRAM")
    heap = _heap_order(migrated_dsn, "SCRAM")
    assert heap and heap != sorted(heap)


def test_the_scrambled_run_lands_in_the_heap_out_of_timestamp_order(migrated_dsn):
    days = load_run_calendar(migrated_dsn, scrambled=True)
    load_run_symbol(migrated_dsn, "UP", [100 + i for i in range(len(days))], scrambled=True)
    heap = _heap_order(migrated_dsn, "UP")
    assert heap and heap != sorted(heap)


def test_the_scrambled_calendar_is_written_out_of_day_order(migrated_dsn):
    # query 2's trading-day ordinal comes from row_number() over an explicit ORDER BY day
    load_run_calendar(migrated_dsn, scrambled=True)
    with connect(migrated_dsn) as conn:
        written = [r[0] for r in conn.execute(
            "SELECT day FROM market_days ORDER BY ctid"
        ).fetchall()]
    assert written and written != sorted(written)


def test_the_sparse_symbol_lands_in_the_heap_out_of_timestamp_order(migrated_dsn):
    # the fourth scrambler, and the one the other three pins do not reach: it is a parameter
    # rather than a module constant, so neutering it left all of them green while
    # test_volatility_query.py's span guard stopped catching a deleted window ORDER BY
    load_sparse_symbol(migrated_dsn, "SPARSE", TRADING_DAYS, (0, 5, 10), scrambled=True)
    heap = _heap_order(migrated_dsn, "SPARSE")
    assert heap and heap != sorted(heap)


def test_every_loader_that_takes_a_scrambled_flag_is_pinned_above():
    # the list is what drifted: three loaders were pinned and a fourth carried the same flag
    # unnoticed, so this asserts the set rather than the count, and a fifth fails here until
    # it has a pin of its own
    flagged = {
        name
        for name, fn in vars(market_fixture).items()
        # defined here rather than merely imported here: inspect.signature raises on some of
        # what a module namespace holds, and an imported helper is not this fixture's promise
        if inspect.isfunction(fn)
        and fn.__module__ == market_fixture.__name__
        and not name.startswith("_")
        and "scrambled" in inspect.signature(fn).parameters
    }
    assert flagged == {"load_sparse_symbol", "load_run_calendar", "load_run_symbol"}


def test_the_bar_shape_carries_distinct_values_so_a_wrong_pick_is_visible():
    # scrambling decides whether a missing ORDER BY reads different rows; these decide whether
    # reading different rows produces a different answer. flattening the shape keeps every
    # pin above green and makes 31 tests in two other files pass on a query that picks wrongly
    opens = [row[0] for row in _BAR_SHAPE]
    highs = [row[1] for row in _BAR_SHAPE]
    lows = [row[2] for row in _BAR_SHAPE]
    closes = [row[3] for row in _BAR_SHAPE]

    assert opens[0] != opens[-1], "open-from-last is indistinguishable from open-from-first"
    assert closes[0] != closes[-1], "close-from-first is indistinguishable from close-from-last"
    # the session high and low each live in exactly one bar, so a max or min taken over the
    # wrong subset misses rather than coincides
    assert highs.count(max(highs)) == 1 and max(highs) != highs[0]
    assert lows.count(min(lows)) == 1 and min(lows) != lows[0]
    # and every bar differs from every other, so no pair of positions can be swapped unseen
    assert len(set(_BAR_SHAPE)) == BARS_PER_SESSION

"""The fixture's own guarantees, which no query test can observe.

Three loaders write bars out of timestamp order on purpose, because heap order is insert
order on a fresh table and that is the only condition under which a window function
missing its ORDER BY gives a different answer. Nothing asserted that they still do: an
identity permutation leaves every query test green while silently disarming the fixture.
"""

from db.session import connect
from tests.market_fixture import (
    BARS_PER_SESSION,
    _SCRAMBLE,
    load_run_calendar,
    load_run_symbol,
    load_scrambled_symbol,
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

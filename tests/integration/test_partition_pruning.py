"""Whether the scalar bound five of the ten queries carry on bars.ts actually prunes.

01, 02, 03, 05 and 06 each join bars to market_days and also repeat the window as a plain
comparison against bars.ts, commented "redundant by logic and required for partition
pruning". Deleting it survives the whole suite in every one of the five, because the join
alone re-derives the same rows -- the logic half really is redundant. Nothing elsewhere
runs EXPLAIN against a scalar-bound query and looks at partition counts, so the half that
is not redundant -- the design measures it at 71 index descents per trading day against
one to four, on every Class A call -- has been resting on the comment alone.
"""

import re
from datetime import date

import pytest

from db.session import connect
from tests.market_fixture import ensure_partition

# six consecutive months so a query narrowed to one has five siblings for the planner to
# remove -- a single trading month, which is all the shared fixture ever spans, cannot
# demonstrate a removal at all
MONTHS = [date(2026, month, 1) for month in range(1, 7)]


def _six_months_of_partitions(dsn: str) -> str:
    with connect(dsn) as conn:
        for month in MONTHS:
            ensure_partition(conn, month)
        conn.commit()
    return dsn


WINDOW = {"start": date(2026, 3, 1), "end": date(2026, 3, 31)}
# all five statements the docstring above names, two of
# which the analytics endpoints serve and whose Class A numbers are published. Each entry is a
# statement and the parameters it declares
STATEMENTS = (
    ("01_volatility.sql", {"symbol": "AAA", **WINDOW}),
    ("02_correlation.sql", {"symbol_a": "AAA", "symbol_b": "BBB", **WINDOW}),
    ("03_gaps.sql", {"symbol": "AAA", **WINDOW}),
    ("05_largest_moves.sql", {"min_move_pct": 0, "limit": 10, **WINDOW}),
    ("06_daily_rollup.sql", {"symbol": "AAA", **WINDOW}),
)
# the rendered spelling of both halves of the bound; the file's own are
# AND b.ts >= :'start'::date and AND b.ts <  :'end'::date + INTERVAL '1 day'. Both, because the
# upper half alone still prunes the three partitions above the window and the control has to remove
# every scalar comparison on the partition key to show the join prunes nothing by itself
_BOUND = re.compile(
    r"\n\s*AND b\.ts >= %\(start\)s::date"
    r"|\n\s*AND b\.ts <\s+%\(end\)s::date \+ INTERVAL '1 day'"
)


def _plan(conn, sql, params):
    return "\n".join(row[0] for row in conn.execute("EXPLAIN " + sql, params).fetchall())


@pytest.mark.parametrize("name,params", STATEMENTS, ids=[name for name, _ in STATEMENTS])
def test_the_scalar_bound_on_bars_ts_prunes_every_partition_outside_the_window(
    migrated_dsn, query_sql, name, params
):
    dsn = _six_months_of_partitions(migrated_dsn)
    sql = query_sql(name)
    # the same statement with the bound's lower half taken out of the RENDERED text, never out of
    # the committed file: it is the negative control, and it is what makes the assertion below a
    # statement about the bound rather than about the join or the calendar
    without_bound, substitutions = _BOUND.subn("", sql)
    assert substitutions == 2, f"{name} carries {substitutions} halves of the bound, expected 2"

    with connect(dsn) as conn:
        plan = _plan(conn, sql, params)
        unbounded = _plan(conn, without_bound, params)

    # the bound is a plain comparison against bars.ts, the partition key, and the bind values are
    # known by executor init -- which plain EXPLAIN reaches even without ANALYZE -- so pruning
    # does not need a live run to show up. five of the six children fall outside the window
    assert "Subplans Removed: 5" in plan, plan
    assert "Subplans Removed" not in unbounded, unbounded

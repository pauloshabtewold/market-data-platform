"""Whether the scalar bound five of the ten queries carry on bars.ts actually prunes.

01, 02, 03, 05 and 06 each join bars to market_days and also repeat the window as a plain
comparison against bars.ts, commented "redundant by logic and required for partition
pruning". Deleting it survives the whole suite in every one of the five, because the join
alone re-derives the same rows -- the logic half really is redundant. Nothing elsewhere
runs EXPLAIN against a scalar-bound query and looks at partition counts, so the half that
is not redundant -- the design measures it at 71 index descents per trading day against
one to four, on every Class A call -- has been resting on the comment alone.
"""

from datetime import date

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


def test_the_scalar_bound_on_bars_ts_prunes_every_partition_outside_the_window(migrated_dsn, query_sql):
    dsn = _six_months_of_partitions(migrated_dsn)

    with connect(dsn) as conn:
        plan = "\n".join(
            row[0] for row in conn.execute(
                "EXPLAIN " + query_sql("06_daily_rollup.sql"),
                {"symbol": "AAA", "start": date(2026, 3, 1), "end": date(2026, 3, 31)},
            ).fetchall()
        )

    # the bound is a plain comparison against bars.ts, the partition key, and the bind values are
    # known by executor init -- which plain EXPLAIN reaches even without ANALYZE -- so pruning
    # does not need a live run to show up. five of the six children fall outside the window
    assert "Subplans Removed: 5" in plan

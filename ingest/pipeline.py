import logging
import time
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg import sql

from ingest.client import FatalVendorError, next_month
from ingest.validate import check_bars, window_bounds

log = logging.getLogger(__name__)

PROBE = "SELECT relispartition FROM pg_class WHERE oid = to_regclass(%s::text)"

INSERT_BARS = """
INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, ts) DO NOTHING
"""

INSERT_PROGRESS = """
INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)
VALUES (%s, %s, now(), %s, %s)
"""

# read back in the unit transaction, not from this fetch: a replay accepting less than is stored
# drops sum(row_count) below count(*) and fails reconciliation on a healthy database
COUNT_UNIT_BARS = "SELECT count(*) FROM bars WHERE symbol = %s AND ts >= %s AND ts < %s"

# correlated per symbol, not grouped: a GROUP BY omits a symbol with no bars, leaving a stale
# value the coverage query reads as ingested
RECOMPUTE_FIRST_BAR_TS = """
UPDATE symbols s
   SET first_bar_ts = (SELECT min(b.ts) FROM bars b WHERE b.symbol = s.symbol)
 WHERE s.first_bar_ts IS DISTINCT FROM (SELECT min(b.ts) FROM bars b WHERE b.symbol = s.symbol)
"""


@dataclass(frozen=True)
class RunSummary:
    units: int
    skipped: int
    rows: int
    rejected: int
    failed: tuple[tuple[str, date], ...]
    elapsed: float


def partition_name(month: date) -> str:
    return f"bars_{month:%Y_%m}"


def ensure_partition(conn: psycopg.Connection, month: date) -> None:
    # the bounds follow the window's first-of-month start: a day here narrows it below its data
    month = month.replace(day=1)
    child = partition_name(month)
    row = conn.execute(PROBE, (f"public.{child}",)).fetchone()

    if row is None:
        # to_regclass is NULL for a missing relation, so this returns zero rows, not a NULL row
        conn.execute(
            sql.SQL("CREATE TABLE {} (LIKE bars INCLUDING ALL)").format(sql.Identifier(child))
        )
    elif row[0]:
        return

    # PARTITION OF takes AccessExclusiveLock on bars; LIKE+ATTACH only ShareUpdateExclusiveLock
    conn.execute(
        sql.SQL("ALTER TABLE bars ATTACH PARTITION {} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(child),
            sql.Literal(month.isoformat()),
            sql.Literal(next_month(month).isoformat()),
        )
    )


def ingest_unit(conn: psycopg.Connection, symbol: str, month: date, fetch) -> tuple[int, int, int]:
    bars = fetch(symbol, month)
    # validated outside the transaction: a rejection costs no transaction time and cannot abort it
    checked = check_bars(bars, month)
    rows = [
        (b.symbol, b.ts, b.open, b.high, b.low, b.close, b.volume, b.trade_count, b.vwap)
        for b in checked.accepted
    ]

    inserted = 0
    with conn.transaction():
        ensure_partition(conn, month)
        with conn.cursor() as cur:
            if rows:
                cur.executemany(INSERT_BARS, rows)
                inserted = cur.rowcount
            lo, hi = window_bounds(month)
            stored = cur.execute(COUNT_UNIT_BARS, (symbol, lo, hi)).fetchone()[0]
            cur.execute(INSERT_PROGRESS, (symbol, month, stored, len(checked.rejected)))

    return stored, len(checked.rejected), inserted


def recompute_first_bar_ts(conn: psycopg.Connection) -> int:
    # after the run, not on insert: resume can ingest an earlier month after a later one
    cur = conn.execute(RECOMPUTE_FIRST_BAR_TS)
    conn.commit()
    return cur.rowcount


def months_between(start: date, end: date) -> list[date]:
    months, cursor = [], start.replace(day=1)
    while cursor <= end:
        months.append(cursor)
        cursor = next_month(cursor)
    return months


def run(
    conn: psycopg.Connection,
    symbols: list[str],
    start: date,
    end: date,
    fetch,
) -> RunSummary:
    done = {
        (row[0], row[1])
        for row in conn.execute("SELECT symbol, month FROM ingest_progress")
    }
    # closes the read's transaction: each unit below commits at top level, not as a savepoint
    conn.commit()

    began = time.monotonic()
    units = skipped = rows = rejected = 0
    failed: list[tuple[str, date]] = []
    for symbol in symbols:
        for month in months_between(start, end):
            if (symbol, month) in done:
                skipped += 1
                continue
            try:
                parsed, refused, inserted = ingest_unit(conn, symbol, month, fetch)
            except FatalVendorError:
                # first, or the except Exception below swallows the one class that must stop the run
                raise
            except Exception as exc:
                # no progress row for a failure: a row here is a permanent skip on every resume
                log.error("%s %s failed: %s", symbol, f"{month:%Y-%m}", exc)
                failed.append((symbol, month))
                if conn.closed:
                    # unclearable as a fatal status: each later unit spends a vendor request first
                    raise
                continue
            log.info("%s %s parsed=%d inserted=%d", symbol, f"{month:%Y-%m}", parsed, inserted)
            units += 1
            rows += parsed
            rejected += refused

    # a tuple so frozen=True means what it says and the dataclass stays hashable
    return RunSummary(units, skipped, rows, rejected, tuple(failed), time.monotonic() - began)

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

# read back inside the unit transaction rather than counting what this fetch accepted: a replay whose accepted set is smaller than what is already stored would otherwise leave sum(row_count) below count(*) and fail the reconciliation on a healthy database
COUNT_UNIT_BARS = "SELECT count(*) FROM bars WHERE symbol = %s AND ts >= %s AND ts < %s"

# correlated per symbol rather than grouped over bars: a GROUP BY cannot express a symbol holding no bars, which would keep a stale value the coverage query then reads as ingested
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
    # the request window is built from the month's first day, so the bounds are too -- a day component reaching here would leave the child's range narrower than its data.
    month = month.replace(day=1)
    child = partition_name(month)
    row = conn.execute(PROBE, (f"public.{child}",)).fetchone()

    if row is None:
        # to_regclass yields NULL for a missing relation so this predicate returns zero rows rather than a row holding NULL.
        conn.execute(
            sql.SQL("CREATE TABLE {} (LIKE bars INCLUDING ALL)").format(sql.Identifier(child))
        )
    elif row[0]:
        return

    # PARTITION OF takes an AccessExclusiveLock on the parent where LIKE plus ATTACH takes only ShareUpdateExclusiveLock.
    conn.execute(
        sql.SQL("ALTER TABLE bars ATTACH PARTITION {} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(child),
            sql.Literal(month.isoformat()),
            sql.Literal(next_month(month).isoformat()),
        )
    )


def ingest_unit(conn: psycopg.Connection, symbol: str, month: date, fetch) -> tuple[int, int, int]:
    bars = fetch(symbol, month)
    # validated outside the transaction, so a rejected bar costs no transaction time and rejection can never abort the unit
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
    # recomputed after the run and never set on insert, because resume can ingest an earlier month after a later one
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
    # closes the read's transaction so each unit below is a top-level one that commits, rather than a savepoint inside it.
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
                # ordered first: the clause below is except Exception and would otherwise swallow the one failure class that must stop the run
                raise
            except Exception as exc:
                # no progress row for a failed unit, because a row here is a permanent skip on every future resume
                log.error("%s %s failed: %s", symbol, f"{month:%Y-%m}", exc)
                failed.append((symbol, month))
                continue
            log.info("%s %s parsed=%d inserted=%d", symbol, f"{month:%Y-%m}", parsed, inserted)
            units += 1
            rows += parsed
            rejected += refused

    # a tuple rather than a list, so frozen=True means what it says and the dataclass stays hashable
    return RunSummary(units, skipped, rows, rejected, tuple(failed), time.monotonic() - began)

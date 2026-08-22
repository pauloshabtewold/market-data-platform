import time
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg import sql

from ingest.client import next_month

PROBE = "SELECT relispartition FROM pg_class WHERE oid = to_regclass(%s::text)"

INSERT_BARS = """
INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, ts) DO NOTHING
"""

INSERT_PROGRESS = """
INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)
VALUES (%s, %s, now(), %s, 0)
"""


@dataclass(frozen=True)
class RunSummary:
    units: int
    skipped: int
    rows: int
    elapsed: float


def partition_name(month: date) -> str:
    return f"bars_{month:%Y_%m}"


def ensure_partition(conn: psycopg.Connection, month: date) -> None:
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


def ingest_unit(conn: psycopg.Connection, symbol: str, month: date, fetch) -> tuple[int, int]:
    bars = fetch(symbol, month)
    rows = [
        (b.symbol, b.ts, b.open, b.high, b.low, b.close, b.volume, b.trade_count, b.vwap)
        for b in bars
    ]

    inserted = 0
    with conn.transaction():
        ensure_partition(conn, month)
        with conn.cursor() as cur:
            if rows:
                cur.executemany(INSERT_BARS, rows)
                inserted = cur.rowcount
            # row_count is the parsed length: a replay affects zero rows under DO NOTHING so an affected count here would break reconciliation against a healthy database.
            cur.execute(INSERT_PROGRESS, (symbol, month, len(rows)))

    return len(rows), inserted


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
    units = skipped = rows = 0
    for symbol in symbols:
        for month in months_between(start, end):
            if (symbol, month) in done:
                skipped += 1
                continue
            parsed, inserted = ingest_unit(conn, symbol, month, fetch)
            print(f"{symbol} {month:%Y-%m} parsed={parsed} inserted={inserted}")
            units += 1
            rows += parsed

    return RunSummary(units, skipped, rows, time.monotonic() - began)

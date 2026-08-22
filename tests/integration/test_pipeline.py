import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest

from db.session import connect
from ingest.client import Bar
from ingest.pipeline import ensure_partition, ingest_unit, partition_name, run

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)


def _bars(symbol: str, month: date, count: int) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=datetime(month.year, month.month, 1, 13, 30 + i, tzinfo=UTC),
            open=Decimal("310.13"),
            high=Decimal("310.44"),
            low=Decimal("309.71"),
            close=Decimal("310.00"),
            volume=140 + i,
            trade_count=4,
            vwap=Decimal("310.13"),
        )
        for i in range(count)
    ]


class RecordingFetcher:
    """Stands in for the bars client so no test spends the shared request budget."""

    def __init__(self, counts=None):
        self.counts = counts or {}
        self.calls = []

    def __call__(self, symbol, month):
        self.calls.append((symbol, month))
        return _bars(symbol, month, self.counts.get((symbol, month), 3))


def _scalar(dsn, query):
    with connect(dsn) as conn:
        return conn.execute(query).fetchone()[0]


def test_a_unit_ingested_twice_leaves_an_identical_row_count(migrated_dsn):
    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        ingest_unit(conn, "AAPL", JUNE, fetch)
        first = conn.execute("SELECT count(*) FROM bars").fetchone()[0]

    with connect(migrated_dsn) as conn:
        conn.execute("DELETE FROM ingest_progress")
        conn.commit()
        parsed, inserted = ingest_unit(conn, "AAPL", JUNE, fetch)

    assert (parsed, inserted) == (3, 0)
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == first == 3


def test_row_count_records_parsed_bars_and_not_the_affected_count(migrated_dsn):
    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        ingest_unit(conn, "AAPL", JUNE, fetch)
        conn.execute("DELETE FROM ingest_progress")
        conn.commit()
        ingest_unit(conn, "AAPL", JUNE, fetch)

    with connect(migrated_dsn) as conn:
        row = conn.execute("SELECT row_count, rejected_count FROM ingest_progress").fetchone()
    # the replay affected zero rows, so a row_count of 0 here is the reconciliation defect
    assert row == (3, 0)


def test_units_already_recorded_are_skipped_and_never_fetched(migrated_dsn):
    with connect(migrated_dsn) as conn:
        conn.execute(
            "INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)"
            " VALUES ('AAPL', %s, now(), 99, 0)",
            (JUNE,),
        )
        conn.commit()

    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL", "MSFT"], JUNE, JUNE, fetch)

    assert (summary.units, summary.skipped, summary.rows) == (1, 1, 3)
    assert fetch.calls == [("MSFT", JUNE)]


def test_resume_skips_the_done_month_and_still_runs_the_incomplete_one(migrated_dsn):
    with connect(migrated_dsn) as conn:
        conn.execute(
            "INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)"
            " VALUES ('AAPL', %s, now(), 3, 0)",
            (JUNE,),
        )
        conn.commit()

    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL"], JUNE, JULY, fetch)

    # a symbol-keyed skip would suppress JULY too, which on the real run drops every remaining month for that ticker
    assert (summary.units, summary.skipped) == (1, 1)
    assert fetch.calls == [("AAPL", JULY)]


def test_rows_route_to_their_month_and_a_second_month_attaches_its_own(migrated_dsn):
    with connect(migrated_dsn) as conn:
        run(conn, ["AAPL"], JUNE, JULY, RecordingFetcher())

    with connect(migrated_dsn) as conn:
        placed = conn.execute(
            "SELECT tableoid::regclass::text, count(*) FROM bars GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert placed == [(partition_name(JUNE), 3), (partition_name(JULY), 3)]

    # the UTC pin is what keeps these bounds off 04:00, so the offsets are part of the assertion
    with connect(migrated_dsn) as conn:
        bounds = conn.execute(
            "SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = %s",
            (partition_name(JUNE),),
        ).fetchone()[0]
    assert "'2026-06-01 00:00:00+00'" in bounds
    assert "'2026-07-01 00:00:00+00'" in bounds


def test_ensure_partition_creates_then_attaches_then_skips(migrated_dsn):
    child = partition_name(JUNE)
    relispartition = f"SELECT relispartition FROM pg_class WHERE oid = to_regclass('public.{child}')"

    with connect(migrated_dsn) as conn:
        assert conn.execute(relispartition).fetchone() is None
        ensure_partition(conn, JUNE)
        conn.commit()
        assert conn.execute(relispartition).fetchone() == (True,)

        # an out-of-band detach is what actually reaches the created-but-unattached state, so the probe stays three-way
        conn.execute(f"ALTER TABLE bars DETACH PARTITION {child}")
        conn.commit()
        assert conn.execute(relispartition).fetchone() == (False,)

        ensure_partition(conn, JUNE)
        conn.commit()
        assert conn.execute(relispartition).fetchone() == (True,)

        ensure_partition(conn, JUNE)
        conn.commit()
        assert conn.execute(relispartition).fetchone() == (True,)


def test_a_failing_fetch_leaves_neither_bars_nor_a_progress_row(migrated_dsn):
    def explode(symbol, month):
        raise RuntimeError("vendor said no")

    with connect(migrated_dsn) as conn:
        with pytest.raises(RuntimeError):
            ingest_unit(conn, "AAPL", JUNE, explode)

    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == 0
    assert _scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress") == 0


def test_a_failing_insert_leaves_neither_bars_nor_a_progress_row(migrated_dsn):
    def one_bad_bar(symbol, month):
        good = _bars(symbol, month, 1)
        return good + [good[0].__class__(**{**good[0].__dict__, "volume": 10**30})]

    with connect(migrated_dsn) as conn:
        with pytest.raises(psycopg.errors.NumericValueOutOfRange):
            ingest_unit(conn, "AAPL", JUNE, one_bad_bar)

    # the progress row must be incapable of existing without its bars, so the partition DDL is inside the same transaction
    assert _scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress") == 0
    assert _scalar(
        migrated_dsn,
        f"SELECT count(*) FROM pg_class WHERE oid = to_regclass('public.{partition_name(JUNE)}')",
    ) == 0


def test_a_completed_unit_is_visible_to_another_connection_before_the_next_one_starts(migrated_dsn):
    seen = []

    def fetch(symbol, month):
        # a separate connection sees a finished unit only if run() left no transaction open around the loop, which is the whole of what surviving a kill -9 rests on
        seen.append(_scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress"))
        return _bars(symbol, month, 3)

    with connect(migrated_dsn) as conn:
        run(conn, ["AAPL"], JUNE, JULY, fetch)

    assert seen == [0, 1]


def test_the_summary_accumulates_across_units_rather_than_recording_the_last_one(migrated_dsn):
    seeded = RecordingFetcher({("AAPL", JUNE): 3, ("MSFT", JUNE): 3})
    with connect(migrated_dsn) as conn:
        ingest_unit(conn, "AAPL", JUNE, seeded)
        ingest_unit(conn, "MSFT", JUNE, seeded)

    fetch = RecordingFetcher({("AAPL", JULY): 5, ("MSFT", JULY): 11})
    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL", "MSFT"], JUNE, JULY, fetch)

    # every one of these four is copied into INGEST_LOG.md, and each counter needs at least two of its own kind or assignment and accumulation agree -- two skips as well as two units, which the first version of this test got wrong
    assert (summary.units, summary.skipped, summary.rows) == (2, 2, 16)
    assert 0 < summary.elapsed < 60


def test_a_month_carrying_a_day_still_attaches_month_aligned_bounds(migrated_dsn):
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, date(2026, 6, 15))
        conn.commit()
        bounds = conn.execute(
            "SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = %s",
            (partition_name(JUNE),),
        ).fetchone()[0]

    # the request window is always the whole month, so bounds taken from a day component would exclude the data they are built for
    assert "'2026-06-01 00:00:00+00'" in bounds
    assert "'2026-07-01 00:00:00+00'" in bounds


def test_the_per_unit_progress_line_goes_through_the_run_log(migrated_dsn, caplog):
    with caplog.at_level(logging.INFO, logger="ingest.pipeline"):
        with connect(migrated_dsn) as conn:
            run(conn, ["AAPL"], JUNE, JUNE, RecordingFetcher())

    # a bare print lands on a block-buffered stdout, where a kill -9 on an unattended run discards every line still sitting in it
    assert [m for m in caplog.messages if m == "AAPL 2026-06 parsed=3 inserted=3"]

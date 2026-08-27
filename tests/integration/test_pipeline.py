import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest

from db.session import connect
from ingest.client import Bar, FatalVendorError, UnitFetchError
from ingest.pipeline import ensure_partition, ingest_unit, partition_name, recompute_first_bar_ts, run

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
AUGUST = date(2026, 8, 1)


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
        parsed, rejected, inserted = ingest_unit(conn, "AAPL", JUNE, fetch)

    assert (parsed, rejected, inserted) == (3, 0, 0)
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
        good = _bars(symbol, month, 2)
        # its own timestamp, or the (symbol, ts) collapse drops it before the insert and nothing reaches the database to fail
        return good[:1] + [good[1].__class__(**{**good[1].__dict__, "volume": 10**30})]

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


def test_summary_rejected_sums_across_units_rather_than_keeping_only_the_last(migrated_dsn):
    def two_units_each_with_a_rejectable_bar(symbol, month):
        good = _bars(symbol, month, 3)
        bad_count = 1 if symbol == "AAPL" else 2
        bad = [good[0].__class__(**{**good[0].__dict__, "volume": -1}) for _ in range(bad_count)]
        return good + bad

    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL", "MSFT"], JUNE, JUNE, two_units_each_with_a_rejectable_bar)

    # assignment or subtraction would leave this at the last unit's count instead of the running total
    assert summary.rejected == 3


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


def test_a_unit_with_one_bad_bar_commits_and_records_a_rejected_count_of_one(migrated_dsn):
    def one_bad_bar(symbol, month):
        good = _bars(symbol, month, 3)
        bad = good[0].__class__(**{**good[0].__dict__, "high": Decimal("1.00")})
        return good + [bad]

    with connect(migrated_dsn) as conn:
        parsed, rejected, inserted = ingest_unit(conn, "AAPL", JUNE, one_bad_bar)

    # rejection is per-bar and never raises, so the unit commits with the good rows and the bad one is only counted
    assert (parsed, rejected, inserted) == (3, 1, 3)
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == 3
    with connect(migrated_dsn) as conn:
        row = conn.execute("SELECT row_count, rejected_count FROM ingest_progress").fetchone()
    assert row == (3, 1)


def test_a_unit_fetch_error_leaves_no_progress_row_and_the_run_continues_to_the_next_unit(migrated_dsn):
    def fetch(symbol, month):
        if symbol == "AAPL":
            raise UnitFetchError("vendor said no")
        return _bars(symbol, month, 3)

    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL", "MSFT"], JUNE, JUNE, fetch)

    # a progress row for a failed unit would be a permanent skip on every future resume
    assert summary.failed == (("AAPL", JUNE),)
    assert summary.units == 1
    assert _scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress WHERE symbol = 'AAPL'") == 0
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars WHERE symbol = 'AAPL'") == 0
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars WHERE symbol = 'MSFT'") == 3


def test_a_failed_first_month_does_not_stop_the_remaining_months_for_that_symbol(migrated_dsn):
    def fetch(symbol, month):
        if month == JUNE:
            raise UnitFetchError("vendor said no")
        return _bars(symbol, month, 3)

    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL"], JUNE, AUGUST, fetch)

    # a break here would abandon JULY and AUGUST behind the failed JUNE unit rather than moving past it
    assert summary.units == 2
    assert summary.failed == (("AAPL", JUNE),)
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars WHERE symbol = 'AAPL'") == 6


def test_a_fatal_vendor_error_stops_the_run_and_the_next_unit_never_runs(migrated_dsn):
    def fetch(symbol, month):
        if symbol == "AAPL":
            raise FatalVendorError("vendor returned 403")
        return _bars(symbol, month, 3)

    # the except Exception clause below would otherwise swallow the one failure class that must stop the run
    with connect(migrated_dsn) as conn:
        with pytest.raises(FatalVendorError):
            run(conn, ["AAPL", "MSFT"], JUNE, JUNE, fetch)

    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars WHERE symbol = 'MSFT'") == 0


def test_a_keyboard_interrupt_from_the_fetch_propagates_out_of_the_run(migrated_dsn):
    def fetch(symbol, month):
        raise KeyboardInterrupt()

    # except Exception does not catch KeyboardInterrupt, so a bare except here would log an operator's ctrl-c as a failed unit
    with connect(migrated_dsn) as conn:
        with pytest.raises(KeyboardInterrupt):
            run(conn, ["AAPL"], JUNE, JUNE, fetch)


def test_recompute_first_bar_ts_sets_each_symbols_minimum_not_the_maximum_or_first_inserted(migrated_dsn):
    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        conn.execute("INSERT INTO symbols (symbol) VALUES ('AAPL'), ('MSFT')")
        conn.commit()
        # ingested out of chronological order so the minimum cannot be mistaken for the first inserted row
        ingest_unit(conn, "AAPL", JULY, fetch)
        ingest_unit(conn, "AAPL", JUNE, fetch)

    with connect(migrated_dsn) as conn:
        changed = recompute_first_bar_ts(conn)
        first_bar_ts = conn.execute("SELECT first_bar_ts FROM symbols WHERE symbol = 'AAPL'").fetchone()[0]

    assert first_bar_ts == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    # MSFT never received a bar, so only the AAPL row actually changed
    assert changed == 1


def test_recompute_first_bar_ts_reverts_to_null_when_a_symbols_bars_are_gone(migrated_dsn):
    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        conn.execute("INSERT INTO symbols (symbol) VALUES ('AAPL')")
        conn.commit()
        ingest_unit(conn, "AAPL", JUNE, fetch)
        recompute_first_bar_ts(conn)
        before = conn.execute("SELECT first_bar_ts FROM symbols WHERE symbol = 'AAPL'").fetchone()[0]

    assert before is not None

    with connect(migrated_dsn) as conn:
        conn.execute("DELETE FROM bars WHERE symbol = 'AAPL'")
        conn.commit()
        recompute_first_bar_ts(conn)
        after = conn.execute("SELECT first_bar_ts FROM symbols WHERE symbol = 'AAPL'").fetchone()[0]

    # a GROUP BY over bars cannot express a symbol holding no bars, which would leave this stale and read as ingested by the coverage query
    assert after is None


def test_recompute_first_bar_ts_is_idempotent_and_reports_zero_changed_on_a_second_call(migrated_dsn):
    fetch = RecordingFetcher()
    with connect(migrated_dsn) as conn:
        conn.execute("INSERT INTO symbols (symbol) VALUES ('AAPL')")
        conn.commit()
        ingest_unit(conn, "AAPL", JUNE, fetch)

    with connect(migrated_dsn) as conn:
        first_call = recompute_first_bar_ts(conn)
        second_call = recompute_first_bar_ts(conn)

    # IS DISTINCT FROM keeps the update off rows that did not move, which is what makes the returned count mean something
    assert first_call == 1
    assert second_call == 0


def test_a_resumed_run_that_ingests_an_earlier_month_last_still_recomputes_its_minimum(migrated_dsn):
    with connect(migrated_dsn) as conn:
        conn.execute("INSERT INTO symbols (symbol) VALUES ('AAPL')")
        conn.commit()
        # simulates a resume: the later month completed in an earlier invocation and the earlier month completes now
        run(conn, ["AAPL"], JULY, JULY, RecordingFetcher())
        run(conn, ["AAPL"], JUNE, JUNE, RecordingFetcher())
        changed = recompute_first_bar_ts(conn)
        first_bar_ts = conn.execute("SELECT first_bar_ts FROM symbols WHERE symbol = 'AAPL'").fetchone()[0]

    # set on first insert rather than recomputed after the run would have frozen this at July's minimum
    assert first_bar_ts == datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert changed == 1


def test_row_count_reconciles_when_a_replay_returns_fewer_bars_than_are_already_stored(migrated_dsn):
    def three(symbol, month):
        return _bars(symbol, month, 3)

    def two(symbol, month):
        return _bars(symbol, month, 2)

    with connect(migrated_dsn) as conn:
        ingest_unit(conn, "AAPL", JUNE, three)
        # clearing progress rows while leaving the bars is the recovery procedure this project has actually used
        conn.execute("DELETE FROM ingest_progress")
        conn.commit()
        ingest_unit(conn, "AAPL", JUNE, two)

    # row_count reads what the unit's window holds rather than what this fetch accepted, so the gate's reconciliation cannot drift
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == _scalar(
        migrated_dsn, "SELECT sum(row_count) FROM ingest_progress"
    ) == 3


def test_the_run_summary_is_hashable_and_its_failed_units_cannot_be_mutated(migrated_dsn):
    with connect(migrated_dsn) as conn:
        summary = run(conn, ["AAPL"], JUNE, JUNE, RecordingFetcher())

    # frozen=True generates __hash__ from every field, so a list here made the dataclass claim an immutability it did not have in either direction
    assert hash(summary) is not None
    assert isinstance(summary.failed, tuple)


def test_a_connection_that_dies_mid_run_stops_the_run_rather_than_walking_the_rest(migrated_dsn):
    calls = []
    # not the usual `with connect(...)`: the connection is dead by the end of this test and the context manager would commit a closed one
    conn = connect(migrated_dsn)
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    conn.commit()

    def fetch(symbol, month):
        calls.append((symbol, month))
        if len(calls) == 2:
            with connect(migrated_dsn) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
                killer.commit()
        return _bars(symbol, month, 3)

    # a dead connection is as unclearable as a fatal status, and continuing spends one vendor request per remaining unit before finding out
    with pytest.raises(psycopg.Error):
        run(conn, ["AAPL"], JUNE, AUGUST, fetch)

    assert calls == [("AAPL", JUNE), ("AAPL", JULY)]


def test_ingest_unit_on_an_empty_payload_reports_zero_parsed_rejected_and_inserted(migrated_dsn):
    with connect(migrated_dsn) as conn:
        # a pre-listing month answers with an empty payload and is a completed unit, which is the branch the `if rows:` guard skips
        assert ingest_unit(conn, "AAPL", JUNE, lambda symbol, month: []) == (0, 0, 0)


def test_an_empty_payload_commits_a_zero_row_progress_row_and_logs_zero_inserted(migrated_dsn, caplog):
    with connect(migrated_dsn) as conn:
        with caplog.at_level(logging.INFO, logger="ingest.pipeline"):
            summary = run(conn, ["AAPL"], JUNE, JUNE, lambda symbol, month: [])

    # this line is what the kill criterion counts against ingest_progress, and an inserted of None would emit no line at all
    assert "AAPL 2026-06 parsed=0 inserted=0" in caplog.messages
    assert (summary.units, summary.rows, summary.failed) == (1, 0, ())
    assert _scalar(migrated_dsn, "SELECT row_count FROM ingest_progress WHERE symbol = 'AAPL'") == 0

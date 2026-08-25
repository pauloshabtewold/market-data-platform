import os
import signal
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from db.session import connect
from ingest.pipeline import recompute_first_bar_ts

SYMBOLS = ("AAA", "BBB")
MONTHS = (date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1))
UNITS = len(SYMBOLS) * len(MONTHS)
BARS_PER_UNIT = 3
# the fetch callback fires once per unit, so killing on the fourth is the only point at which the parent knows three units committed without polling for it
KILL_AT = 4

# the child inherits none of this process's monkeypatching, so its floor comes from the environment instead. pydantic-settings ranks environment variables above env_file, so these beat the repository's real .env without moving the child's cwd -- which must not move, because a cwd- or __file__-derived import path resolves inside a mutation harness's instrumented tree and its trampoline then cannot find its config.
DEAD_ENV = {
    "ALPACA_KEY_ID": "dead",
    "ALPACA_SECRET_KEY": "dead",
    "ALPACA_TRADING_HOST": "http://127.0.0.1:1",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
}

CHILD = '''
import os, sys
from datetime import UTC, date, datetime
from decimal import Decimal
from db.session import connect
from ingest.client import Bar
from ingest.pipeline import run

kill_at = int(os.environ["CRASH_AT"])
dsn = os.environ["DATABASE_URL"]
print("child dsn:", dsn, flush=True)

calls = [0]


def fetch(symbol, month):
    calls[0] += 1
    if calls[0] == kill_at:
        # a self-directed SIGKILL is a real uncatchable death of a real process, which no in-process test can stand in for
        os.kill(os.getpid(), 9)
    return [
        Bar(
            symbol=symbol,
            ts=datetime(month.year, month.month, 15, 13, 30 + i, tzinfo=UTC),
            open=Decimal("10"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10"),
            volume=100 + i, trade_count=4, vwap=Decimal("10"),
        )
        for i in range(3)
    ]


with connect(dsn) as conn:
    run(conn, ["AAA", "BBB"], date(2026, 4, 1), date(2026, 6, 1), fetch)
sys.exit(0)
'''


def _run_child(dsn: str, crash_at: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        # derived file-relatively and never from a temporary directory: relocating a subprocess test's cwd put the child inside an instrumented tree and took a whole mutation campaign down at its stats step
        cwd=Path(__file__).resolve().parents[2],
        # overlaid rather than replacing os.environ, so a harness running this suite keeps its own variables in the child
        env={**os.environ, **DEAD_ENV, "DATABASE_URL": dsn, "CRASH_AT": str(crash_at)},
        capture_output=True,
        text=True,
    )


def _seed_calendar_and_symbols(dsn: str) -> None:
    with connect(dsn) as conn:
        for month in MONTHS:
            day = date(month.year, month.month, 15)
            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%s, %s, %s, 390)",
                (
                    day,
                    datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
                    datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
                ),
            )
        for symbol in SYMBOLS:
            conn.execute(
                "INSERT INTO symbols (symbol, name, exchange, active) VALUES (%s, %s, 'X', true)",
                (symbol, symbol),
            )
        conn.commit()


def _scalar(dsn: str, query: str):
    with connect(dsn) as conn:
        return conn.execute(query).fetchone()[0]


def test_a_run_killed_mid_flight_keeps_every_unit_it_had_already_committed(migrated_dsn):
    killed = _run_child(migrated_dsn, KILL_AT)

    # the parent observes the real signal rather than an exit code the child chose, which is what makes this a kill and not a return
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert _scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress") == KILL_AT - 1
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == (KILL_AT - 1) * BARS_PER_UNIT


def test_the_killed_child_wrote_to_the_container_and_never_to_the_configured_database(migrated_dsn):
    killed = _run_child(migrated_dsn, KILL_AT)

    # the cwd is fixed at the repository root and that is where the real .env lives, so a child left to resolve its own DSN would write these synthetic bars into the development database and then be killed mid-write
    reported = [l for l in killed.stdout.splitlines() if l.startswith("child dsn:")]
    assert reported == [f"child dsn: {migrated_dsn}"]


def test_a_restart_after_a_kill_skips_the_committed_units_and_finishes_the_rest(migrated_dsn):
    assert _run_child(migrated_dsn, KILL_AT).returncode == -signal.SIGKILL

    restarted = _run_child(migrated_dsn, 0)

    assert restarted.returncode == 0, restarted.stderr
    assert _scalar(migrated_dsn, "SELECT count(*) FROM ingest_progress") == UNITS
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == UNITS * BARS_PER_UNIT


def test_bars_reconcile_against_the_summed_progress_row_counts_after_a_crash_and_a_restart(migrated_dsn):
    assert _run_child(migrated_dsn, KILL_AT).returncode == -signal.SIGKILL
    assert _run_child(migrated_dsn, 0).returncode == 0

    # the reconciliation the whole row_count decision exists to make possible, and the one this feature's gate reads against the real load
    assert _scalar(migrated_dsn, "SELECT count(*) FROM bars") == _scalar(
        migrated_dsn, "SELECT sum(row_count) FROM ingest_progress"
    )


def test_the_coverage_query_reports_no_missing_units_after_a_crash_and_a_restart(migrated_dsn, query_sql):
    _seed_calendar_and_symbols(migrated_dsn)
    assert _run_child(migrated_dsn, KILL_AT).returncode == -signal.SIGKILL
    assert _run_child(migrated_dsn, 0).returncode == 0

    with connect(migrated_dsn) as conn:
        # without this every symbol keeps first_bar_ts NULL, the query excludes all of them, and missing_units reads 0 on a database where nothing was ingested at all
        recompute_first_bar_ts(conn)
        rows = conn.execute(
            query_sql("09_coverage.sql"), {"start": date(2026, 4, 1), "end": date(2026, 6, 30)}
        ).fetchall()

    summary = dict(rows[-3:])
    # read first, because it is what proves the missing_units zero below was measured over the symbols rather than over the empty set
    assert summary["uningested_symbols"] == 0
    assert summary["missing_units"] == 0

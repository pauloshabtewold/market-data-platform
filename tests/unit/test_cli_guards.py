import logging
import os
import subprocess
import sys
from datetime import date
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

import ingest.__main__ as cli
from config import settings
from ingest.__main__ import main


def _tickers(tmp_path, *symbols):
    path = tmp_path / "tickers.txt"
    path.write_text("\n".join(symbols))
    return str(path)


def test_the_tickers_file_flag_is_required_by_the_parser():
    # argparse's own required=True, not one of the guard branches this module writes itself
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_a_nonexistent_tickers_file_is_refused_with_exit_code_2_on_stderr(tmp_path, capsys):
    missing = tmp_path / "missing.txt"

    code = main(["--tickers-file", str(missing)])

    # OSError's own errno maps to no particular exit code, so the guard's own 2 is what a caller can rely on
    assert code == 2
    out, err = capsys.readouterr()
    assert str(missing) in err
    assert out == ""


def test_an_inverted_month_range_is_refused_before_any_network_call(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--start-month", "2026-08", "--end-month", "2026-06"])

    assert code == 2
    assert "after --end-month" in capsys.readouterr().err


def test_a_single_month_range_is_not_read_as_inverted(monkeypatch, tmp_path):
    ran = {}

    def fake_run(conn, symbols, start, end, fetch):
        ran["months"] = (start, end)
        raise SystemExit(0)

    monkeypatch.setattr("ingest.__main__.run", fake_run)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _NullClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    # the equal bound is the narrowed re-seed form, which spends two requests where the bare form launches the whole universe
    try:
        main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--symbol", "AAPL",
              "--start-month", "2026-06", "--end-month", "2026-06"])
    except SystemExit:
        pass

    assert ran["months"] == (date(2026, 6, 1), date(2026, 6, 1))


def test_neither_the_vendor_transport_nor_the_database_is_reachable_from_this_suite():
    # every guard case here passes by returning 2 before the transport is built, so a mutation that removes one runs a real ingest against whatever .env is on the machine; asserted by identity rather than by letting main() fall through, since that test would be the live run if the fixture ever went away.
    for factory in (cli.AlpacaClient, cli.connect):
        with pytest.raises(AssertionError, match="reached the vendor transport"):
            factory("unused")


def test_a_repeated_symbol_is_collapsed_before_the_run(monkeypatch, tmp_path):
    seen = {}

    def fake_run(conn, symbols, start, end, fetch):
        seen["symbols"] = symbols
        raise SystemExit(0)

    monkeypatch.setattr("ingest.__main__.run", fake_run)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _NullClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    try:
        main(["--tickers-file", _tickers(tmp_path, "AAPL", "MSFT"), "--symbol", "AAPL", "--symbol", "AAPL"])
    except SystemExit:
        pass

    # the second AAPL would fail its own ingest_progress insert and abandon everything still queued
    assert seen["symbols"] == ["AAPL"]


def test_seeding_covers_the_file_while_the_bars_phase_covers_the_narrowing(monkeypatch, tmp_path):
    seen = {}

    def fake_seed(conn, client, tickers):
        seen["seeded"] = tickers
        return _Seeded()

    def fake_run(conn, symbols, start, end, fetch):
        seen["ran"] = symbols
        raise SystemExit(0)

    monkeypatch.setattr("ingest.__main__.seed_symbols", fake_seed)
    monkeypatch.setattr("ingest.__main__.run", fake_run)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _NullClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    try:
        main(["--tickers-file", _tickers(tmp_path, "AAPL", "MSFT"), "--symbol", "AAPL"])
    except SystemExit:
        pass

    # seeding the narrowing instead makes the reconciling delete true of every symbol outside it, and none of them carries bars or progress rows yet to be spared by the orphan guards
    assert seen["seeded"] == ["AAPL", "MSFT"]
    assert seen["ran"] == ["AAPL"]


def test_the_run_call_is_wired_to_the_real_dsn_conn_client_and_a_client_bound_fetch(monkeypatch, tmp_path):
    seen = {}
    conn = _NullConn()
    client = _NullClient()

    def fake_connect(dsn):
        seen["dsn"] = dsn
        return conn

    def fake_load_calendar(passed_conn, passed_client):
        seen["calendar"] = (passed_conn, passed_client)
        return _Calendar()

    def fake_seed_symbols(passed_conn, passed_client, tickers):
        seen["seed"] = (passed_conn, passed_client)
        return _Seeded()

    def fake_run(passed_conn, symbols, start, end, fetch):
        seen["run_conn"] = passed_conn
        seen["fetch"] = fetch
        raise SystemExit(0)

    def fake_fetch_bars(passed_client, symbol, month):
        seen.setdefault("fetch_calls", []).append((passed_client, symbol, month))
        return []

    monkeypatch.setattr("ingest.__main__.connect", fake_connect)
    monkeypatch.setattr("ingest.__main__.AlpacaClient", lambda: client)
    monkeypatch.setattr("ingest.__main__.load_calendar", fake_load_calendar)
    monkeypatch.setattr("ingest.__main__.seed_symbols", fake_seed_symbols)
    monkeypatch.setattr("ingest.__main__.run", fake_run)
    monkeypatch.setattr("ingest.__main__.fetch_bars", fake_fetch_bars)

    try:
        main(["--tickers-file", _tickers(tmp_path, "AAPL")])
    except SystemExit:
        pass

    assert seen["dsn"] == settings.DATABASE_URL
    assert seen["calendar"] == (conn, client)
    assert seen["seed"] == (conn, client)
    assert seen["run_conn"] is conn

    fetch = seen["fetch"]
    assert isinstance(fetch, partial)
    assert fetch.args == (client,)
    # calling it is what proves the bound client is the one that reaches fetch_bars, not just the first positional slot
    fetch("AAPL", date(2026, 6, 1))
    assert seen["fetch_calls"] == [(client, "AAPL", date(2026, 6, 1))]


class _Calendar:
    days, first, last = 1, "2026-06-01", "2026-06-30"


class _Seeded:
    upserted, deleted, refused, inactive = 0, [], [], []


class _Summary:
    units, skipped, rows, elapsed = 2, 1, 17, 4.8
    rejected, failed = 3, (("MSFT", date(2026, 5, 1)),)


class _SummaryWithNoFailures:
    units, skipped, rows, elapsed = 2, 1, 17, 4.8
    rejected, failed = 0, ()


class _NullClient:
    def __init__(self):
        self.request_counts = {"calendar": 0, "symbols": 0, "bars": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _SpentClient(_NullClient):
    def __init__(self):
        super().__init__()
        self.request_counts = {"calendar": 1, "symbols": 1, "bars": 3}


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    # main() runs the first_bar_ts recompute against whatever connection it is handed, and the recompute reads rowcount off the cursor
    def execute(self, *args, **kwargs):
        return SimpleNamespace(rowcount=0)

    def commit(self):
        return None


def test_a_month_outside_the_declared_window_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--start-month", "2010-01"])

    assert code == 2
    # one keystroke from 2020-01, and unclamped it enumerates 127 months the calendar has no rows for
    assert "must fall inside" in capsys.readouterr().err


def test_an_end_month_past_the_declared_window_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--end-month", "2027-03"])

    assert code == 2
    assert "must fall inside" in capsys.readouterr().err


@pytest.mark.parametrize(
    "key, value",
    [("INGEST_START", date(2020, 8, 3)), ("INGEST_END", date(2026, 6, 15))],
)
def test_a_window_that_does_not_span_whole_months_is_refused(monkeypatch, tmp_path, capsys, key, value):
    monkeypatch.setattr(settings, key, value)

    code = main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    assert code == 2
    # the unit of work is a whole month, so a mid-month bound requests days outside the window it declares
    assert "must span whole months" in capsys.readouterr().err


def test_a_repeated_line_in_the_ticker_file_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL", "MSFT", "AAPL")])

    assert code == 2
    # the seeded count is checked against this file, so a repeat makes the two disagree by construction
    assert "repeats AAPL" in capsys.readouterr().err


def test_a_symbol_the_ticker_file_does_not_carry_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL", "MSFT"), "--symbol", "TYPO"])

    assert code == 2
    # unguarded a typo runs to completion, spends one bars request per month in the window, and leaves progress rows keyed on a symbol the universe has no row for
    assert "does not carry TYPO" in capsys.readouterr().err


def test_a_ticker_file_holding_no_tickers_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "", "", "")])

    assert code == 2
    # seeding against an empty universe makes the reconciling delete true of every row, which is the 100-to-5 cut this database has already taken once
    assert "no tickers" in capsys.readouterr().err


def test_a_whitespace_only_line_in_the_ticker_file_is_refused(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL", "   ", "MSFT")])

    assert code == 2
    # `grep -c .` counts this line and the reader would drop it, so the seeded count would come out one below the file
    assert "only whitespace" in capsys.readouterr().err


def test_a_run_that_aborts_still_reports_the_requests_it_spent(monkeypatch, tmp_path, caplog):
    def explode(conn, symbols, start, end, fetch):
        raise RuntimeError("vendor said no")

    monkeypatch.setattr("ingest.__main__.run", explode)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        with pytest.raises(RuntimeError):
            main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    # every other figure is re-derivable from ingest_progress afterwards; the request counts die with the process
    aborted = [m for m in caplog.messages if m.startswith("run incomplete:")]
    assert len(aborted) == 1
    assert "bars_requests=3" in aborted[0]
    assert "to resume" in aborted[0]


def test_the_resume_command_carries_the_flags_the_run_was_given(monkeypatch, tmp_path, caplog):
    def explode(conn, symbols, start, end, fetch):
        raise RuntimeError("vendor said no")

    monkeypatch.setattr("ingest.__main__.run", explode)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        with pytest.raises(RuntimeError):
            main([
                "--tickers-file", _tickers(tmp_path, "AAPL", "MSFT"),
                "--symbol", "AAPL", "--start-month", "2026-01", "--end-month", "2026-06",
            ])

    # without them the operator is handed a command that walks every ticker over the whole window, which is the spend the window guards exist to prevent
    resume = [m for m in caplog.messages if m.startswith("run incomplete:")][0]
    # the whole command, not just the flags: asserting the flags alone passes on a line that lost the program and the file it runs against
    assert "rerun `python -m ingest --tickers-file" in resume
    assert "--symbol AAPL" in resume
    assert "--start-month 2026-01" in resume
    assert "--end-month 2026-06" in resume


def test_the_resume_command_names_the_actual_tickers_file_path(monkeypatch, tmp_path, caplog):
    def explode(conn, symbols, start, end, fetch):
        raise RuntimeError("vendor said no")

    monkeypatch.setattr("ingest.__main__.run", explode)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    tickers_file = _tickers(tmp_path, "AAPL")
    with caplog.at_level(logging.INFO, logger="ingest"):
        with pytest.raises(RuntimeError):
            main(["--tickers-file", tickers_file])

    # str(None) satisfies a check for the literal "--tickers-file" prefix just as well as the real path would
    resume = [m for m in caplog.messages if m.startswith("run incomplete:")][0]
    assert f"--tickers-file {tickers_file}" in resume


def test_a_completed_run_reports_itself_complete(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("ingest.__main__.run",
                        lambda conn, symbols, start, end, fetch: _Summary())
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        code = main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    # only the abort path was asserted, so a run that reported every success as incomplete stayed green and would send an operator back to rerun a finished load
    assert code == 0
    line = [m for m in caplog.messages if m.startswith("run complete:")]
    assert len(line) == 1
    assert "units=2 skipped=1" in line[0] and "rows=17" in line[0]
    assert not [m for m in caplog.messages if m.startswith("run incomplete:")]


def test_a_run_that_dies_before_the_transport_still_reports(monkeypatch, tmp_path, caplog):
    class _Exploding:
        def __init__(self):
            raise RuntimeError("no credentials")

    monkeypatch.setattr("ingest.__main__.AlpacaClient", _Exploding)

    with caplog.at_level(logging.INFO, logger="ingest"):
        with pytest.raises(RuntimeError):
            main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    # request_counts does not exist yet on this path, so the zero fallback is the only thing between the operator and a TypeError raised inside `finally` that would bury the real cause
    aborted = [m for m in caplog.messages if m.startswith("run incomplete:")]
    assert len(aborted) == 1
    assert "calendar_requests=0 symbols_requests=0 bars_requests=0" in aborted[0]


# the autouse fixture patches names in this process and stops at the process boundary, so a child gets its floor from the environment instead. pydantic-settings ranks environment variables above env_file -- measured -- so these beat the repository's real .env without moving the child's cwd, and the cwd must not move: the generated mutation harness runs this suite from a copied tree where a cwd- or __file__-derived import path resolves inside mutants/, and the child then imports an instrumented package whose trampoline cannot find its config.
DEAD_ENV = {
    "ALPACA_KEY_ID": "dead",
    "ALPACA_SECRET_KEY": "dead",
    "ALPACA_TRADING_HOST": "http://127.0.0.1:1",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
    "DATABASE_URL": "postgresql://dead:dead@127.0.0.1:1/dead",
}


def test_the_run_reaches_a_real_stream_at_info(tmp_path):
    # basicConfig is a no-op once a handler exists and pytest installs one, so the level it sets is invisible in process -- the same blindness that let D-048's stream move unnoticed. A subprocess is the only place the emitted bytes are real.
    tickers = _tickers(tmp_path, "AAPL")
    program = (
        "import sys, types\n"
        "import ingest.__main__ as M\n"
        "class C:\n"
        "    request_counts = {'calendar': 1, 'symbols': 1, 'bars': 2}\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): pass\n"
        "class N:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): pass\n"
        "    def execute(self, *a, **k): return types.SimpleNamespace(rowcount=0)\n"
        "    def commit(self): pass\n"
        "M.AlpacaClient = C\n"
        "M.connect = lambda dsn: N()\n"
        "M.load_calendar = lambda c, cl: types.SimpleNamespace(days=1, first='a', last='b')\n"
        "M.seed_symbols = lambda c, cl, t: types.SimpleNamespace(upserted=1, deleted=[], refused=[], inactive=[])\n"
        "M.run = lambda *a, **k: types.SimpleNamespace(units=2, skipped=1, rows=17, elapsed=4.8, rejected=0, failed=())\n"
        f"sys.exit(M.main(['--tickers-file', {str(tickers)!r}]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=Path(__file__).resolve().parent.parent.parent,
        # overlaid rather than replacing os.environ, so a harness running this suite keeps its own variables in the child
        env={**os.environ, **DEAD_ENV},
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    # an unattended run whose level was never set emits nothing at all, and the operator learns that after the run rather than during it
    assert "run complete: units=2 skipped=1" in result.stderr
    assert "calendar: 1 days loaded" in result.stderr


def test_a_completed_runs_report_line_carries_the_rejected_and_failed_counters(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("ingest.__main__.run",
                        lambda conn, symbols, start, end, fetch: _Summary())
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    line = [m for m in caplog.messages if m.startswith("run complete:")][0]
    # appended to the tail rather than inserted into it, so these keep matching what they matched before the counters existed
    assert "units=2 skipped=1" in line and "rows=17" in line
    assert "rejected=3" in line
    assert "failed=1" in line


def test_a_completed_run_with_failed_units_names_them_on_a_second_line(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("ingest.__main__.run",
                        lambda conn, symbols, start, end, fetch: _Summary())
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    # re-running the same command retries exactly these units and nothing else
    failed_lines = [m for m in caplog.messages if m.startswith("failed units:")]
    assert len(failed_lines) == 1
    assert "MSFT 2026-05" in failed_lines[0]
    assert "rerun `python -m ingest --tickers-file" in failed_lines[0]


def test_a_completed_run_with_no_failed_units_omits_the_failed_units_line(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("ingest.__main__.run",
                        lambda conn, symbols, start, end, fetch: _SummaryWithNoFailures())
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    # a run that failed nothing must not print an empty failure list
    assert not [m for m in caplog.messages if m.startswith("failed units:")]


def test_main_recomputes_first_bar_ts_after_a_completed_run_and_logs_the_count(monkeypatch, tmp_path, caplog):
    calls = []

    def fake_recompute(conn):
        calls.append(conn)
        return 42

    monkeypatch.setattr("ingest.__main__.run",
                        lambda conn, symbols, start, end, fetch: _Summary())
    monkeypatch.setattr("ingest.__main__.recompute_first_bar_ts", fake_recompute)
    monkeypatch.setattr("ingest.__main__.load_calendar", lambda conn, client: _Calendar())
    monkeypatch.setattr("ingest.__main__.seed_symbols", lambda conn, client, tickers: _Seeded())
    monkeypatch.setattr("ingest.__main__.AlpacaClient", _SpentClient)
    monkeypatch.setattr("ingest.__main__.connect", lambda dsn: _NullConn())

    with caplog.at_level(logging.INFO, logger="ingest"):
        main(["--tickers-file", _tickers(tmp_path, "AAPL")])

    assert len(calls) == 1
    assert "first_bar_ts: 42 symbols recomputed" in caplog.messages

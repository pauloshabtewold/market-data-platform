import logging
from datetime import date

import pytest

import ingest.__main__ as cli
from config import settings
from ingest.__main__ import main


def _tickers(tmp_path, *symbols):
    path = tmp_path / "tickers.txt"
    path.write_text("\n".join(symbols))
    return str(path)


def test_an_inverted_month_range_is_refused_before_any_network_call(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--start-month", "2026-08", "--end-month", "2026-06"])

    assert code == 2
    assert "after --end-month" in capsys.readouterr().err


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


class _Calendar:
    days, first, last = 1, "2026-06-01", "2026-06-30"


class _Seeded:
    upserted, deleted, refused, inactive = 0, [], [], []


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

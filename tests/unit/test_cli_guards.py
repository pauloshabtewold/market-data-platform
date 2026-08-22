from datetime import date

from ingest.__main__ import main


def _tickers(tmp_path, *symbols):
    path = tmp_path / "tickers.txt"
    path.write_text("\n".join(symbols))
    return str(path)


def test_an_inverted_month_range_is_refused_before_any_network_call(tmp_path, capsys):
    code = main(["--tickers-file", _tickers(tmp_path, "AAPL"), "--start-month", "2026-08", "--end-month", "2026-06"])

    assert code == 2
    assert "after --end-month" in capsys.readouterr().err


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
    request_counts = {"calendar": 0, "symbols": 0, "bars": 0}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

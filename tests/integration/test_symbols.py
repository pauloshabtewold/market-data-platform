from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from db.session import connect
from ingest.pipeline import ensure_partition
from ingest.symbols import seed_symbols

UNIVERSE = ["AAPL", "MSFT", "NVDA"]


class StubAssets:
    """Stands in for /v2/assets so the suite never spends the shared request budget."""

    def __init__(self, symbols, inactive=()):
        self._assets = [
            {
                "symbol": s,
                "name": f"{s} Inc",
                "exchange": "NASDAQ",
                "status": "inactive" if s in inactive else "active",
            }
            for s in symbols
        ]
        self.params = []

    def get_json(self, base_url, path, params, phase):
        self.params.append((path, params, phase))
        return self._assets


def _rows(dsn):
    with connect(dsn) as conn:
        return conn.execute("SELECT symbol, name, exchange, active FROM symbols ORDER BY symbol").fetchall()


def _mark_ingested(dsn, with_progress: str, with_bars: str):
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO ingest_progress (symbol, month, completed_at, row_count, rejected_count)"
            " VALUES (%s, %s, now(), 1, 0)",
            (with_progress, date(2026, 6, 1)),
        )
        ensure_partition(conn, date(2026, 6, 1))
        conn.execute(
            "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                with_bars,
                datetime(2026, 6, 1, 13, 30, tzinfo=UTC),
                Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), 1, 1, Decimal("1"),
            ),
        )
        conn.commit()


def test_seeding_writes_one_row_per_ticker_and_asks_for_every_status(migrated_dsn):
    client = StubAssets(UNIVERSE + ["TSLA"], inactive=["NVDA"])
    with connect(migrated_dsn) as conn:
        summary = seed_symbols(conn, client, UNIVERSE)

    assert summary.upserted == 3
    assert summary.inactive == ["NVDA"]
    assert _rows(migrated_dsn) == [
        ("AAPL", "AAPL Inc", "NASDAQ", True),
        ("MSFT", "MSFT Inc", "NASDAQ", True),
        ("NVDA", "NVDA Inc", "NASDAQ", False),
    ]
    # a status filter would make active true by construction and record the request rather than the asset
    assert client.params[0] == ("/v2/assets", {"asset_class": "us_equity"}, "symbols")


def test_a_second_seeding_against_a_trimmed_file_mirrors_the_deletion(migrated_dsn):
    with connect(migrated_dsn) as conn:
        seed_symbols(conn, StubAssets(UNIVERSE), UNIVERSE)
        summary = seed_symbols(conn, StubAssets(UNIVERSE), UNIVERSE[:2])

    assert summary.deleted == ["NVDA"]
    assert summary.refused == []
    assert [r[0] for r in _rows(migrated_dsn)] == ["AAPL", "MSFT"]


def test_a_ticker_with_no_asset_row_aborts_naming_the_ticker(migrated_dsn):
    with connect(migrated_dsn) as conn:
        with pytest.raises(RuntimeError, match="NVDA"):
            seed_symbols(conn, StubAssets(["AAPL", "MSFT"]), UNIVERSE)

    assert _rows(migrated_dsn) == []


def test_the_reconciling_delete_refuses_to_orphan_bars_or_progress_rows(migrated_dsn):
    with connect(migrated_dsn) as conn:
        seed_symbols(conn, StubAssets(UNIVERSE + ["TSLA"]), UNIVERSE + ["TSLA"])
    _mark_ingested(migrated_dsn, with_progress="MSFT", with_bars="NVDA")

    with connect(migrated_dsn) as conn:
        summary = seed_symbols(conn, StubAssets(UNIVERSE + ["TSLA"]), ["AAPL"])

    # there is no foreign key, so an unconditional delete would neither fail nor cascade -- it would orphan silently
    assert summary.deleted == ["TSLA"]
    assert summary.refused == ["MSFT", "NVDA"]
    assert [r[0] for r in _rows(migrated_dsn)] == ["AAPL", "MSFT", "NVDA"]

import logging
from dataclasses import dataclass

import psycopg

from config import settings

log = logging.getLogger(__name__)

ASSETS_PATH = "/v2/assets"

UPSERT = """
INSERT INTO symbols (symbol, name, exchange, active)
VALUES (%(symbol)s, %(name)s, %(exchange)s, %(active)s)
ON CONFLICT (symbol) DO UPDATE
   SET name = EXCLUDED.name,
       exchange = EXCLUDED.exchange,
       active = EXCLUDED.active
"""

# deleting a symbol that still has bars or progress rows would orphan them silently, so the mirror refuses those and the trimmed count stays reachable.
RECONCILE = """
DELETE FROM symbols s
 WHERE s.symbol <> ALL(%(keep)s)
   AND NOT EXISTS (SELECT 1 FROM ingest_progress p WHERE p.symbol = s.symbol)
   AND NOT EXISTS (SELECT 1 FROM bars b WHERE b.symbol = s.symbol)
RETURNING s.symbol
"""

REFUSED = """
SELECT s.symbol
  FROM symbols s
 WHERE s.symbol <> ALL(%(keep)s)
 ORDER BY s.symbol
"""


@dataclass(frozen=True)
class SeedSummary:
    upserted: int
    deleted: list[str]
    refused: list[str]
    inactive: list[str]


def seed_symbols(conn: psycopg.Connection, client, tickers: list[str]) -> SeedSummary:
    # no status filter: filtering to active assets would make active true by construction and record the request rather than the asset.
    assets = client.get_json(
        settings.ALPACA_TRADING_HOST,
        ASSETS_PATH,
        {"asset_class": "us_equity"},
        "symbols",
    )
    by_symbol = {asset["symbol"]: asset for asset in assets}

    missing = [t for t in tickers if t not in by_symbol]
    if missing:
        raise RuntimeError(f"{ASSETS_PATH} carries no asset row for {' '.join(missing)}")

    rows = [
        {
            "symbol": t,
            "name": by_symbol[t].get("name"),
            "exchange": by_symbol[t].get("exchange"),
            "active": by_symbol[t].get("status") == "active",
        }
        for t in tickers
    ]
    inactive = [r["symbol"] for r in rows if not r["active"]]

    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
        refusable = {r[0] for r in cur.execute(REFUSED, {"keep": tickers})}
        deleted = sorted(r[0] for r in cur.execute(RECONCILE, {"keep": tickers}))
    conn.commit()

    refused = sorted(refusable - set(deleted))
    for symbol in inactive:
        # an inactive constituent is a survivorship signal worth reporting rather than a lookup failure.
        log.warning("symbols: %s is not active at the vendor", symbol)
    for symbol in refused:
        log.warning("symbols: %s left in place, it still has bars or progress rows", symbol)

    return SeedSummary(len(rows), deleted, refused, inactive)

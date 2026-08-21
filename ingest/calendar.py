from dataclasses import dataclass

import psycopg

from config import settings

CALENDAR_PATH = "/v2/calendar"

# an explicit AT TIME ZONE cast ignores the session setting, which is why the calendar uses one and the partition DDL cannot.
INSERT_DAY = """
WITH d AS (
    SELECT %(day)s::date AS day,
           (%(day)s || ' ' || %(open)s)::timestamp AT TIME ZONE 'America/New_York' AS open_ts,
           (%(day)s || ' ' || %(close)s)::timestamp AT TIME ZONE 'America/New_York' AS close_ts
)
INSERT INTO market_days (day, open_ts, close_ts, session_minutes)
SELECT day, open_ts, close_ts, (EXTRACT(epoch FROM close_ts - open_ts) / 60)::int
FROM d
ON CONFLICT (day) DO NOTHING
"""


@dataclass(frozen=True)
class CalendarSummary:
    days: int
    first: str
    last: str


def load_calendar(conn: psycopg.Connection, client) -> CalendarSummary:
    start = settings.INGEST_START.isoformat()
    end = settings.INGEST_END.isoformat()
    rows = client.get_json(
        settings.ALPACA_TRADING_HOST,
        CALENDAR_PATH,
        {"start": start, "end": end},
        "calendar",
    )
    if not rows:
        raise RuntimeError(f"{CALENDAR_PATH} returned no trading days for {start}..{end}")

    # open and close are the regular session; session_open and session_close are the extended session and would give a 960-minute day.
    params = [{"day": row["date"], "open": row["open"], "close": row["close"]} for row in rows]
    with conn.cursor() as cur:
        cur.executemany(INSERT_DAY, params)
    conn.commit()

    days = [p["day"] for p in params]
    return CalendarSummary(len(days), min(days), max(days))

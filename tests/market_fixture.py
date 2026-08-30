"""Shared fixture data for the ten analytical queries.

A plain module rather than a conftest: importing a helper from a conftest keeps pytest
green and wedges the generated mutation harness (bugs.md D-097).

The calendar carries the three shapes section 6.2 #10 requires, and each one is load
bearing for a different trap:

* a **half day** (210 minutes, 13:00 ET close), so a hardcoded 390 is catchable;
* a **holiday**, present only by its absence from ``market_days`` -- the table has no
  ``is_open`` flag, so "next trading day" has to mean the next row rather than the next
  calendar day;
* sessions on **both sides of a DST transition** -- Friday 2026-03-06 opens 14:30Z under
  EST and Monday 2026-03-09 opens 13:30Z under EDT, both 09:30 ET. That pair is the only
  thing that catches time-of-day bucketing done on a UTC hour, because the trap needs the
  same wall-clock open to land in two different UTC hours and one row cannot show it.

The transition day itself is a Sunday with no session and is deliberately not in the set.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from db.session import connect

# 2026-03-08 is the second Sunday in March, so these two Mondays and Fridays straddle it
DST_FRIDAY_EST = date(2026, 3, 6)
DST_MONDAY_EDT = date(2026, 3, 9)
PLAIN_TUESDAY = date(2026, 3, 10)
HOLIDAY = date(2026, 3, 11)
HALF_DAY = date(2026, 3, 12)

TRADING_DAYS = (DST_FRIDAY_EST, DST_MONDAY_EDT, PLAIN_TUESDAY, HALF_DAY)

WINDOW_START = date(2026, 3, 1)
WINDOW_END = date(2026, 3, 31)

# 09:30 ET resolves an hour later in UTC under EST than under EDT, which is the whole point
_OPEN_UTC = {DST_FRIDAY_EST: (14, 30)}
_CLOSE_UTC = {DST_FRIDAY_EST: (21, 0)}
_DEFAULT_OPEN_UTC = (13, 30)
_DEFAULT_CLOSE_UTC = (20, 0)
# a 09:30 -> 13:00 ET early close is 210 minutes, never 390
_HALF_CLOSE_UTC = (17, 0)

SESSION_MINUTES = {
    DST_FRIDAY_EST: 390,
    DST_MONDAY_EDT: 390,
    PLAIN_TUESDAY: 390,
    HALF_DAY: 210,
}

# Five bars per session, shaped so a rollup that reads only the first and last bar fails:
# the high lives in bar 1 and the low in bar 2, and the last bar's open differs from the
# first bar's open, so open-from-last and close-from-first are both catchable.
#          open,    high,    low,    close,  volume
_BAR_SHAPE = (
    (100.00, 101.00, 99.00, 100.50, 10),
    (100.50, 105.00, 100.00, 104.00, 20),  # the day's high
    (104.00, 104.50, 95.00, 96.00, 30),  # the day's low
    (96.00, 99.00, 95.50, 98.00, 40),
    (98.00, 102.00, 97.00, 101.25, 50),  # the day's close
)
BARS_PER_SESSION = len(_BAR_SHAPE)

# Each day is offset so no two sessions share a rollup, which keeps a query that collapses
# the day boundary from passing by coincidence.
_DAY_OFFSET = {
    DST_FRIDAY_EST: 0.0,
    DST_MONDAY_EDT: 10.0,
    PLAIN_TUESDAY: 20.0,
    HALF_DAY: 30.0,
}


def open_ts(day: date) -> datetime:
    hour, minute = _OPEN_UTC.get(day, _DEFAULT_OPEN_UTC)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def close_ts(day: date) -> datetime:
    if day == HALF_DAY:
        hour, minute = _HALF_CLOSE_UTC
    else:
        hour, minute = _CLOSE_UTC.get(day, _DEFAULT_CLOSE_UTC)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def bar_ts(day: date, minutes_after_open: int) -> datetime:
    return open_ts(day) + timedelta(minutes=minutes_after_open)


def expected_rollup(day: date) -> dict:
    """The hand-computed rollup for one session, derived from _BAR_SHAPE, not from the query."""
    off = _DAY_OFFSET[day]
    return {
        "open": _BAR_SHAPE[0][0] + off,
        "high": max(b[1] for b in _BAR_SHAPE) + off,
        "low": min(b[2] for b in _BAR_SHAPE) + off,
        "close": _BAR_SHAPE[-1][3] + off,
        "volume": sum(b[4] for b in _BAR_SHAPE),
        "bars": BARS_PER_SESSION,
    }


def load_calendar(dsn: str) -> None:
    with connect(dsn) as conn:
        for day in TRADING_DAYS:
            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%s, %s, %s, %s)",
                (day, open_ts(day), close_ts(day), SESSION_MINUTES[day]),
            )
        conn.commit()


def _ensure_partition(conn, day: date) -> None:
    name = f"bars_{day.year}_{day.month:02d}"
    start = date(day.year, day.month, 1)
    end = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (LIKE bars INCLUDING ALL)")
    attached = conn.execute(
        "SELECT relispartition FROM pg_class WHERE oid = to_regclass(%s)", (f"public.{name}",)
    ).fetchone()[0]
    if not attached:
        # partition bounds are DDL literals rather than parameters, and they resolve against the
        # session zone at DDL time -- which is why every migration pins UTC before declaring one
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute(
            f"ALTER TABLE bars ATTACH PARTITION {name}"
            f" FOR VALUES FROM ('{start}') TO ('{end}')"
        )


def load_symbol(dsn: str, symbol: str, days=TRADING_DAYS, *, extended_hours: bool = False) -> None:
    """Five in-session bars per day, written symbol-major and ts-ascending as the pipeline writes."""
    with connect(dsn) as conn:
        _ensure_partition(conn, TRADING_DAYS[0])
        conn.execute(
            "INSERT INTO symbols (symbol, name, exchange, active, first_bar_ts)"
            " VALUES (%s, %s, 'X', true, %s)",
            (symbol, symbol, open_ts(min(days))),
        )
        for day in days:
            off = _DAY_OFFSET[day]
            for minute, (o, h, low, c, vol) in enumerate(_BAR_SHAPE):
                conn.execute(
                    "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (symbol, bar_ts(day, minute), o + off, h + off, low + off, c + off,
                     vol, 4, (h + low) / 2 + off),
                )
            if extended_hours:
                # one bar 30 minutes before the open and one exactly on close_ts: the second is
                # the half-open boundary, and a <= anywhere would pull it into the session
                for stamp in (bar_ts(day, -30), close_ts(day)):
                    conn.execute(
                        "INSERT INTO bars (symbol, ts, open, high, low, close, volume, trade_count, vwap)"
                        " VALUES (%s, %s, 1, 1, 1, 1, 7, 1, 1)",
                        (symbol, stamp),
                    )
        conn.commit()


def load(dsn: str, symbols=("AAA",), *, extended_hours: bool = False) -> str:
    load_calendar(dsn)
    for symbol in symbols:
        load_symbol(dsn, symbol, extended_hours=extended_hours)
    return dsn

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from psycopg_pool import ConnectionPool

import db.sql
from api.deps import get_pool
from api.errors import (
    INVALID_PARAMS_MESSAGE,
    INVALID_RANGE_MESSAGE,
    UNKNOWN_SYMBOL_MESSAGE,
    ApiError,
)
from api.pagination import (
    BARS_CURSOR,
    DAILY_CURSOR,
    SYMBOLS_CURSOR,
    CursorShape,
    _instant_bounds,
    decode_cursor,
    paginate,
)
from config import settings

router = APIRouter()

# page 1's lower bound, below every ingestible bar, so one text serves both pages rather than two
# branching on whether a cursor was supplied
_BEFORE_ANY_BAR = datetime(1970, 1, 1, tzinfo=timezone.utc)
# the same trick on the symbol key: symbol is the primary key and therefore NOT NULL, so every
# real row sorts above the empty string
_BEFORE_ANY_SYMBOL = ""

# one named pair per endpoint, read from settings at import: spec line 508 puts /symbols and /bars
# in the raw-row class and /daily in the aggregating one, and a pair handed to the wrong call site
# is invisible in a response -- 1000 and 100 are both above every fixture this suite has
_SYMBOLS_CAPS = (settings.BARS_PAGE_DEFAULT, settings.BARS_PAGE_MAX)
_BARS_CAPS = (settings.BARS_PAGE_DEFAULT, settings.BARS_PAGE_MAX)
_DAILY_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)

# the table is symbols and never bars: a known symbol with no rows in the requested window is a
# 200 with an empty list, and an existence check against bars cannot tell that from a 404
REQUIRE_SYMBOL_SQL = "SELECT 1 FROM symbols WHERE symbol = %(symbol)s"

# the cast is load-bearing and measured: psycopg deduplicates the repeated name to one parameter,
# and with no filter supplied that parameter binds NULL, which Postgres refuses to type (42P08)
_SYMBOLS_SQL = (
    "SELECT symbol, name, exchange, active, first_bar_ts FROM symbols"
    " WHERE symbol > %(after)s"
    " AND (%(active)s::boolean IS NULL OR active = %(active)s)"
    " ORDER BY symbol LIMIT %(fetch)s"
)

# lo and hi come from _instant_bounds, never from the raw dates: a timestamptz compared with a
# date resolves to that date's UTC midnight, which drops the final day's whole session
_BARS_SQL = (
    "SELECT ts, open, high, low, close, volume, trade_count, vwap FROM bars"
    " WHERE symbol = %(symbol)s AND ts >= %(lo)s AND ts <= %(hi)s AND ts > %(after)s"
    " ORDER BY ts LIMIT %(fetch)s"
)

# rendered ONCE at import and never on the request path: a read_text() per request on a Class A
# endpoint, frozen SQL that becomes runtime-mutable so its captured plans stop describing what
# runs, and a malformed query file that becomes a request-time 500 instead of an import failure
_DAILY_ROLLUP = db.sql.render("06_daily_rollup.sql").rstrip().removesuffix(";")
# the outer SELECT drops the rollup's own symbol column without touching the committed file, and
# the outer ORDER BY converts an in-practice guarantee about subquery ordering into a stated one
# the newlines are load-bearing: the rendered file opens on a -- comment, which on one line would
# swallow the rest of the wrapper's own first line
_DAILY_SQL = (
    "SELECT day, open, high, low, close, volume, bars FROM (\n"
    f"{_DAILY_ROLLUP}\n"
    ") AS page ORDER BY day LIMIT %(fetch)s"
)


def resolve_request(
    *,
    page_default: int,
    page_max: int,
    shape: CursorShape | None = None,
    start: date | None = None,
    end: date | None = None,
    cursor: str | None = None,
    raw_limit: int | None = None,
    max_window_days: int | None = None,
) -> tuple[dict | None, int]:
    """Run the 400 tier then the 422 tier, and return before any connection is acquired.

    That ordering is structural rather than conventional: the existence check needs a connection
    and nothing acquires one until this has returned, so a 404 cannot precede a 400 or a 422 --
    and a request that is about to be refused never spends one of DB_POOL_MAX checkouts.
    """
    # the cursor is decoded before the window is range-validated, so an inverted start/end makes
    # almost any cursor a 400 cursor_outside_window rather than a 422 start_after_end. That looks
    # wrong and is what spec line 507 mandates: params ahead of range semantics
    cursor_values = decode_cursor(shape, cursor, start, end) if cursor is not None else None

    # the RESOLVED limit and never the client's: with no floor on either page DEFAULT in config, a
    # zero or negative one would otherwise reach paginate, whose refusal is a 500
    limit = raw_limit if raw_limit is not None else page_default
    if not 1 <= limit <= page_max:
        raise ApiError(
            400,
            "invalid_params",
            INVALID_PARAMS_MESSAGE,
            {"reason": "limit_out_of_range", "limit": limit, "max": page_max},
        )

    if start is not None and end is not None:
        # spec line 507's own enumeration order, and the only self-consistent one: an inverted
        # window makes (end - start).days negative, so the length rule could not fire on one
        if start > end:
            raise ApiError(
                422, "invalid_range", INVALID_RANGE_MESSAGE, {"reason": "start_after_end"}
            )
        if start < settings.INGEST_START or end > settings.INGEST_END:
            # isoformat and not the date objects settings holds: a date in a detail is a TypeError
            # inside the error handler, and the client gets a 500 instead of this 422
            raise ApiError(
                422,
                "invalid_range",
                INVALID_RANGE_MESSAGE,
                {
                    "reason": "outside_ingested_range",
                    "min": settings.INGEST_START.isoformat(),
                    "max": settings.INGEST_END.isoformat(),
                },
            )
        # the difference and not the inclusive count: 2026-04-01 to 2026-06-30 is 90 days, which
        # is exactly the cap and is the window the Class A targets are measured at
        requested_days = (end - start).days
        if max_window_days is not None and requested_days > max_window_days:
            raise ApiError(
                422,
                "invalid_range",
                INVALID_RANGE_MESSAGE,
                {
                    "reason": "window_too_long",
                    "max_days": max_window_days,
                    "requested_days": requested_days,
                },
            )

    return cursor_values, limit


def require_symbol(conn, symbol: str) -> None:
    # only the row's presence is read: under the pool's dict factory an unaliased SELECT 1 keys
    # the row '?column?', so anything reading a field out of it breaks on a name nobody chose
    if conn.execute(REQUIRE_SYMBOL_SQL, {"symbol": symbol}).fetchone() is None:
        raise ApiError(
            404,
            "unknown_symbol",
            UNKNOWN_SYMBOL_MESSAGE,
            {"reason": "unknown_symbol", "symbol": symbol},
        )


@router.get("/symbols")
def list_symbols(
    active: bool | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _SYMBOLS_CAPS
    cursor_values, resolved = resolve_request(
        cursor=cursor,
        raw_limit=limit,
        shape=SYMBOLS_CURSOR,
        page_default=page_default,
        page_max=page_max,
    )
    after = _BEFORE_ANY_SYMBOL if cursor_values is None else cursor_values["symbol"]
    with pool.connection() as conn:
        rows = conn.execute(
            _SYMBOLS_SQL, {"after": after, "active": active, "fetch": resolved + 1}
        ).fetchall()
    page = paginate(rows, resolved, SYMBOLS_CURSOR)
    return {"data": page.data, "next_cursor": page.next_cursor}


@router.get("/symbols/{symbol}/bars")
def list_bars(
    symbol: str,
    start: date,
    end: date,
    limit: int | None = None,
    cursor: str | None = None,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _BARS_CAPS
    # no window-length cap here: spec line 630 says /bars accepts any window inside the ingested
    # range, and a cap would make the deep-pagination measurement unrunnable at Feature 7
    cursor_values, resolved = resolve_request(
        start=start,
        end=end,
        cursor=cursor,
        raw_limit=limit,
        shape=BARS_CURSOR,
        page_default=page_default,
        page_max=page_max,
    )
    lo, hi = _instant_bounds(start, end)
    after = _BEFORE_ANY_BAR if cursor_values is None else cursor_values["ts"]
    with pool.connection() as conn:
        require_symbol(conn, symbol)
        rows = conn.execute(
            _BARS_SQL,
            {"symbol": symbol, "lo": lo, "hi": hi, "after": after, "fetch": resolved + 1},
        ).fetchall()
    page = paginate(rows, resolved, BARS_CURSOR)
    return {"data": page.data, "next_cursor": page.next_cursor}


@router.get("/symbols/{symbol}/daily")
def list_daily(
    symbol: str,
    start: date,
    end: date,
    limit: int | None = None,
    cursor: str | None = None,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _DAILY_CAPS
    cursor_values, resolved = resolve_request(
        start=start,
        end=end,
        cursor=cursor,
        raw_limit=limit,
        shape=DAILY_CURSOR,
        page_default=page_default,
        page_max=page_max,
        max_window_days=settings.AGG_MAX_WINDOW_DAYS,
    )
    # the page NARROWS the committed query's own :start rather than filtering its output, because
    # :start also drives the scan bound -- an outer WHERE day > cursor would leave the aggregation
    # spanning the client's full window on every page, which is the shape spec line 476 warns
    # against. It is safe because a New York session opens strictly after the UTC midnight of its
    # own calendar day, so b.ts >= start::date never drops a bar belonging to session start
    page_start = start if cursor_values is None else cursor_values["day"] + timedelta(days=1)
    with pool.connection() as conn:
        require_symbol(conn, symbol)
        rows = conn.execute(
            _DAILY_SQL,
            {"symbol": symbol, "start": page_start, "end": end, "fetch": resolved + 1},
        ).fetchall()
    page = paginate(rows, resolved, DAILY_CURSOR)
    return {"data": page.data, "next_cursor": page.next_cursor}

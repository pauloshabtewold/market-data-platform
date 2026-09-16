from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator, WithJsonSchema
from pydantic_core import PydanticCustomError
from psycopg_pool import ConnectionPool

import db.sql
from api.deps import get_pool
from api.errors import (
    INVALID_PARAMS_MESSAGE,
    INVALID_RANGE_MESSAGE,
    RESPONSE_400,
    RESPONSE_404,
    RESPONSE_422,
    RESPONSE_500,
    UNKNOWN_SYMBOL_MESSAGE,
    ApiError,
)
from api.pagination import (
    BARS_CURSOR,
    DAILY_CURSOR,
    SYMBOLS_CURSOR,
    UNIVERSE_CURSOR,
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
# one per analytics endpoint rather than one shared tuple: three byte-identical pairs cannot be
# told apart by value, so only a named constant makes the one a handler READS observable
_VOLATILITY_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)
_GAPS_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)
_MOVES_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)

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

# published in the generated page, because a required window reads as a limitation unless it says
# why; the partition counts behind the why are the /bars windows in docs/QUERY_PERFORMANCE.md
_BARS_DESCRIPTION = (
    "Minute bars for one symbol inside a required window, oldest first, keyset-paginated with "
    "`limit` and `cursor`. The window is required by design rather than as a limitation: without "
    "one, even the first page is planned across an index scan on every monthly partition of the "
    "bar table."
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
# no wrapper on these two: neither paginates, and each committed file already projects exactly the
# response columns in the response order, so a subquery would only move the plan the Class A
# number is taken on
_VOLATILITY_SQL = db.sql.render("01_volatility.sql")
_GAPS_SQL = db.sql.render("03_gaps.sql")

# the endpoint form of query 5, which 05_largest_moves.sql is deliberately not: that file ranks by
# magnitude, and a ranking has to read the whole window before it knows its first row, so nothing
# streams it in order and no cursor over it is stable. This one is chronological on (ts, symbol),
# which is an index this database already has. The projection stops at open and close because the
# hot-window partial index covers those two and nothing else
_MOVES_SQL = """
SELECT b.ts, b.symbol,
       round(b.open, 4)                            AS open,
       round(b.close, 4)                           AS close,
       round(100 * (b.close - b.open) / b.open, 4) AS move_pct
FROM bars b
JOIN market_days m
  -- the half-open pair alone, which implies the rollup's day equality: that equality makes the join
  -- hashable, and a hashed join reads every remaining row of the window to return one page
  ON b.ts >= m.open_ts AND b.ts < m.close_ts
WHERE m.day >= %(start)s AND m.day <= %(end)s
      -- redundant by logic and required for pruning: a row comparison prunes nothing, so without this every partition up to hi is planned
      AND b.ts >= %(after_ts)s
      AND (b.ts, b.symbol) > (%(after_ts)s, %(after_symbol)s)
      AND b.ts <= %(hi)s
      AND b.open <> 0
      AND abs(100 * (b.close - b.open) / b.open) >= %(min_move_pct)s::numeric
ORDER BY b.ts, b.symbol
LIMIT %(fetch)s"""

# Postgres numeric refuses more precision than either of these rather than rounding
# (NumericValueOutOfRange), so a value Decimal accepts can still fail the query; counted from the
# parsed Decimal's own digits and exponent, without normalising, since normalising would undercount
# a value like 0.10 that carries a real trailing zero
_NUMERIC_MAX_DIGITS_BEFORE_POINT = 131_072
_NUMERIC_MAX_DIGITS_AFTER_POINT = 16_383


def _reject_numeric_overflow(value: Decimal) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    if isinstance(exponent, str):
        # 'n' (NaN) or 'F' (Infinity): Query's own finite_number check refuses both before this runs
        return value
    digits_before = max(len(digits) + exponent, 0)
    digits_after = max(-exponent, 0)
    if digits_before > _NUMERIC_MAX_DIGITS_BEFORE_POINT or digits_after > _NUMERIC_MAX_DIGITS_AFTER_POINT:
        raise PydanticCustomError(
            "numeric_out_of_range", "min_move_pct is out of range for a Postgres numeric"
        )
    return value


# Query lives inside the Annotated alias rather than as the parameter's default value: this
# project's FastAPI discards any Annotated metadata that is not itself a FieldInfo/Depends
# whenever the default is a bare Query(...), which would silently drop AfterValidator.
# WithJsonSchema replaces Decimal's own anyOf(number, string) rendering, whose string branch
# admits "-1" and rejects "1e3", with the plain number the service actually accepts
MinMovePct = Annotated[
    Decimal,
    AfterValidator(_reject_numeric_overflow),
    WithJsonSchema({"type": "number", "minimum": 0, "default": 0}),
    Query(ge=0),
]


def _limit_schema(page_default: int, page_max: int) -> dict:
    # published for documentation only -- the real floor and cap are enforced in resolve_request,
    # against the RESOLVED limit, so this schema cannot change today's limit_out_of_range detail
    return {"type": "integer", "minimum": 1, "maximum": page_max, "default": page_default}


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
    and a request refused BY THESE TWO TIERS never spends one of DB_POOL_MAX checkouts. The 404
    is not covered: require_symbol runs inside the connection block, so an unknown symbol is
    refused holding a checkout.
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
    # a NUL byte cannot have been ingested, and binding one raises psycopg.DataError rather than
    # matching no row -- refused before the query runs so that crash cannot happen
    if "\x00" in symbol:
        raise ApiError(
            404,
            "unknown_symbol",
            UNKNOWN_SYMBOL_MESSAGE,
            {"reason": "unknown_symbol", "symbol": symbol},
        )
    # only the row's presence is read: under the pool's dict factory an unaliased SELECT 1 keys
    # the row '?column?', so anything reading a field out of it breaks on a name nobody chose
    if conn.execute(REQUIRE_SYMBOL_SQL, {"symbol": symbol}).fetchone() is None:
        raise ApiError(
            404,
            "unknown_symbol",
            UNKNOWN_SYMBOL_MESSAGE,
            {"reason": "unknown_symbol", "symbol": symbol},
        )


@router.get(
    "/symbols",
    summary="List ingested symbols",
    # this endpoint takes no window, so its 422 is unreachable -- it is declared anyway because
    # FastAPI adds a 422 of its own, in the framework's shape rather than this one, to any route
    # that declares a query parameter and does not document one itself
    responses={400: RESPONSE_400, 422: RESPONSE_422, 500: RESPONSE_500},
)
def list_symbols(
    active: bool | None = None,
    limit: Annotated[int, WithJsonSchema(_limit_schema(*_SYMBOLS_CAPS))] | None = None,
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


@router.get(
    "/symbols/{symbol}/bars",
    summary="Minute bars for one symbol",
    description=_BARS_DESCRIPTION,
    responses={400: RESPONSE_400, 404: RESPONSE_404, 422: RESPONSE_422, 500: RESPONSE_500},
)
def list_bars(
    symbol: str,
    start: date,
    end: date,
    limit: Annotated[int, WithJsonSchema(_limit_schema(*_BARS_CAPS))] | None = None,
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


@router.get(
    "/symbols/{symbol}/daily",
    summary="Daily bars for one symbol",
    responses={400: RESPONSE_400, 404: RESPONSE_404, 422: RESPONSE_422, 500: RESPONSE_500},
)
def list_daily(
    symbol: str,
    start: date,
    end: date,
    limit: Annotated[int, WithJsonSchema(_limit_schema(*_DAILY_CAPS))] | None = None,
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


@router.get(
    "/analytics/volatility",
    summary="Realized volatility by half-hour bucket",
    responses={400: RESPONSE_400, 404: RESPONSE_404, 422: RESPONSE_422, 500: RESPONSE_500},
)
def analytics_volatility(
    symbol: str,
    start: date,
    end: date,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _VOLATILITY_CAPS
    # no limit and no cursor parameter: the result is one row per half-hour bucket of a session and
    # has no page 2, so the resolved limit is discarded and next_cursor is always an explicit null.
    # The pair is still passed, which is what makes the constant this handler reads observable
    resolve_request(
        start=start,
        end=end,
        page_default=page_default,
        page_max=page_max,
        max_window_days=settings.AGG_MAX_WINDOW_DAYS,
    )
    with pool.connection() as conn:
        require_symbol(conn, symbol)
        rows = conn.execute(
            _VOLATILITY_SQL, {"symbol": symbol, "start": start, "end": end}
        ).fetchall()
    return {"data": rows, "next_cursor": None}


@router.get(
    "/analytics/gaps",
    summary="Overnight gap distribution for one symbol",
    responses={400: RESPONSE_400, 404: RESPONSE_404, 422: RESPONSE_422, 500: RESPONSE_500},
)
def analytics_gaps(
    symbol: str,
    start: date,
    end: date,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _GAPS_CAPS
    resolve_request(
        start=start,
        end=end,
        page_default=page_default,
        page_max=page_max,
        max_window_days=settings.AGG_MAX_WINDOW_DAYS,
    )
    with pool.connection() as conn:
        require_symbol(conn, symbol)
        rows = conn.execute(_GAPS_SQL, {"symbol": symbol, "start": start, "end": end}).fetchall()
    # 03_gaps.sql has no GROUP BY, so it answers exactly one row on any input: a window holding no
    # bars is a row of zeros and nulls rather than an empty list
    return {"data": rows, "next_cursor": None}


@router.get(
    "/analytics/largest-moves",
    summary="Minute moves at or above a threshold, universe-wide",
    responses={400: RESPONSE_400, 422: RESPONSE_422, 500: RESPONSE_500},
)
def analytics_largest_moves(
    start: date,
    end: date,
    min_move_pct: MinMovePct = 0,
    limit: Annotated[int, WithJsonSchema(_limit_schema(*_MOVES_CAPS))] | None = None,
    cursor: str | None = None,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _MOVES_CAPS
    cursor_values, resolved = resolve_request(
        start=start,
        end=end,
        cursor=cursor,
        raw_limit=limit,
        shape=UNIVERSE_CURSOR,
        page_default=page_default,
        page_max=page_max,
        max_window_days=settings.AGG_MAX_WINDOW_DAYS,
    )
    lo, hi = _instant_bounds(start, end)
    # page 1 starts at the window's own lower instant rather than at _BEFORE_ANY_BAR, so
    # _MOVES_SQL's redundant after_ts bound prunes on page 1 exactly as it does on every later one
    after_ts = lo if cursor_values is None else cursor_values["ts"]
    after_symbol = _BEFORE_ANY_SYMBOL if cursor_values is None else cursor_values["symbol"]
    with pool.connection() as conn:
        rows = conn.execute(
            _MOVES_SQL,
            {
                "start": start,
                "end": end,
                "after_ts": after_ts,
                "after_symbol": after_symbol,
                "hi": hi,
                "min_move_pct": min_move_pct,
                "fetch": resolved + 1,
            },
        ).fetchall()
    page = paginate(rows, resolved, UNIVERSE_CURSOR)
    return {"data": page.data, "next_cursor": page.next_cursor}

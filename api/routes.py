import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
)
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
    RESPONSE_DEFAULT,
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

# page 1's lower bound, below every ingestible bar, so one text serves both pages
_BEFORE_ANY_BAR = datetime(1970, 1, 1, tzinfo=timezone.utc)
# same trick on the symbol key: it is the primary key, so NOT NULL, and every row sorts above ''
_BEFORE_ANY_SYMBOL = ""

# one pair per endpoint: a pair handed to the wrong call site is invisible in a response
_SYMBOLS_CAPS = (settings.BARS_PAGE_DEFAULT, settings.BARS_PAGE_MAX)
_BARS_CAPS = (settings.BARS_PAGE_DEFAULT, settings.BARS_PAGE_MAX)
_DAILY_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)
# one per analytics endpoint, not one shared tuple: three byte-identical pairs cannot be told apart
# by value, so only a named constant makes the one a handler READS observable
_VOLATILITY_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)
_GAPS_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)
_MOVES_CAPS = (settings.AGG_PAGE_DEFAULT, settings.AGG_PAGE_MAX)

# symbols and never bars: a known symbol with no rows in the window is a 200 with an empty list,
# which an existence check against bars cannot tell from a 404
REQUIRE_SYMBOL_SQL = "SELECT 1 FROM symbols WHERE symbol = %(symbol)s"

# the cast is load-bearing and measured: psycopg dedupes the repeated name to one parameter, which
# binds NULL when no filter is supplied, and Postgres refuses to type that (42P08)
_SYMBOLS_SQL = (
    "SELECT symbol, name, exchange, active, first_bar_ts FROM symbols"
    " WHERE symbol > %(after)s"
    " AND (%(active)s::boolean IS NULL OR active = %(active)s)"
    " ORDER BY symbol LIMIT %(fetch)s"
)

# published in the generated page: a required window reads as a limitation unless it says why. The
# partition counts behind it are the /bars windows in docs/QUERY_PERFORMANCE.md
_BARS_DESCRIPTION = (
    "Minute bars for one symbol inside a required window, oldest first, keyset-paginated with "
    "`limit` and `cursor`. The window is required by design rather than as a limitation: without "
    "one, even the first page is planned across an index scan on every monthly partition of the "
    "bar table."
)

# lo and hi come from _instant_bounds, never the raw dates: a timestamptz compared with a date
# resolves to that date's UTC midnight and drops the final day's whole session
_BARS_SQL = (
    "SELECT ts, open, high, low, close, volume, trade_count, vwap FROM bars"
    " WHERE symbol = %(symbol)s AND ts >= %(lo)s AND ts <= %(hi)s AND ts > %(after)s"
    " ORDER BY ts LIMIT %(fetch)s"
)

# rendered once at import: a malformed query file must fail at import, not at request time
_DAILY_ROLLUP = db.sql.render("06_daily_rollup.sql").rstrip().removesuffix(";")
# the newlines are load-bearing: the rendered file opens on a -- comment, which on one line would
# swallow the rest of the wrapper
_DAILY_SQL = (
    "SELECT day, open, high, low, close, volume, bars FROM (\n"
    f"{_DAILY_ROLLUP}\n"
    ") AS page ORDER BY day LIMIT %(fetch)s"
)
# no wrapper on these two: neither paginates, and each file already projects the response columns in
# order, so a subquery would only move the plan the Class A number is taken on
_VOLATILITY_SQL = db.sql.render("01_volatility.sql")
_GAPS_SQL = db.sql.render("03_gaps.sql")

# 05_largest_moves.sql ranks by magnitude, which no cursor can walk; this is chronological on
# (ts, symbol), and stops at open and close because the hot-window index covers those two alone
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
      -- a zero open aborts the page, not the row; a NaN passes both guards and reaches the wire as
      -- the bare token NaN, which no JSON parser accepts. Deviation in docs/QUERY_PERFORMANCE.md
      AND b.open <> 0
      AND b.open <> 'NaN'::numeric
      AND b.close <> 'NaN'::numeric
      AND abs(100 * (b.close - b.open) / b.open) >= %(min_move_pct)s::numeric
ORDER BY b.ts, b.symbol
LIMIT %(fetch)s"""

# Postgres numeric refuses more precision than these rather than rounding, so a value Decimal
# accepts can still fail the query. Unnormalised: 0.10's trailing zero is real
_NUMERIC_MAX_DIGITS_BEFORE_POINT = 131_072
_NUMERIC_MAX_DIGITS_AFTER_POINT = 16_383
# the largest exponent Postgres numeric reads (INT32_MAX / 2, measured): the only bound on a zero's
# positive exponent, since a zero stores no digits before the point however far it moves
_NUMERIC_MAX_EXPONENT = 1_073_741_823


# declared through `responses` and NEVER response_model, which would re-serialise every row on the
# request path. next_cursor takes no default, so the document marks it required as well as nullable


class SymbolRow(BaseModel):
    symbol: str
    name: str | None
    exchange: str | None
    active: bool | None
    first_bar_ts: datetime | None


class SymbolsPage(BaseModel):
    data: list[SymbolRow]
    next_cursor: str | None


class BarRow(BaseModel):
    ts: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    trade_count: int | None
    vwap: float | None


class BarsPage(BaseModel):
    data: list[BarRow]
    next_cursor: str | None


class DailyRow(BaseModel):
    day: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    bars: int


class DailyPage(BaseModel):
    data: list[DailyRow]
    next_cursor: str | None


class VolatilityBucket(BaseModel):
    bucket_minute: int
    returns: int
    # null where the statistic is undefined: stddev_samp of a single return is NULL, and an
    # annualised figure derived from it is NULL with it
    avg_minutes_per_return: float | None
    stddev_pct: float | None
    annualized_pct: float | None


class VolatilityPage(BaseModel):
    data: list[VolatilityBucket]
    next_cursor: str | None


class GapsSummary(BaseModel):
    # 03_gaps.sql has no GROUP BY, so always exactly one row: a window with no gap answers zero
    # counts and null distribution fields
    gaps: int
    gaps_spanning_a_skipped_session: int
    mean_pct: float | None
    stddev_pct: float | None
    min_pct: float | None
    p25_pct: float | None
    median_pct: float | None
    p75_pct: float | None
    max_pct: float | None
    gaps_up: int
    gaps_down: int
    gaps_flat: int


class GapsPage(BaseModel):
    data: list[GapsSummary]
    next_cursor: str | None


class MoveRow(BaseModel):
    ts: datetime
    symbol: str
    open: float | None
    close: float | None
    move_pct: float | None


class MovesPage(BaseModel):
    data: list[MoveRow]
    next_cursor: str | None


_PAGE_200 = "A page of results, oldest first, with next_cursor null on the last page."
_WHOLE_200 = "The whole result for the window; this endpoint does not paginate, so next_cursor is null."
RESPONSE_200_SYMBOLS = {"model": SymbolsPage, "description": _PAGE_200}
RESPONSE_200_BARS = {"model": BarsPage, "description": _PAGE_200}
RESPONSE_200_DAILY = {"model": DailyPage, "description": _PAGE_200}
RESPONSE_200_VOLATILITY = {"model": VolatilityPage, "description": _WHOLE_200}
RESPONSE_200_GAPS = {"model": GapsPage, "description": _WHOLE_200}
RESPONSE_200_MOVES = {"model": MovesPage, "description": _PAGE_200}


def _reject_numeric_overflow(value: Decimal) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    if isinstance(exponent, str):
        # unreachable: pydantic's Decimal refuses non-finite values first. Returned rather than
        # counted, since a NaN or Infinity has no exponent to count
        return value
    if any(digits):
        too_wide = len(digits) + exponent > _NUMERIC_MAX_DIGITS_BEFORE_POINT
    else:
        too_wide = exponent > _NUMERIC_MAX_EXPONENT
    if too_wide or -exponent > _NUMERIC_MAX_DIGITS_AFTER_POINT:
        raise PydanticCustomError(
            "numeric_out_of_range", "min_move_pct is out of range for a Postgres numeric"
        )
    return value


# pydantic's lax `date` also accepts a midnight ISO datetime, discarding the offset, and a Unix
# timestamp; both are misread, so the validator below refuses them
_A_PLAIN_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# measured: pydantic-core accepts t, T, _ and a space and nothing else. Used only to name which
# misreading a refusal is, never whether to refuse
_A_TIME_SEPARATOR = re.compile(r"\d{4}-\d{2}-\d{2}[tT_ ]")
_A_DATE = TypeAdapter(date)
_MIDNIGHT = time(0, 0)
_CARRIES_A_TIME_MESSAGE = (
    "a window bound is a calendar date (YYYY-MM-DD); a time or an offset on one cannot be honoured,"
    " so it is refused rather than dropped"
)
_NOT_A_DATE_MESSAGE = (
    "a window bound is a calendar date (YYYY-MM-DD); a timestamp is read as a different day than it"
    " names, and the published schema admits no other spelling"
)


def _reject_a_window_bound_that_is_not_a_calendar_date(value):
    # asked of pydantic rather than pattern-matched: a pattern also claims the neighbours pydantic
    # itself rejects. Judged over every type lax `date` accepts, not only the str HTTP delivers
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return value
    if isinstance(value, (int, float)):
        # bool is an int in Python, and no reading of True is a calendar date either
        raise PydanticCustomError("date_is_not_a_calendar_date", _NOT_A_DATE_MESSAGE)
    if isinstance(value, datetime):
        # a datetime is a date subclass, so this must precede the date branch. pydantic keeps the
        # literal date and discards the offset, the same misreading as the text form
        if value.tzinfo is not None or value.time() != _MIDNIGHT:
            raise PydanticCustomError("date_carries_a_time", _CARRIES_A_TIME_MESSAGE)
        return value
    if not isinstance(value, str):
        return value
    if _A_PLAIN_DATE.fullmatch(value):
        return value
    # pydantic refuses every padded plain date, so fullmatch above and its accept set agree exactly
    # on what a calendar date is, and nothing needs stripping
    try:
        _A_DATE.validate_python(value)
    except ValidationError:
        return value
    if _A_TIME_SEPARATOR.match(value):
        raise PydanticCustomError("date_carries_a_time", _CARRIES_A_TIME_MESSAGE)
    raise PydanticCustomError("date_is_not_a_calendar_date", _NOT_A_DATE_MESSAGE)


# the date bound is load-bearing -- it makes the partition comparison IMMUTABLE and the pruning
# stable -- so the remedy for the coercions above is a validator, never a type change
WindowBound = Annotated[date, BeforeValidator(_reject_a_window_bound_that_is_not_a_calendar_date)]


# Query inside the Annotated alias, not as the default: a bare Query(...) default makes this
# FastAPI drop AfterValidator. Query first, so ge=0 refuses a negative before digits are counted
MinMovePct = Annotated[
    Decimal,
    Query(ge=0),
    AfterValidator(_reject_numeric_overflow),
    WithJsonSchema({"type": "number", "minimum": 0, "default": 0}),
]


def _limit_schema(page_default: int, page_max: int) -> dict:
    # documentation only -- the real floor and cap are enforced in resolve_request against the
    # RESOLVED limit, so this schema cannot change today's limit_out_of_range detail
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
    # the cursor decodes before the window is range-validated, so an inverted window answers 400
    # rather than 422. Spec line 507 mandates params ahead of range semantics
    cursor_values = decode_cursor(shape, cursor, start, end) if cursor is not None else None

    # the RESOLVED limit, never the client's: with no floor on either page DEFAULT in config, a zero
    # or negative one would reach paginate, whose refusal is a 500
    limit = raw_limit if raw_limit is not None else page_default
    if not 1 <= limit <= page_max:
        raise ApiError(
            400,
            "invalid_params",
            INVALID_PARAMS_MESSAGE,
            {"reason": "limit_out_of_range", "limit": limit, "max": page_max},
        )

    if start is not None and end is not None:
        # spec line 507's enumeration order, and the only self-consistent one: an inverted window
        # makes (end - start).days negative, so the length rule could not fire on it
        if start > end:
            raise ApiError(
                422, "invalid_range", INVALID_RANGE_MESSAGE, {"reason": "start_after_end"}
            )
        if start < settings.INGEST_START or end > settings.INGEST_END:
            # isoformat, not the date objects settings holds: a date in a detail is a TypeError in
            # the error handler, and the client gets a 500 instead of this 422
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
        # the difference, not the inclusive count: 2026-04-01 to 2026-06-30 is 90 days, exactly the
        # cap, and the window the Class A targets are measured at
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
    # matching no row -- refused before the query runs
    if "\x00" in symbol:
        raise ApiError(
            404,
            "unknown_symbol",
            UNKNOWN_SYMBOL_MESSAGE,
            {"reason": "unknown_symbol", "symbol": symbol},
        )
    # only presence is read: under the pool's dict factory an unaliased SELECT 1 keys the row
    # '?column?', so reading a field out of it breaks on a name nobody chose
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
    # no window, so no 422; default is declared because FastAPI adds a 422 in its own shape to any
    # route with a parameter documenting none of 422, 4XX or default
    responses={
        200: RESPONSE_200_SYMBOLS,
        400: RESPONSE_400,
        500: RESPONSE_500,
        "default": RESPONSE_DEFAULT,
    },
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
    responses={
        200: RESPONSE_200_BARS,
        400: RESPONSE_400,
        404: RESPONSE_404,
        422: RESPONSE_422,
        500: RESPONSE_500,
    },
)
def list_bars(
    symbol: str,
    start: WindowBound,
    end: WindowBound,
    limit: Annotated[int, WithJsonSchema(_limit_schema(*_BARS_CAPS))] | None = None,
    cursor: str | None = None,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _BARS_CAPS
    # no window-length cap here: spec line 630 says /bars accepts any window inside the ingested
    # range, and the per-page cost published for it is measured over the whole history
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
    responses={
        200: RESPONSE_200_DAILY,
        400: RESPONSE_400,
        404: RESPONSE_404,
        422: RESPONSE_422,
        500: RESPONSE_500,
    },
)
def list_daily(
    symbol: str,
    start: WindowBound,
    end: WindowBound,
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
    # the page narrows the committed query's :start rather than filtering its output, because :start
    # also drives the scan bound: an outer WHERE would re-aggregate the full window on every page
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
    responses={
        200: RESPONSE_200_VOLATILITY,
        400: RESPONSE_400,
        404: RESPONSE_404,
        422: RESPONSE_422,
        500: RESPONSE_500,
    },
)
def analytics_volatility(
    symbol: str,
    start: WindowBound,
    end: WindowBound,
    pool: ConnectionPool = Depends(get_pool),
):
    page_default, page_max = _VOLATILITY_CAPS
    # one row per half-hour bucket, so no page 2: the limit is discarded and next_cursor always
    # null. The pair is still passed, which makes the constant this handler reads observable
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
    responses={
        200: RESPONSE_200_GAPS,
        400: RESPONSE_400,
        404: RESPONSE_404,
        422: RESPONSE_422,
        500: RESPONSE_500,
    },
)
def analytics_gaps(
    symbol: str,
    start: WindowBound,
    end: WindowBound,
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
    # 03_gaps.sql has no GROUP BY, so exactly one row on any input: a window with no bars is a row
    # of zeros and nulls, not an empty list
    return {"data": rows, "next_cursor": None}


@router.get(
    "/analytics/largest-moves",
    summary="Minute moves at or above a threshold, universe-wide",
    responses={
        200: RESPONSE_200_MOVES,
        400: RESPONSE_400,
        422: RESPONSE_422,
        500: RESPONSE_500,
    },
)
def analytics_largest_moves(
    start: WindowBound,
    end: WindowBound,
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
    # page 1 starts at the window's own lower instant, not _BEFORE_ANY_BAR, so _MOVES_SQL's
    # redundant after_ts bound prunes on page 1 as it does on every later one
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

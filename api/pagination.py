import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Callable

from api.errors import INVALID_CURSOR_MESSAGE, ApiError


@dataclass(frozen=True)
class CursorShape:
    name: str
    fields: tuple[str, ...]
    types: dict[str, type]
    parsers: dict[str, Callable]
    renderers: dict[str, Callable]
    window_field: str
    window_bounds: Callable[[date, date], tuple]


def _parse_utc_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # encode_cursor always writes a UTC offset, so naive is not one it issued
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def _instant_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    # end-of-day: the last bar of a window's final day is inside the window
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
    )


def _day_bounds(start: date, end: date) -> tuple[date, date]:
    return (start, end)


class CursorWindowMisuse(RuntimeError):
    """decode_cursor was given a window for a shape that has none: a structurally wrong call."""


def _no_window(start: date, end: date) -> tuple:
    # RuntimeError, not the TypeError the parsers comprehension below catches: moving the window
    # checks into that try would make this bug a 400 unparsable_ts paging an unvalidated cursor
    raise CursorWindowMisuse(
        "the symbols cursor has no date window; decode_cursor must be called without start or end"
    )


def _render_day(value: date) -> str:
    # datetime subclasses date: date.isoformat drops the time, so two rows render to one cursor
    if type(value) is not date:
        raise TypeError(f"day renders a date, not {type(value).__name__}")
    return value.isoformat()


def _render_symbol(value: str) -> str:
    # bare str renders anything: None round-trips as the literal "None", paging against no symbol
    if type(value) is not str:
        raise TypeError(f"symbol renders a str, not {type(value).__name__}")
    return value


def _render_instant(value: datetime) -> str:
    # naive renders fine and fails this module's decoder: a next_cursor whose request is refused
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"ts renders an aware datetime, not {type(value).__name__}")
    return value.isoformat()


def _is_safe_postgres_text(value: str) -> bool:
    # a NUL and an unpaired surrogate (JSON \ud800) are valid JSON but bind as no Postgres
    # parameter: a 400 here, not a psycopg.DataError/UnicodeEncodeError at bind time
    if "\x00" in value:
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


BARS_CURSOR = CursorShape(
    name="bars",
    fields=("ts",),
    types={"ts": str},
    parsers={"ts": _parse_utc_instant},
    renderers={"ts": _render_instant},
    window_field="ts",
    window_bounds=_instant_bounds,
)

DAILY_CURSOR = CursorShape(
    name="daily",
    fields=("day",),
    types={"day": str},
    parsers={"day": date.fromisoformat},
    renderers={"day": _render_day},
    window_field="day",
    window_bounds=_day_bounds,
)

SYMBOLS_CURSOR = CursorShape(
    name="symbols",
    fields=("symbol",),
    types={"symbol": str},
    parsers={"symbol": str},
    renderers={"symbol": _render_symbol},
    # a real field: decode_cursor always reads values[shape.window_field]. no date window, so the
    # bounds refuse
    window_field="symbol",
    window_bounds=_no_window,
)

UNIVERSE_CURSOR = CursorShape(
    name="universe",
    fields=("ts", "symbol"),
    types={"ts": str, "symbol": str},
    parsers={"ts": _parse_utc_instant, "symbol": str},
    renderers={"ts": _render_instant, "symbol": _render_symbol},
    window_field="ts",
    window_bounds=_instant_bounds,
)


def encode_cursor(shape: CursorShape, values: dict) -> str:
    payload = {field: shape.renderers[field](values[field]) for field in shape.fields}
    body = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode()


def decode_cursor(
    shape: CursorShape, raw: str, start: date | None = None, end: date | None = None
) -> dict:
    try:
        # strict, not lenient: a splice the lenient decoder rebuilds into the original payload must
        # not round-trip to a 200. ValueError also covers non-utf-8 bytes
        body = base64.b64decode(raw, altchars=b"-_", validate=True)
        text = body.decode()
    except ValueError:
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "not_base64"})

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        # json.loads raises a plain ValueError past its int-to-str digit limit (JSONDecodeError is
        # one too) and RecursionError on deep nesting
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "not_json"})

    if not isinstance(payload, dict) or set(payload) != set(shape.fields):
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "wrong_fields"})

    for field in shape.fields:
        # type is, not isinstance: bool subclasses int; exact for whatever shape adds one next
        if type(payload[field]) is not shape.types[field]:
            raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "wrong_types"})
        # before any parser: a value that cannot bind as a Postgres parameter never reaches a query
        if shape.types[field] is str and not _is_safe_postgres_text(payload[field]):
            raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "wrong_types"})

    try:
        values = {field: shape.parsers[field](payload[field]) for field in shape.fields}
    except (ValueError, TypeError, OverflowError):
        # a year-1/9999 instant with a nonzero offset overflows astimezone, not ValueError
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "unparsable_ts"})

    value = values[shape.window_field]
    # each bound resolves from its own date, so one alone applies; a window_bounds low end reading
    # `end` would need this call rewritten
    if start is not None and value < shape.window_bounds(start, start)[0]:
        raise ApiError(
            400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "cursor_outside_window"}
        )
    if end is not None and value > shape.window_bounds(end, end)[1]:
        raise ApiError(
            400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "cursor_outside_window"}
        )

    return values


@dataclass(frozen=True)
class Page:
    data: list
    next_cursor: str | None


class PaginationError(RuntimeError):
    """More than limit + 1 rows reached paginate: the caller over-fetched."""


def paginate(rows: list, limit: int, shape: CursorShape) -> Page:
    # at limit 0 rows[limit - 1] is rows[-1], emitting the probe row's cursor -- what this prevents
    if limit < 1:
        raise PaginationError(f"a limit of {limit} cannot page anything")
    if len(rows) > limit + 1:
        raise PaginationError(f"{len(rows)} rows for a limit of {limit}")
    data = rows[:limit]
    if len(rows) <= limit:
        return Page(data=data, next_cursor=None)
    # the probe row proves a next page exists; never returned, never a cursor
    return Page(data=data, next_cursor=encode_cursor(shape, rows[limit - 1]))

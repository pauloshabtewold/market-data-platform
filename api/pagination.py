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
        # a naive result cannot have been issued by encode_cursor, which always writes a UTC offset
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def _instant_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    # end is bracketed to end-of-day because the last bar of a window's final day is inside that window
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
    )


def _day_bounds(start: date, end: date) -> tuple[date, date]:
    return (start, end)


def _render_day(value: date) -> str:
    # datetime subclasses date, so date.isoformat would silently drop the time and render two
    # different rows to the same cursor rather than failing on the wrong type
    if type(value) is not date:
        raise TypeError(f"day renders a date, not {type(value).__name__}")
    return value.isoformat()


def _render_instant(value: datetime) -> str:
    # a naive value renders happily and then fails this module's own decoder, so the endpoint would
    # hand a client a next_cursor its next request is refused for
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"ts renders an aware datetime, not {type(value).__name__}")
    return value.isoformat()


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

UNIVERSE_CURSOR = CursorShape(
    name="universe",
    fields=("ts", "symbol"),
    types={"ts": str, "symbol": str},
    parsers={"ts": _parse_utc_instant, "symbol": str},
    renderers={"ts": _render_instant, "symbol": str},
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
        # strict, not the urlsafe-lenient decoder: a splice the lenient form reconstructs back into
        # the original payload must be rejected here instead of round-tripping to a 200. ValueError
        # also covers a decoded payload that is not valid utf-8.
        body = base64.b64decode(raw, altchars=b"-_", validate=True)
        text = body.decode()
    except ValueError:
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "not_base64"})

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "not_json"})

    if not isinstance(payload, dict) or set(payload) != set(shape.fields):
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "wrong_fields"})

    for field in shape.fields:
        # type(...) is, not isinstance: bool is a subclass of int, and this must stay exact for
        # whatever shape adds one next
        if type(payload[field]) is not shape.types[field]:
            raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "wrong_types"})

    try:
        values = {field: shape.parsers[field](payload[field]) for field in shape.fields}
    except (ValueError, TypeError):
        raise ApiError(400, "invalid_cursor", INVALID_CURSOR_MESSAGE, {"reason": "unparsable_ts"})

    value = values[shape.window_field]
    # each bound is resolved from its own date alone, so that one supplied without the other still
    # applies -- a window_bounds whose low end reads `end` would need this call rewritten
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
    # rows[limit - 1] is rows[-1] at a limit of zero, which emits the probe row's own cursor -- the
    # exact off-by-one this function exists to prevent
    if limit < 1:
        raise PaginationError(f"a limit of {limit} cannot page anything")
    if len(rows) > limit + 1:
        raise PaginationError(f"{len(rows)} rows for a limit of {limit}")
    data = rows[:limit]
    if len(rows) <= limit:
        return Page(data=data, next_cursor=None)
    # the probe row proves a next page exists; it is never itself returned or turned into a cursor
    return Page(data=data, next_cursor=encode_cursor(shape, rows[limit - 1]))

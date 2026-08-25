import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ingest.client import Bar, next_month

log = logging.getLogger(__name__)


def window_bounds(month: date) -> tuple[datetime, datetime]:
    first = month.replace(day=1)
    nxt = next_month(first)
    # built from the same half-open month the partition bounds use, or a bar that passes here routes to a partition the unit never created
    return (
        datetime(first.year, first.month, 1, tzinfo=UTC),
        datetime(nxt.year, nxt.month, 1, tzinfo=UTC),
    )


def reject_reason(bar: Bar, lo: datetime, hi: datetime) -> str | None:
    # vwap is a vendor-computed average rather than a price, so it is deliberately excluded from this check
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
        return "non-positive price"
    if bar.high < bar.low:
        return "high below low"
    if not (bar.low <= bar.open <= bar.high) or not (bar.low <= bar.close <= bar.high):
        return "open or close outside high/low range"
    if bar.volume < 0:
        return "negative volume"
    if not (lo <= bar.ts < hi):
        return "timestamp outside ingest window"
    return None


@dataclass(frozen=True)
class CheckedBars:
    accepted: list[Bar]
    rejected: list[tuple[Bar, str]]


def check_bars(bars: list[Bar], month: date) -> CheckedBars:
    lo, hi = window_bounds(month)
    accepted: list[Bar] = []
    rejected: list[tuple[Bar, str]] = []
    seen: set[tuple[str, datetime]] = set()

    for bar in bars:
        reason = reject_reason(bar, lo, hi)
        if reason is not None:
            log.warning("%s %s rejected: %s", bar.symbol, bar.ts, reason)
            rejected.append((bar, reason))
            continue
        key = (bar.symbol, bar.ts)
        if key in seen:
            # ON CONFLICT (symbol, ts) DO NOTHING stores one row per key, so counting both here would break the row_count reconciliation against count(*) FROM bars
            log.info("%s %s duplicate, collapsing to first occurrence", bar.symbol, bar.ts)
            continue
        seen.add(key)
        accepted.append(bar)

    return CheckedBars(accepted=accepted, rejected=rejected)

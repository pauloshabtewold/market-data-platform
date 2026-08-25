from datetime import UTC, date, datetime
from decimal import Decimal

from ingest.client import Bar
from ingest.validate import CheckedBars, check_bars, reject_reason, window_bounds

MONTH = date(2026, 6, 1)
LO, HI = window_bounds(MONTH)


def bar(**overrides) -> Bar:
    fields = dict(
        symbol="AAPL",
        ts=datetime(2026, 6, 10, 14, 30, tzinfo=UTC),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=5,
        trade_count=1,
        vwap=Decimal("10"),
    )
    return Bar(**{**fields, **overrides})


def test_a_well_formed_bar_is_accepted_untouched():
    assert reject_reason(bar(), LO, HI) is None


def test_a_non_positive_price_is_rejected():
    assert reject_reason(bar(low=Decimal("0")), LO, HI) == "non-positive price"


def test_a_high_below_low_is_rejected_even_when_open_and_close_are_also_out_of_range():
    b = bar(high=Decimal("8"), open=Decimal("8"), close=Decimal("8"))
    assert reject_reason(b, LO, HI) == "high below low"


def test_an_open_or_close_outside_the_high_low_range_is_rejected():
    assert reject_reason(bar(open=Decimal("12")), LO, HI) == "open or close outside high/low range"
    assert reject_reason(bar(close=Decimal("8")), LO, HI) == "open or close outside high/low range"


def test_a_negative_volume_is_rejected():
    assert reject_reason(bar(volume=-1), LO, HI) == "negative volume"


def test_a_timestamp_outside_the_window_is_rejected():
    assert reject_reason(bar(ts=HI), LO, HI) == "timestamp outside ingest window"


def test_a_zero_vwap_is_accepted_because_vwap_is_not_a_price_rule():
    # vwap is a vendor-computed average rather than a price, so a zero here must not be treated as a non-positive price
    assert reject_reason(bar(vwap=Decimal("0")), LO, HI) is None


def test_the_months_first_instant_is_kept_and_the_next_months_first_instant_is_rejected():
    assert reject_reason(bar(ts=LO), LO, HI) is None
    assert reject_reason(bar(ts=HI), LO, HI) == "timestamp outside ingest window"


def test_check_bars_splits_a_mixed_list_into_accepted_and_rejected_without_raising():
    good = bar()
    bad = bar(volume=-1)

    out = check_bars([good, bad], MONTH)

    assert isinstance(out, CheckedBars)
    assert out.accepted == [good]
    assert out.rejected == [(bad, "negative volume")]


def test_two_identical_bars_collapse_to_one_accepted_and_zero_rejected():
    b = bar()

    out = check_bars([b, b], MONTH)

    assert len(out.accepted) == 1
    assert out.rejected == []


def test_check_bars_keeps_processing_after_a_rejected_bar_rather_than_stopping():
    bad = bar(volume=-1)
    first_good = bar(ts=datetime(2026, 6, 10, 14, 31, tzinfo=UTC))
    second_good = bar(ts=datetime(2026, 6, 10, 14, 32, tzinfo=UTC))

    out = check_bars([bad, first_good, second_good], MONTH)

    # a break here would stop at the first rejection and lose every bar queued behind it
    assert out.accepted == [first_good, second_good]
    assert out.rejected == [(bad, "negative volume")]


def test_check_bars_keeps_processing_after_a_duplicate_rather_than_stopping():
    b = bar()
    other = bar(ts=datetime(2026, 6, 10, 14, 31, tzinfo=UTC))

    out = check_bars([b, b, other], MONTH)

    # a break here would stop at the duplicate and lose every bar queued behind it
    assert out.accepted == [b, other]


def test_window_bounds_returns_tz_aware_utc_datetimes_ending_at_the_next_months_first_instant():
    lo, hi = window_bounds(date(2026, 6, 1))

    assert lo == datetime(2026, 6, 1, tzinfo=UTC)
    assert hi == datetime(2026, 7, 1, tzinfo=UTC)
    assert lo.tzinfo is UTC
    assert hi.tzinfo is UTC


def test_a_non_positive_open_is_refused_as_a_price_and_not_as_a_range_violation():
    # ordering matters: without the first rule this same bar still fails, but on the range check, and rejected_count stops being auditable by reason
    assert reject_reason(bar(open=Decimal("0")), LO, HI) == "non-positive price"


def test_a_non_positive_high_is_refused_as_a_price_and_not_as_a_low_above_high():
    assert reject_reason(bar(high=Decimal("0")), LO, HI) == "non-positive price"


def test_a_non_positive_close_is_refused_as_a_price_and_not_as_a_range_violation():
    assert reject_reason(bar(close=Decimal("0")), LO, HI) == "non-positive price"


def test_a_bar_priced_below_a_dollar_is_kept():
    # the rule is non-positive, not implausibly small: a floor anywhere above zero silently refuses real rows and inflates rejected_count
    cheap = bar(open=Decimal("0.55"), high=Decimal("0.60"), low=Decimal("0.40"), close=Decimal("0.50"))
    assert reject_reason(cheap, LO, HI) is None


def test_a_flat_bar_whose_four_prices_are_equal_is_kept():
    # a minute with one trade prints open == high == low == close, which is ordinary on this feed and must not be refused as high-below-low or out-of-range
    flat = bar(open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"))
    assert reject_reason(flat, LO, HI) is None


def test_a_bar_opening_on_its_low_and_closing_on_its_high_is_kept():
    # the range rule is closed at both ends, and a minute whose open or close is its own extreme is the common case rather than the edge
    edge = bar(open=Decimal("9"), high=Decimal("11"), low=Decimal("9"), close=Decimal("11"))
    assert reject_reason(edge, LO, HI) is None


def test_a_zero_volume_bar_is_kept():
    # the rule is negative volume: refusing zero would drop a minute that printed no size but is otherwise well formed
    assert reject_reason(bar(volume=0), LO, HI) is None

import logging
from datetime import MINYEAR, date, timedelta
from typing import Annotated

from pydantic import AfterValidator, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty or all whitespace")
    # stripped: libpq refuses a leading-space DSN and echoes the whole string, password included;
    # a padded credential or host reads as a wrong one
    return value.strip()


# an empty or all-space DSN is libpq's connect-with-every-default, reaching whatever PGHOST and 5432
# point at; an empty credential reads at the vendor as a bad key
NonBlankStr = Annotated[str, AfterValidator(_not_blank)]


def _hot_window_cutoff(ingest_end: date, months: int) -> date | None:
    # date_trunc('month', end - (months - 1) months); None where that falls before year 1
    year, month_index = divmod(ingest_end.year * 12 + ingest_end.month - months, 12)
    if year < MINYEAR:
        return None
    return date(year, month_index + 1, 1)


class Settings(BaseSettings):
    # an empty value is a value: it outranks .env and the default, so `KEY=` is refused, reported or
    # read as unset by name, never silently replaced by another source's
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ALPACA_KEY_ID: NonBlankStr
    ALPACA_SECRET_KEY: NonBlankStr
    ALPACA_TRADING_HOST: NonBlankStr
    INGEST_START: date
    INGEST_END: date
    DATABASE_URL: NonBlankStr

    ALPACA_FEED: NonBlankStr = "iex"
    ALPACA_ADJUSTMENT: NonBlankStr = "split,spin-off"
    ALPACA_LIMIT: int = 10000
    RATE_LIMIT_RPM: int = 200
    HTTP_MAX_ATTEMPTS: int = 5
    AGG_MAX_WINDOW_DAYS: int = 90
    # one value for both the hot-window index predicate and the month list the deploy copies to RDS,
    # so index and copy cannot drift. Counted back from INGEST_END's month, it must reach
    # AGG_MAX_WINDOW_DAYS before INGEST_END -- the widest window the AGGREGATING endpoints accept;
    # /bars has no cap. hot_window_configuration_problems holds the rule, create_app enforces it
    HOT_WINDOW_MONTHS: int = 4

    # measured on the loaded database: 19 root blocks for a default page, 401 at the cap, on the
    # worst of three windows. docs/QUERY_PERFORMANCE.md has the windows and the index behind each
    BARS_PAGE_DEFAULT: int = 1000
    BARS_PAGE_MAX: int = 10000
    # measured on /analytics/largest-moves, the only endpoint reading these that fills a default
    # page: at min_move_pct = 0, page 1's worst over two windows is 121 root blocks and 1,024 at the
    # cap, and a cursor inside a session reads more. docs/QUERY_PERFORMANCE.md has both, what a
    # threshold costs, and the bounds a request runs under
    AGG_PAGE_DEFAULT: int = 100
    AGG_PAGE_MAX: int = 1000
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10
    # NonBlankStr like the other six string keys, and the closed vocabulary below is checked against
    # the stripped value
    LOG_LEVEL: NonBlankStr = "INFO"
    # the e2e suite's target, defaulted because CI starts no service. The window is exactly
    # AGG_MAX_WINDOW_DAYS and sits inside the hot window the deploy copies, so the same suite runs
    # against either database. Plain str, not NonBlankStr: e2e_configuration_problems reports an
    # empty value by name before the first request, and the API and ingest keep starting
    E2E_BASE_URL: str = "http://127.0.0.1:8000"
    E2E_START: date = date(2026, 4, 1)
    E2E_END: date = date(2026, 6, 30)
    E2E_SYMBOLS: str = "AAPL,MSFT,NVDA"

    BARS_PER_TICKER_DAY: float | None = None
    DEEP_PAGE_DEPTH: int | None = None
    HEAP_INDEX_BYTE_RATIO: float | None = None
    # query 7's ceiling, and a different index: 9 and 10 scan the PK, 7 needs vwap
    # and volume and can only be served by the covering index. Its numerator is the whole table's
    # heap over every child, where the key above divides one partition's heap by that partition's PK
    # -- so its partner is the pooled whole-table ratio, not the key above. Two keys because on the
    # same basis they measure far apart, and one invites judging 7 against the PK's number
    HEAP_INDEX_COVERING_RATIO: float | None = None

    @field_validator(
        "BARS_PER_TICKER_DAY",
        "DEEP_PAGE_DEPTH",
        "HEAP_INDEX_BYTE_RATIO",
        "HEAP_INDEX_COVERING_RATIO",
        mode="before",
    )
    @classmethod
    def _an_empty_measured_key_is_unset(cls, value):
        # .env.example ships these four as `KEY=` until measured, and unset is what makes require()
        # name the key at use rather than a parse error killing every import. Blank, not only empty:
        # dotenv trims an unquoted value, so `KEY=` and `KEY=   ` arrive as "" while `KEY="  "` keep
        # its spaces -- one intent written three ways, and a rule against "" alone misreads two
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _page_pool_and_log_level_bounds_hold(self):
        if self.BARS_PAGE_DEFAULT > self.BARS_PAGE_MAX:
            raise ValueError(
                f"BARS_PAGE_DEFAULT={self.BARS_PAGE_DEFAULT} exceeds"
                f" BARS_PAGE_MAX={self.BARS_PAGE_MAX}"
            )
        if self.AGG_PAGE_DEFAULT > self.AGG_PAGE_MAX:
            raise ValueError(
                f"AGG_PAGE_DEFAULT={self.AGG_PAGE_DEFAULT} exceeds"
                f" AGG_PAGE_MAX={self.AGG_PAGE_MAX}"
            )
        if self.DB_POOL_MIN > self.DB_POOL_MAX:
            raise ValueError(
                f"DB_POOL_MIN={self.DB_POOL_MIN} exceeds DB_POOL_MAX={self.DB_POOL_MAX}"
            )
        if self.DB_POOL_MAX < 1:
            raise ValueError(
                f"DB_POOL_MAX={self.DB_POOL_MAX} is below the minimum pool size of 1"
            )
        # checked here because basicConfig accepts a bad level silently once a handler already exists
        if self.LOG_LEVEL not in logging.getLevelNamesMapping():
            raise ValueError(
                f"LOG_LEVEL={self.LOG_LEVEL!r} is not one of"
                f" {sorted(logging.getLevelNamesMapping())}"
            )
        return self


def hot_window_configuration_problems(settings: Settings) -> list[str]:
    # out of Settings for e2e_configuration_problems' reason: only the API serves from the
    # hot-window index, so db.migrate and the ingest must start without it
    months = settings.HOT_WINDOW_MONTHS
    if months < 1:
        return [
            f"HOT_WINDOW_MONTHS={months} is below 1; the hot window has to hold at least"
            f" INGEST_END={settings.INGEST_END}'s own month"
        ]
    cutoff = _hot_window_cutoff(settings.INGEST_END, months)
    if cutoff is None:
        return [
            f"HOT_WINDOW_MONTHS={months} reaches back before year 1 from"
            f" INGEST_END={settings.INGEST_END}"
        ]
    # here, beside the rule it protects: a missing floor does not merely accept a
    # bad value, it switches this check off, since the span below is never negative and so can only
    # clear a cap of zero or less. AGG_MAX_WINDOW_DAYS is read by api/routes.py alone, and a floor
    # Settings would stop db.migrate and the ingest importing over a key neither reads
    if settings.AGG_MAX_WINDOW_DAYS < 1:
        return [
            f"AGG_MAX_WINDOW_DAYS={settings.AGG_MAX_WINDOW_DAYS} is below the minimum window of"
            " 1 day; this check compares against it and cannot fire below it"
        ]
    span = (settings.INGEST_END - cutoff).days
    if span < settings.AGG_MAX_WINDOW_DAYS:
        return [
            f"HOT_WINDOW_MONTHS={months} anchored at INGEST_END={settings.INGEST_END} spans"
            f" {span} days back to {cutoff}, short of"
            f" AGG_MAX_WINDOW_DAYS={settings.AGG_MAX_WINDOW_DAYS}; the hot-window index would not"
            " cover the widest window an endpoint can ask for"
        ]
    return []


def e2e_configuration_problems(settings: Settings) -> list[str]:
    # apart from Settings on purpose: as a model validator, advancing INGEST_END without editing the
    # E2E_* keys would stop api.main, db.migrate and ingest starting, not just the suite that reads
    # them -- tests/e2e/conftest.py checks them before its first request instead
    problems: list[str] = []
    # a one-day window has no first session distinct from its last; how many a longer one holds
    # depends on the data, which the suite reads from the service
    if settings.E2E_START >= settings.E2E_END:
        problems.append(
            f"E2E_START={settings.E2E_START} is not before E2E_END={settings.E2E_END}"
        )
    span = (settings.E2E_END - settings.E2E_START).days
    if span > settings.AGG_MAX_WINDOW_DAYS:
        problems.append(
            f"E2E_START={settings.E2E_START}..E2E_END={settings.E2E_END} spans {span} days, over"
            f" AGG_MAX_WINDOW_DAYS={settings.AGG_MAX_WINDOW_DAYS}"
        )
    if settings.E2E_START < settings.INGEST_START or settings.E2E_END > settings.INGEST_END:
        problems.append(
            f"E2E_START={settings.E2E_START}..E2E_END={settings.E2E_END} is outside the ingested"
            f" range INGEST_START={settings.INGEST_START}..INGEST_END={settings.INGEST_END}"
        )
    # the suite's over-the-cap request starts here, and resolve_request refuses a start outside the
    # ingested range before reading the length, so that refusal needs these days to exist
    oversized_start = settings.E2E_END - timedelta(days=settings.AGG_MAX_WINDOW_DAYS + 1)
    if oversized_start < settings.INGEST_START:
        problems.append(
            f"E2E_END={settings.E2E_END} minus AGG_MAX_WINDOW_DAYS={settings.AGG_MAX_WINDOW_DAYS}"
            f" + 1 days is {oversized_start}, before INGEST_START={settings.INGEST_START}; the"
            " over-the-cap window the suite requests would be refused as out of range, not as too long"
        )
    hot_window = hot_window_configuration_problems(settings)
    problems.extend(hot_window)
    if not hot_window:
        cutoff = _hot_window_cutoff(settings.INGEST_END, settings.HOT_WINDOW_MONTHS)
        if settings.E2E_START < cutoff:
            problems.append(
                f"E2E_START={settings.E2E_START} is before the hot window's own cutoff {cutoff}"
                f" (HOT_WINDOW_MONTHS={settings.HOT_WINDOW_MONTHS} back from"
                f" INGEST_END={settings.INGEST_END}); the suite would not run unchanged against"
                " the deployed hot-window copy"
            )
        # the deployed copy's ingested range starts at the cutoff, so the same request needs room there
        if oversized_start < cutoff:
            problems.append(
                f"E2E_END={settings.E2E_END} minus AGG_MAX_WINDOW_DAYS={settings.AGG_MAX_WINDOW_DAYS}"
                f" + 1 days is {oversized_start}, before the hot window's own cutoff {cutoff}; on the"
                " deployed hot-window copy the over-the-cap window would be refused as out of range"
            )
    # every entry, not one: "AAPL,,MSFT" has two real symbols and an empty slot, which any()
    # reads as fine because the two real ones already make it non-empty
    if not all(symbol.strip() for symbol in settings.E2E_SYMBOLS.split(",")):
        problems.append(f"E2E_SYMBOLS={settings.E2E_SYMBOLS!r} carries an empty symbol")
    if not settings.E2E_BASE_URL.startswith(("http://", "https://")):
        problems.append(f"E2E_BASE_URL={settings.E2E_BASE_URL!r} has no http:// or https:// scheme")
    return problems


settings = Settings()

_MEASURED_BY = {
    "BARS_PER_TICKER_DAY": "the sample ingest run",
    "DEEP_PAGE_DEPTH": "the sample ingest run against the loaded calendar",
    "HEAP_INDEX_BYTE_RATIO": "the loaded partition's heap and index sizes",
    "HEAP_INDEX_COVERING_RATIO": "the whole table's heap against the covering index (symbol, ts) INCLUDE (vwap, volume)",
}

# a key added to Settings with no entry above would raise KeyError, not this function's contract
_MEASURED_FALLBACK = "a measurement recorded in docs/"


def require(name: str):
    # required at use, not at import: each is measured by a run an import-time refusal would have
    # made unrunnable -- the sample ingest for the first three, Feature 4's sweep for the ratio
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(
            f"{name} is unset; it is measured from {_MEASURED_BY.get(name, _MEASURED_FALLBACK)} and written"
            " into .env by hand; docs/METHODOLOGY.md carries every measured value with its arithmetic"
        )
    return value

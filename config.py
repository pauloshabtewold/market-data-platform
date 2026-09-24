import logging
from datetime import MINYEAR, date, timedelta
from typing import Annotated

from pydantic import AfterValidator, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty or all whitespace")
    # stripped: libpq refuses a connection string with a leading space and quotes the whole string,
    # password included, back in the message, and a padded credential or host reads as a wrong one
    return value.strip()


# an empty or all-space DSN is libpq's connect-with-every-default, which reaches whatever PGHOST and
# port 5432 point at, and an empty credential or host reads at the vendor as a bad key
NonBlankStr = Annotated[str, AfterValidator(_not_blank)]


def _hot_window_cutoff(ingest_end: date, months: int) -> date | None:
    # the first day of the month (months - 1) months before ingest_end's own, the day Postgres's
    # date_trunc('month', end - (months - 1) months) gives; None where that falls before year 1
    year, month_index = divmod(ingest_end.year * 12 + ingest_end.month - months, 12)
    if year < MINYEAR:
        return None
    return date(year, month_index + 1, 1)


class Settings(BaseSettings):
    # an empty value is a value: it outranks .env and the default, so `KEY=` in the environment is
    # refused, reported or read as unset under its own name, never replaced by another source's value
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
    # the hot-window partial index's predicate AND the month list Feature 10 copies to RDS -- one
    # value doing both jobs, so the local index and the deployed copy cannot drift apart. Counted
    # back from INGEST_END's month, it has to reach AGG_MAX_WINDOW_DAYS before INGEST_END, the widest
    # window the AGGREGATING endpoints accept -- /bars passes no window cap and takes the whole
    # ingested range. hot_window_configuration_problems holds that rule and create_app enforces it
    HOT_WINDOW_MONTHS: int = 4

    # confirmed against the loaded database rather than left at a guess: a default page costs
    # 19 root blocks and a page at the cap costs 401 on the worst of three measured windows.
    # docs/QUERY_PERFORMANCE.md carries the windows, the counts and the index behind each
    BARS_PAGE_DEFAULT: int = 1000
    BARS_PAGE_MAX: int = 10000
    # confirmed on /analytics/largest-moves, the one endpoint reading these that fills a page at the
    # default: at min_move_pct = 0, page 1's worst reading across two windows is 121 root blocks for
    # a default page and 1,024 at the cap -- a cursor inside a session reads more on that window.
    # docs/QUERY_PERFORMANCE.md carries both, what a threshold costs, and the bounds a request runs
    # under, which a threshold scan over a window without the hot-window index can reach
    AGG_PAGE_DEFAULT: int = 100
    AGG_PAGE_MAX: int = 1000
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10
    LOG_LEVEL: str = "INFO"
    # the end-to-end suite's target, defaulted because CI starts no service and exports none of
    # them. The window is exactly AGG_MAX_WINDOW_DAYS and sits inside the hot window Feature 10
    # copies to RDS, so the identical suite runs against the deployed database unchanged. Plain
    # str, not NonBlankStr: e2e_configuration_problems reports an empty value by name before the
    # suite's first request, and the API and the ingest, which never read it, keep starting
    E2E_BASE_URL: str = "http://127.0.0.1:8000"
    E2E_START: date = date(2026, 4, 1)
    E2E_END: date = date(2026, 6, 30)
    E2E_SYMBOLS: str = "AAPL,MSFT,NVDA"

    BARS_PER_TICKER_DAY: float | None = None
    DEEP_PAGE_DEPTH: int | None = None
    HEAP_INDEX_BYTE_RATIO: float | None = None
    # query 7's ceiling, and a different index from the one above: 9 and 10 scan the PK, while 7
    # needs vwap and volume too and can only be served by the covering index. two keys rather than
    # one because on identical data they measure far apart, and a single key invites query 7 being
    # judged against the PK's number -- which is flattering and cites an index it cannot use
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
        # .env.example ships these four as `KEY=` until they are measured, and unset is what makes
        # require() name the key at use rather than a parse error stopping every import of config
        return None if value == "" else value

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
    # kept out of Settings construction for the reason e2e_configuration_problems is: only the API
    # serves from the hot-window index, so db.migrate and the ingest have to start without it
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
    # apart from Settings construction on purpose: as a model validator, advancing INGEST_END
    # without editing the E2E_* keys in step would stop api.main, db.migrate and ingest from
    # starting, not only the e2e suite that reads these keys -- tests/e2e/conftest.py checks them
    # before its first request instead
    problems: list[str] = []
    # a one-day window has no first session distinct from its last; how many sessions a longer
    # window holds depends on the data, which the suite reads from the service
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
    # ingested range before it reads the length, so the refusal the suite expects needs these days
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
    # every entry, not just one: "AAPL,,MSFT" carries two real symbols and one empty slot, and an
    # any() check reads that as fine because the two real ones already make it non-empty
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
    "HEAP_INDEX_COVERING_RATIO": "the loaded partition's heap against the covering index (symbol, ts) INCLUDE (vwap, volume)",
}

# a key added to Settings without a matching entry above would otherwise raise KeyError instead of this function's contract
_MEASURED_FALLBACK = "a measurement recorded in docs/"


def require(name: str):
    # required at use and not at import: these are measured by runs that config.py refusing to import would have made unrunnable -- the sample ingest for the first three, Feature 4's index sweep for the covering ratio
    value = getattr(settings, name)
    if value is None:
        raise RuntimeError(
            f"{name} is unset; it is measured from {_MEASURED_BY.get(name, _MEASURED_FALLBACK)} and written"
            " into .env by hand; docs/METHODOLOGY.md carries every measured value with its arithmetic"
        )
    return value

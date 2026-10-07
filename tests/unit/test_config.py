import os
import subprocess
import sys
from datetime import date

import pytest
from pydantic import ValidationError

import config
from config import Settings

REQUIRED = {
    "ALPACA_KEY_ID": "key",
    "ALPACA_SECRET_KEY": "secret",
    "ALPACA_TRADING_HOST": "https://paper-api.alpaca.markets",
    "INGEST_START": "2020-08-01",
    "INGEST_END": "2026-06-30",
    "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:5432/marketdata",
}

MEASURED = (
    "BARS_PER_TICKER_DAY",
    "DEEP_PAGE_DEPTH",
    "HEAP_INDEX_BYTE_RATIO",
    "HEAP_INDEX_COVERING_RATIO",
)

DEFAULTED = (
    "ALPACA_FEED",
    "ALPACA_ADJUSTMENT",
    "ALPACA_LIMIT",
    "RATE_LIMIT_RPM",
    "HTTP_MAX_ATTEMPTS",
    "AGG_MAX_WINDOW_DAYS",
    "HOT_WINDOW_MONTHS",
    "BARS_PAGE_DEFAULT",
    "BARS_PAGE_MAX",
    "AGG_PAGE_DEFAULT",
    "AGG_PAGE_MAX",
    "DB_POOL_MIN",
    "DB_POOL_MAX",
    "LOG_LEVEL",
    "E2E_BASE_URL",
    "E2E_START",
    "E2E_END",
    "E2E_SYMBOLS",
)

# duplicated from config.py's _MEASURED_BY on purpose -- reading it back would let a changed
# phrase move both sides of an equality assertion and the mutation it exists to catch would survive
MEASURED_PHRASES = {
    "BARS_PER_TICKER_DAY": "the sample ingest run",
    "DEEP_PAGE_DEPTH": "the sample ingest run against the loaded calendar",
    "HEAP_INDEX_BYTE_RATIO": "the loaded partition's heap and index sizes",
    "HEAP_INDEX_COVERING_RATIO": (
        "the whole table's heap against the covering index"
        " (symbol, ts) INCLUDE (vwap, volume)"
    ),
}

# same reasoning as MEASURED_PHRASES, for config.py's _MEASURED_FALLBACK
MEASURED_FALLBACK_PHRASE = "a measurement recorded in docs/"

EXPECTED_REQUIRE_MESSAGE = (
    "{key} is unset; it is measured from {phrase} and written into .env by hand;"
    " docs/METHODOLOGY.md carries every measured value with its arithmetic"
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in MEASURED + DEFAULTED:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def no_env(monkeypatch):
    # broader than clean_env -- also strips the required keys, so a construction below can only
    # be satisfied by the file or kwargs under test, never by whatever the ambient shell exports
    for key in Settings.model_fields:
        monkeypatch.delenv(key, raising=False)


def test_measured_keys_are_absent_legitimately(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    for key in MEASURED:
        assert getattr(settings, key) is None


def test_measured_and_measured_by_name_the_same_keys():
    # a duplicated list is what let this tuple drift a key behind config.py's _MEASURED_BY once
    assert set(MEASURED) == set(config._MEASURED_BY)


def test_the_three_lists_between_them_name_every_setting_on_the_right_side():
    # clean_env deletes exactly what these tuples name, so a key added to config.py and not to
    # DEFAULTED is read from the real environment by every test that asks for a clean one -- which
    # is what HOT_WINDOW_MONTHS did from Feature 4 until Feature 5, with no test able to notice.
    #
    # REQUIRED is asserted against the same source rather than used to define "not covered". As the
    # excluded side it was a third hand-written list that nothing compared with anything, so moving
    # a key into it at its own default hid that key from clean_env exactly as the drift above did.
    required = {name for name, field in Settings.model_fields.items() if field.is_required()}
    # a config.py mutation of a required key has to be applied in isolating form -- for LOG_LEVEL's
    # default that means LOG_LEVEL=INFO in the environment -- or module-level settings = Settings()
    # turns the kill into six collection errors that name no test, which reads as a survivor
    assert set(REQUIRED) == required
    assert set(MEASURED) | set(DEFAULTED) == set(Settings.model_fields) - required


def test_require_raises_naming_the_missing_key(monkeypatch):
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", None)
    with pytest.raises(RuntimeError) as excinfo:
        config.require("DEEP_PAGE_DEPTH")
    assert str(excinfo.value) == EXPECTED_REQUIRE_MESSAGE.format(
        key="DEEP_PAGE_DEPTH", phrase=MEASURED_PHRASES["DEEP_PAGE_DEPTH"]
    )


def test_require_returns_the_value_once_measured(monkeypatch):
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", 402_000)
    assert config.require("DEEP_PAGE_DEPTH") == 402_000


def test_missing_credential_fails_at_construction(monkeypatch):
    # the positive control first, because Settings checks a required value for its type and, for a
    # string, that it is not blank, and nothing more -- the model validator reads defaulted keys -- so
    # each ValidationError below would be raised against a configuration no operator could
    # use: a plaintext host, an unusable DSN or an ingest window that runs backwards
    supplied = Settings(_env_file=None, **REQUIRED)
    assert supplied.ALPACA_KEY_ID and supplied.ALPACA_SECRET_KEY
    assert supplied.ALPACA_TRADING_HOST.startswith("https://")
    assert supplied.DATABASE_URL.startswith("postgresql://")
    assert supplied.INGEST_START < supplied.INGEST_END

    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    without_key_id = {k: v for k, v in REQUIRED.items() if k != "ALPACA_KEY_ID"}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **without_key_id)
    assert "ALPACA_KEY_ID" in str(excinfo.value)


def test_defaults_match_the_documented_values(clean_env):
    # every default pinned by value: the three-list guard above reads names and never values
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.ALPACA_FEED == "iex"
    assert settings.ALPACA_ADJUSTMENT == "split,spin-off"
    assert settings.ALPACA_LIMIT == 10000
    assert settings.RATE_LIMIT_RPM == 200
    assert settings.HTTP_MAX_ATTEMPTS == 5
    assert settings.AGG_MAX_WINDOW_DAYS == 90
    assert settings.HOT_WINDOW_MONTHS == 4
    assert settings.BARS_PAGE_DEFAULT == 1000
    assert settings.BARS_PAGE_MAX == 10000
    assert settings.AGG_PAGE_DEFAULT == 100
    assert settings.AGG_PAGE_MAX == 1000
    assert settings.DB_POOL_MIN == 1
    assert settings.DB_POOL_MAX == 10
    assert settings.LOG_LEVEL == "INFO"
    assert settings.E2E_BASE_URL == "http://127.0.0.1:8000"
    assert settings.E2E_START == date(2026, 4, 1)
    assert settings.E2E_END == date(2026, 6, 30)
    assert settings.E2E_SYMBOLS == "AAPL,MSFT,NVDA"


def test_require_raises_runtime_error_for_a_key_no_one_mapped(monkeypatch):
    # a later feature adds the Settings field and forgets the _MEASURED_BY entry: still RuntimeError, never KeyError
    monkeypatch.setattr(config, "_MEASURED_BY", {})
    monkeypatch.setattr(config.settings, "DEEP_PAGE_DEPTH", None)
    with pytest.raises(RuntimeError) as excinfo:
        config.require("DEEP_PAGE_DEPTH")
    assert str(excinfo.value) == EXPECTED_REQUIRE_MESSAGE.format(
        key="DEEP_PAGE_DEPTH", phrase=MEASURED_FALLBACK_PHRASE
    )


def test_every_optional_setting_is_reachable_through_require(monkeypatch):
    optional = [
        name for name, field in Settings.model_fields.items() if field.default is None
    ]
    for name in optional:
        monkeypatch.setattr(config.settings, name, None)
        with pytest.raises(RuntimeError, match=name):
            config.require(name)


def _short_hot_window(months, end, span, cutoff, agg):
    return (
        f"HOT_WINDOW_MONTHS={months} anchored at INGEST_END={end} spans {span} days back to"
        f" {cutoff}, short of AGG_MAX_WINDOW_DAYS={agg}; the hot-window index would not cover the"
        " widest window an endpoint can ask for"
    )


def test_the_hot_window_cutoff_is_the_first_of_the_month_postgres_date_trunc_gives():
    # each row checked against Postgres 16's date_trunc('month', end - (months - 1) months); the
    # first three put INGEST_END's month minus (months - 1) on 0, -1 and -2, borrowing a year, and
    # the fourth is a December end that borrows none
    cases = [
        (date(2026, 3, 31), 4, date(2025, 12, 1)),
        (date(2026, 2, 28), 4, date(2025, 11, 1)),
        (date(2026, 1, 31), 4, date(2025, 10, 1)),
        (date(2025, 12, 31), 4, date(2025, 9, 1)),
        (date(2026, 6, 30), 4, date(2026, 3, 1)),
        (date(2026, 6, 30), 1, date(2026, 6, 1)),
        (date(2026, 1, 1), 1, date(2026, 1, 1)),
        (date(2026, 12, 15), 12, date(2026, 1, 1)),
        (date(2026, 12, 15), 13, date(2025, 12, 1)),
        (date(2024, 2, 29), 25, date(2022, 2, 1)),
        (date(2026, 6, 30), 24306, date(1, 1, 1)),
        (date(2026, 6, 30), 24295, date(1, 12, 1)),
    ]
    for end, months, expected in cases:
        assert config._hot_window_cutoff(end, months) == expected, (end, months)
    assert config._hot_window_cutoff(date(2026, 6, 30), 24307) is None


def test_a_hot_window_is_accepted_at_its_exact_span_and_refused_one_day_short(clean_env):
    # at INGEST_END=2026-06-30: 4 months back is 2026-03-01, a 121-day span, and 3 months back is
    # 2026-04-01, a 90-day span -- each pinned at both edges
    def problems(months, agg):
        return config.hot_window_configuration_problems(
            Settings(_env_file=None, HOT_WINDOW_MONTHS=months, AGG_MAX_WINDOW_DAYS=agg, **REQUIRED)
        )

    assert problems(4, 121) == []
    assert problems(4, 122) == [_short_hot_window(4, "2026-06-30", 121, "2026-03-01", 122)]
    assert problems(3, 90) == []
    assert problems(3, 91) == [_short_hot_window(3, "2026-06-30", 90, "2026-04-01", 91)]


def test_a_hot_window_counted_back_across_the_year_boundary_lands_on_the_right_month(clean_env):
    # INGEST_END in March at 4 months puts the month arithmetic on 0 exactly -- the December
    # before -- with February and January ends borrowing a year the same way, and a December end
    # that borrows none
    for end, span, cutoff in (
        ("2026-03-31", 120, "2025-12-01"),
        ("2026-02-28", 119, "2025-11-01"),
        ("2026-01-31", 122, "2025-10-01"),
        ("2025-12-31", 121, "2025-09-01"),
    ):
        fields = {**REQUIRED, "INGEST_END": end}
        at_span = Settings(_env_file=None, AGG_MAX_WINDOW_DAYS=span, **fields)
        assert config.hot_window_configuration_problems(at_span) == [], end
        over = Settings(_env_file=None, AGG_MAX_WINDOW_DAYS=span + 1, **fields)
        assert config.hot_window_configuration_problems(over) == [
            _short_hot_window(4, end, span, cutoff, span + 1)
        ], end


def test_a_hot_window_refusal_does_not_stop_settings_from_constructing(clean_env):
    # INGEST_END=2026-05-01 at the defaults (H=4, A=90): 4 months back is 2026-02-01, an 89-day
    # span. Refused by the API, which serves from the index, never by db.migrate or the ingest
    settings = Settings(_env_file=None, **{**REQUIRED, "INGEST_END": "2026-05-01"})
    assert settings.INGEST_END == date(2026, 5, 1)
    assert config.hot_window_configuration_problems(settings) == [
        _short_hot_window(4, "2026-05-01", 89, "2026-02-01", 90)
    ]


def test_a_hot_window_under_one_month_is_refused_by_name(clean_env):
    def problems(months, agg=90):
        return config.hot_window_configuration_problems(
            Settings(_env_file=None, HOT_WINDOW_MONTHS=months, AGG_MAX_WINDOW_DAYS=agg, **REQUIRED)
        )

    for months in (0, -1, -12):
        assert problems(months) == [
            f"HOT_WINDOW_MONTHS={months} is below 1; the hot window has to hold at least"
            " INGEST_END=2026-06-30's own month"
        ]
    # one month at INGEST_END=2026-06-30 reaches back to 2026-06-01, 29 days
    assert problems(1, 29) == []
    assert problems(1, 30) == [_short_hot_window(1, "2026-06-30", 29, "2026-06-01", 30)]


def test_a_hot_window_reaching_back_before_year_one_is_refused_by_name(clean_env):
    def problems(months):
        return config.hot_window_configuration_problems(
            Settings(_env_file=None, HOT_WINDOW_MONTHS=months, **REQUIRED)
        )

    assert problems(24306) == []
    for months in (24307, 10**6):
        assert problems(months) == [
            f"HOT_WINDOW_MONTHS={months} reaches back before year 1 from INGEST_END=2026-06-30"
        ]


def test_a_huge_hot_window_is_answered_without_walking_every_month(repo_root, tmp_path):
    # a subprocess, so an implementation that steps one month at a time fails on the timeout instead
    # of hanging the suite; cwd is empty so no .env can reach the child
    env = {k: v for k, v in os.environ.items() if k not in Settings.model_fields}
    env.update(REQUIRED)
    env.update(
        {"PYTHONPATH": str(repo_root), "PYTHONDONTWRITEBYTECODE": "1", "HOT_WINDOW_MONTHS": str(10**15)}
    )
    script = (
        "import config\n"
        "print(config.__file__)\n"
        "print(config.hot_window_configuration_problems(config.settings))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    config_file, problems = result.stdout.splitlines()
    assert config_file == str(repo_root / "config.py")
    assert problems == repr(
        [f"HOT_WINDOW_MONTHS={10**15} reaches back before year 1 from INGEST_END=2026-06-30"]
    )


def _e2e_problems(**overrides):
    fields = {**REQUIRED, **overrides}
    return config.e2e_configuration_problems(Settings(_env_file=None, **fields))


def _no_room(end, oversized_start, bound):
    return (
        f"E2E_END={end} minus AGG_MAX_WINDOW_DAYS=90 + 1 days is {oversized_start}, before"
        f" INGEST_START={bound}; the over-the-cap window the suite requests would be refused as out"
        " of range, not as too long"
    )


def _no_room_in_hot_window(end, oversized_start, cutoff):
    return (
        f"E2E_END={end} minus AGG_MAX_WINDOW_DAYS=90 + 1 days is {oversized_start}, before the hot"
        f" window's own cutoff {cutoff}; on the deployed hot-window copy the over-the-cap window would"
        " be refused as out of range"
    )


def _before_cutoff(start, cutoff="2026-03-01", months=4):
    return (
        f"E2E_START={start} is before the hot window's own cutoff {cutoff}"
        f" (HOT_WINDOW_MONTHS={months} back from INGEST_END=2026-06-30); the suite would not run"
        " unchanged against the deployed hot-window copy"
    )


def _outside(start, end, ingest_start="2020-08-01"):
    return (
        f"E2E_START={start}..E2E_END={end} is outside the ingested range"
        f" INGEST_START={ingest_start}..INGEST_END=2026-06-30"
    )


def test_e2e_configuration_problems_is_empty_for_the_defaults(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert config.e2e_configuration_problems(settings) == []


def test_an_e2e_window_must_start_before_it_ends(clean_env):
    # one day apart is the shortest window accepted; the same day and a reversed pair are refused
    assert _e2e_problems(E2E_START="2026-06-29", E2E_END="2026-06-30") == []
    assert _e2e_problems(E2E_START="2026-06-30", E2E_END="2026-06-30") == [
        "E2E_START=2026-06-30 is not before E2E_END=2026-06-30"
    ]
    assert _e2e_problems(E2E_START="2026-06-30", E2E_END="2026-06-29") == [
        "E2E_START=2026-06-30 is not before E2E_END=2026-06-29"
    ]


def test_an_e2e_window_over_the_cap_is_a_configuration_problem(clean_env):
    # 90 days is the cap itself and accepted (the defaults); 91 is one over
    assert _e2e_problems(E2E_START="2026-03-31", E2E_END="2026-06-30") == [
        "E2E_START=2026-03-31..E2E_END=2026-06-30 spans 91 days, over AGG_MAX_WINDOW_DAYS=90"
    ]


def test_an_e2e_window_ending_past_ingest_end_is_a_configuration_problem(clean_env):
    # ending ON INGEST_END is the defaults and accepted; one day past it, with the start moved in step
    # so the span stays at the cap, is refused -- every request would answer outside_ingested_range
    assert _e2e_problems(E2E_START="2026-04-02", E2E_END="2026-07-01") == [
        _outside("2026-04-02", "2026-07-01")
    ]


def test_an_e2e_window_starting_before_ingest_start_is_a_configuration_problem(clean_env):
    # E2E_START on INGEST_START is accepted by this rule; one day before it is refused. Both also
    # leave no room for the over-the-cap window, which starts 91 days before E2E_END
    on_start = {"INGEST_START": "2026-04-01", "E2E_START": "2026-04-01", "E2E_END": "2026-06-30"}
    assert _e2e_problems(**on_start) == [_no_room("2026-06-30", "2026-03-31", "2026-04-01")]
    day_after = {**on_start, "INGEST_START": "2026-04-02"}
    assert _e2e_problems(**day_after) == [
        _outside("2026-04-01", "2026-06-30", ingest_start="2026-04-02"),
        _no_room("2026-06-30", "2026-03-31", "2026-04-02"),
    ]


def test_the_over_the_cap_window_needs_its_first_day_inside_the_ingested_range(clean_env):
    # E2E_END minus 91 days landing ON INGEST_START is accepted; one day short is refused
    assert _e2e_problems(INGEST_START="2026-03-31") == []
    assert _e2e_problems(INGEST_START="2026-04-01", E2E_START="2026-04-02") == [
        _no_room("2026-06-30", "2026-03-31", "2026-04-01")
    ]


def test_an_e2e_window_starting_before_the_hot_window_cutoff_is_a_configuration_problem(clean_env):
    # the deployed hot-window copy starts at 2026-03-01 (H=4 back from INGEST_END=2026-06-30).
    # Starting on it is accepted by this rule, a day before is refused; a window that starts before
    # the cutoff and ends after it is refused by its start
    assert _e2e_problems(E2E_START="2026-03-01", E2E_END="2026-05-30") == [
        _no_room_in_hot_window("2026-05-30", "2026-02-28", "2026-03-01")
    ]
    assert _e2e_problems(E2E_START="2026-02-28", E2E_END="2026-05-29") == [
        _before_cutoff("2026-02-28"),
        _no_room_in_hot_window("2026-05-29", "2026-02-27", "2026-03-01"),
    ]


def test_the_over_the_cap_window_needs_its_first_day_inside_the_hot_window(clean_env):
    # E2E_END minus 91 days landing ON the cutoff is accepted, a day before it refused; and
    # HOT_WINDOW_MONTHS=3, which the API accepts at INGEST_END=2026-06-30, leaves no such room
    assert _e2e_problems(E2E_START="2026-03-02", E2E_END="2026-05-31") == []
    assert _e2e_problems(E2E_START="2026-03-01", E2E_END="2026-05-30") == [
        _no_room_in_hot_window("2026-05-30", "2026-02-28", "2026-03-01")
    ]
    assert _e2e_problems(HOT_WINDOW_MONTHS=3) == [
        _no_room_in_hot_window("2026-06-30", "2026-03-31", "2026-04-01")
    ]


def test_a_hot_window_the_api_refuses_is_an_e2e_problem_and_skips_the_cutoff_rules(clean_env):
    # the service would not start, and a cutoff that cannot be computed must not raise here
    assert _e2e_problems(HOT_WINDOW_MONTHS=0) == [
        "HOT_WINDOW_MONTHS=0 is below 1; the hot window has to hold at least"
        " INGEST_END=2026-06-30's own month"
    ]
    assert _e2e_problems(HOT_WINDOW_MONTHS=24307) == [
        "HOT_WINDOW_MONTHS=24307 reaches back before year 1 from INGEST_END=2026-06-30"
    ]
    assert _e2e_problems(HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=91) == [
        _short_hot_window(3, "2026-06-30", 90, "2026-04-01", 91)
    ]


def test_e2e_symbols_with_no_symbol_at_all_is_a_configuration_problem(clean_env):
    for symbols in (",", ""):
        assert _e2e_problems(E2E_SYMBOLS=symbols) == [
            f"E2E_SYMBOLS={symbols!r} carries an empty symbol"
        ]


def test_e2e_symbols_with_one_empty_entry_among_real_ones_is_a_configuration_problem(clean_env):
    # AAPL and MSFT alone make any() true, so every entry has to be checked for the empty slot
    # between them, and an entry of spaces is as empty as one of nothing
    for symbols in ("AAPL,,MSFT", "AAPL, ,MSFT", "AAPL,MSFT,"):
        assert _e2e_problems(E2E_SYMBOLS=symbols) == [
            f"E2E_SYMBOLS={symbols!r} carries an empty symbol"
        ]
    # spaces around a real symbol are the fixture's to strip, not a problem
    assert _e2e_problems(E2E_SYMBOLS="AAPL, MSFT") == []


def test_e2e_base_url_without_a_scheme_is_a_configuration_problem(clean_env):
    for url in ("127.0.0.1:8000", "", "ftp://127.0.0.1:8000"):
        assert _e2e_problems(E2E_BASE_URL=url) == [
            f"E2E_BASE_URL={url!r} has no http:// or https:// scheme"
        ]
    # a deployed target is https
    assert _e2e_problems(E2E_BASE_URL="https://market-data.example") == []
    assert _e2e_problems(E2E_BASE_URL="http://127.0.0.1:8000") == []


def test_e2e_defaults_construct_inside_the_ingested_and_hot_ranges(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.E2E_START == date(2026, 4, 1)
    assert settings.E2E_END == date(2026, 6, 30)


def test_settings_constructs_even_when_the_e2e_window_is_one_the_function_refuses(clean_env):
    # these rules are deliberately not a model validator: advancing INGEST_END without editing the
    # E2E_* keys must not stop api.main, db.migrate or ingest from starting -- only the e2e suite
    # that reads the function refuses
    settings = Settings(_env_file=None, E2E_START="2019-01-01", E2E_END="2019-03-01", **REQUIRED)
    assert settings.E2E_START == date(2019, 1, 1)
    assert config.e2e_configuration_problems(settings) != []


def test_a_bars_page_default_over_its_max_is_refused(clean_env):
    with pytest.raises(ValidationError, match="BARS_PAGE_DEFAULT"):
        Settings(_env_file=None, BARS_PAGE_DEFAULT=20000, **REQUIRED)


def test_an_agg_page_default_over_its_max_is_refused(clean_env):
    with pytest.raises(ValidationError, match="AGG_PAGE_DEFAULT"):
        Settings(_env_file=None, AGG_PAGE_DEFAULT=5000, **REQUIRED)


def test_a_pool_minimum_over_its_maximum_is_refused(clean_env):
    with pytest.raises(ValidationError, match="DB_POOL_MIN"):
        Settings(_env_file=None, DB_POOL_MIN=11, **REQUIRED)


def test_a_pool_maximum_below_one_is_refused(clean_env):
    # DB_POOL_MIN=0 alongside DB_POOL_MAX=0 so this trips only the floor rule, not DB_POOL_MIN > DB_POOL_MAX too
    with pytest.raises(ValidationError, match="DB_POOL_MAX"):
        Settings(_env_file=None, DB_POOL_MIN=0, DB_POOL_MAX=0, **REQUIRED)


def test_a_window_cap_below_one_day_is_refused_and_the_hot_window_gate_still_fires_at_the_floor(
    clean_env,
):
    # the floor is not about accepting a silly number: hot_window_configuration_problems compares
    # (INGEST_END - cutoff).days, which is never negative, against this key, so at zero or below the
    # only reachable answer is "no problems" and the startup gate goes quiet for every hot window.
    # It lives in that check and not in Settings, so it names the key without stopping the two
    # programs that never read it -- which is what the second half here pins.
    # The third half is what makes it a floor rather than a rename: at 1, the gate still refuses
    for cap in (0, -1, -1000):
        settings = Settings(_env_file=None, AGG_MAX_WINDOW_DAYS=cap, **REQUIRED)
        assert config.hot_window_configuration_problems(settings) == [
            f"AGG_MAX_WINDOW_DAYS={cap} is below the minimum window of 1 day; this check compares"
            " against it and cannot fire below it"
        ]
    required = {key: value for key, value in REQUIRED.items() if key != "INGEST_END"}
    at_the_floor = Settings(
        _env_file=None,
        AGG_MAX_WINDOW_DAYS=1,
        HOT_WINDOW_MONTHS=1,
        INGEST_END="2026-06-01",
        **required,
    )
    assert config.hot_window_configuration_problems(at_the_floor) == [
        _short_hot_window(1, date(2026, 6, 1), 0, date(2026, 6, 1), 1)
    ]


def test_a_window_cap_the_api_refuses_lets_migrate_and_the_ingest_start(repo_root):
    # the other half of moving that floor out of Settings. AGG_MAX_WINDOW_DAYS is read by
    # api/routes.py alone, so a floor in Settings would stop db.migrate and the ingest importing over
    # a key neither reads -- and config builds its settings at import, so only a child process shows
    # it. The cwd stays at the repository root and the value comes through env=, which
    # pydantic-settings ranks above env_file: a child whose cwd moves cannot find an instrumented
    # tree's config under the generated mutation harness
    env = {key: value for key, value in os.environ.items() if key not in Settings.model_fields}
    env.update(REQUIRED)
    env.update({"AGG_MAX_WINDOW_DAYS": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    script = (
        "import config, db.migrate, ingest.__main__, ingest.pipeline\n"
        "print(config.settings.AGG_MAX_WINDOW_DAYS)\n"
        "try:\n"
        "    import api.main\n"
        "except RuntimeError as exc:\n"
        "    print(repr(str(exc)))\n"
        "else:\n"
        "    print('api.main imported')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "0",
        repr(
            "refusing to build the app:\n- AGG_MAX_WINDOW_DAYS=0 is below the minimum window of"
            " 1 day; this check compares against it and cannot fire below it"
        ),
    ]


def test_an_unknown_log_level_is_refused(clean_env):
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(_env_file=None, LOG_LEVEL="NOTALEVEL", **REQUIRED)


def test_require_names_the_measurement_behind_every_measured_key(monkeypatch):
    for key, phrase in MEASURED_PHRASES.items():
        monkeypatch.setattr(config.settings, key, None)
        with pytest.raises(RuntimeError) as excinfo:
            config.require(key)
        assert str(excinfo.value) == EXPECTED_REQUIRE_MESSAGE.format(key=key, phrase=phrase)


def test_the_page_and_pool_keys_are_integers(clean_env, monkeypatch):
    numeric_keys = (
        "BARS_PAGE_DEFAULT",
        "BARS_PAGE_MAX",
        "AGG_PAGE_DEFAULT",
        "AGG_PAGE_MAX",
        "DB_POOL_MIN",
        "DB_POOL_MAX",
    )
    defaults = Settings(_env_file=None, **REQUIRED)
    for key in numeric_keys:
        assert type(getattr(defaults, key)) is int
    assert type(defaults.LOG_LEVEL) is str

    # values supplied as environment strings, the way a real .env supplies them -- proves
    # coercion happens rather than that a literal of the right type was written in the class body
    for key in numeric_keys:
        monkeypatch.setenv(key, str(getattr(defaults, key)))
    from_env = Settings(_env_file=None, **REQUIRED)
    for key in numeric_keys:
        assert type(getattr(from_env, key)) is int
    assert type(from_env.LOG_LEVEL) is str


def test_the_committed_env_example_constructs_with_the_measured_keys_unset(no_env, repo_root):
    # anchored at the repository root, not the cwd, and the four `KEY=` lines asserted present, so
    # the construction below is about empty values and not about a file that lost them
    example = repo_root / ".env.example"
    lines = example.read_text().splitlines()
    for key in MEASURED:
        assert lines.count(f"{key}=") == 1, key
    settings = Settings(_env_file=example)
    assert settings.ALPACA_KEY_ID == "your-alpaca-key-id"
    for key in MEASURED:
        assert getattr(settings, key) is None


def _env_file(tmp_path, **values):
    path = tmp_path / "settings.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return path


def test_an_empty_database_url_in_the_environment_beats_the_dsn_in_the_env_file(
    no_env, monkeypatch, tmp_path
):
    # `DATABASE_URL="$SCRATCH_DSN"` with the variable unset must fail, never read .env's DSN
    env_file = _env_file(tmp_path, **REQUIRED)
    assert Settings(_env_file=env_file).DATABASE_URL == REQUIRED["DATABASE_URL"]
    for empty in ("", "   "):
        monkeypatch.setenv("DATABASE_URL", empty)
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=env_file)
        assert [error["loc"] for error in excinfo.value.errors()] == [("DATABASE_URL",)]


def test_an_empty_value_is_refused_under_its_own_name_wherever_it_comes_from(
    no_env, monkeypatch, tmp_path
):
    # every key but the four measured ones and the two the e2e rules read: in the environment over a
    # file carrying a real value, and as `KEY=` in the file itself
    reported_by_the_e2e_rules = ("E2E_BASE_URL", "E2E_SYMBOLS")
    refusing = [
        key for key in Settings.model_fields
        if key not in MEASURED and key not in reported_by_the_e2e_rules
    ]
    assert len(refusing) == 22
    defaults = Settings(_env_file=None, **REQUIRED)
    real = {key: str(getattr(defaults, key)) for key in refusing}
    for key in refusing:
        for empty in ("", "   "):
            monkeypatch.setenv(key, empty)
            with pytest.raises(ValidationError) as excinfo:
                Settings(_env_file=_env_file(tmp_path, **real))
            assert key in str(excinfo.value), (key, empty)
            monkeypatch.delenv(key)
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=_env_file(tmp_path, **{**real, key: ""}))
        assert key in str(excinfo.value), key


def test_an_empty_e2e_target_never_resolves_to_the_default(no_env, monkeypatch, tmp_path):
    # the default is the local service in front of production, so an empty override has to stay
    # empty and be reported, from the environment over a file and from the file alone
    env_file = _env_file(tmp_path, **REQUIRED, E2E_BASE_URL="http://127.0.0.1:1")
    monkeypatch.setenv("E2E_BASE_URL", "")
    from_env = Settings(_env_file=env_file)
    monkeypatch.delenv("E2E_BASE_URL")
    from_file = Settings(_env_file=_env_file(tmp_path, **REQUIRED, E2E_BASE_URL=""))
    for settings in (from_env, from_file):
        assert settings.E2E_BASE_URL == ""
        assert config.e2e_configuration_problems(settings) == [
            "E2E_BASE_URL='' has no http:// or https:// scheme"
        ]


def test_an_empty_measured_key_is_unset_wherever_it_comes_from(no_env, monkeypatch, tmp_path):
    # unset, so require() names it; a measured value of 0 is a value, not an empty one, whether it
    # arrives as text or as a number
    measured = {
        "BARS_PER_TICKER_DAY": "387.36",
        "DEEP_PAGE_DEPTH": "1000000",
        "HEAP_INDEX_BYTE_RATIO": "3.1",
        "HEAP_INDEX_COVERING_RATIO": "1.9",
    }
    env_file = _env_file(tmp_path, **REQUIRED, **measured)
    assert Settings(_env_file=env_file).DEEP_PAGE_DEPTH == 1000000
    for key in MEASURED:
        monkeypatch.setenv(key, "")
    settings = Settings(_env_file=env_file)
    for key in MEASURED:
        assert getattr(settings, key) is None, key
    for key in MEASURED:
        monkeypatch.setenv(key, "0")
    zero = Settings(_env_file=env_file)
    for key in MEASURED:
        assert getattr(zero, key) == 0, key
        monkeypatch.delenv(key)
    numeric_zero = Settings(_env_file=None, **REQUIRED, **{key: 0 for key in MEASURED})
    for key in MEASURED:
        assert getattr(numeric_zero, key) == 0, key
    # blank as well as empty, and from both spellings a .env line can carry: python-dotenv trims an
    # unquoted value, so only the quoted form reaches Settings with its spaces intact -- a rule
    # written against "" alone reads that one as a measurement and refuses to parse it
    for key in MEASURED:
        monkeypatch.setenv(key, "   ")
    blank_from_env = Settings(_env_file=env_file)
    for key in MEASURED:
        assert getattr(blank_from_env, key) is None, key
        monkeypatch.delenv(key)
    quoted = tmp_path / "quoted.env"
    quoted.write_text(
        "".join(f"{key}={value}\n" for key, value in REQUIRED.items())
        + "".join(f'{key}="   "\n' for key in MEASURED)
    )
    blank_from_file = Settings(_env_file=quoted)
    for key in MEASURED:
        assert getattr(blank_from_file, key) is None, key


def test_a_value_padded_with_whitespace_is_read_without_it():
    # libpq refuses a connection string with a leading space and quotes the whole string, password
    # included, back in the message it raises
    padded = dict(
        REQUIRED,
        DATABASE_URL="  postgresql://appuser:s3cr3t@127.0.0.1:5432/marketdata  ",
        ALPACA_KEY_ID=" key ",
        ALPACA_TRADING_HOST="\thttps://paper-api.alpaca.markets\n",
        ALPACA_FEED=" iex ",
        # the seventh string key, stripped like the six above: unstripped, a padded value fails the
        # closed vocabulary below, which is a refusal for a trailing space in a .env line
        LOG_LEVEL="  INFO  ",
    )
    settings = Settings(_env_file=None, **padded)
    assert settings.DATABASE_URL == "postgresql://appuser:s3cr3t@127.0.0.1:5432/marketdata"
    assert settings.ALPACA_KEY_ID == "key"
    assert settings.ALPACA_TRADING_HOST == "https://paper-api.alpaca.markets"
    assert settings.ALPACA_FEED == "iex"
    assert settings.LOG_LEVEL == "INFO"
    # and the refusal's own words for a value that is nothing but padding, which is what tells a
    # blank value apart from a missing one in the line an operator reads
    with pytest.raises(ValidationError, match="must not be empty or all whitespace"):
        Settings(_env_file=None, **dict(REQUIRED, ALPACA_KEY_ID="   "))

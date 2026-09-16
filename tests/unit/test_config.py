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
        "the loaded partition's heap against the covering index"
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
    # turns the kill into six collection errors that name no test, which is D-222's shape
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
    # the positive control first, because Settings declares no rule over a required value beyond its
    # type -- both model validators read defaulted keys -- so REQUIRED reaches every construction in
    # this file unchecked, and each ValidationError below would still be raised against a
    # configuration no operator could use: empty credentials, a plaintext host, an unusable DSN or an
    # ingest window that runs backwards
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


def test_a_hot_window_too_short_for_the_widest_endpoint_window_is_refused():
    # anchored at REQUIRED's INGEST_END=2026-06-30: 3 months back is 2026-04-01, a 90-day span --
    # one day short of 91. The old unanchored check read this case as "89 days" (Feb+Mar+Apr), a
    # month position INGEST_END never actually sits at
    with pytest.raises(ValidationError, match="90 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=91, **REQUIRED)


def test_the_hot_window_floor_follows_the_window_it_has_to_contain():
    # anchored at INGEST_END=2026-06-30: 4 months back is 2026-03-01, a 121-day span -- the exact
    # floor, pinned at both edges so a floor that were the literal 4 could not also produce this
    Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=121, **REQUIRED)
    with pytest.raises(ValidationError, match="90 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=120, **REQUIRED)
    with pytest.raises(ValidationError, match="121 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=122, **REQUIRED)


def test_a_hot_window_ending_before_the_widest_window_can_start_is_refused():
    # INGEST_END=2026-05-01 at the defaults (H=4, A=90): 4 months back is 2026-02-01, an 89-day
    # span. The old check never read INGEST_END at all, so it accepted this deployment regardless
    with pytest.raises(ValidationError, match="89 days"):
        Settings(_env_file=None, **{**REQUIRED, "INGEST_END": "2026-05-01"})


def test_a_hot_window_anchored_at_a_short_february_is_refused():
    # INGEST_END=2026-02-28 with AGG_MAX_WINDOW_DAYS widened to 120: 4 months back is
    # 2025-11-01, a 119-day span -- one day short
    with pytest.raises(ValidationError, match="119 days"):
        Settings(
            _env_file=None,
            AGG_MAX_WINDOW_DAYS=120,
            **{**REQUIRED, "INGEST_END": "2026-02-28"},
        )


def test_e2e_configuration_problems_is_empty_for_the_defaults(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert config.e2e_configuration_problems(settings) == []


def test_e2e_start_after_end_is_a_configuration_problem(clean_env):
    settings = Settings(_env_file=None, E2E_START="2026-06-30", E2E_END="2026-04-01", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("E2E_START" in p and "after" in p for p in problems), problems


def test_e2e_window_over_the_cap_is_a_configuration_problem(clean_env):
    # 91 days, one over AGG_MAX_WINDOW_DAYS=90
    settings = Settings(_env_file=None, E2E_START="2026-03-31", E2E_END="2026-06-30", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("91 days" in p for p in problems), problems


def test_e2e_window_outside_the_ingested_range_is_a_configuration_problem(clean_env):
    # inside the 90-day cap and inside the hot window's own span, but before INGEST_START=2020-08-01
    settings = Settings(_env_file=None, E2E_START="2019-01-01", E2E_END="2019-03-01", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("ingested" in p for p in problems), problems


def test_e2e_start_before_the_hot_window_cutoff_is_a_configuration_problem(clean_env):
    # inside the ingested range and under the cap, but the deployed hot-window copy starts at
    # 2026-03-01 (H=4 back from INGEST_END=2026-06-30), so this suite would not run against it
    settings = Settings(_env_file=None, E2E_START="2025-01-01", E2E_END="2025-03-31", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("cutoff" in p for p in problems), problems


def test_e2e_symbols_with_no_symbol_at_all_is_a_configuration_problem(clean_env):
    settings = Settings(_env_file=None, E2E_SYMBOLS=",", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("E2E_SYMBOLS" in p for p in problems), problems


def test_e2e_symbols_with_one_empty_entry_among_real_ones_is_a_configuration_problem(clean_env):
    # "any" let this one through: AAPL and MSFT alone make the generator non-empty, so only
    # checking EVERY entry catches the empty slot between them
    settings = Settings(_env_file=None, E2E_SYMBOLS="AAPL,,MSFT", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("E2E_SYMBOLS" in p for p in problems), problems


def test_e2e_base_url_without_a_scheme_is_a_configuration_problem(clean_env):
    settings = Settings(_env_file=None, E2E_BASE_URL="127.0.0.1:8000", **REQUIRED)
    problems = config.e2e_configuration_problems(settings)
    assert any("E2E_BASE_URL" in p for p in problems), problems


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


def test_an_empty_value_in_the_committed_env_example_still_constructs(no_env):
    # the four measured keys ship as `KEY=` in .env.example; an unedited copy has to construct
    settings = Settings(_env_file=".env.example")
    for key in MEASURED:
        assert getattr(settings, key) is None


def test_an_empty_required_key_still_fails_as_missing(no_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    without_dsn = {k: v for k, v in REQUIRED.items() if k != "DATABASE_URL"}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **without_dsn)
    assert excinfo.value.errors()[0]["type"] == "missing"
    assert "DATABASE_URL" in str(excinfo.value)


def test_an_empty_defaulted_key_takes_its_default(clean_env, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "")
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.LOG_LEVEL == "INFO"

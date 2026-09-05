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
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    without_key_id = {k: v for k, v in REQUIRED.items() if k != "ALPACA_KEY_ID"}
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **without_key_id)
    assert "ALPACA_KEY_ID" in str(excinfo.value)


def test_defaults_match_the_documented_values(clean_env):
    settings = Settings(_env_file=None, **REQUIRED)
    assert settings.ALPACA_FEED == "iex"
    assert settings.ALPACA_ADJUSTMENT == "split,spin-off"
    assert settings.ALPACA_LIMIT == 10000
    assert settings.RATE_LIMIT_RPM == 200
    assert settings.HTTP_MAX_ATTEMPTS == 5
    assert settings.AGG_MAX_WINDOW_DAYS == 90
    assert settings.BARS_PAGE_DEFAULT == 1000
    assert settings.BARS_PAGE_MAX == 10000
    assert settings.AGG_PAGE_DEFAULT == 100
    assert settings.AGG_PAGE_MAX == 1000
    assert settings.DB_POOL_MIN == 1
    assert settings.DB_POOL_MAX == 10
    assert settings.LOG_LEVEL == "INFO"


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
    # three consecutive months can be as few as 89 days (Feb+Mar+Apr), which cannot hold a
    # 90-day request, so the hot-window index would miss rows the endpoint is entitled to ask for
    with pytest.raises(ValidationError, match="89 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, **REQUIRED)


def test_the_hot_window_floor_follows_the_window_it_has_to_contain():
    # 120 is the exact minimum four months can guarantee -- pinned at both edges so a floor that
    # were the literal 4 rather than this arithmetic could not also produce the 89-day rejection
    Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=120, **REQUIRED)
    with pytest.raises(ValidationError, match="89 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=3, AGG_MAX_WINDOW_DAYS=120, **REQUIRED)
    with pytest.raises(ValidationError, match="120 days"):
        Settings(_env_file=None, HOT_WINDOW_MONTHS=4, AGG_MAX_WINDOW_DAYS=121, **REQUIRED)


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

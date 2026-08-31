from datetime import UTC, date, datetime

import psycopg
import pytest

from db.session import connect
from tests.market_fixture import PLAIN_TUESDAY, close_ts, ensure_partition, open_ts


def _market_day(conn, day, open_ts_, close_ts_, minutes):
    conn.execute(
        "INSERT INTO market_days (day, open_ts, close_ts, session_minutes) VALUES (%s,%s,%s,%s)",
        (day, open_ts_, close_ts_, minutes),
    )


def test_the_ts_symbol_index_exists_on_the_parent(migrated_dsn):
    with connect(migrated_dsn) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename='bars' AND indexname='bars_ts_symbol_idx'"
        ).fetchone()

    # the universe-wide endpoints sort by (ts, symbol) and the PK is keyed (symbol, ts), so it
    # cannot serve that order -- without this index keyset pagination performs worse than offset
    assert row is not None
    assert "(ts, symbol)" in row[0]


def test_every_child_partition_inherits_the_index(migrated_dsn):
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        conn.commit()
        attached = conn.execute(
            "SELECT count(*) FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE i.indrelid = 'bars_2026_03'::regclass AND c.relname LIKE '%ts_symbol%'"
        ).fetchone()[0]

    # created on the parent precisely so a partition the pipeline creates at runtime cannot end up
    # without one. this is the contrast with the hot-window partial index, which is deliberately
    # created on recent children directly
    assert attached == 1


def test_the_index_is_attached_to_the_parent_index_rather_than_standing_alone(migrated_dsn):
    with connect(migrated_dsn) as conn:
        ensure_partition(conn, PLAIN_TUESDAY)
        conn.commit()
        parent = conn.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'bars_ts_symbol_idx'::regclass"
        ).fetchone()[0]

    assert parent == 1


@pytest.mark.parametrize("column", ["open_ts", "close_ts", "session_minutes"])
def test_a_null_in_a_derived_calendar_column_is_rejected(migrated_dsn, column):
    values = {
        "day": date(2026, 3, 10),
        "open_ts": datetime(2026, 3, 10, 13, 30, tzinfo=UTC),
        "close_ts": datetime(2026, 3, 10, 20, 0, tzinfo=UTC),
        "session_minutes": 390,
    }
    values[column] = None

    with connect(migrated_dsn) as conn:
        # session_minutes is derived from close_ts - open_ts, so a NULL in any of the three makes
        # every coverage denominator NULL -- and a NULL denominator reads as "no data" rather than
        # as the malformed calendar row it is
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                "INSERT INTO market_days (day, open_ts, close_ts, session_minutes)"
                " VALUES (%(day)s, %(open_ts)s, %(close_ts)s, %(session_minutes)s)", values
            )


@pytest.mark.parametrize("minutes", [0, -1, -390])
def test_a_non_positive_session_is_rejected(migrated_dsn, minutes):
    with connect(migrated_dsn) as conn:
        # a zero session divides by zero in every coverage ratio and a negative one runs the
        # percentage backwards. the loader has rejected these since Feature 1; this is the belt
        with pytest.raises(psycopg.errors.CheckViolation):
            _market_day(conn, date(2026, 3, 10),
                        datetime(2026, 3, 10, 13, 30, tzinfo=UTC),
                        datetime(2026, 3, 10, 20, 0, tzinfo=UTC), minutes)


def test_a_well_formed_calendar_row_still_inserts(migrated_dsn):
    with connect(migrated_dsn) as conn:
        _market_day(conn, PLAIN_TUESDAY, open_ts(PLAIN_TUESDAY), close_ts(PLAIN_TUESDAY), 390)
        conn.commit()
        stored = conn.execute("SELECT count(*) FROM market_days").fetchone()[0]

    # read beside the rejection cases above: a constraint that refused everything would satisfy
    # them and break the calendar loader
    assert stored == 1


def test_a_half_day_is_a_valid_session_length(migrated_dsn):
    with connect(migrated_dsn) as conn:
        # 210 minutes is a 09:30-13:00 ET early close and must not trip the positivity check
        _market_day(conn, PLAIN_TUESDAY, open_ts(PLAIN_TUESDAY),
                    datetime(2026, 3, 10, 17, 0, tzinfo=UTC), 210)
        conn.commit()

    with connect(migrated_dsn) as conn:
        assert conn.execute("SELECT session_minutes FROM market_days").fetchone()[0] == 210

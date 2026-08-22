import psycopg

from db.session import connect


def _database(dsn: str) -> str:
    return dsn.rsplit("/", 1)[1]


def test_connect_pins_utc_where_the_server_default_is_not(fresh_dsn):
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        conn.autocommit = True
        conn.execute(f'ALTER DATABASE "{_database(dsn)}" SET TimeZone TO \'America/New_York\'')

    with connect(dsn) as conn:
        assert conn.execute("SHOW TimeZone").fetchone() == ("UTC",)
        # the container this suite runs against already defaults to UTC, so only a server that does not can tell the pin from the default
        assert conn.execute(
            "SELECT setting FROM pg_settings WHERE name = 'TimeZone' AND source = 'session'"
        ).fetchone() == ("UTC",)


def test_a_bare_connection_to_the_same_database_does_not_get_utc(fresh_dsn):
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        conn.autocommit = True
        conn.execute(f'ALTER DATABASE "{_database(dsn)}" SET TimeZone TO \'America/New_York\'')

    # the control for the test above: without connect()'s pin the session lands on the database default, which is what shifts partition bounds four hours
    with psycopg.connect(dsn) as bare:
        assert bare.execute("SHOW TimeZone").fetchone() == ("America/New_York",)

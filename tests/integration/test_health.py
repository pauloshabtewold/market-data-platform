from fastapi.testclient import TestClient

from api.main import create_app
from db.session import connect


def _database(dsn: str) -> str:
    return dsn.rsplit("/", 1)[1]


def test_health_answers_two_hundred_against_a_migrated_database(migrated_dsn):
    with TestClient(create_app(migrated_dsn)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_a_pooled_connection_reports_utc(migrated_dsn):
    with connect(migrated_dsn) as conn:
        conn.autocommit = True
        conn.execute(
            f'ALTER DATABASE "{_database(migrated_dsn)}" SET TimeZone TO \'America/New_York\''
        )

    app = create_app(migrated_dsn)
    with TestClient(app):
        with app.state.pool.connection() as conn:
            assert conn.execute("SHOW TimeZone").fetchone() == ("UTC",)
            assert conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'TimeZone' AND source = 'session'"
            ).fetchone() == ("UTC",)

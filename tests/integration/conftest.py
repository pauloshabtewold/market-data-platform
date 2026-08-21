from uuid import uuid4

import pytest
from testcontainers.community.postgres import PostgresContainer

from db.migrate import apply
from db.session import connect


@pytest.fixture(scope="session")
def container():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres


def _dsn(container, dbname: str) -> str:
    return (
        f"postgresql://{container.username}:{container.password}"
        f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}/{dbname}"
    )


@pytest.fixture
def fresh_dsn(container):
    # the DSN comes from the container rather than from DATABASE_URL, which on a runner points at nothing and locally points at the development database.
    def make() -> str:
        name = f"t_{uuid4().hex[:12]}"
        with connect(_dsn(container, container.dbname)) as conn:
            conn.autocommit = True
            conn.execute(f'CREATE DATABASE "{name}"')
        return _dsn(container, name)

    return make


@pytest.fixture
def migrated_dsn(fresh_dsn) -> str:
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        apply(conn)
    return dsn


@pytest.fixture
def schema_ref_dsn(fresh_dsn, repo_root) -> str:
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        conn.execute((repo_root / "db" / "schema.sql").read_text())
        conn.commit()
    return dsn

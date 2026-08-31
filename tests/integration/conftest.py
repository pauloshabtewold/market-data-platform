import re
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


_PLACEHOLDER = re.compile(r":'([a-z_]+)'")


@pytest.fixture
def query_sql(repo_root):
    # rendered rather than run through psql in the container: the generated mutation harness substitutes a stub container fixture carrying no get_wrapped_container, so a fixture reaching for one errors on every test in a campaign
    def render(name: str) -> str:
        text = (repo_root / "db" / "queries" / name).read_text()
        # a file that declares none is legal -- the spec binds no parameters for several of the ten -- and must render rather than raise
        header = re.findall(r"-- parameters:([^\n]*)", text)
        # "none" is the explicit empty declaration: 4, 7, 8 and 10 are deliberately whole-window and the spec requires that case be named in the file rather than inferred from a missing header, so the word has to parse to no parameters rather than to one called "none"
        declared = {token for token in header[0].split() if token.startswith(":")} if header else set()
        assert declared or not header or header[0].split()[:1] == ["none"], (
            f"{name}: the parameter header names neither a :parameter nor 'none'"
        )
        substituted = {f":{n}" for n in _PLACEHOLDER.findall(text)}
        # a parameter added to the body without being declared in the header would otherwise be bound silently and never checked
        assert substituted == declared, f"{name}: body binds {substituted}, header declares {declared}"
        # every other percent is doubled first: psycopg reads a bare % as its own placeholder, so a "100%" in a comment fails the bind with an error naming neither comments nor that file
        rendered = _PLACEHOLDER.sub(r"%(\1)s", text.replace("%", "%%"))
        assert ":'" not in rendered, f"{name}: a psql placeholder survived the render"
        return rendered

    return render

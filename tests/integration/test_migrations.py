import pytest

from db.migrate import MIGRATIONS_DIR, apply
from db.session import connect

PARENT_TABLES = """
SELECT c.relname, c.relkind::text, c.relpersistence::text, coalesce(pg_get_partkeydef(c.oid), '')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY 1
"""

PARENT_COLUMNS = """
SELECT c.relname, a.attname, a.attnum, format_type(a.atttypid, a.atttypmod),
       a.attnotnull, coalesce(pg_get_expr(d.adbin, d.adrelid), '')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY 1, 3
"""

PARENT_CONSTRAINTS = """
SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY 1, 2
"""

PARENT_INDEXES = """
SELECT c.relname, i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid)
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY 1, 2
"""

PROJECTION = (PARENT_TABLES, PARENT_COLUMNS, PARENT_CONSTRAINTS, PARENT_INDEXES)

EXPECTED_TABLES = {"bars", "ingest_progress", "market_days", "schema_migrations", "symbols"}


def _project(dsn: str) -> list[list[tuple]]:
    with connect(dsn) as conn:
        return [conn.execute(query).fetchall() for query in PROJECTION]


def test_migrations_apply_from_zero(fresh_dsn):
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        applied = apply(conn)
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert applied == len(sorted(MIGRATIONS_DIR.glob("*.sql")))
    assert versions == [path.stem for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def test_reapplying_migrations_is_a_no_op(migrated_dsn):
    with connect(migrated_dsn) as conn:
        assert apply(conn) == 0


def test_schema_sql_matches_the_migrated_state(migrated_dsn, schema_ref_dsn):
    migrated = _project(migrated_dsn)
    reference = _project(schema_ref_dsn)
    # guards the comparison against passing on two empty projections.
    assert {row[0] for row in migrated[0]} == EXPECTED_TABLES
    assert migrated == reference


def test_apply_refuses_to_report_success_when_no_migration_files_shipped(migrated_dsn, monkeypatch, tmp_path):
    monkeypatch.setattr("db.migrate.MIGRATIONS_DIR", tmp_path / "missing")

    with pytest.raises(RuntimeError, match="no migrations found"):
        with connect(migrated_dsn) as conn:
            apply(conn)


def test_the_ledger_records_the_order_the_migrations_ran_in(fresh_dsn):
    dsn = fresh_dsn()
    with connect(dsn) as conn:
        apply(conn)
        # ORDER BY version re-sorts whatever order they ran in, so applied_at is the only column that can see a reordering
        ran = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY applied_at")]

    assert ran == [path.stem for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]

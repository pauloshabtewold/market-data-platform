from pathlib import Path

import psycopg

from config import settings
from db.session import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def apply(conn: psycopg.Connection) -> int:
    conn.execute(LEDGER_DDL)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    conn.commit()

    count = 0
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.stem in applied:
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        # one file per transaction, so a failure halfway through leaves the ledger honest.
        conn.commit()
        count += 1
    return count


def main() -> None:
    with connect(settings.DATABASE_URL) as conn:
        count = apply(conn)
    print(f"applied {count} migrations")


if __name__ == "__main__":
    main()

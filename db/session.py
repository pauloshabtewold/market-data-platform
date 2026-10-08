import psycopg


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn)
    # compose is UTC, the v2 RDS parameter group is not, and a bare partition
    # bound uses the session zone
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn

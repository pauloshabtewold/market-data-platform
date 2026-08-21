import psycopg


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn)
    # the compose service is already UTC but the RDS parameter group at v2 is not, and a bare partition bound resolves against the session zone
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn

-- the destination parent state
-- child partitions are runtime artifacts the pipeline creates, so none is declared here
-- the numbered migrations in db/migrations are the path.
SET TIME ZONE 'UTC';

CREATE TABLE schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bars (
    symbol      text,
    ts          timestamptz,
    open        numeric,
    high        numeric,
    low         numeric,
    close       numeric,
    volume      bigint,
    trade_count int,
    vwap        numeric,
    PRIMARY KEY (symbol, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE symbols (
    symbol       text PRIMARY KEY,
    name         text,
    exchange     text,
    active       boolean,
    first_bar_ts timestamptz
);

CREATE TABLE market_days (
    day             date PRIMARY KEY,
    open_ts         timestamptz,
    close_ts        timestamptz,
    session_minutes int
);

CREATE TABLE ingest_progress (
    symbol         text,
    month          date,
    completed_at   timestamptz,
    row_count      int,
    rejected_count int,
    PRIMARY KEY (symbol, month)
);

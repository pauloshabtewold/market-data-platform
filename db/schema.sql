-- child partitions are runtime artifacts the pipeline creates, so none is declared here
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

-- the universe-wide endpoints sort by (ts, symbol), which the PK cannot serve. on the parent, so
-- every child the pipeline creates later inherits it; contrast the hot-window partial index, which
-- is created on recent children directly and is deliberately not here
CREATE INDEX bars_ts_symbol_idx ON bars (ts, symbol);

CREATE TABLE symbols (
    symbol       text PRIMARY KEY,
    name         text,
    exchange     text,
    active       boolean,
    first_bar_ts timestamptz
);

CREATE TABLE market_days (
    day             date PRIMARY KEY,
    -- session_minutes is derived from close_ts - open_ts, so a NULL in any of the three makes every
    -- coverage denominator NULL and a non-positive session divides by zero or runs negative
    open_ts         timestamptz NOT NULL,
    close_ts        timestamptz NOT NULL,
    session_minutes int NOT NULL,
    CONSTRAINT market_days_session_minutes_positive CHECK (session_minutes > 0)
);

CREATE TABLE ingest_progress (
    symbol         text,
    month          date,
    completed_at   timestamptz,
    row_count      int,
    rejected_count int,
    PRIMARY KEY (symbol, month)
);

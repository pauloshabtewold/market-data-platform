-- bare partition bounds resolve against the session zone at DDL time, so an unpinned run stores every bound four hours off
SET TIME ZONE 'UTC';

-- raised for this session only: the index below builds a B-tree over 41.7M rows and at the 64 MB
-- default that build spills continuously. It moves nothing a query measures
SET maintenance_work_mem = '256MB';

-- the universe-wide endpoints sort by (ts, symbol), which the PK cannot serve: without this index
-- the plan degrades to a Sort, making keyset pagination worse than offset.

-- on the parent, so a partition created later cannot silently lack it
CREATE INDEX bars_ts_symbol_idx ON bars (ts, symbol);

-- the schema-side half of the calendar's validation. session_minutes derives from
-- close_ts - open_ts, so a NULL makes every coverage denominator NULL
ALTER TABLE market_days
    ALTER COLUMN open_ts         SET NOT NULL,
    ALTER COLUMN close_ts        SET NOT NULL,
    ALTER COLUMN session_minutes SET NOT NULL,
    ADD CONSTRAINT market_days_session_minutes_positive CHECK (session_minutes > 0);

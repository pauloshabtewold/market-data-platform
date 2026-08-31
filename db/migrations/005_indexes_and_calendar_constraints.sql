-- bare partition bounds resolve against the session zone at DDL time, so an unpinned run stores every bound four hours off
SET TIME ZONE 'UTC';

-- Raised for this session only, and only because the index below builds a B-tree over 41.7M
-- rows: at the 64 MB default that build spills continuously. It changes build wall-clock and
-- nothing a query ever measures, and it costs nothing on the empty database CI migrates.
SET maintenance_work_mem = '256MB';

-- The universe-wide endpoints sort by (ts, symbol) and the PK, keyed (symbol, ts), cannot serve
-- that order. Without this index the row-comparison plan degrades to bitmap or seq scans under a
-- Sort node -- keyset pagination performing worse than offset, which is the failure the whole
-- pagination design exists to avoid. With it the plan is an ordered Append of per-partition index
-- scans and global (ts, symbol) order is preserved.
--
-- Created on the parent deliberately, unlike the hot-window partial index: this one's predicate is
-- true of every row, so every child wants a copy, and attaching them to a partitioned index is
-- what keeps a partition created later by the pipeline from silently lacking one.
CREATE INDEX bars_ts_symbol_idx ON bars (ts, symbol);

-- The schema-side half of the calendar's validation. The loader has rejected a malformed vendor
-- row since Feature 1; this is the belt behind it, held back until now because it needs a numbered
-- migration and schema.sql moving with it. session_minutes is derived from close_ts - open_ts, so
-- a NULL in any of the three makes every coverage denominator NULL, and a non-positive session
-- makes coverage divide by zero or run negative.
ALTER TABLE market_days
    ALTER COLUMN open_ts         SET NOT NULL,
    ALTER COLUMN close_ts        SET NOT NULL,
    ALTER COLUMN session_minutes SET NOT NULL,
    ADD CONSTRAINT market_days_session_minutes_positive CHECK (session_minutes > 0);

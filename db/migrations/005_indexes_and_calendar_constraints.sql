-- bare partition bounds resolve against the session zone at DDL time, so an unpinned run stores every bound four hours off
SET TIME ZONE 'UTC';

-- Raised for this session only, because the index below builds a B-tree over 41.7M rows and at the
-- 64 MB default that build spills continuously. It moves build wall-clock and nothing a query
-- measures, and costs nothing on the empty database CI migrates.
SET maintenance_work_mem = '256MB';

-- The universe-wide endpoints sort by (ts, symbol); the PK, keyed (symbol, ts), cannot serve that
-- order. Without this index the row-comparison plan degrades to bitmap or seq scans under a Sort --
-- keyset pagination performing worse than offset, the failure the pagination design exists to
-- avoid. With it the plan is an ordered Append of per-partition index scans.
--
-- On the parent, unlike the hot-window partial index: this predicate is true of every row, so every
-- child wants a copy, and a partitioned index is what stops a later partition silently lacking one.
CREATE INDEX bars_ts_symbol_idx ON bars (ts, symbol);

-- The schema-side half of the calendar's validation; the loader has rejected a malformed vendor row
-- since Feature 1 and this is the belt behind it, needing a numbered migration and schema.sql to
-- move with it. session_minutes derives from close_ts - open_ts, so a NULL in any of the three
-- makes every coverage denominator NULL and a non-positive session divides by zero or runs
-- negative.
ALTER TABLE market_days
    ALTER COLUMN open_ts         SET NOT NULL,
    ALTER COLUMN close_ts        SET NOT NULL,
    ALTER COLUMN session_minutes SET NOT NULL,
    ADD CONSTRAINT market_days_session_minutes_positive CHECK (session_minutes > 0);

-- query 10: missing-minute share per symbol per month. class C, 09_coverage.sql's complement.
-- parameters: :start :end

-- no scalar bound on bars.ts, for 09_coverage.sql's reason: it would flip the planner's free
-- choice and corrupt the byte ratio being measured.

-- MISSING minutes, not zero-volume minutes, and that is the whole metric: the feed emits no bar
-- at all for a printless minute, so the zero-volume form returns 0.00% for every symbol.

-- not a duplicate of 9 despite being its complement: 9 is per symbol and gates the pipeline, 10
-- is per MONTH and never gates, and they differ on a symbol's first partial month

-- bounded is NOT MATERIALIZED for 09_coverage.sql's reason. One observation per variant, not a
-- median: 131.1 s serial with a 1.2 GB spill against 42.6 s parallel with no spill
WITH bounded AS NOT MATERIALIZED (
    SELECT day, open_ts, close_ts, session_minutes
    FROM market_days
    WHERE day >= :'start'::date AND day <= :'end'::date
),
ingested AS (
    -- a symbol with no first_bar_ts was never ingested; reporting it as 100% missing would put an
    -- ingest failure on the same line as a liquidity finding
    SELECT symbol FROM symbols WHERE first_bar_ts IS NOT NULL
),
expected AS (
    -- every session minute of every month in the window, per ingested symbol, NOT floored at
    -- first_bar_ts
    SELECT i.symbol,
           date_trunc('month', d.day)::date AS month,
           sum(d.session_minutes)::numeric  AS expected_minutes,
           count(*)                         AS sessions
    FROM ingested i
    CROSS JOIN bounded d
    GROUP BY i.symbol, date_trunc('month', d.day)::date
),
actual AS (
    SELECT b.symbol,
           date_trunc('month', d.day)::date AS month,
           count(*)::numeric                AS bars
    FROM bounded d
    JOIN bars b
      -- the New York trading-date equality is what gives the planner a hash; the half-open pair
      -- alone is neither mergejoinable nor hashable
      ON d.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= d.open_ts
     AND b.ts <  d.close_ts
    GROUP BY b.symbol, date_trunc('month', d.day)::date
)
SELECT
    e.symbol,
    e.month,
    e.sessions,
    e.expected_minutes                                   AS expected,
    coalesce(a.bars, 0)                                  AS actual,
    e.expected_minutes - coalesce(a.bars, 0)             AS missing,
    round(100 * (e.expected_minutes - coalesce(a.bars, 0))
          / nullif(e.expected_minutes, 0), 2)            AS missing_pct
FROM expected e
LEFT JOIN actual a ON a.symbol = e.symbol AND a.month = e.month
ORDER BY e.symbol, e.month;

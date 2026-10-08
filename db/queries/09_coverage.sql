-- query 9: per-symbol coverage, and the pipeline's gate query. one file, never a second copy.
-- parameters: :start :end
-- they bound the market_days day range and nothing else. no scalar bound on bars.ts: Class C,
-- judged on a byte ratio measured on the unbounded form, which a 100%-true range predicate flips
-- by changing the planner's free choice.

-- bounded is NOT MATERIALIZED deliberately: referenced three times it would be materialised by
-- default, and a materialised CTE carries no statistics, so the planner cannot cost a hash join
-- against it, falls back to a merge join and sorts every bar in the database. Inlining restores
-- market_days' statistics and the plan becomes a parallel hash join. Measured on the full
-- universe, medians of three interleaved runs: 179.4 s serial, merge join and a spill, against
-- 58.3 s with 71 parallel scans, 2 workers and no spill. docs/QUERY_PERFORMANCE.md carries the
-- four-variant table and the run spread.
WITH bounded AS NOT MATERIALIZED (
    SELECT m.day, m.open_ts, m.close_ts, m.session_minutes
    FROM market_days m
    WHERE m.day >= :'start' AND m.day <= :'end'
),
ingested AS (
    -- no first_bar_ts means not ingested yet: excluded here, counted on its own line below
    SELECT s.symbol, s.first_bar_ts FROM symbols s WHERE s.first_bar_ts IS NOT NULL
),
sessions AS (
    -- floored on close_ts, not open_ts: open_ts drops a symbol's first partial session from the
    -- denominator while its bars stay in the numerator, and coverage then exceeds 100%
    SELECT i.symbol, d.day, d.open_ts, d.close_ts, d.session_minutes
    FROM ingested i
    JOIN bounded d ON d.close_ts > i.first_bar_ts
),
expected AS (
    SELECT symbol, sum(session_minutes)::numeric AS minutes FROM sessions GROUP BY symbol
),
actual AS (
    -- joined to the 1,484-row calendar, not the 148,400-row session set: a symbol equality key
    -- makes the sides 148k against 41.7M, which the planner serves with a merge join and a
    -- 5,264 kB spill. dropping it costs nothing -- first_bar_ts is MIN(ts), so no bar precedes
    -- its own symbol's floor, and the LEFT JOIN below already discards any symbol not in
    -- expected. the 1.2 GB also filed against this query in docs/QUERY_PERFORMANCE.md belongs to
    -- a variant that drops the key but still materialises bounded, and ships nowhere
    SELECT b.symbol, count(*)::numeric AS bars
    FROM bounded d
    JOIN bars b
      -- the New York trading-date equality is what gives the planner a hash; the half-open pair
      -- alone is neither mergejoinable nor hashable
      ON d.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= d.open_ts
     AND b.ts <  d.close_ts
    GROUP BY b.symbol
),
per_symbol AS (
    SELECT e.symbol,
           e.minutes AS expected,
           coalesce(a.bars, 0) AS actual,
           round(100 * coalesce(a.bars, 0) / nullif(e.minutes, 0), 2) AS coverage_pct
    FROM expected e
    LEFT JOIN actual a ON a.symbol = e.symbol
),
expected_units AS (
    -- every trading month of the window for every ingested symbol, NOT floored at first_bar_ts:
    -- a late-listed symbol still gets a progress row per pre-listing month, an empty vendor
    -- payload being a completed unit, while a FAILED month leaves none. flooring would drop
    -- exactly those failed leading months from the expected set and report zero missing on a run
    -- that lost them
    SELECT i.symbol, m.month
    FROM ingested i
    CROSS JOIN (SELECT DISTINCT date_trunc('month', day)::date AS month FROM bounded) m
),
missing AS (
    SELECT u.symbol, u.month
    FROM expected_units u
    WHERE NOT EXISTS (
        SELECT 1 FROM ingest_progress p WHERE p.symbol = u.symbol AND p.month = u.month
    )
)
SELECT label, value
FROM (
    SELECT 0 AS band, 0 AS ord, p.symbol::text AS label, p.coverage_pct AS value
    FROM per_symbol p
    UNION ALL
    -- counts cast without a scale: they print 0, not 0.00, while the percentage keeps two places
    SELECT 1, 1, 'missing_units', (SELECT count(*) FROM missing)::numeric
    UNION ALL
    SELECT 1, 2, 'coverage_pct',
           (SELECT round(100 * sum(actual) / nullif(sum(expected), 0), 2) FROM per_symbol)
    UNION ALL
    SELECT 1, 3, 'uningested_symbols',
           (SELECT count(*) FROM symbols WHERE first_bar_ts IS NULL)::numeric
) banded
ORDER BY band, ord, label;

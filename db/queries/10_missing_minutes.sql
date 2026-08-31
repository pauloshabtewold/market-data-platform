-- query 10: missing-minute share per symbol per month, over the window.
-- parameters: :start :end
-- class C: it reads every row of the universe. Judged on evidence of optimality, not a speedup.
--
-- NO scalar bound on bars.ts, the same exception 09_coverage.sql takes and for the same reason:
-- this file is judged on a byte-ratio-versus-block-ratio agreement measured on the UNBOUNDED
-- form, and a range predicate true of every row is not free to the planner -- it can flip the
-- free choice between a seq scan and an index-only scan and corrupt the number being measured.
--
-- MISSING minutes, not zero-volume minutes, and the difference is the whole metric. The feed
-- emits no bar at all when a minute has no print, and documents that a bar is only emitted if
-- none of its fields is 0 -- so a stored minute bar with volume = 0 essentially cannot exist and
-- the zero-volume version of this query returns 0.00% for every symbol in every month. That query
-- looks implemented, runs fast, and measures nothing. The liquidity signal on iex lives in the
-- minutes that produced no bar, which is why the metric is absence.
--
-- Arithmetic worth saying out loud, because a reviewer spots it in ten seconds and it is not a
-- defect: over the same slice this is 100 - query 9's coverage. It is the complement, deliberately
-- kept as its own file because the roles differ -- 9 is one number per symbol over the whole
-- window and is the pipeline's gate query; 10 is per symbol per MONTH so seven years of drift is
-- visible, and it never blocks a gate. The two do NOT complement on a symbol's first partial
-- month: 9's denominator is floored at first_bar_ts and this one's is not, deliberately, because
-- a month a symbol had not yet listed for really did have session minutes it produced no bar in.

-- bounded is NOT MATERIALIZED for the same reason 09_coverage.sql's is: it is referenced
-- twice, so Postgres materialises it by default, and a materialised CTE carries no
-- statistics for the planner to cost a hash join against. Without it this plans as a merge
-- join that sorts every bar in the database. One observation per variant, not a median: 131.1 s
-- serial with a 1.2 GB spill, against 42.6 s with 71 parallel scans, 2 workers and no spill. The
-- structural half is a plan property and holds; treat the two timings as indicative, not a ratio.
WITH bounded AS NOT MATERIALIZED (
    SELECT day, open_ts, close_ts, session_minutes
    FROM market_days
    WHERE day >= :'start'::date AND day <= :'end'::date
),
ingested AS (
    -- a symbol with no first_bar_ts has not been ingested at all; reporting it as 100% missing
    -- would put an ingest failure on the same line as a liquidity finding
    SELECT symbol FROM symbols WHERE first_bar_ts IS NOT NULL
),
expected AS (
    -- every session minute of every month in the window, for every ingested symbol, and
    -- deliberately NOT floored at first_bar_ts
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
      -- the equality key over the New York trading date is what gives the planner a hash; the
      -- half-open pair alone is neither mergejoinable nor hashable
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

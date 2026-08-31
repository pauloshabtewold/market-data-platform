-- query 3: overnight gap distribution, prior close to next open, per symbol.
-- parameters: :symbol :start :end
-- class A, target <100 ms. bind :end = INGEST_END and :start = :end - AGG_MAX_WINDOW_DAYS.
--
-- Consumes the daily rollup. 06_daily_rollup.sql is the definition of record for the session
-- and for the open/close pair; the CTE below is that query, inlined because a .sql file
-- cannot import another. Change one and change both.
--
-- "Next" means the next row in market_days, never the next calendar day: a Friday close pairs
-- with a Monday open, and a close before a holiday pairs with the session after it.

WITH session_bars AS (
    SELECT m.day, b.ts, b.open, b.close
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE b.symbol = :'symbol'
      AND m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for partition pruning; the + 1 day keeps the final session
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
),
rollup AS (
    SELECT day,
           (array_agg(open  ORDER BY ts))[1]      AS open,
           (array_agg(close ORDER BY ts DESC))[1] AS close
    FROM session_bars
    GROUP BY day
),
calendar AS (
    -- the trading-day ordinal over market_days, which is what makes "next session" mean the
    -- next row rather than the next date. a symbol that printed nothing on a trading day has
    -- no rollup row for it, so its gap spans more than one session and says so below
    SELECT day, row_number() OVER (ORDER BY day) AS session_no
    FROM market_days
    WHERE day >= :'start'::date AND day <= :'end'::date
),
gaps AS (
    SELECT r.day,
           r.open,
           lag(r.close)     OVER (ORDER BY c.session_no) AS prev_close,
           c.session_no - lag(c.session_no) OVER (ORDER BY c.session_no) AS sessions_spanned
    FROM rollup r
    JOIN calendar c ON c.day = r.day
),
pct AS (
    SELECT 100 * (open - prev_close) / prev_close AS gap_pct, sessions_spanned
    FROM gaps
    WHERE prev_close IS NOT NULL AND prev_close <> 0
)
SELECT
    count(*)                                                          AS gaps,
    count(*) FILTER (WHERE sessions_spanned > 1)                      AS gaps_spanning_a_skipped_session,
    round(avg(gap_pct), 4)                                            AS mean_pct,
    round(stddev_samp(gap_pct), 4)                                    AS stddev_pct,
    round(min(gap_pct), 4)                                            AS min_pct,
    -- percentile_disc rather than percentile_cont, deliberately: cont has no numeric variant and
    -- returns double precision, whose last digits will not match a hand-computed expectation.
    -- disc also returns a gap that was actually observed rather than an interpolation between two
    round(percentile_disc(0.25) WITHIN GROUP (ORDER BY gap_pct), 4)   AS p25_pct,
    round(percentile_disc(0.50) WITHIN GROUP (ORDER BY gap_pct), 4)   AS median_pct,
    round(percentile_disc(0.75) WITHIN GROUP (ORDER BY gap_pct), 4)   AS p75_pct,
    round(max(gap_pct), 4)                                            AS max_pct,
    count(*) FILTER (WHERE gap_pct > 0)                               AS gaps_up,
    count(*) FILTER (WHERE gap_pct < 0)                               AS gaps_down,
    count(*) FILTER (WHERE gap_pct = 0)                               AS gaps_flat
FROM pct;

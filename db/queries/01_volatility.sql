-- query 1: realized volatility by time-of-day bucket, per symbol.
-- parameters: :symbol :start :end
-- class A, target <100 ms. bind :end = INGEST_END and :start = :end - AGG_MAX_WINDOW_DAYS.
--
-- Reads bars directly rather than the rollup: it buckets on ts - open_ts, which a
-- one-row-per-day rollup cannot express. The session definition is 06_daily_rollup.sql's.
--
-- Multi-minute returns are KEPT and NOT SCALED. On iex a minute with no print produces no
-- row, so a return can span several minutes; scaling by 1/sqrt(minutes) would assume returns
-- are iid diffusion increments, and gaps here correlate with thin liquidity, so the
-- correction would systematically inflate quiet periods. avg_minutes_per_return is emitted
-- per bucket instead, which makes the sparsity visible rather than modelling it away.

WITH session_bars AS (
    SELECT m.day, m.open_ts, b.ts, b.close
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
returns AS (
    SELECT
        -- minutes since that day's own open, never a UTC hour: date_trunc('hour', ts) puts the
        -- same 09:30 ET open in hour 14 for the EST months and hour 13 for the EDT months, so
        -- the opening burst splits across two buckets and every bucket is contaminated by its
        -- neighbour for a third of the sample. minutes-since-open is DST-immune and half-day
        -- correct by construction
        ((EXTRACT(epoch FROM ts - open_ts) / 60)::int / 30) * 30 AS bucket_minute,
        close,
        -- one named window, so the two lags cannot drift apart into disagreeing about which row
        -- is previous. the ORDER BY is not optional: without it Postgres returns rows in
        -- arbitrary physical order and every return is garbage that still produces a plausible
        -- stddev. the day in the PARTITION BY is what keeps the opening bar's return from being
        -- computed against the previous session's close -- that is the overnight gap, and
        -- query 3 measures it separately
        lag(close) OVER w AS prev_close,
        EXTRACT(epoch FROM ts - lag(ts) OVER w) / 60 AS span_minutes
    FROM session_bars
    WINDOW w AS (PARTITION BY day ORDER BY ts)
),
pct AS (
    SELECT bucket_minute,
           100 * (close - prev_close) / prev_close AS return_pct,
           span_minutes
    FROM returns
    -- each session's first bar has no prior bar inside its own session, so it carries no return
    WHERE prev_close IS NOT NULL AND prev_close <> 0
)
SELECT
    bucket_minute,
    count(*)                                                   AS returns,
    round(avg(span_minutes), 2)                                AS avg_minutes_per_return,
    round(stddev_samp(return_pct), 6)                          AS stddev_pct,
    -- 252 trading days x 390 session minutes = 98,280 minutes a year. the ::numeric anchor is
    -- load-bearing: bare sqrt(98280) resolves to double precision, which poisons the whole
    -- expression and makes round(..., 4) a hard 42883 error rather than a rounding
    round(stddev_samp(return_pct) * sqrt(98280::numeric), 4)   AS annualized_pct
FROM pct
GROUP BY bucket_minute
ORDER BY bucket_minute;

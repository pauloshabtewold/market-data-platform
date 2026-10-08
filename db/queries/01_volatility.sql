-- query 1: realized volatility by time-of-day bucket, per symbol.
-- parameters: :symbol :start :end
-- class A, target <100 ms. bind :end = INGEST_END and :start = :end - AGG_MAX_WINDOW_DAYS.
--
-- Buckets on ts - open_ts, which a daily rollup cannot express, so it reads bars. Session
-- definition is 06_daily_rollup.sql's.
--
-- Multi-minute returns are KEPT and NOT SCALED: a printless iex minute yields no row, and
-- 1/sqrt(minutes) would assume iid diffusion increments where gaps track thin liquidity,
-- inflating quiet periods. avg_minutes_per_return exposes the sparsity instead.

WITH session_bars AS (
    SELECT m.day, m.open_ts, b.ts, b.close
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE b.symbol = :'symbol'
      AND m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for partition pruning; + 1 day keeps the final session
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
),
returns AS (
    SELECT
        -- minutes since the day's own open, never a UTC hour: date_trunc('hour', ts) bins one
        -- 09:30 ET open into hour 14 under EST, 13 under EDT -- opening burst split, every bucket
        -- contaminated for a third of the sample. minutes-since-open is DST- and half-day-proof
        ((EXTRACT(epoch FROM ts - open_ts) / 60)::int / 30) * 30 AS bucket_minute,
        close,
        -- one named window, so the two lags cannot disagree on which row is previous. without its
        -- ORDER BY rows come in physical order and every return is garbage with a plausible
        -- stddev. PARTITION BY day keeps the opening bar off the prior session's close -- the
        -- overnight gap, which query 3 measures
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
    -- a session's first bar has no prior bar inside it, so it carries no return
    WHERE prev_close IS NOT NULL AND prev_close <> 0
)
SELECT
    bucket_minute,
    count(*)                                                   AS returns,
    round(avg(span_minutes), 2)                                AS avg_minutes_per_return,
    round(stddev_samp(return_pct), 6)                          AS stddev_pct,
    -- 252 trading days x 390 session minutes = 98,280 minutes a year. the ::numeric anchor is
    -- load-bearing: bare sqrt(98280) is double precision and round(..., 4) then errors 42883
    round(stddev_samp(return_pct) * sqrt(98280::numeric), 4)   AS annualized_pct
FROM pct
GROUP BY bucket_minute
ORDER BY bucket_minute;

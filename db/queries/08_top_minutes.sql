-- query 8: the highest-volume minutes market-wide, per year.
-- parameters: none
-- Universe-wide, whole window: the parameter table binds nothing here. Class C -- it must read
-- every row to know which minutes are the largest, so it is judged on evidence of optimality,
-- not a speedup. NO scalar bound on bars.ts: nothing to bind. Session definition is
-- 06_daily_rollup.sql's.
--
-- "Market-wide" sums the minute across every symbol that printed in it, never the single largest
-- bar: a per-symbol maximum would only rank the busiest symbol's opening minutes and say nothing
-- about the market.

WITH session_bars AS (
    SELECT b.ts, b.symbol, b.volume, b.trade_count, m.day
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
),
per_minute AS (
    SELECT
        -- the New York trading year, not the UTC one: a 2021-01-01 00:30Z bar belongs to the
        -- 2020-12-31 session, which market_days resolved above
        EXTRACT(year FROM day)::int AS year,
        ts, day,
        sum(volume)            AS volume,
        sum(trade_count)       AS trades,
        count(*)               AS symbols
    FROM session_bars
    GROUP BY year, ts, day
),
ranked AS (
    SELECT year, ts, day, volume, trades, symbols,
           -- ties broken on ts: without it two minutes of equal volume swap places between runs
           -- and the README table stops reproducing
           row_number() OVER (PARTITION BY year ORDER BY volume DESC, ts) AS rank_in_year
    FROM per_minute
)
SELECT year, rank_in_year, ts, volume, trades, symbols,
       -- the ET wall clock: "which minute" is the whole question, and a UTC stamp hides whether
       -- it was the open, the close or mid-session
       to_char(ts AT TIME ZONE 'America/New_York', 'HH24:MI') AS et_minute
FROM ranked
WHERE rank_in_year <= 10
ORDER BY year, rank_in_year;

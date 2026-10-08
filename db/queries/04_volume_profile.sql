-- query 4: volume profile by hour of the session, across the whole universe.
-- parameters: none
-- Universe-wide, whole window: the parameter table binds nothing here. Class C -- every row is
-- read by definition, so it is judged on evidence of optimality, not a speedup. NO scalar bound
-- on bars.ts: nothing to bind, and a predicate true of every row is not free to the planner.
-- Session definition is 06_daily_rollup.sql's.
--
-- Bucketed on ts - open_ts like query 1: under the mandated TimeZone=UTC, date_trunc('hour', ts)
-- bins one 09:30 ET open into hour 14 in EST months and 13 in EDT, smearing every hour into its
-- neighbour across the third of the sample in the other offset. Minutes-since-open is DST-immune
-- and half-day-correct -- a 13:00 ET close adds no late hours.

WITH session_bars AS (
    SELECT
        (EXTRACT(epoch FROM b.ts - m.open_ts) / 60)::int / 60 AS session_hour,
        b.symbol, b.volume, b.trade_count
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
)
SELECT
    session_hour,
    -- the bucket start in wall-clock ET, since "hour 0" is not what a reader wants and every
    -- session opens at 09:30 ET. not a range: the close truncates the last bucket, by 30 minutes
    -- on a normal day and entirely on a half day, so "15:30-16:30" would be wrong on every row
    to_char(TIME '09:30' + (session_hour || ' hours')::interval, 'HH24:MI') AS et_from,
    count(*)                                        AS bars,
    count(DISTINCT symbol)                          AS symbols,
    sum(volume)                                     AS volume,
    sum(trade_count)                                AS trades,
    round(100.0 * sum(volume) / sum(sum(volume)) OVER (), 4) AS pct_of_volume,
    round(avg(volume), 1)                           AS mean_volume_per_bar
FROM session_bars
GROUP BY session_hour
ORDER BY session_hour;

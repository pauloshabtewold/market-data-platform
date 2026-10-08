-- query 4: volume profile by hour of the session, universe-wide. class C, judged on optimality.
-- parameters: none

-- bucketed on ts - open_ts like query 1: date_trunc('hour', ts) bins a 09:30 ET open into hour 14
-- in EST and 13 in EDT. Minutes-since-open is DST-immune and half-day-correct

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
    -- the bucket start in wall-clock ET, not a range: the close truncates the last bucket, so
    -- "15:30-16:30" would be wrong on every row
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

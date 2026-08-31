-- query 4: volume profile by hour of the session, across the whole universe.
-- parameters: none
-- Universe-wide, whole window: the parameter table binds nothing for this file.
-- class C: an unfiltered full-universe aggregation. Every row is read by definition, so this is
-- judged on evidence of optimality rather than on a speedup.
--
-- NO scalar bound on bars.ts: there is no :start or :end to bind, and a predicate true of every
-- row is not free to the planner.
--
-- Session definition is 06_daily_rollup.sql's.
--
-- Bucketed on ts - open_ts, exactly as query 1 is, and for the same reason: date_trunc('hour', ts)
-- under the mandated TimeZone=UTC puts the same 09:30 ET open in hour 14 for the EST months and
-- hour 13 for the EDT months, so a whole-history profile would smear every hour into its
-- neighbour for the third of the sample that falls in the other offset. Minutes-since-open is
-- DST-immune, and it is half-day-correct too: a 13:00 ET close simply contributes no late hours.

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
    -- the start of the bucket in wall-clock ET, because "hour 0" is not what a reader wants to see
    -- in a profile and the mapping is fixed: every session opens at 09:30 ET. deliberately not a
    -- range: the last bucket is truncated by the close -- 30 minutes on a normal day and none at
    -- all on a half day -- so a "15:30-16:30" label would be wrong on every row it appeared on
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

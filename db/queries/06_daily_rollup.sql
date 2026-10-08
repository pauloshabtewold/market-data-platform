-- query 6: daily OHLCV rollup. 2 and 3 consume it; the rest inherit its session definition.
-- parameters: :symbol :start :end

WITH session_bars AS (
    SELECT m.day, b.ts, b.open, b.high, b.low, b.close, b.volume
    FROM bars b
    JOIN market_days m
      -- the New York trading-date equality is what gives the planner a hash; the half-open pair
      -- alone is neither mergejoinable nor hashable
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     -- half-open, so session_minutes is the exact expected bar count: a 16:00-labelled
     -- closing-auction print sits outside it
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE b.symbol = :'symbol'
      AND m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for pruning; + 1 day keeps the final session, which a
      -- bare < :end drops because :end is a date, midnight under TimeZone=UTC
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
)
SELECT
    :'symbol'                             AS symbol,
    day,
    -- ordered aggregates, not first_value/last_value: the ORDER BY sits inside the aggregate
    -- where it cannot be dropped, so the default-frame trap cannot make the close the open
    (array_agg(open  ORDER BY ts))[1]     AS open,
    max(high)                             AS high,
    min(low)                              AS low,
    (array_agg(close ORDER BY ts DESC))[1] AS close,
    sum(volume)                           AS volume,
    count(*)                              AS bars
FROM session_bars
GROUP BY day
ORDER BY day;

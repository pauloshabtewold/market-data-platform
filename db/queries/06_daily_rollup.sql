-- query 6: daily OHLCV rollup from minute bars. build this one first -- 2 and 3 consume it,
-- and 1, 4, 5, 7, 8, 9 and 10 read bars directly but inherit the session definition below.
-- parameters: :symbol :start :end
-- class A, target <100 ms. bind :end = INGEST_END and :start = :end - AGG_MAX_WINDOW_DAYS.

WITH session_bars AS (
    SELECT m.day, b.ts, b.open, b.high, b.low, b.close, b.volume
    FROM bars b
    JOIN market_days m
      -- the equality key over the New York trading date is what gives the planner a hash;
      -- the half-open pair alone is neither mergejoinable nor hashable
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     -- half-open, so session_minutes is the exact expected bar count: a 16:00-labelled
     -- closing-auction print is deliberately outside the session
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE b.symbol = :'symbol'
      AND m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for pruning: the join predicate on open_ts/close_ts
      -- prunes nothing, and the + 1 day keeps the final session, which a bare < :end would
      -- drop entirely because :end is a date and resolves to midnight under TimeZone=UTC
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
)
SELECT
    :'symbol'                             AS symbol,
    day,
    -- ordered aggregates rather than first_value/last_value: the ORDER BY sits inside the
    -- aggregate where it cannot be dropped, so the default-frame trap that turns a daily
    -- close into the 09:30 bar's close has nowhere to happen
    (array_agg(open  ORDER BY ts))[1]     AS open,
    max(high)                             AS high,
    min(low)                              AS low,
    (array_agg(close ORDER BY ts DESC))[1] AS close,
    sum(volume)                           AS volume,
    count(*)                              AS bars
FROM session_bars
GROUP BY day
ORDER BY day;

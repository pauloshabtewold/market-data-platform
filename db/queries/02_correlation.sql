-- query 2: rolling 30-day correlation between two symbols' daily returns. class B.
-- parameters: :symbol_a :symbol_b :start :end

-- the rollup CTE is 06_daily_rollup.sql inlined and that file is the definition of record; change
-- both. The pivot to one row per day is what makes the frame's 30 rows 30 TRADING days

WITH session_bars AS (
    SELECT b.symbol, m.day, b.ts, b.close
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE b.symbol IN (:'symbol_a', :'symbol_b')
      AND m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for partition pruning; + 1 day keeps the final session
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
),
rollup AS (
    SELECT symbol, day, (array_agg(close ORDER BY ts DESC))[1] AS close
    FROM session_bars
    GROUP BY symbol, day
),
returns AS (
    SELECT symbol, day,
           close / nullif(lag(close) OVER (PARTITION BY symbol ORDER BY day), 0) - 1 AS ret
    FROM rollup
),
pivoted AS (
    -- both returns as columns on one row, so the row-counting frame below counts trading days
    SELECT day,
           max(ret) FILTER (WHERE symbol = :'symbol_a') AS ret_a,
           max(ret) FILTER (WHERE symbol = :'symbol_b') AS ret_b
    FROM returns
    WHERE ret IS NOT NULL
    GROUP BY day
    HAVING count(*) FILTER (WHERE ret IS NOT NULL) = 2
)
SELECT
    day,
    -- the frame is pinned: the default silently computes an EXPANDING correlation, and corr(v,v)
    -- = 1 under both, so no property assertion catches it
    round(corr(ret_a, ret_b) OVER w ::numeric, 4) AS corr_30d,
    count(*) OVER w                               AS rows_in_window
FROM pivoted
WINDOW w AS (ORDER BY day ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
ORDER BY day;

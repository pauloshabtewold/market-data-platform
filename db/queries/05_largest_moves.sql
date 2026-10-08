-- query 5: largest single-minute moves, universe-wide. class C, the RANKED top-N form.
-- parameters: :start :end :min_move_pct :limit

-- ranking by magnitude reads the whole window before it knows which N come out, so no index
-- streams that order. The class A statement is the endpoint form, chronological and keyset.

-- the index a reviewer asks for, (ts, (abs(100*(close-open)/open)) DESC), changes nothing: the
-- market_days join is not in it, so the planner still cannot stream in ranked order.

-- :min_move_pct casts to numeric at the boundary because numeric >= float8 has no operator, and
-- a float bound would silently cast the exact left side to double

WITH session_bars AS (
    SELECT b.symbol, m.day, b.ts, b.open, b.close, b.high, b.low, b.volume
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for partition pruning; + 1 day keeps the final session,
      -- which a bare < :end drops because :end is a date and resolves to midnight
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
),
moves AS (
    SELECT symbol, day, ts, open, close, high, low, volume,
           100 * (close - open) / open          AS move_pct,
           abs(100 * (close - open) / open)     AS abs_move_pct
    FROM session_bars
    -- a zero open cannot be stored but is guarded anyway, since a division by zero aborts the
    -- scan. NaN needs its own clause: Postgres orders NaN above every number, so it passes `<> 0`
    WHERE open <> 0
      AND open <> 'NaN'::numeric
      AND close <> 'NaN'::numeric
)
SELECT symbol, day, ts,
       to_char(ts AT TIME ZONE 'America/New_York', 'HH24:MI') AS et_minute,
       round(open, 4)         AS open,
       round(close, 4)        AS close,
       round(move_pct, 4)     AS move_pct,
       volume
FROM moves
WHERE abs_move_pct >= :'min_move_pct'::numeric
-- ties broken on (symbol, ts) so the top-N is deterministic and the README table reproduces
ORDER BY abs_move_pct DESC, symbol, ts
LIMIT :'limit'::int;

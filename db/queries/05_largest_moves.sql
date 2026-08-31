-- query 5: the largest single-minute moves, universe-wide, with symbol and date.
-- parameters: :start :end :min_move_pct :limit
-- class C, NOT class A. This file is the RANKED top-N form. The endpoint form served at
-- /analytics/largest-moves is a different query -- keyset-paginated and chronological, bounded,
-- riding the (ts, symbol) index -- and it is the one that is class A. Ranking by magnitude has to
-- look at every row in the window before it knows which N come out, so no index streams it in
-- ranked order and no amount of tuning makes this bounded work.
--
-- Section 4 records the negative result already paid for: at the widest window
-- AGG_MAX_WINDOW_DAYS allows, adding the expression index a reviewer asks for --
-- (ts, (abs(100*(close-open)/open)) DESC) -- changes nothing, because the session-bounds join to
-- market_days is not in the index so the planner cannot stream in ranked order.
--
-- Session definition is 06_daily_rollup.sql's.
--
-- :min_move_pct is cast to numeric at the boundary rather than compared against a float. The
-- endpoint validates it as a float, and numeric >= float8 has no operator, so an uncast
-- comparison silently casts the exact left side to double precision. At the load-bearing
-- min_move_pct = 0 that is harmless; at every other threshold it is a float decision made
-- invisibly, and this is where it is made visible instead.

WITH session_bars AS (
    SELECT b.symbol, m.day, b.ts, b.open, b.close, b.high, b.low, b.volume
    FROM bars b
    JOIN market_days m
      ON m.day = (b.ts AT TIME ZONE 'America/New_York')::date
     AND b.ts >= m.open_ts AND b.ts < m.close_ts
    WHERE m.day >= :'start'::date AND m.day <= :'end'::date
      -- redundant by logic and required for partition pruning; the + 1 day keeps the final
      -- session, which a bare < :end would drop because :end is a date and resolves to midnight
      AND b.ts >= :'start'::date
      AND b.ts <  :'end'::date + INTERVAL '1 day'
),
moves AS (
    SELECT symbol, day, ts, open, close, high, low, volume,
           100 * (close - open) / open          AS move_pct,
           abs(100 * (close - open) / open)     AS abs_move_pct
    FROM session_bars
    -- a bar cannot be stored with a zero open (the feed emits no bar when any field is 0), but
    -- the guard is here rather than assumed: a division by zero would abort the whole scan
    WHERE open <> 0
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

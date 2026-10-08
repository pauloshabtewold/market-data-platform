-- query 5: the largest single-minute moves, universe-wide, with symbol and date.
-- parameters: :start :end :min_move_pct :limit
-- class C, NOT class A: the RANKED top-N form. Ranking by magnitude reads every row in the
-- window before it knows which N come out, so no index streams that order and no tuning bounds
-- it. Class A is a different statement, the endpoint form at /analytics/largest-moves:
-- keyset-paginated, chronological, bounded, on the (ts, symbol) index.
--
-- Section 4 records this negative result, already paid for: at the widest AGG_MAX_WINDOW_DAYS
-- window the index a reviewer asks for -- (ts, (abs(100*(close-open)/open)) DESC) -- changes
-- nothing, the market_days session-bounds join not being in it, so the planner still cannot
-- stream in ranked order.
--
-- Session definition is 06_daily_rollup.sql's.
--
-- :min_move_pct casts to numeric at the boundary, and the endpoint validates a Decimal, because
-- numeric >= float8 has no operator: a float bound silently casts the exact left side to double.
-- Harmless at the load-bearing min_move_pct = 0, an invisible float decision at any other
-- threshold -- made visible here.

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
    -- the feed emits no bar when any field is 0, so a zero open cannot be stored; guarded anyway,
    -- since a division by zero aborts the whole scan. NaN needs its own clause and gets nothing
    -- from that one: Postgres orders NaN above every number, so a NaN open passes `<> 0` and a
    -- NaN in either column carries through the division into the published move
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

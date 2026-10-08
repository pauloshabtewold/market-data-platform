-- query 7: VWAP per symbol per day, checked against the feed's own per-bar vwap column.
-- parameters: none
-- Universe-wide, whole window: the parameter table binds nothing here. Class C -- no index
-- improves it and no rewrite avoids the scan, so it is judged on evidence of optimality, not a
-- speedup. Session definition is 06_daily_rollup.sql's.
--
-- NO scalar bound on bars.ts, deliberately: a range predicate true of every row is still
-- evaluated on every row, and pushing one into an Index Cond makes the covering-index path
-- cheaper -- flipping the planner's free choice from Seq Scan to Index Only Scan and corrupting
-- the very ratio HEAP_INDEX_COVERING_RATIO is measured against. Measure the file, not a variant.
--
-- The index it would scan is the covering one, (symbol, ts) INCLUDE (vwap, volume) -- never the
-- PK, which carries neither vwap nor volume and can never serve it index-only. So reading
-- symbol, ts, vwap and volume and NOTHING ELSE is a constraint, not a coincidence, and adding a
-- column is not free: reconstructing a typical price from high, low and close reads well and
-- quietly makes an index-only scan impossible -- measured, the forced scan then reported 0
-- index-only scans and 682,813 root blocks against the seq scan's 514,260, a ratio of 0.75x,
-- with the byte-ratio prediction missing by 60%.
--
-- Everything stays numeric and exact: no corr, sqrt, ln or percentile_cont here to cross into
-- double precision.

WITH stamped AS (
    -- every bar, extended hours included: that delta is what this query exists to explain
    SELECT b.symbol,
           (b.ts AT TIME ZONE 'America/New_York')::date AS day,
           b.ts, b.vwap, b.volume
    FROM bars b
),
joined AS (
    SELECT s.symbol, s.day, s.vwap, s.volume,
           -- half-open, so a 16:00-labelled closing-auction print sits outside the session
           (s.ts >= m.open_ts AND s.ts < m.close_ts) AS in_session
    FROM stamped s
    JOIN market_days m ON m.day = s.day
)
SELECT
    symbol,
    day,
    count(*) FILTER (WHERE in_session)                                  AS session_bars,
    count(*) FILTER (WHERE NOT in_session)                              AS extended_bars,
    -- the correct daily VWAP: the per-bar vwap volume-weighted over the session alone
    round(sum(vwap * volume) FILTER (WHERE in_session)
          / nullif(sum(volume) FILTER (WHERE in_session), 0), 6)        AS vwap_session,
    -- the naive one: the vwap column aggregated without asking which session each bar fell in
    round(sum(vwap * volume) / nullif(sum(volume), 0), 6)               AS vwap_all_hours,
    -- the delta in basis points, which makes the mismatch readable at a glance
    round(10000 * (sum(vwap * volume) / nullif(sum(volume), 0)
                   - sum(vwap * volume) FILTER (WHERE in_session)
                     / nullif(sum(volume) FILTER (WHERE in_session), 0))
          / nullif(sum(vwap * volume) FILTER (WHERE in_session)
                   / nullif(sum(volume) FILTER (WHERE in_session), 0), 0), 4)
                                                                        AS extended_hours_bp
FROM joined
GROUP BY symbol, day
ORDER BY symbol, day;

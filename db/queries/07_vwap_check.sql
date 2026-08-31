-- query 7: VWAP per symbol per day, checked against the feed's own per-bar vwap column.
-- parameters: none
-- Universe-wide, whole window: the parameter table binds nothing for this file.
-- class C: an unfiltered full-universe aggregation. No index improves it and no rewrite avoids
-- the scan, so it is judged on evidence of optimality rather than on a speedup.
--
-- NO scalar bound on bars.ts, deliberately. A range predicate true of every row is still
-- evaluated on every row, and pushing one into an Index Cond instead makes the covering-index
-- path cheaper -- which flips the planner's free choice from Seq Scan to Index Only Scan and
-- corrupts the very ratio HEAP_INDEX_COVERING_RATIO is measured against. Measure the file,
-- not a convenient variant of it.
--
-- The index this query would scan is the covering one, (symbol, ts) INCLUDE (vwap, volume) --
-- never the PK, which carries neither vwap nor volume and so can never serve it index-only.
--
-- Session definition is 06_daily_rollup.sql's. Everything here stays in numeric and is exact:
-- there is no corr, sqrt, ln or percentile_cont in this file to cross into double precision.

WITH stamped AS (
    -- every bar, including extended hours: the delta between the session figure and the
    -- all-hours one is the thing this query exists to explain
    SELECT b.symbol,
           (b.ts AT TIME ZONE 'America/New_York')::date AS day,
           b.ts, b.vwap, b.volume, b.high, b.low, b.close
    FROM bars b
),
joined AS (
    SELECT s.symbol, s.day, s.vwap, s.volume, s.high, s.low, s.close,
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
    -- the correct daily VWAP: the feed's per-bar vwap volume-weighted over the session alone
    round(sum(vwap * volume) FILTER (WHERE in_session)
          / nullif(sum(volume) FILTER (WHERE in_session), 0), 6)        AS vwap_session,
    -- the naive one, which is what you get by aggregating the vwap column without asking which
    -- session each bar fell in
    round(sum(vwap * volume) / nullif(sum(volume), 0), 6)               AS vwap_all_hours,
    -- reconstructed from the OHLC the feed sent alongside, session-bounded. a per-bar vwap that
    -- disagrees with its own bar's typical price is a feed inconsistency rather than a session one
    round(sum((high + low + close) / 3 * volume) FILTER (WHERE in_session)
          / nullif(sum(volume) FILTER (WHERE in_session), 0), 6)        AS vwap_typical,
    -- the two deltas in basis points, which is what makes the mismatch readable at a glance
    round(10000 * (sum(vwap * volume) / nullif(sum(volume), 0)
                   - sum(vwap * volume) FILTER (WHERE in_session)
                     / nullif(sum(volume) FILTER (WHERE in_session), 0))
          / nullif(sum(vwap * volume) FILTER (WHERE in_session)
                   / nullif(sum(volume) FILTER (WHERE in_session), 0), 0), 4)
                                                                        AS extended_hours_bp,
    round(10000 * (sum((high + low + close) / 3 * volume) FILTER (WHERE in_session)
                   / nullif(sum(volume) FILTER (WHERE in_session), 0)
                   - sum(vwap * volume) FILTER (WHERE in_session)
                     / nullif(sum(volume) FILTER (WHERE in_session), 0))
          / nullif(sum(vwap * volume) FILTER (WHERE in_session)
                   / nullif(sum(volume) FILTER (WHERE in_session), 0), 0), 4)
                                                                        AS typical_price_bp
FROM joined
GROUP BY symbol, day
ORDER BY symbol, day;

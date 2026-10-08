-- query 7: VWAP per symbol per day against the feed's own vwap column. class C.
-- parameters: none

-- NO scalar bound on bars.ts: pushing one into an Index Cond flips the planner's free choice to
-- an index-only scan and corrupts the ratio HEAP_INDEX_COVERING_RATIO is measured against.

-- the index is the covering (symbol, ts) INCLUDE (vwap, volume), so reading those four columns and
-- NOTHING ELSE is a constraint: adding one read 682,813 root blocks against the seq scan's 514,260

-- everything stays numeric and exact: nothing here crosses into double precision

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

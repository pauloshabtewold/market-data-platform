SET TIME ZONE 'UTC';

-- non trading days are absent rather than flagged, so there is no is_open column.
CREATE TABLE market_days (
    day             date PRIMARY KEY,
    open_ts         timestamptz,
    close_ts        timestamptz,
    session_minutes int
);

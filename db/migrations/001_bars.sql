-- bare partition bounds resolve against the session zone at DDL time, so an unpinned run stores every bound four hours off
SET TIME ZONE 'UTC';

CREATE TABLE bars (
    symbol      text,
    ts          timestamptz,
    open        numeric,
    high        numeric,
    low         numeric,
    close       numeric,
    volume      bigint,
    trade_count int,
    vwap        numeric,
    PRIMARY KEY (symbol, ts)
) PARTITION BY RANGE (ts);

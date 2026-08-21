SET TIME ZONE 'UTC';

-- month is the first day of the month, which is what makes the unit key a date.
CREATE TABLE ingest_progress (
    symbol         text,
    month          date,
    completed_at   timestamptz,
    row_count      int,
    rejected_count int,
    PRIMARY KEY (symbol, month)
);

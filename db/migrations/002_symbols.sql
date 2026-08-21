SET TIME ZONE 'UTC';

CREATE TABLE symbols (
    symbol       text PRIMARY KEY,
    name         text,
    exchange     text,
    active       boolean,
    first_bar_ts timestamptz
);

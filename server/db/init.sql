-- adsb18 — Database schema
-- PostgreSQL 14+

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Positions — partitioned by month, stores every position update from ADS-B
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL,
    ts              TIMESTAMPTZ     NOT NULL,
    icao            CHAR(6)         NOT NULL,   -- hex ICAO address, e.g. '3C6444'
    feeder_id       INTEGER,                    -- FK → feeders.id
    callsign        VARCHAR(9),                 -- flight number / tail, e.g. 'SU100'
    altitude        INTEGER,                    -- feet (barometric)
    ground_speed    SMALLINT,                   -- knots
    track           SMALLINT,                   -- degrees 0-359
    lat             REAL,                       -- degrees
    lon             REAL,                       -- degrees
    vertical_rate   SMALLINT,                   -- ft/min, positive = climbing
    squawk          VARCHAR(4),                 -- transponder code
    is_on_ground    BOOLEAN         DEFAULT FALSE,
    rssi            REAL,                       -- signal strength dBm (if available)
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_pos_icao_ts ON positions (icao, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pos_ts      ON positions (ts DESC);
CREATE INDEX IF NOT EXISTS idx_pos_latlon  ON positions (lat, lon) WHERE lat IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Aircraft — metadata + live state for aircraft.json generation
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aircraft (
    icao            CHAR(6)         PRIMARY KEY,
    registration    VARCHAR(16),                -- tail number, e.g. 'VP-BFE'
    type_code       VARCHAR(8),                 -- ICAO type, e.g. 'B738'
    description     VARCHAR(64),               -- 'Boeing 737-800'
    country         VARCHAR(32),
    -- live state (updated on every message)
    last_seen       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    first_seen      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_callsign   VARCHAR(9),
    last_lat        REAL,
    last_lon        REAL,
    last_altitude   INTEGER,
    last_speed      SMALLINT,
    last_track      SMALLINT,
    last_vrate      SMALLINT,
    last_squawk     VARCHAR(4),
    is_on_ground    BOOLEAN         DEFAULT FALSE,
    msg_count       BIGINT          DEFAULT 0,
    last_pos_seen   TIMESTAMPTZ                     -- last time lat/lon was updated
);

CREATE INDEX IF NOT EXISTS idx_aircraft_last_seen ON aircraft (last_seen DESC);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Feeders — connected Pi receivers
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feeders (
    id              SERIAL          PRIMARY KEY,
    name            VARCHAR(64)     NOT NULL UNIQUE,  -- e.g. 'perm-pi5'
    lat             REAL,                       -- receiver location
    lon             REAL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_connected  TIMESTAMPTZ,
    msg_count       BIGINT          DEFAULT 0
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Partition management — auto-create monthly partitions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION create_monthly_partition(tbl TEXT, yr INT, mo INT)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    part_name TEXT;
    from_date DATE;
    to_date   DATE;
BEGIN
    part_name := format('%s_%s_%s', tbl, yr, lpad(mo::TEXT, 2, '0'));
    from_date := make_date(yr, mo, 1);
    to_date   := from_date + INTERVAL '1 month';
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = part_name AND n.nspname = 'public'
    ) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            part_name, tbl, from_date, to_date
        );
        RAISE NOTICE 'Created partition: %', part_name;
    END IF;
END;
$$;

-- Create partitions for current month + next 2 months
DO $$
DECLARE
    cur  DATE := date_trunc('month', NOW());
    i    INT;
BEGIN
    FOR i IN 0..2 LOOP
        PERFORM create_monthly_partition(
            'positions',
            EXTRACT(YEAR  FROM cur + (i || ' months')::INTERVAL)::INT,
            EXTRACT(MONTH FROM cur + (i || ' months')::INTERVAL)::INT
        );
    END LOOP;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Retention — drop partitions older than 6 months
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION drop_old_partitions(tbl TEXT, keep_months INT DEFAULT 6)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    cutoff DATE := date_trunc('month', NOW()) - (keep_months || ' months')::INTERVAL;
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT c.relname AS part_name
        FROM pg_inherits i
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE p.relname = tbl
          AND c.relname < format('%s_%s_%s', tbl,
              EXTRACT(YEAR FROM cutoff)::INT,
              lpad(EXTRACT(MONTH FROM cutoff)::INT::TEXT, 2, '0'))
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I', rec.part_name);
        RAISE NOTICE 'Dropped old partition: %', rec.part_name;
    END LOOP;
END;
$$;

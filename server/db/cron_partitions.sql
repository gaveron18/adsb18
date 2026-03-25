-- Run this monthly (e.g. via pg_cron or cron job) to:
--   1. Create partition for next month
--   2. Drop partitions older than 6 months

SELECT create_monthly_partition(
    'positions',
    EXTRACT(YEAR  FROM NOW() + INTERVAL '1 month')::INT,
    EXTRACT(MONTH FROM NOW() + INTERVAL '1 month')::INT
);

SELECT drop_old_partitions('positions', 6);

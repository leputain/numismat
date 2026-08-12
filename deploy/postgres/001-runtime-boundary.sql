-- Applied by the official PostgreSQL image only when it initializes a new data directory.
-- Per-role REVOKE cannot override privileges inherited from PUBLIC, so establish the
-- database-wide baseline before any runtime role is created.
DO $finbot$
DECLARE
    database_name text;
BEGIN
    FOR database_name IN
        SELECT datname
        FROM pg_database
        WHERE datallowconn AND datname <> 'template0'
    LOOP
        IF database_name = current_database() THEN
            EXECUTE format(
                'REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC',
                database_name
            );
        ELSE
            EXECUTE format(
                'REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC',
                database_name
            );
        END IF;
    END LOOP;
END
$finbot$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

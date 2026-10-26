-- Reapply the read-only account setup to an existing database volume.
-- Safe to rerun; this never drops or recreates application tables.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 't2s_readonly'
    ) THEN
        CREATE ROLE t2s_readonly LOGIN PASSWORD 't2s_readonly_password'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
    ELSE
        ALTER ROLE t2s_readonly
            WITH LOGIN
            PASSWORD 't2s_readonly_password'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS
            NOINHERIT;
    END IF;
END
$$;

ALTER ROLE t2s_readonly SET statement_timeout = '5s';
ALTER ROLE t2s_readonly SET default_transaction_read_only = on;

REVOKE ALL ON SCHEMA core FROM t2s_readonly;
GRANT USAGE ON SCHEMA core TO t2s_readonly;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA core FROM t2s_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO t2s_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE t2s_user IN SCHEMA core
    REVOKE ALL ON TABLES FROM t2s_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE t2s_user IN SCHEMA core
    GRANT SELECT ON TABLES TO t2s_readonly;

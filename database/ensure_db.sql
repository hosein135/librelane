-- Bootstrap role + database if missing (run as postgres superuser against "postgres").
-- Safe / idempotent — does not DROP existing data.
--
--   psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -f database/ensure_db.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'theapp') THEN
        CREATE ROLE theapp WITH LOGIN PASSWORD 'theapp' VALID UNTIL 'infinity';
    END IF;
END
$$;

SELECT format(
    'CREATE DATABASE theapp WITH OWNER theapp TEMPLATE template0 ENCODING %L',
    'UTF8'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'theapp')
\gexec

GRANT ALL PRIVILEGES ON DATABASE theapp TO theapp;

\connect theapp

DO $$
BEGIN
    IF (SELECT COALESCE(rolsuper, false) FROM pg_roles WHERE rolname = current_user) THEN
        EXECUTE 'GRANT ALL ON SCHEMA public TO theapp';
    END IF;
END
$$;

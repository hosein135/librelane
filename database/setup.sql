-- PostgreSQL setup for LibreLane web (full wipe + recreate).
-- Default app connection: host 127.0.0.1, port 5432, database theapp, user theapp.
--
-- WARNING: This script DROPS database "theapp" (if it exists) and recreates it.
-- All data in that database is destroyed.
--
-- For day-to-day / run.sh use the non-destructive scripts instead:
--   database/ensure_db.sql     — create role/database if missing
--   database/ensure_schema.sql — create tables without wiping
--
-- Usage (run as superuser; connect to postgres, not theapp):
--   psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -f database/setup.sql

\connect postgres

SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'theapp'
  AND pid <> pg_backend_pid();

DROP DATABASE IF EXISTS theapp;
DROP ROLE IF EXISTS theapp;

CREATE ROLE theapp WITH LOGIN PASSWORD 'theapp' VALID UNTIL 'infinity';
CREATE DATABASE theapp
    WITH OWNER theapp
    TEMPLATE template0
    ENCODING 'UTF8';
GRANT ALL PRIVILEGES ON DATABASE theapp TO theapp;

\connect theapp

DO $$
BEGIN
    IF (SELECT COALESCE(rolsuper, false) FROM pg_roles WHERE rolname = current_user) THEN
        EXECUTE 'GRANT ALL ON SCHEMA public TO theapp';
    END IF;
END
$$;

DROP TABLE IF EXISTS flow_run_files CASCADE;
DROP TABLE IF EXISTS flow_step_results CASCADE;
DROP TABLE IF EXISTS flow_runs CASCADE;
DROP TABLE IF EXISTS users CASCADE;

CREATE TABLE users (
    id bigserial PRIMARY KEY,
    username varchar(100) NOT NULL UNIQUE,
    password varchar(100) NOT NULL,
    email varchar(100) NOT NULL,
    session_nonce text,
    first_name varchar(100) NOT NULL DEFAULT '',
    last_name varchar(100) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX users_email_lower_unique ON users (lower(email));

CREATE TABLE flow_runs (
    id bigserial PRIMARY KEY,
    owner_user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name varchar(256) NOT NULL DEFAULT 'spm',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    status varchar(20) NOT NULL DEFAULT 'pending',
    design_name varchar(128) NOT NULL DEFAULT 'spm',
    pdk varchar(64) NOT NULL DEFAULT 'sky130A',
    pdk_family varchar(64) NOT NULL DEFAULT 'sky130',
    pdk_root varchar(512) NOT NULL DEFAULT '~/.ciel',
    clock_period double precision NOT NULL DEFAULT 10.0,
    work_dir varchar(1024) NOT NULL DEFAULT '',
    temp_folder_name varchar(512) NOT NULL DEFAULT '',
    current_step_index integer NOT NULL DEFAULT -1,
    error_message text NOT NULL DEFAULT '',
    setup_log text NOT NULL DEFAULT '',
    artifacts_stored boolean NOT NULL DEFAULT false
);

CREATE INDEX flow_runs_owner_status_idx ON flow_runs (owner_user_id, status);

CREATE TABLE flow_step_results (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES flow_runs (id) ON DELETE CASCADE,
    "order" integer NOT NULL,
    step_id varchar(128) NOT NULL,
    title varchar(256) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'pending',
    log text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    output jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (run_id, "order")
);

CREATE INDEX flow_step_results_run_idx ON flow_step_results (run_id);

CREATE TABLE flow_run_files (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES flow_runs (id) ON DELETE CASCADE,
    relative_path varchar(1024) NOT NULL,
    content bytea NOT NULL,
    size_bytes bigint NOT NULL DEFAULT 0,
    content_type varchar(128) NOT NULL DEFAULT 'application/octet-stream',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, relative_path)
);

CREATE INDEX flow_run_files_run_idx ON flow_run_files (run_id);

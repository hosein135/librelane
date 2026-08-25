-- Idempotent schema ensure for LibreLane web (safe for run.sh).
-- Does NOT drop the database or wipe data.
--
-- Connect as app user (preferred) or superuser:
--   PGPASSWORD=theapp psql -h 127.0.0.1 -p 5432 -U theapp -d theapp -f database/ensure_schema.sql

CREATE TABLE IF NOT EXISTS users (
    id bigserial PRIMARY KEY,
    username varchar(100) NOT NULL UNIQUE,
    password varchar(100) NOT NULL,
    email varchar(100) NOT NULL,
    session_nonce text,
    first_name varchar(100) NOT NULL DEFAULT '',
    last_name varchar(100) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name varchar(100) NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name varchar(100) NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

UPDATE users SET email = lower(trim(email)) WHERE email IS NOT NULL AND email <> lower(trim(email));
DO $$
BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_unique ON users (lower(email));
EXCEPTION
    WHEN unique_violation THEN
        RAISE NOTICE 'users_email_lower_unique skipped: duplicate emails already exist';
END
$$;

CREATE TABLE IF NOT EXISTS flow_runs (
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
    artifacts_stored boolean NOT NULL DEFAULT false,
    disk_bytes bigint NOT NULL DEFAULT 0,
    db_bytes bigint NOT NULL DEFAULT 0
);

ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS name varchar(256) NOT NULL DEFAULT 'spm';
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS temp_folder_name varchar(512) NOT NULL DEFAULT '';
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS artifacts_stored boolean NOT NULL DEFAULT false;
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS disk_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS db_bytes bigint NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS flow_runs_owner_status_idx
    ON flow_runs (owner_user_id, status);

CREATE TABLE IF NOT EXISTS flow_step_results (
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

ALTER TABLE flow_step_results ADD COLUMN IF NOT EXISTS output jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS flow_step_results_run_idx ON flow_step_results (run_id);

-- Persisted run artifacts (BYTEA). On-disk workdirs live under DATA_DIR/runs/.
CREATE TABLE IF NOT EXISTS flow_run_files (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES flow_runs (id) ON DELETE CASCADE,
    relative_path varchar(1024) NOT NULL,
    content bytea NOT NULL,
    size_bytes bigint NOT NULL DEFAULT 0,
    content_type varchar(128) NOT NULL DEFAULT 'application/octet-stream',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, relative_path)
);

CREATE INDEX IF NOT EXISTS flow_run_files_run_idx ON flow_run_files (run_id);

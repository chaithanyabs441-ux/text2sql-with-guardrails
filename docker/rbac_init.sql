-- Additive authentication and authorization tables. Safe to run on an existing DB.
CREATE TABLE IF NOT EXISTS t2s_auth_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(120) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role VARCHAR(24) NOT NULL CHECK (role IN ('employee', 'analyst', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS t2s_auth_sessions (
    token_hash VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES t2s_auth_users(id),
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    revoked_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS t2s_auth_datasets (
    dataset_id VARCHAR(80) PRIMARY KEY,
    display_name VARCHAR(160) NOT NULL,
    schema_name VARCHAR(80) NOT NULL,
    tables_json TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS t2s_auth_dataset_grants (
    user_id INTEGER NOT NULL REFERENCES t2s_auth_users(id),
    dataset_id VARCHAR(80) NOT NULL REFERENCES t2s_auth_datasets(dataset_id),
    granted_by INTEGER NOT NULL REFERENCES t2s_auth_users(id),
    created_at DOUBLE PRECISION NOT NULL,
    CONSTRAINT uq_t2s_auth_user_dataset UNIQUE (user_id, dataset_id)
);

CREATE TABLE IF NOT EXISTS t2s_auth_audit_events (
    id SERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES t2s_auth_users(id),
    event_type VARCHAR(80) NOT NULL,
    target_user_id INTEGER REFERENCES t2s_auth_users(id),
    dataset_id VARCHAR(80),
    detail_json TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS t2s_auth_login_attempts (
    id SERIAL PRIMARY KEY,
    username VARCHAR(120) NOT NULL,
    remote_addr VARCHAR(80) NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_t2s_auth_login_attempts_created
    ON t2s_auth_login_attempts (created_at);
CREATE INDEX IF NOT EXISTS ix_t2s_auth_audit_actor_type
    ON t2s_auth_audit_events (actor_user_id, event_type, created_at);

INSERT INTO t2s_auth_datasets
    (dataset_id, display_name, schema_name, tables_json, is_active, created_at)
VALUES
    ('core', 'SafeQuery sample database', 'core',
     '["regions","categories","products","customers","employees","orders",'
     '"order_items","payments","returns","support_tickets"]', TRUE,
     EXTRACT(EPOCH FROM CURRENT_TIMESTAMP))
ON CONFLICT (dataset_id) DO NOTHING;

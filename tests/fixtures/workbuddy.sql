CREATE TABLE __workbuddy_drizzle_migrations (
            id SERIAL PRIMARY KEY,
            hash text NOT NULL,
            created_at numeric
        );

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT,
    custom_title TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_activity_at INTEGER,
    deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0,
    source_mode TEXT,
    is_background_automation INTEGER,
    mode TEXT,
    model TEXT,
    expert_id TEXT,
    expert_locale TEXT,
    expert_runtime_identity TEXT,
    expert_marketplace TEXT,
    permission_mode TEXT,
    use_sandbox_cli INTEGER,
    project_id TEXT
, plugin_context_json TEXT, addon_selection TEXT, session_settings TEXT, last_user_prompt_expert_selection TEXT, context_window INTEGER, buddy_snapshot_id TEXT, buddy_binding_json TEXT, thought_level TEXT, transport TEXT NOT NULL DEFAULT 'local', conversation_origin TEXT, visibility TEXT, group_id TEXT, group_title TEXT, agent_dirty INTEGER, agent_dirty_at INTEGER, agent_last_synced INTEGER, verified_at INTEGER, unread INTEGER NOT NULL DEFAULT 0);

CREATE TABLE workspaces (
    path TEXT PRIMARY KEY,
    last_opened_at INTEGER NOT NULL
);

CREATE TABLE automations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'recurring',
    next_run_at INTEGER,
    last_run_at INTEGER,
    cwds TEXT NOT NULL DEFAULT '[]',
    rrule TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT,
    valid_from TEXT,
    valid_until TEXT,
    model_id TEXT,
    model_is_thinking INTEGER NOT NULL DEFAULT 0,
    skills_json TEXT NOT NULL DEFAULT '[]',
    push_to_wechat INTEGER NOT NULL DEFAULT 0,
    push_to_wecom_bot INTEGER NOT NULL DEFAULT 0,
    wecom_bot_source TEXT,
    owner_user_id TEXT,
    owner_status TEXT NOT NULL DEFAULT 'legacy_unassigned',
    owner_source TEXT,
    expert_id TEXT,
    expert_marketplace TEXT,
    connector_ids_json TEXT NOT NULL DEFAULT '[]',
    permission_mode TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted_at INTEGER
, context_window INTEGER, reasoning_effort TEXT);

CREATE TABLE automation_runs (
    thread_id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    read_at INTEGER,
    thread_title TEXT,
    source_cwd TEXT,
    runs_json TEXT,
    result_success INTEGER,
    metadata_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
, failure_code TEXT, reason_code TEXT);

CREATE TABLE automation_runtime_state (
    automation_id TEXT PRIMARY KEY,
    last_run_at INTEGER,
    last_error TEXT,
    running INTEGER NOT NULL DEFAULT 0,
    running_started_at INTEGER,
    running_conversation_id TEXT,
    metadata_json TEXT
);

CREATE TABLE automation_delivery_outbox (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'wechatmp',
    automation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    automation_name TEXT NOT NULL,
    owner_user_id TEXT,
    host_id TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_run_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expire_at INTEGER,
    last_error_code TEXT,
    last_error_message TEXT,
    delivery_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    finished_at INTEGER
);

CREATE TABLE migration_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE session_usage (
    session_id TEXT PRIMARY KEY,
    used INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    credit_json TEXT
);

CREATE TABLE buddy_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    template_version INTEGER NOT NULL,
    snapshot_format_version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_automations_owner
    ON automations(owner_user_id, owner_status, deleted_at);

CREATE UNIQUE INDEX automation_delivery_outbox_dedupe_key_unique
    ON automation_delivery_outbox(dedupe_key);

CREATE INDEX idx_automation_delivery_outbox_status_next_run
    ON automation_delivery_outbox(status, next_run_at);

CREATE INDEX idx_automation_delivery_outbox_channel_status_next_run
    ON automation_delivery_outbox(channel, status, next_run_at);

CREATE INDEX idx_automation_delivery_outbox_run
    ON automation_delivery_outbox(automation_id, run_id);

CREATE INDEX idx_automation_delivery_outbox_owner_status
    ON automation_delivery_outbox(owner_user_id, status);

CREATE INDEX idx_automation_delivery_outbox_supersede_lookup
    ON automation_delivery_outbox(automation_id, channel, owner_user_id, host_id, created_at);

CREATE INDEX idx_automation_runs_automation_id
    ON automation_runs(automation_id);

CREATE INDEX idx_automation_runs_created_at
    ON automation_runs(created_at);

CREATE INDEX idx_sessions_buddy_snapshot_id
    ON sessions(buddy_snapshot_id);

CREATE INDEX idx_sessions_cloud_sort
    ON sessions(user_id, last_activity_at, created_at)
    WHERE transport = 'cloud' AND deleted_at = -1;

CREATE INDEX idx_sessions_cloud_group
    ON sessions(user_id, conversation_origin, group_id)
    WHERE transport = 'cloud' AND deleted_at = -1;

CREATE INDEX idx_sessions_cloud_project
    ON sessions(user_id, project_id, last_activity_at)
    WHERE transport = 'cloud' AND deleted_at = -1;

CREATE INDEX idx_sessions_cloud_verified
    ON sessions(user_id, verified_at)
    WHERE transport = 'cloud';
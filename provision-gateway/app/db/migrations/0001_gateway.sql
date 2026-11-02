-- T1 — gateway schema (owned by gateway_app; fullset only).
-- Frozen by task doc §3.3.2. The registry schema must already exist with its
-- tables (the -api migrates first); the gateway never creates a registry object.

CREATE TABLE gateway.users (
    id               uuid PRIMARY KEY,
    api_user_id      uuid NOT NULL UNIQUE REFERENCES registry.api_users(id),
    role             text NOT NULL CHECK (role IN ('viewer', 'special', 'admin')),
    password_hash    text NOT NULL,
    is_approved      boolean NOT NULL,
    is_active        boolean NOT NULL,
    is_public_shared boolean NOT NULL,
    deletion_state   text NOT NULL DEFAULT 'none'
                     CHECK (deletion_state IN ('none', 'pending', 'final')),
    deleted_at       timestamptz,
    created_at       timestamptz,
    approved_at      timestamptz,
    last_login_at    timestamptz
);

CREATE TABLE gateway.user_logins (
    id     bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES gateway.users(id),
    login  citext NOT NULL UNIQUE,
    kind   text NOT NULL CHECK (kind IN ('username', 'email')),
    UNIQUE (user_id, kind)
);

CREATE TABLE gateway.api_keys (
    id           bigserial PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES gateway.users(id),
    label        text NOT NULL,
    token_hash   text NOT NULL UNIQUE,
    mask         text,
    is_default   boolean NOT NULL,
    created_at   timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    is_revoked   boolean NOT NULL,
    last_used_at timestamptz
);

CREATE UNIQUE INDEX api_keys_one_default
    ON gateway.api_keys (user_id)
    WHERE is_default AND NOT is_revoked;

CREATE TABLE gateway.acl_grants (
    id              bigserial PRIMARY KEY,
    grantee_user_id uuid NOT NULL REFERENCES gateway.users(id),
    shared_user_id  uuid NOT NULL REFERENCES gateway.users(id),
    state           text NOT NULL CHECK (state IN ('active', 'inactive')),
    created_at      timestamptz,
    updated_at      timestamptz,
    UNIQUE (grantee_user_id, shared_user_id)
);

CREATE TABLE gateway.stream_exposures (
    handle_hash    text PRIMARY KEY,
    owner_user_id  uuid NOT NULL REFERENCES gateway.users(id),
    service_id     uuid NOT NULL REFERENCES registry.services(id),
    sub_service    text NOT NULL,
    port           integer NOT NULL,
    canonical_name text NOT NULL,
    created_at     timestamptz,
    revoked_at     timestamptz
);

CREATE UNIQUE INDEX stream_exposures_one_live
    ON gateway.stream_exposures (service_id, sub_service, port)
    WHERE revoked_at IS NULL;

CREATE TABLE gateway.tasks (
    task_id         text PRIMARY KEY,
    collision_token text NOT NULL UNIQUE,
    procedure_id    uuid NOT NULL,
    service_id      uuid,
    executor        text NOT NULL DEFAULT 'gateway',
    kind            text NOT NULL CHECK (kind IN ('generate', 'analyze')),
    operation       text NOT NULL,
    origin          text NOT NULL CHECK (origin IN ('deploy', 'fix', 'standalone')),
    status          text NOT NULL
                    CHECK (status IN ('queued', 'running', 'awaiting_input', 'paused',
                                      'completed', 'failed', 'cancelled', 'unknown')),
    phase           text,
    phase_index     integer,
    phase_total     integer,
    progress        text,
    error           text,
    log_ref         text,
    usage           jsonb,
    request         jsonb,
    result          jsonb,
    target_snapshot jsonb,
    events          jsonb,
    pending_input   jsonb,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    deleted_at      timestamptz
);

CREATE INDEX tasks_by_procedure ON gateway.tasks (procedure_id, created_at DESC);
CREATE INDEX tasks_by_service ON gateway.tasks (service_id, created_at DESC);

CREATE TABLE gateway.pre_deploy_sessions (
    service_id          uuid PRIMARY KEY,
    procedure_id        uuid NOT NULL,
    owner_api_user_id   uuid NOT NULL REFERENCES registry.api_users(id),
    service_name        text NOT NULL,
    service_label       text NOT NULL,
    recipe_path         text NOT NULL,
    state               text NOT NULL
                        CHECK (state IN ('generating', 'paused', 'refused',
                                         'generated', 'deploying')),
    reason              text,
    detail              text,
    options             jsonb,
    options_secret_enc  bytea,
    options_key_version integer,
    generation_task_id  text REFERENCES gateway.tasks(task_id) ON DELETE SET NULL,
    deploy_task_id      text,
    guide               text,
    slot                text,
    use_global_proxy    boolean,
    created_at          timestamptz,
    updated_at          timestamptz
);

CREATE UNIQUE INDEX pre_deploy_active_key
    ON gateway.pre_deploy_sessions (owner_api_user_id, service_name, service_label);

CREATE TABLE gateway.audit_log (
    id                 bigserial PRIMARY KEY,
    actor_user_id      uuid REFERENCES gateway.users(id),
    action             text NOT NULL,
    target_api_user_id uuid,
    target_service_id  uuid,
    target_user        text,
    target_service     text,
    target_label       text,
    detail_json        jsonb,
    status             text NOT NULL,
    error_message      text,
    ip_address         text,
    created_at         timestamptz
);

-- Existing gateway config tables, moved to the gateway schema unchanged.
CREATE TABLE gateway.gateway_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE gateway.system_config (
    id    bigserial PRIMARY KEY,
    key   text NOT NULL UNIQUE,
    value text NOT NULL DEFAULT ''
);

CREATE TABLE gateway.llm_config (
    id                bigserial PRIMARY KEY,
    mode              text NOT NULL DEFAULT 'byok',
    agent_url         text,
    agent_model       text,
    byok_api_key_enc  text,
    byok_base_url     text,
    byok_model        text,
    is_active         boolean NOT NULL DEFAULT false,
    system_prompt     text,
    reasoning_effort  text NOT NULL DEFAULT 'low',
    max_tokens        integer NOT NULL DEFAULT 100000,
    tool_rounds_max   integer NOT NULL DEFAULT 6,
    updated_at        timestamptz DEFAULT now()
);

CREATE TABLE gateway.proxy_configs (
    id              bigserial PRIMARY KEY,
    name            text,
    protocol        text NOT NULL DEFAULT 'http',
    host            text NOT NULL,
    port            integer NOT NULL DEFAULT 8080,
    username_enc    text,
    password_enc    text,
    is_active       boolean NOT NULL DEFAULT false,
    reachable       text,
    last_checked_at timestamptz,
    last_error      text,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

CREATE TABLE gateway.service_templates (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    description text,
    category    text,
    compose_j2  text NOT NULL,
    nginx_j2    text,
    env_template text,
    dockerfile  text,
    icon        text,
    is_builtin  boolean NOT NULL DEFAULT false,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

CREATE TABLE gateway.loop_leases (
    name         text PRIMARY KEY,
    locked_until double precision NOT NULL DEFAULT 0
);

-- One read-only union over both task families (R4.5). Gateway-only columns are
-- NULL for registry tasks. target_display resolves a service (live or
-- tombstone) to "{service_name} ({recipe_path})", renders the bare
-- service_name for the root recipe, and falls back to the task snapshot.
CREATE VIEW gateway.task_view AS
SELECT t.task_id,
       t.procedure_id,
       t.service_id,
       s.service_name,
       s.recipe_path,
       t.operation AS job_type,
       t.executor,
       t.kind,
       t.operation,
       t.origin,
       t.status,
       t.phase,
       t.phase_index,
       t.phase_total,
       t.progress,
       t.error,
       t.log_ref,
       t.usage,
       t.collision_token,
       t.target_snapshot,
       t.request,
       t.result,
       t.pending_input,
       t.events,
       CASE
           WHEN s.id IS NOT NULL AND COALESCE(s.recipe_path, '') NOT IN ('', '.')
               THEN s.service_name || ' (' || s.recipe_path || ')'
           WHEN s.id IS NOT NULL THEN s.service_name
           WHEN t.target_snapshot->>'service_name' IS NOT NULL
               THEN COALESCE(t.target_snapshot->>'display', t.target_snapshot->>'service_name')
           ELSE NULL
       END AS target_display,
       t.deleted_at,
       t.created_at,
       t.updated_at
FROM gateway.tasks t
LEFT JOIN registry.services s ON s.id = t.service_id
UNION ALL
SELECT r.task_id,
       r.procedure_id,
       r.service_id,
       s.service_name,
       s.recipe_path,
       r.operation AS job_type,
       r.executor,
       r.kind,
       r.operation,
       NULL::text AS origin,
       r.status,
       r.phase,
       NULL::integer AS phase_index,
       NULL::integer AS phase_total,
       r.progress,
       r.error,
       r.log_ref,
       NULL::jsonb AS usage,
       NULL::text AS collision_token,
       r.target_snapshot,
       r.request,
       r.result,
       NULL::jsonb AS pending_input,
       NULL::jsonb AS events,
       CASE
           WHEN s.id IS NOT NULL AND COALESCE(s.recipe_path, '') NOT IN ('', '.')
               THEN s.service_name || ' (' || s.recipe_path || ')'
           WHEN s.id IS NOT NULL THEN s.service_name
           WHEN r.target_snapshot->>'service_name' IS NOT NULL
               THEN COALESCE(r.target_snapshot->>'display', r.target_snapshot->>'service_name')
           ELSE NULL
       END AS target_display,
       r.deleted_at,
       r.created_at,
       r.updated_at
FROM registry.tasks r
LEFT JOIN registry.services s ON s.id = r.service_id;

-- The per-request registry read path is gateway_reader; it may read the union
-- view but no gateway auth table.
GRANT USAGE ON SCHEMA gateway TO gateway_reader;
GRANT SELECT ON gateway.task_view TO gateway_reader;

-- Thin dashboard/CLI migration (task doc 2026-11-05-0, GRILLING G87/G88).
--
-- The pre-deploy record becomes the workflow record: the intake operation
-- (`deploy` | `generate`), the replayable deploy request and its encrypted
-- credentials, the effective selection and env name. `gateway.label_reservations`
-- is the locked pre-deploy identity the gateway owns; a row exists only while
-- its tuple is reserved (handoff and session delete both release it).
--
-- Gateway schema only. No registry object is created, altered or written.

ALTER TABLE gateway.pre_deploy_sessions
    ADD COLUMN operation        text  NOT NULL DEFAULT 'deploy',
    ADD COLUMN deploy_request   jsonb,
    ADD COLUMN deploy_secret_enc bytea,
    ADD COLUMN deploy_key_version integer,
    ADD COLUMN selection        jsonb,
    ADD COLUMN env_name         text;

ALTER TABLE gateway.pre_deploy_sessions
    ADD CONSTRAINT pre_deploy_sessions_operation_check
    CHECK (operation IN ('deploy', 'generate'));

-- A failed REGISTER is a terminal, visible failure record (R2/R8); the
-- generation-failure path still removes its session (C7).
ALTER TABLE gateway.pre_deploy_sessions
    DROP CONSTRAINT IF EXISTS pre_deploy_sessions_state_check;
ALTER TABLE gateway.pre_deploy_sessions
    ADD CONSTRAINT pre_deploy_sessions_state_check
    CHECK (state IN ('generating', 'paused', 'refused',
                     'generated', 'deploying', 'failed'));

CREATE TABLE gateway.label_reservations (
    owner_api_user_id uuid NOT NULL REFERENCES registry.api_users(id) ON DELETE CASCADE,
    service_name      text NOT NULL,
    label             text NOT NULL,
    service_id        uuid NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_api_user_id, service_name, label)
);

CREATE INDEX label_reservations_by_service
    ON gateway.label_reservations (service_id);

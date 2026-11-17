-- Alerts Center (task doc 2026-11-17-0, rev 4). Gateway schema only.
--
-- One durable alert record covers both kinds:
--   * event     — an indexed stream-conf hold; `source_key = 'hold:<task_id>'`
--                 dedupes the ingest and the row stays active indefinitely;
--   * condition — a state that holds and clears; `condition_key` is the
--                 identity and a partial unique index permits exactly one
--                 active row per key, so a recurrence after resolution inserts
--                 a NEW row (fresh read state) instead of re-opening the old.
-- Read state (`read_at`/`read_by`) is global to the row and orthogonal to
-- `status`; there is no delete in this round — the row is the audit record.
--
-- `gateway.grant_sync_state` is the durable per-source targeted-sync outcome
-- the grant-sync condition reads; it has no FK to the alert record.

CREATE TABLE IF NOT EXISTS gateway.alerts (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key     text UNIQUE,
    kind           text NOT NULL CHECK (kind IN ('event', 'condition')),
    condition_key  text,
    status         text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'resolved')),
    type           text NOT NULL,
    severity       text NOT NULL,
    title          text NOT NULL,
    message        text NOT NULL,
    task_id        text,
    service_id     uuid,
    user_name      text,
    service_name   text,
    label          text,
    context        jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz,
    resolved_at    timestamptz,
    read_at        timestamptz,
    read_by        uuid
);

CREATE INDEX IF NOT EXISTS alerts_status_read_created
    ON gateway.alerts (status, read_at, created_at DESC);
CREATE INDEX IF NOT EXISTS alerts_type_created
    ON gateway.alerts (type, created_at DESC);
-- One active row per condition key. Event rows carry a NULL condition_key and
-- are excluded by the predicate (a unique index permits many NULLs anyway).
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_active_condition
    ON gateway.alerts (condition_key)
    WHERE status = 'active' AND condition_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS gateway.grant_sync_state (
    source_service_id uuid PRIMARY KEY,
    last_outcome      text NOT NULL,
    reason            text,
    task_id           text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

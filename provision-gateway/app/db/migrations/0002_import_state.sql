-- T6 — cutover stage marker for the gateway-side import. Each step is
-- idempotent on its own; the marker lets a retried cutover resume.
CREATE TABLE gateway.import_state (
    stage        text PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT now(),
    detail       jsonb
);

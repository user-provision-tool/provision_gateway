-- T1 — gateway queue lane column (GRILLING G94).
--
-- The gateway's generation queue has no maintenance kind (generate/analyze are
-- operator), so every gateway row is `operator`; the column exists so both
-- task tables share one shape, and the gateway's read model can expose the
-- -api's two lanes (R7). `last_plan_operator_at` records the gateway planner's
-- own last plan tick; the -api's maintenance timestamp is read from
-- `registry.queue_state` through `gateway_reader`.

ALTER TABLE gateway.tasks ADD COLUMN IF NOT EXISTS lane text NOT NULL
    DEFAULT 'operator';
ALTER TABLE gateway.tasks DROP CONSTRAINT IF EXISTS tasks_lane_check;
ALTER TABLE gateway.tasks ADD CONSTRAINT tasks_lane_check
    CHECK (lane IN ('operator', 'maintenance'));

UPDATE gateway.tasks SET lane = 'operator' WHERE lane <> 'operator';

CREATE INDEX IF NOT EXISTS tasks_lane_queue_order
    ON gateway.tasks (lane, status, plan_seq, created_at)
    WHERE deleted_at IS NULL;

ALTER TABLE gateway.queue_state ADD COLUMN IF NOT EXISTS
    last_plan_operator_at timestamptz;

UPDATE gateway.queue_state
    SET last_plan_operator_at = COALESCE(last_plan_operator_at, last_dispatch_at, now())
    WHERE id = 1;

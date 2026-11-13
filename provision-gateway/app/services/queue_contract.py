"""The bounded task-queue contract — gateway mirror (T0, GRILLING G92).

Same names, states, error codes, per-kind bounds and stats keys as the -api's
``lib/queue_contract.py``. The two serializers are tested against these values,
so a drift shows up as a test failure rather than as a UI difference.
"""

from __future__ import annotations

QUEUED = "queued"
PLANNED = "planned"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

STATES = (QUEUED, PLANNED, RUNNING, COMPLETED, FAILED, CANCELLED, UNKNOWN)
ACTIVE_STATES = (QUEUED, PLANNED, RUNNING)
TERMINAL_STATES = (COMPLETED, FAILED, CANCELLED)

ERR_QUEUE_FULL = "queue_full"
ERR_QUEUE_UNAVAILABLE = "queue_unavailable"
ERR_CANCELLATION_UNCONFIRMED = "cancellation_unconfirmed"

ERROR_CODES = (ERR_QUEUE_FULL, ERR_QUEUE_UNAVAILABLE, ERR_CANCELLATION_UNCONFIRMED)

KINDS = ("generate", "fix_analysis", "deploy", "rebuild", "remove", "update")

RUN_TIMEOUTS = {
    "generate": 1200,
    "fix_analysis": 600,
    "deploy": 1800,
    "rebuild": 1800,
    "remove": 300,
    "update": 120,
}

CANCEL_TIMEOUTS = {
    "generate": 120,
    "fix_analysis": 120,
    "deploy": 300,
    "rebuild": 300,
    "remove": 120,
    "update": 60,
}

DEFAULT_QUEUE_CAP = 20
DEFAULT_WORKERS = 4
DEFAULT_PLAN_SIZE = 4
DEFAULT_SPAN_S = 300
DEFAULT_HEARTBEAT_INTERVAL_S = 5
DEFAULT_HEARTBEAT_STALE_S = 30
DEFAULT_PLANNER_INTERVAL_S = 2

CONFIG_KEYS = (
    "queue_cap", "workers", "plan_size", "span_s",
    "heartbeat_interval_s", "heartbeat_stale_s", "planner_interval_s",
    "run_timeout_generate", "run_timeout_fix_analysis", "run_timeout_deploy",
    "run_timeout_rebuild", "run_timeout_remove", "run_timeout_update",
    "cancel_timeout_generate", "cancel_timeout_fix_analysis", "cancel_timeout_deploy",
    "cancel_timeout_rebuild", "cancel_timeout_remove", "cancel_timeout_update",
)

STATS_TOP_KEYS = (
    "queue", "counts", "workers", "pinned_workers", "cap", "span_s",
    "plan_size", "heartbeat", "planner_interval_s", "tasks", "kinds", "config",
)

STATS_HEARTBEAT_KEYS = ("last_dispatch_at", "age_s", "alive", "stale_after_s")

STATS_TASK_KEYS = (
    "status", "kind", "plan_seq", "position",
    "eta_start_s", "eta_finish_s", "started_at", "finished_at",
)

STATS_KIND_KEYS = ("median_s", "samples")


class GenerationCapacityError(RuntimeError):
    """A generation was refused before it could be accepted (G29/G92).

    The old pool refusal used ``generation_at_capacity``; the bounded queue
    distinguishes ``queue_full`` (20 queued) and ``queue_unavailable`` (stale
    planner heartbeat). The base class is kept so existing handlers that catch
    a capacity refusal keep working; ``code`` carries the actionable reason.
    """

    code = "generation_at_capacity"

    def __init__(self, in_flight: int = 0, capacity: int = 0):
        self.in_flight = in_flight
        self.capacity = capacity
        super().__init__(
            f"generation at capacity: {in_flight} job(s) running (limit {capacity})"
        )


class QueueFullError(GenerationCapacityError):
    code = ERR_QUEUE_FULL

    def __init__(self, queued: int, cap: int):
        self.queued = queued
        self.capacity = cap
        RuntimeError.__init__(
            self,
            f"queue full: {queued} task(s) queued (limit {cap}); "
            "wait for work to start, or cancel a queued task",
        )


class QueueUnavailableError(GenerationCapacityError):
    code = ERR_QUEUE_UNAVAILABLE

    def __init__(self, age_s: float | None, stale_after_s: int):
        self.age_s = age_s
        self.stale_after_s = stale_after_s
        RuntimeError.__init__(
            self,
            "queue unavailable: the planner heartbeat is "
            + ("missing" if age_s is None else f"{age_s:.0f}s old")
            + f" (stale after {stale_after_s}s)",
        )


def config_from_row(row: dict | None) -> dict:
    row = row or {}
    cfg = {
        "queue_cap": DEFAULT_QUEUE_CAP,
        "workers": DEFAULT_WORKERS,
        "plan_size": DEFAULT_PLAN_SIZE,
        "span_s": DEFAULT_SPAN_S,
        "heartbeat_interval_s": DEFAULT_HEARTBEAT_INTERVAL_S,
        "heartbeat_stale_s": DEFAULT_HEARTBEAT_STALE_S,
        "planner_interval_s": DEFAULT_PLANNER_INTERVAL_S,
    }
    for kind in KINDS:
        cfg[f"run_timeout_{kind}"] = RUN_TIMEOUTS[kind]
        cfg[f"cancel_timeout_{kind}"] = CANCEL_TIMEOUTS[kind]
    for key in CONFIG_KEYS:
        value = row.get(key)
        if value is not None:
            try:
                cfg[key] = int(value)
            except (TypeError, ValueError):
                pass
    return cfg

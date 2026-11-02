"""Compatibility aliases for the retired SQLite ``generation_jobs`` model.

T7 moved the gateway's task family into ``gateway.tasks`` (``GatewayTask``);
there is no ``generation_jobs`` table any more. This module keeps the import
path and the historical status names so existing callers/tests resolve to the
ONE model instead of a second, stale mapping.
"""

from __future__ import annotations

from .gateway_task import (  # noqa: F401 — re-exported for existing imports
    STATUS_AWAITING_INPUT as JOB_STATUS_AWAITING_INPUT,
    STATUS_CANCELLED as JOB_STATUS_CANCELLED,
    STATUS_COMPLETED as JOB_STATUS_COMPLETED,
    STATUS_FAILED as JOB_STATUS_FAILED,
    STATUS_PAUSED as JOB_STATUS_PAUSED,
    STATUS_QUEUED as JOB_STATUS_QUEUED,
    STATUS_RUNNING as JOB_STATUS_RUNNING,
    ORIGIN_DEPLOY as JOB_ORIGIN_DEPLOY,
    ORIGIN_FIX as JOB_ORIGIN_FIX,
    ORIGIN_STANDALONE as JOB_ORIGIN_STANDALONE,
    GatewayTask as GenerationJob,
    job_origin,
)

__all__ = [
    "GenerationJob",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_AWAITING_INPUT",
    "JOB_STATUS_PAUSED",
    "JOB_STATUS_COMPLETED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_CANCELLED",
    "JOB_ORIGIN_FIX",
    "JOB_ORIGIN_DEPLOY",
    "JOB_ORIGIN_STANDALONE",
    "job_origin",
]

"""Read-only ``registry.tasks`` reads (R9.2).

The gateway no longer polls the -api over HTTP for task completion: the task
rows are read directly through the ``gateway_reader`` role, which has SELECT on
``registry.tasks`` and nothing else. Submit and cancel remain HTTP calls — the
-`api` owns the queue.
"""

from __future__ import annotations

from typing import Any


def get(task_id: str) -> dict[str, Any] | None:
    from ..database import ReaderSessionLocal
    from sqlalchemy import text

    db = ReaderSessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT task_id, status, kind, operation, error, result, "
                "       plan_seq, started_at, finished_at, created_at, updated_at "
                "FROM registry.tasks WHERE task_id=:id"
            ),
            {"id": task_id},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()


def status(task_id: str) -> str | None:
    row = get(task_id)
    return row["status"] if row else None


def active_for_service(service_id: str) -> dict[str, Any] | None:
    """The latest non-terminal queue row for one instance, if any."""
    from ..database import ReaderSessionLocal
    from sqlalchemy import text

    db = ReaderSessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT task_id, status, kind, operation, plan_seq, started_at, "
                "       created_at FROM registry.tasks "
                "WHERE service_id=:sid AND deleted_at IS NULL "
                "AND status IN ('queued','planned','running') "
                "ORDER BY created_at DESC, task_id DESC LIMIT 1"
            ),
            {"sid": service_id},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()

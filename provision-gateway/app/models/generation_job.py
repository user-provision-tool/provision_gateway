"""GenerationJob ORM model — async LLM generation job persistence (SQLite)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from ..database import Base

# Job lifecycle: queued → running → completed | failed | cancelled
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

# TTL for finished jobs (pruned on list) — 3 days.
JOB_TTL_DAYS = 3


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_name = Column(String(255), nullable=False, index=True)
    recipe_path = Column(String(500), nullable=False, default="")
    job_type = Column(String(50), nullable=False, default="generate_missing")  # 'generate_missing' | single-category
    status = Column(String(20), nullable=False, default=JOB_STATUS_QUEUED, index=True)
    phase = Column(String(50), nullable=False, default="pending")  # compose | nginx | env | pending
    phase_index = Column(Integer, nullable=False, default=0)
    phase_total = Column(Integer, nullable=False, default=1)
    progress = Column(String(2000), nullable=False, default="")  # human-readable progress line
    # Request fields (selection + prompt + metadata), JSON-encoded.
    request = Column(Text, nullable=False, default="{}")
    # Result: {"files": {filename: content}} for completed jobs.
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    # Live token-usage stats {"prompt","completion","reasoning"} (design §2).
    usage = Column(Text, nullable=True)
    # Per-file structured events [{file, status, ts, usage}] for the panel.
    events = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self, include_result: bool = False) -> dict:
        data = {
            "id": self.id,
            "service_name": self.service_name,
            "recipe_path": self.recipe_path,
            "job_type": self.job_type,
            "status": self.status,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "phase_total": self.phase_total,
            "progress": self.progress,
            "error": self.error,
            "usage": json.loads(self.usage) if self.usage else None,
            "events": json.loads(self.events) if self.events else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_result:
            # result is persisted as a JSON string (generation_jobs._set_status
            # json.dumps it) — parse it back so callers get the dict (the
            # deploy panel reads result.files directly).
            try:
                data["result"] = json.loads(self.result) if self.result else None
            except (TypeError, ValueError):
                data["result"] = self.result
        return data

"""GenerationJob ORM model — async LLM generation job persistence (SQLite)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from ..database import Base

# Job lifecycle: queued → running → completed | failed | cancelled
#                            ↘ awaiting_input → running → completed
#
# `awaiting_input` is NOT terminal: the env phase parks here when it needs an
# operator-supplied external credential, and resumes via POST /jobs/{id}/input.
# It is deliberately excluded from startup recovery (which fails interrupted
# jobs) and from the TTL prune — the work already paid for must survive a
# gateway restart, and a forgotten popup must not silently discard the answer.
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_AWAITING_INPUT = "awaiting_input"
# G27: a PAUSE is a TERMINAL status on this leg, carrying the payload the
# next leg starts from. It is deliberately NOT in `prune_finished`'s
# terminal set — a record waiting for the operator is never pruned (G30).
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

# The TTL for finished jobs is CONFIGURED, not a constant here: G30's cleanup
# contract requires one TTL with one default, shared with the -api's
# `TASK_TTL_SECONDS` (a week). See `settings.GATEWAY_JOB_TTL_SECONDS`.

# The three PATHS a generation job can come from. This helper is the ONE place
# the question "who owns this job?" is answered, so the server's own behaviour
# (which jobs open a pre-deploy session) and the payload the dashboard reads
# (which surface may act on a job) cannot disagree.
#
# - `fix`        — the analysis step of `check`. An INSTANCE card owns it: it is
#                  about a failed instance's log, and it never pauses (one
#                  `analyze` phase, no files).
# - `deploy`     — the deploy path: the deploy panel's auto-run generation and
#                  its manual "Generate Missing Files via LLM". It carries a
#                  `user_name`, which is exactly what makes it open a PRE-DEPLOY
#                  SESSION — so a SERVICE CARD owns it, and that card's `check`
#                  is the surface that can continue the deploy.
# - `standalone` — the Source Projects panel ("Generate missing files with AI").
#                  It sends no `user_name`, opens no session and has no card, so
#                  the PROJECT ROW owns it — its row slot is the only surface
#                  such a pause can be answered from.
JOB_ORIGIN_FIX = "fix"
JOB_ORIGIN_DEPLOY = "deploy"
JOB_ORIGIN_STANDALONE = "standalone"


def job_origin(request: dict | str | None, job_type: str | None) -> str:
    """Which path created this job — see the three constants above.

    Derived from what the job itself RECORDS (its `job_type` and the request it
    was created with, both persisted on the row), never inferred from the
    absence of something elsewhere: `user_name` in the request IS the deploy
    path's signature, because it is what opens the pre-deploy session.
    """
    if (job_type or "") == "fix":
        return JOB_ORIGIN_FIX
    if isinstance(request, str):
        try:
            request = json.loads(request or "{}")
        except (TypeError, ValueError):
            request = {}
    req = request if isinstance(request, dict) else {}
    return JOB_ORIGIN_DEPLOY if req.get("user_name") else JOB_ORIGIN_STANDALONE


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
    # Set while status == awaiting_input: what to ask the operator for, and
    # everything needed to finish the env phase WITHOUT re-running the LLM —
    # {"phase", "env_name", "vars": [names], "resume": {"mech", "kvs"}, "warnings"}.
    pending_input = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self, include_result: bool = False) -> dict:
        data = {
            "id": self.id,
            # Namespaced, self-describing id (GRILLING G28): `gt-` for the
            # gateway's family, `api-` for the -api's — so an id can never be
            # mistaken for one of the other store's tasks in a list, a log
            # filename or a URL. The numeric primary key stays (it is the
            # foreign key everywhere); this is what callers DISPLAY and
            # accept.
            "display_id": f"gt-{self.id}",
            "service_name": self.service_name,
            "recipe_path": self.recipe_path,
            "job_type": self.job_type,
            # WHICH PATH created this job (see `job_origin`) — the ownership
            # marker its surfaces read: a `deploy` job's pause belongs to the
            # service card (whose `check` can continue the deploy), a
            # `standalone` one to the Source Projects row, a `fix` analysis to
            # the instance card. Without it a surface could only guess, and the
            # row did: it acted on whichever job was newest for its recipe, so a
            # deploy's pause appeared on the row too — where answering it ran
            # the generation and silently skipped the deploy.
            "origin": job_origin(self.request, self.job_type),
            "status": self.status,
            "phase": self.phase,
            "phase_index": self.phase_index,
            "phase_total": self.phase_total,
            "progress": self.progress,
            "error": self.error,
            "usage": json.loads(self.usage) if self.usage else None,
            "events": json.loads(self.events) if self.events else None,
            # Present only while parked in `awaiting_input` — the panel uses it
            # to render the credential popup. Contains no credential VALUES
            # (the operator has not supplied them yet).
            "pending_input": json.loads(self.pending_input) if self.pending_input else None,
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

"""Pre-deploy session — the gateway's own record of work that is not an instance yet.

G10 restated, not bent: **registered instances live in the -api's registry;
pre-deploy sessions live here.** A session is a generation/deploy that has not
reached the -api, so writing it into the -api's registry would make it look
registered — reconciliation and the status scans would pick it up as an instance
that does not exist.

**Why this is a table and not a JSON file (GRILLING G31).** It used to be
`gateway_data/pre-deploy-registry.json`, guarded by an in-process
`threading.RLock`. The gateway runs `uvicorn --workers 4`, so four processes did
read-modify-write on that one file with a lock that does not span processes: two
workers could read the same state, both decide a session was stale, and the
second write silently discarded the first (found as **D2**). As a table the same
operation is a transaction, and the concurrency is the database's problem.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base


class PreDeploySession(Base):
    __tablename__ = "pre_deploy_sessions"

    # `user|service|recipe_path|label` — the combination the session reserves.
    key = Column(String(700), primary_key=True)
    user_name = Column(String(255), nullable=False, index=True)
    service_name = Column(String(255), nullable=False)
    recipe_path = Column(String(500), nullable=False, default="")
    label = Column(String(50), nullable=False, default="0")
    # `generating` | `paused` | `refused`. Under G27 a `paused` session is the
    # TERMINAL record of a finished leg, not a parked one — the answer starts a
    # new job.
    state = Column(String(20), nullable=False, default="generating", index=True)
    # Why it is in that state: `at_capacity` | `compose_fix` | `input` |
    # `llm_fix` | `generation_at_capacity` (G29).
    reason = Column(String(50), nullable=False, default="")
    # The operator-facing explanation (the -api's own wording on a refusal).
    detail = Column(Text, nullable=False, default="")
    # The gateway job this session belongs to — the CURRENT one, which under G27
    # is re-pointed when a new leg starts.
    job_id = Column(Integer, nullable=True, index=True)
    # The LLM fixing guide (reason == "llm_fix"), which `check` pre-fills.
    guide = Column(Text, nullable=False, default="")
    # The FAILED deployment's task id — what the analysis read, and what makes
    # the log findable after the -api entry is gone (G26).
    task_id = Column(String(64), nullable=False, default="")
    # The slot the analysis named as faulty ("compose" | "nginx" | "env").
    slot = Column(String(20), nullable=False, default="")
    # Whether this session's deploy carried the global proxy. `check` replays the
    # path's own options (G20/G26 row 5), and the registry entry that recorded
    # them is gone by the time the restart runs — so the session is where it
    # lives.
    use_global_proxy = Column(Boolean, nullable=False, default=False)
    # The EXACT deploy payload a refusal was given, JSON-encoded, so Retry can
    # replay it verbatim (G20).
    options = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        """The shape callers have always seen (the old JSON row)."""
        out: dict = {
            "key": self.key,
            "user_name": self.user_name,
            "service_name": self.service_name,
            "recipe_path": self.recipe_path or "",
            "label": self.label,
            "state": self.state,
            "reason": self.reason or "",
            "job_id": self.job_id,
            "guide": self.guide or "",
            "task_id": self.task_id or "",
            "slot": self.slot or "",
            "use_global_proxy": bool(self.use_global_proxy),
            "updated_at": self.updated_at.timestamp() if self.updated_at else None,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.options:
            try:
                out["options"] = json.loads(self.options)
            except (TypeError, ValueError):
                pass
        return out

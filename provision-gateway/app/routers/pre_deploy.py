"""Pre-deploy session endpoints (doc §8b).

Closing the deploy panel must keep the session alive and give it a way back —
explicitly NOT `close = cancel`. The job already survives (it is a server-side
gateway task); these endpoints are what make it reachable again, service-scoped,
instead of only on the Tasks page.

Sessions describe work that is NOT an instance yet, which is why they live in the
gateway's own store rather than the -api's registry (G10 restated, not bent).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..middleware import require_admin
from ..services import pre_deploy_registry as pdr

router = APIRouter(prefix="/api/pre-deploy", tags=["pre-deploy"])


@router.get("")
async def list_pre_deploy(current_admin: dict = Depends(require_admin)):
    """Every live pre-deploy session, plus the fix analyses in flight.

    `analyses` is what makes a `failed` instance render as **`failed +
    analyzing`** (GRILLING G26 row 2). While an analysis runs the -api entry is
    deliberately untouched and NO session exists yet — the session is written
    only when the analysis succeeds, as the first half of add-then-delete — so
    neither store can express that state alone. It is computed gateway-side,
    from the gateway's own job, and costs no `-api → gateway` interaction.
    """
    from ..database import SessionLocal
    from ..services.generation_jobs import generation_jobs

    sessions = pdr.list_sessions()
    db = SessionLocal()
    try:
        analyses = generation_jobs.list_fix_analyses(db)
        # `job_live` is what tells the card whether `cancel` belongs on it. The
        # job's STATUS is authoritative and only the gateway knows it — a
        # session's `job_id` alone cannot distinguish "parked, cancellable" from
        # "finished hours ago", and the card must not guess (G24 rule 4: cancel
        # follows the job, and disappears when the job stops).
        for row in sessions:
            jid = row.get("job_id")
            if jid is None:
                row["job_live"] = False
                continue
            try:
                job = generation_jobs.get_job(db, int(jid))
                st = (job.status if job is not None else None)
            except Exception:
                st = None
            row["job_live"] = st in ("queued", "running", "awaiting_input")
    except Exception:
        analyses = []
        for row in sessions:
            row.setdefault("job_live", False)
    finally:
        db.close()
    return {"sessions": sessions, "analyses": analyses, "count": len(sessions)}


@router.get("/{user}/{service}/{label}")
async def get_pre_deploy(
    user: str, service: str, label: str, recipe_path: str = "",
    current_admin: dict = Depends(require_admin),
):
    row = pdr.get_session(user, service, recipe_path, label)
    if row is None:
        raise HTTPException(404, "no pre-deploy session for that combination")
    return row


# A `POST /{user}/{service}/{label}/retry` endpoint lived here until 2026-09-26: it
# replayed a refused deployment's stored options straight to the -api, with no
# generation. It was DELETED because nothing called it — the dashboard's `Retry`
# opens the Deploy panel with `initialOptions`, so the operator confirms what is
# replayed (G20's revised ruling 4) — nothing tested it, and no API reference
# listed it. Two mechanisms for one action is how a reader ends up implementing
# against the dead one.


@router.delete("/{user}/{service}/{label}")
async def delete_pre_deploy(
    user: str, service: str, label: str, recipe_path: str = "",
    current_admin: dict = Depends(require_admin),
):
    """Delete the record — **implicitly cancelling a live job first**.

    User ruling (2026-09-23): *"the delete implicitly forcing a cancel first"*.
    The record used to refuse with a 409 while its job was live, telling the
    operator to cancel first — which turned `delete` into a dead end whenever no
    cancel was on screen, and made the button's meaning depend on a second
    button. Deleting IS the operator saying "stop this and forget it", so the
    two steps are one action: cancel the job (never orphan it — the reason the
    guard existed), then drop the record.

    Cancelling rather than killing is deliberate: it goes through the same path
    the `cancel` button uses, so the job's own bookkeeping (the in-process
    discard flag, the in-flight LLM call it is awaiting) is unwound the one way
    the system knows. **A `paused` job included** (GRILLING G34): its leg has
    stopped, but the object still exists, and dropping the card without
    cancelling it is exactly the orphan this rule forbids.
    """
    row = pdr.get_session(user, service, recipe_path, label)
    if row is None:
        raise HTTPException(404, "no pre-deploy session for that combination")
    job_id = row.get("job_id")
    cancelled = False
    if job_id is not None:
        from ..services.generation_jobs import generation_jobs
        # `job_status` (not `get_job`) — it owns its session, so it cannot be
        # called without one. This guard read `get_job(int(job_id))` with no
        # `db` and swallowed the TypeError, so it never fired either way.
        #
        # The cancellable set is the manager's own `CANCELLABLE_STATUSES`, not a
        # copy of it. It includes `paused`: that leg has STOPPED but its job
        # still EXISTS, so skipping it would drop the record and leave the object
        # paused forever — the state G34 forbids, and how the five orphaned
        # paused jobs (gt-315/316/325/328/332) arose.
        if generation_jobs.job_status(int(job_id)) in generation_jobs.CANCELLABLE_STATUSES:
            from ..database import SessionLocal
            db = SessionLocal()
            try:
                generation_jobs.cancel_job(db, int(job_id))
                cancelled = True
            finally:
                db.close()
    pdr.remove_session(user, service, recipe_path, label)
    return {"deleted": True, "cancelled_job": cancelled}

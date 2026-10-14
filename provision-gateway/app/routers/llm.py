"""LLM router — /api/llm/* endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import require_admin
from ..services.audit_service import log_action
from ..services.generation_jobs import (
    GenerationCapacityError,
    GenerationJobError,
    generation_jobs,
)
from ..models.generation_job import JOB_ORIGIN_DEPLOY, job_origin
from ..services.llm_service import llm_service

router = APIRouter(prefix="/api/llm", tags=["llm"])


# ---------------------------------------------------------------------------
# Multi-config management
# ---------------------------------------------------------------------------

@router.get("/configs")
async def list_llm_configs(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all LLM configs."""
    configs = llm_service.list_configs(db)
    active = llm_service.get_config(db)
    return {"configs": configs, "active": active}


@router.post("/configs")
async def create_llm_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new LLM config."""
    config = llm_service.create_config(db, req)
    log_action(db, action="llm_config_create", admin_id=current_admin["id"], status="success")
    return {"config": config.to_dict()}


@router.put("/configs/{config_id}/activate")
async def activate_llm_config(
    config_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Activate an LLM config."""
    config = llm_service.activate_config(db, config_id)
    if not config:
        raise HTTPException(404, "Config not found")
    log_action(db, action="llm_config_activate", admin_id=current_admin["id"], status="success")
    return {"activated": True, "config": config.to_dict()}


@router.delete("/configs/{config_id}")
async def delete_llm_config(
    config_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete an LLM config."""
    if not llm_service.delete_config(db, config_id):
        raise HTTPException(404, "Config not found")
    return {"deleted": True}


@router.get("/config")
async def get_llm_config(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get current LLM configuration."""
    return llm_service.get_config(db)


@router.put("/config")
async def update_llm_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update LLM configuration."""
    try:
        config = llm_service.save_config(db, req)
    except Exception as e:
        raise HTTPException(500, str(e))
    
    log_action(db, action="llm_config", admin_id=current_admin["id"], status="success")
    return {"updated": True, "config": config.to_dict()}


@router.post("/test")
async def test_llm_connection(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Test the LLM connection."""
    try:
        result = await llm_service.test_connection(db)
    except Exception as e:
        raise HTTPException(502, f"LLM test failed: {e}")
    return result


@router.post("/generate")
async def generate_config(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generate a config file using LLM (legacy single-shot path).

    New callers should use the async job endpoints (POST /api/llm/jobs) —
    the agent-with-tools generation with validation self-repair runs there.
    """
    config_type = req.get("type")
    if config_type not in ("docker_compose", "nginx_conf", "env_file", "dockerfile"):
        raise HTTPException(400, f"Invalid type: {config_type}. Must be one of: docker_compose, nginx_conf, env_file, dockerfile")

    context = req.get("context", {})

    try:
        result = await llm_service.generate_config(db, config_type, context)
    except Exception as e:
        raise HTTPException(502, f"LLM generation failed: {e}")

    log_action(db, action="llm_generate", admin_id=current_admin["id"],
               status="success", detail={"type": config_type})
    return result


# ---------------------------------------------------------------------------
# Async generation jobs (file-selection-and-generation design §Generation)
# ---------------------------------------------------------------------------

@router.post("/jobs", status_code=202)
async def create_generation_job(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create an async LLM generation job.

    Body::

        {
          "service_name": "dify",
          "recipe_path": "docker",           # "" = root recipe
          "job_type": "generate_missing",    # 'generate_missing' | per-category
          "category": "compose",             # for per-category jobs
          "prompt": "...",                   # mandatory for empty recipes
          "selection": {"compose": [...], "nginx": ..., "env": [...], "profiles": [...]},
          "deploy_metadata": {"user_name": ..., "label": ..., "domain": ...},
        }

    ``generate_missing`` runs the ordered phases compose → nginx → env as ONE
    job (progress per phase); per-category jobs run a single phase.
    """
    # --- G27: ANSWERING A PAUSE CREATES A NEW JOB -------------------------
    # A pause is a TERMINAL leg carrying a payload (`result.pause`). The answer
    # does not mutate anything: it posts here, with that payload under
    # `resume`, and this creates the NEXT leg. The carried `request` IS the new
    # job's request, so the path continues from the state the leg left behind —
    # and `values` is the operator's input (the edited guidance, or the external
    # credential values).
    #
    # `POST /jobs/{id}/input` — which rewrote the same row and re-ran it — is
    # gone with that model.
    _resume = req.get("resume")
    # The leg this answer CONSUMES. Read before `req` is replaced by the carried
    # request; acted on once the new leg exists (see the tail of this handler).
    _answered_job_id = req.get("parent_job_id")
    if isinstance(_resume, dict) and _resume.get("request"):
        carried = dict(_resume["request"])
        _values = {str(k): str(v) for k, v in (req.get("values") or {}).items()}
        # The GUIDED-FIX kinds (the compose conflict, and R1.3's two validation
        # checks) each carry an EDITABLE guidance that becomes the next leg's
        # prompt; `input` is the credential pause and carries values instead.
        # The set is named once, here, because the three kinds differ only in
        # which file they name — the answer's shape is the same.
        if _resume.get("kind") in ("compose_fix", "validate_ports_fix", "validate_conf_fix") \
                and _values.get("guidance"):
            carried["prompt"] = _values["guidance"]
        if _resume.get("kind") == "input" and _values:
            carried["resume_values"] = _values
        # Identity comes from the caller (the card knows its own target); the
        # carried request already holds it, so only fill gaps.
        carried.setdefault("user_name", req.get("user_name") or "")
        carried.setdefault("label", str(req.get("label", "0")))
        carried["job_type"] = carried.get("job_type") or "generate_missing"
        req = carried

    job_type = req.get("job_type") or "generate_missing"
    if job_type == "fix":
        # `LLM based fix` shares this entry point: the failed instance, its
        # task id and the failed task's whole log are resolved server-side.
        service_name, recipe_path, req = await _enrich_fix_request(req)
    else:
        service_name = req.get("service_name")
        recipe_path = req.get("recipe_path") or ""
    if not service_name:
        raise HTTPException(400, "'service_name' is required")
    # §8b: a deploy-scoped generation opens a PRE-DEPLOY SESSION, so closing
    # the panel keeps it alive and reachable (explicitly not `close = cancel`).
    # The combination is reserved while the session exists. The standalone
    # Generate panel has no user/label, so it gets no session — that path stays
    # Tasks-page-only, by design.
    # The fix ANALYSIS is not a pre-deploy session (GRILLING G26): while it runs
    # the instance is still in the -api registry and its status is `failed`, and
    # the session is written only AFTER the analysis succeeds — by the handoff,
    # as part of the add-then-delete. Creating one here would also reserve the
    # combination against the very deploy the analysis is about to enable, and
    # would show a card for work that has not been paused yet.
    _user = req.get("user_name") or ""
    _label = str(req.get("label", "0"))
    # ONE home for "who owns this job": `job_origin` is what the job's own
    # payload exposes to the dashboard, so the session this entry point opens
    # and the surface that may act on the job cannot drift apart. `deploy` is
    # the path that carries a `user_name` — the fix analysis is explicitly not.
    _session_scoped = job_origin(req, job_type) == JOB_ORIGIN_DEPLOY
    if _session_scoped:
        from ..services import pre_deploy_registry as pdr
        _reserved = pdr.is_reserved(_user, service_name, recipe_path, _label)
        # G26 row 5: `check` on a PAUSED session confirms the fixing guide and
        # restarts the path, and that restart IS this generation — so the paused
        # session reserves the very combination it has to generate FOR, and
        # refusing it left the operator able to open the panel, confirm, and get
        # a 409 with no way forward. A guided restart (`fix`) against a session
        # that is WAITING (`paused`) is that resume, not a competing request.
        # A `generating` session still reserves, so a live job cannot be
        # clobbered by one.
        _existing = (pdr.get_session(_user, service_name, recipe_path, _label)
                     if _reserved else None)
        _resuming_own_pause = (
            _existing is not None
            and _existing.get("state") == "paused"
            # R3-F49 — and ANSWERING the pause counts as resuming it. The panel
            # answers a pause by starting a NEW job that consumes the paused leg:
            # the body carries `resume` (the pause being answered) and
            # `parent_job_id` (the leg it ends). Only `fix` — the `check` button's
            # guided restart — was recognised here, so a plain answer was refused
            # by the very reservation its own pause had created: the operator saw
            # a 409 they could not clear, the paused job could never be consumed,
            # and every later deploy of that service hit the same 409. Measured
            # 2026-10-10; the deadlock was total, with no way forward in the UI.
            #
            # Read `_resume` / `_answered_job_id`, NOT `req`: by this point the
            # handler has replaced `req` with the CARRIED request (line ~196), so
            # a lookup on `req` finds neither key — measured, and it is why the
            # first version of this fix still answered 409.
            and bool(req.get("fix") or _resume or _answered_job_id)
        )
        # G29: the SAME escape for the operator's own RETRY of a refused
        # deployment. A gateway refusal is recorded on the session (below), and
        # that record reserves the key — which left the refusal's own Retry
        # unable to run the generation it exists for, because the generation is
        # exactly what was refused and nothing was produced: the card offered a
        # Retry that could only ever answer 409 (or, before this, rendered
        # disabled because the record carried no options). The -api's refusal
        # never hit this: its Retry replays complete options and runs no
        # generation (G20).
        _resuming_own_refusal = (
            _existing is not None and _existing.get("state") == "refused"
        )
        if _reserved and not (_resuming_own_pause or _resuming_own_refusal):
            raise HTTPException(409, {
                "error": "combination_reserved",
                "detail": ("a pre-deploy session already exists for this "
                           "(user, service, label) — check, cancel or delete it first"),
            })
    try:
        job = generation_jobs.create_job(db, service_name, recipe_path, job_type, req)
    except GenerationJobError as e:
        raise HTTPException(400, str(e))
    except GenerationCapacityError as e:
        # GRILLING G29: the GATEWAY's own pool refused. Recorded on the session
        # exactly as the -api's refusal is (G20/§4d), so the card can show it
        # with its own remedy instead of a toast that scrolls away. The two
        # refusals stay DISTINCT codes — `at_capacity` means "cancel a hanging
        # task and retry", `generation_at_capacity` means "wait for a running
        # generation, or cancel one".
        if _session_scoped:
            from ..services import pre_deploy_registry as _pdr
            # G29: the refused record carries the request it was made with, the
            # same way the -api's refusal carries the options it was SENT. The
            # generation request holds the deploy's selection and proxy plus its
            # `deploy_metadata`, so the card's Retry can open the panel on the
            # same deployment. `passwd` is not among them and is not wanted: a
            # generation refusal happens BEFORE the -api sees the deploy, and a
            # refusal is no place for a credential (see `refusal_options`, which
            # also blanks the URL for both refusal kinds).
            _meta = req.get("deploy_metadata") or {}
            _pdr.mark_refused(
                user=_user, service=service_name, recipe_path=recipe_path,
                label=_label, reason=e.code, detail=str(e),
                options=_pdr.refusal_options({
                    **{k: v for k, v in req.items() if k != "deploy_metadata"},
                    "user_name": _user,
                    "service_name": service_name,
                    "label": _label,
                    # The panel seeds its Service picker from `project_root`
                    # (the recipe), exactly as the -api refusal's options do.
                    "project_root": recipe_path or service_name,
                    **_meta,
                }, use_global_proxy=bool(req.get("use_global_proxy"))),
            )
        raise HTTPException(503, {
            "error": e.code,
            "detail": str(e),
            "running": e.in_flight,
            "capacity": e.capacity,
        })

    if _session_scoped:
        from ..services import pre_deploy_registry as pdr
        pdr.add_session(
            user=_user, service=service_name, recipe_path=recipe_path, label=_label,
            state="generating", job_id=job.id,
            # G20 precedent, applied to the session THIS entry point creates: a
            # resumed path replays the options it was created with, so the
            # session must record the proxy. The request is the ONLY source
            # here — unlike the fix handoff there is no instance yet (the
            # session exists precisely so that none does), so nothing can be
            # derived from a registry entry. Omitting it defaulted the session
            # to `use_global_proxy: false` even when the panel's box was ticked,
            # and a `check` on the parked session then replayed `false` (T12,
            # `finalgrill|probe-conflict||1`, generate entry point).
            use_global_proxy=bool(req.get("use_global_proxy")),
        )

    log_action(db, action="llm_generation_job", admin_id=current_admin["id"],
               target_service=service_name, status="created",
               detail={"job_id": job.id, "job_type": job_type})

    # --- G34: the ANSWERED leg is consumed ---------------------------------
    # Answering a pause creates the next leg (G27) — and the leg it answered
    # keeps its `paused` status forever after, because a pause is TERMINAL on its
    # leg and nothing else ever writes to that row. That is a paused object with
    # nothing owning it: the exact state G34's "no card gone while its object
    # stays paused" rule forbids, it makes the Source Projects row offer `Check`
    # for work that is already done, and its record can never leave (a paused
    # record is never swept — G30). So the answer ends it, through the same
    # transition every other surface uses: cancel, never delete.
    #
    # AFTER the new job exists: the path always has a live leg, and a failed
    # create cannot consume the pause it was answering.
    if _answered_job_id is not None and job is not None:
        from ..database import SessionLocal
        from ..services.generation_jobs import generation_jobs as _gj
        from ..models.generation_job import JOB_STATUS_PAUSED as _PAUSED

        _db = SessionLocal()
        try:
            _parent = _gj.get_job(_db, int(_answered_job_id))
            # Same target, still parked: anything else is not ours to touch.
            if (_parent is not None
                    and _parent.status == _PAUSED
                    and _parent.service_name == service_name
                    and (_parent.recipe_path or "") == (recipe_path or "")):
                _gj.cancel_job(_db, int(_answered_job_id))
        except Exception:
            pass  # the answer already stands — bookkeeping must not undo it
        finally:
            _db.close()

    return {"job_id": job.id, "status": job.status, "job_type": job_type}


async def _enrich_fix_request(req: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Resolve a `LLM based fix` request into a full generation request.

    Runs through the SAME ``POST /jobs`` entry point as every other generation,
    so the dashboard reuses the existing job hook (live popup → review gate →
    save) unchanged. It only fills in what the caller cannot know: the failed
    instance's `task_id`, its registry-recorded `failure`, and the failed task's
    log — fed WHOLE — all of which live in the -api
    (llm-based-fix-todo.md §1/§4/§5).

    Returns ``(service_name, recipe_path, request)``.
    """
    from ..services.file_sets import list_file_sets
    from ..services.provision_service import provision_service

    user_name = req.get("user_name")
    service_name = req.get("service_name")
    label = str(req.get("label", "0"))
    if not user_name or not service_name:
        raise HTTPException(400, "'user_name' and 'service_name' are required")

    # 1) The instance, from the instance store (never from the task record).
    try:
        payload = await provision_service.get_user(user_name)
    except Exception as e:
        raise provision_service.as_http_error(e)

    entry: dict[str, Any] | None = None
    for u in payload.get("user_status", []):
        if u.get("user_name") != user_name:
            continue
        for bucket in ("failed_services", "unhealthy_services",
                       "healthy_services", "missing_services"):
            for s in (u.get(bucket) or []):
                if (s.get("service_name") == service_name
                        and str(s.get("label", "0")) == label):
                    entry = s
                    break
    if entry is None:
        raise HTTPException(404, f"Instance not found: {user_name}/{service_name}/{label}")
    if entry.get("status") != "failed":
        raise HTTPException(409, "Only a failed instance can be fixed")

    # 2) Availability is decided where the registry and the tasks both live (§5).
    if not entry.get("fix_available"):
        raise HTTPException(409, {
            "error": "task_log_gone",
            "detail": ("task log is gone, cannot fix the deployment files with LLM, "
                       "just remove this service and retry"),
        })
    task_id = entry.get("task_id") or ""
    try:
        log_payload = await provision_service.get_task_log(task_id)
    except Exception as e:
        raise provision_service.as_http_error(e)

    # 3) The stored selection IS the standard generation context for this recipe.
    recipe_path = entry.get("recipe_path") or ""
    selection: dict[str, Any] = {}
    try:
        sets = (list_file_sets(service_name).get("file_sets") or {})
        selection = sets.get(recipe_path) or sets.get("") or {}
    except Exception:
        selection = {}

    request: dict[str, Any] = {
        **req,
        "fix": True,
        "task_id": task_id,
        "task_log": log_payload.get("log") or "",
        "failure": entry.get("failure") or {},
        "selection": req.get("selection") or selection,
        "user_name": user_name,
        "label": label,
        "deploy_metadata": {
            "user_name": user_name,
            "label": label,
            "domain": req.get("domain") or "localhost",
        },
    }
    return service_name, recipe_path, request


@router.get("/jobs")
async def list_generation_jobs(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List generation jobs (oldest TTL-pruned; newest 200 returned)."""
    return {"jobs": generation_jobs.list_jobs(db)}


@router.get("/jobs/{job_id}")
async def get_generation_job(
    job_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Poll one generation job (progress + result when completed)."""
    # G30's trigger rule: the TTL sweep runs on READS. This is the gateway's
    # single-record read, the counterpart of the -api's `TaskManager.get()`,
    # which sweeps the same way. It is a polled endpoint, so the sweep is one
    # indexed SELECT that finds nothing in the common case.
    generation_jobs.prune_finished(db)
    job = generation_jobs.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict(include_result=True)


@router.delete("/jobs/{job_id}")
async def cancel_generation_job(
    job_id: int,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Cancel a generation job — queued, running, parked OR paused.

    A paused job is cancellable (GRILLING G34): G27 made a pause a terminal leg
    whose job has stopped but still exists, so ending it is a status transition
    to `cancelled` here, never a removal. The set lives in the manager
    (`CANCELLABLE_STATUSES`), which is also what the card's Delete reads.
    """
    job = generation_jobs.cancel_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    log_action(db, action="llm_generation_job_cancel", admin_id=current_admin["id"],
               target_service=job.service_name, status="cancelled",
               detail={"job_id": job.id})
    return {"job_id": job.id, "status": job.status}

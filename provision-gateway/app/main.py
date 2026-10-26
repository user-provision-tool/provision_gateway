"""Provision Gateway — FastAPI application entry point."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db, SessionLocal
from .models.gateway_setting import GatewaySetting
from .routers import (
    auth_router,
    audit_router,
    llm_router,
    pre_deploy_router,
    services_router,
    stream_router,
    system_router,
    tasks_router,
    users_router,
)

_start_time = time.time()

# Background task handles
_reconcile_task: asyncio.Task | None = None
_docker_events_task: asyncio.Task | None = None
_project_monitor_task: asyncio.Task | None = None


def _get_reconcile_interval_min() -> int:
    """Read reconciliation interval from gateway_settings (default 0 = disabled)."""
    try:
        db = SessionLocal()
        row = db.query(GatewaySetting).filter(GatewaySetting.key == "reconciliation_interval_min").first()
        db.close()
        return int(row.value) if row else 0
    except Exception:
        return 0


# `_pre_deploy_reconcile_loop` stood here until 2026-10-16: a 90-second patrol
# that dropped pre-deploy sessions whose key appeared in the -api registry, or
# which had no live job. BOTH halves are gone, and the reasons matter:
#
#   * "no live job => stale" existed only because a finished leg's record kept
#     its combination RESERVED forever, refusing every later generation with 409
#     (its docstring recorded that found live, blocking a deploy verification
#     three times). That was a MISSING STATE, not a cleanup problem: the record
#     had nowhere to step down to. `STATE_GENERATED` is that state now — a record
#     that holds nothing needs nobody to patrol for it.
#   * "key in the registry => registered => drop" compared a DB row against a
#     membership list harvested from the -api's registry, and that list was built
#     from four buckets INCLUDING `failed_services` — so retrying a failed
#     instance had its live record deleted mid-generation. Measured 2026-10-14:
#     60 consecutive probe misses, and the operator's card gone while its
#     generation was still running. The deploy's handoff already removes the
#     record on its way into the -api (remove-then-register), so this rule had
#     nothing to catch even when it fired correctly.
#
# What is left is `pre_deploy_registry.reconcile`, called ONCE in `lifespan`
# below for the one case nothing else covers: a record whose runner died with the
# gateway, which nothing will ever write a state for.


async def _reconcile_loop():
    """Background loop: periodically trigger reconciliation on provision-api."""
    while True:
        try:
            # ONE HOME (GRILLING G31, D4): one worker triggers the -api's
            # reconcile, not four.
            from .services.loop_lease import acquire_lease
            if not acquire_lease("provision_api_reconcile", 600):
                await asyncio.sleep(90)
                continue
            interval = _get_reconcile_interval_min()
        except Exception:
            interval = 0
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(f"{settings.PROVISION_API_URL}/reconcile")
        except Exception:
            pass  # Silently skip if provision-api is unreachable
        await asyncio.sleep(interval * 60)


async def _docker_events_monitor():
    """Watch Docker events for provision-nginx restarts.

    Runs the blocking docker-py events stream in a thread executor. When a
    restart/start event is detected, triggers reconciliation via provision-api.
    The actual reconciliation is also covered by _reconcile_loop on schedule.
    """
    try:
        # ONE HOME (GRILLING G31, D4): only the lease holder watches. This
        # function is NOT inside an interval loop — it holds a long-lived docker
        # events stream — so a non-holder RETURNS rather than skipping a turn.
        from .services.loop_lease import acquire_lease
        if not acquire_lease("docker_events_monitor", 3600):
            return
        import docker
    except ImportError:
        return

    import requests as sync_requests

    last_triggered = 0.0

    def _blocking_event_loop():
        """Run in thread executor — blocking docker events stream."""
        nonlocal last_triggered
        while True:
            try:
                client = docker.from_env()
                events = client.events(
                    filters={"type": ["container"], "container": ["subnet-acl-nginx"], "event": ["restart", "start"]},
                    decode=True,
                )
                for event in events:
                    if event.get("status") in ("restart", "start"):
                        now = time.time()
                        if now - last_triggered < 30:
                            continue
                        last_triggered = now
                        print(f"[gateway] subnet-acl-nginx {event['status']} detected — triggering reconciliation")
                        try:
                            sync_requests.post(f"{settings.PROVISION_API_URL}/reconcile", timeout=30)
                        except Exception:
                            pass
            except Exception:
                time.sleep(30)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _blocking_event_loop)


async def _project_monitor_loop():
    """Background loop: periodically scan source_projects for new directories.

    Uses ServiceManager.scan_for_new_projects() to detect projects that
    were manually placed into PROVISION_DIR/source_projects. Detected
    projects are stored in an event buffer that the frontend can poll
    via GET /api/services/notifications.
    """
    from .services.service_manager import service_manager as _sm
    while True:
        try:
            # ONE HOME (GRILLING G31, D4): one worker rescans source_projects.
            from .services.loop_lease import acquire_lease
            if not acquire_lease("project_monitor", 120):
                await asyncio.sleep(10)
                continue
            new = _sm.scan_for_new_projects()
            if new:
                names = [p["name"] for p in new]
                print(f"[gateway] Detected new project(s) in source_projects: {names}")
        except Exception:
            pass  # Silently retry on next cycle
        await asyncio.sleep(10)  # Poll every 10 seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global _reconcile_task, _docker_events_task, _project_monitor_task
    # Startup
    init_db()
    print(f"[gateway] Database initialized at {settings.DATABASE_URL}")

    # The pre-deploy store moved from `pre-deploy-registry.json` into the table
    # `pre_deploy_sessions` (GRILLING G31). Import any sessions the file still
    # holds before anything reads the table — otherwise a live refusal the
    # operator has not acted on yet would vanish with the file. Idempotent, and
    # the file is renamed once imported.
    from .services.pre_deploy_registry import migrate_legacy_json
    _imported = migrate_legacy_json()
    if _imported:
        print(f"[gateway] pre-deploy registry: imported {_imported} session(s) from the legacy JSON file")

    # Initialize Registry and HostnameIndex for ACL/service access
    from .services.registry import Registry
    from .services.hostname_index import HostnameIndex
    app.state.registry = Registry(settings.REGISTRY_FILE)
    app.state.hostname_index = HostnameIndex(settings.REGISTRY_FILE)
    print(f"[gateway] Registry + HostnameIndex initialized (registry: {settings.REGISTRY_FILE})")

    # v4 §6.1.5: backfill is_default for existing users on startup (GAP-25).
    # Log failures (schema drift / migration gaps) instead of swallowing them —
    # a silently-failed backfill hides the default-key feature being broken.
    try:
        from .services import auth_service
        backfilled = auth_service.backfill_default_keys(SessionLocal())
        if backfilled:
            print(f"[gateway] Backfilled default API key for {backfilled} user(s)")
    except Exception as exc:
        print(f"[gateway] WARNING: default-key backfill failed: {exc}")

    # Generation jobs left queued/running by a previous process are dead —
    # mark them failed ("interrupted by gateway restart") so polls resolve.
    try:
        from .services.generation_jobs import generation_jobs
        stale = generation_jobs.recover_stale_jobs(SessionLocal())
        if stale:
            print(f"[gateway] Marked {stale} stale generation job(s) as failed (restart recovery)")
    except Exception as exc:
        print(f"[gateway] WARNING: generation-job recovery failed: {exc}")

    # §8b: pre-deploy sessions are closed against what actually exists now — the
    # stale ones were marked failed above, so nothing is live for them. This is
    # the ONLY time `reconcile` runs (see the note where the loop used to be): it
    # tidies records whose runner died with the gateway, and it spares the ones in
    # the operator's hands — `paused`, `refused`, and `generated`.
    try:
        from .services import pre_deploy_registry as _pdr
        rep = _pdr.reconcile(live_job_ids=set())
        if rep["dropped_stale"]:
            print(f"[gateway] pre-deploy reconcile: dropped "
                  f"{len(rep['dropped_stale'])} stale session(s)")
    except Exception as exc:
        print(f"[gateway] WARNING: pre-deploy reconcile failed: {exc}")

    _reconcile_task = asyncio.create_task(_reconcile_loop())
    print("[gateway] Scheduled reconciliation background task started")
    _docker_events_task = asyncio.create_task(_docker_events_monitor())
    print("[gateway] Docker events monitor started (watching subnet-acl-nginx)")
    _project_monitor_task = asyncio.create_task(_project_monitor_loop())
    print("[gateway] Project directory monitor started (polling source_projects every 10s)")
    yield
    # Shutdown
    if _reconcile_task:
        _reconcile_task.cancel()
    if _docker_events_task:
        _docker_events_task.cancel()
    if _project_monitor_task:
        _project_monitor_task.cancel()
    print("[gateway] Shutting down")


app = FastAPI(
    title="Provision Gateway",
    version="1.0.0",
    description="Web UI and management layer for provision-api",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow dashboard origin
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Dashboard runs on same machine; tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth_router)
app.include_router(system_router)
app.include_router(services_router)
app.include_router(users_router)
app.include_router(tasks_router)
app.include_router(llm_router)
app.include_router(pre_deploy_router)
app.include_router(audit_router)
app.include_router(stream_router)


# ---------------------------------------------------------------------------
# Health check (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness/readiness probe."""
    uptime_sec = int(time.time() - _start_time)
    return {
        "status": "ok",
        "db": "connected",
        "provision_api": "unknown",  # Will be checked by system status endpoint
        "uptime_sec": uptime_sec,
    }

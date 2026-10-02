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


async def _pre_deploy_reconcile_loop():
    """§8b: periodically close the two-write gap for pre-deploy sessions.

    `reconcile` used to run ONLY at startup, so a session whose job had since
    finished — a completed generation the operator never deployed — kept its
    combination RESERVED forever, and every later generation for the same
    (user, service, label) was refused with 409 until the gateway restarted.
    Found live: a stale session blocked a deploy verification three times.

    A session is dropped when its key already exists in the -api registry (the
    registration succeeded, so the session is done) or when nothing is working
    on it any more (no live job). Both facts are needed, so this loop reads the
    registry once per pass.
    """
    while True:
        try:
            # ONE HOME (GRILLING G31, D4). Each worker runs the lifespan, so
            # without this every loop runs FOUR times — and THIS one was the
            # damaging case: it does a read-modify-write on shared state.
            from .services.loop_lease import acquire_lease
            if not acquire_lease("pre_deploy_reconcile", 300):
                await asyncio.sleep(90)
                continue
            from .services import pre_deploy_registry as _pdr
            from .services.provision_service import provision_service

            sessions = _pdr.list_sessions()
            if sessions:
                registered: set[str] = set()
                try:
                    payload = await provision_service.list_users()
                    for u in payload.get("user_status", []):
                        uname = u.get("user_name") or ""
                        for bucket in ("healthy_services", "unhealthy_services",
                                       "missing_services", "failed_services"):
                            for svc in (u.get(bucket) or []):
                                registered.add(
                                    f"{uname}|{svc.get('service_name', '')}|"
                                    f"{svc.get('recipe_path') or ''}|{svc.get('label', '0')}"
                                )
                except Exception:
                    # Cannot read the registry this pass — only the live-job rule
                    # can be applied, which is still enough to free a session
                    # nothing is working on.
                    registered = set()
                live: set[int] = set()
                try:
                    from .services.generation_jobs import generation_jobs
                    for row in sessions:
                        jid = row.get("job_id")
                        if jid is None:
                            continue
                        # `job_status` (not `get_job`) — it owns its session.
                        # This called `get_job(int(jid))` with no `db`, and the
                        # TypeError was swallowed below, so `live` was always
                        # empty and every session without a registry entry was
                        # dropped... except the exception fired BEFORE the drop,
                        # leaving the sweep a complete no-op. See `job_status`.
                        if generation_jobs.job_status(int(jid)) in (
                            "queued", "running", "awaiting_input"
                        ):
                            live.add(int(jid))
                except Exception:
                    live = set()
                rep = _pdr.reconcile(registered, live)
                if rep["dropped_registered"] or rep["dropped_stale"]:
                    print(f"[gateway] pre-deploy reconcile: dropped "
                          f"{len(rep['dropped_stale'])} stale, "
                          f"{len(rep['dropped_registered'])} already-registered session(s)")
        except Exception:
            pass
        await asyncio.sleep(90)


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
    # stale ones were marked failed above, so nothing is live for them.
    try:
        from .services import pre_deploy_registry as _pdr
        rep = _pdr.reconcile(set(), live_job_ids=set())
        if rep["dropped_stale"] or rep["dropped_registered"]:
            print(f"[gateway] pre-deploy reconcile: dropped {rep['dropped_stale']} stale, "
                  f"{rep['dropped_registered']} already-registered session(s)")
    except Exception as exc:
        print(f"[gateway] WARNING: pre-deploy reconcile failed: {exc}")

    _reconcile_task = asyncio.create_task(_reconcile_loop())
    _pre_deploy_task = asyncio.create_task(_pre_deploy_reconcile_loop())
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

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
    services_router,
    system_router,
    tasks_router,
    users_router,
)

_start_time = time.time()

# Background task handles
_reconcile_task: asyncio.Task | None = None
_docker_events_task: asyncio.Task | None = None


def _get_reconcile_interval_min() -> int:
    """Read reconciliation interval from gateway_settings (default 0 = disabled)."""
    try:
        db = SessionLocal()
        row = db.query(GatewaySetting).filter(GatewaySetting.key == "reconciliation_interval_min").first()
        db.close()
        return int(row.value) if row else 0
    except Exception:
        return 0


async def _reconcile_loop():
    """Background loop: periodically trigger reconciliation on provision-api."""
    while True:
        try:
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
                    filters={"type": ["container"], "container": ["provision-nginx"], "event": ["restart", "start"]},
                    decode=True,
                )
                for event in events:
                    if event.get("status") in ("restart", "start"):
                        now = time.time()
                        if now - last_triggered < 30:
                            continue
                        last_triggered = now
                        print(f"[gateway] provision-nginx {event['status']} detected — triggering reconciliation")
                        try:
                            sync_requests.post(f"{settings.PROVISION_API_URL}/reconcile", timeout=30)
                        except Exception:
                            pass
            except Exception:
                time.sleep(30)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _blocking_event_loop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global _reconcile_task, _docker_events_task
    # Startup
    init_db()
    print(f"[gateway] Database initialized at {settings.DATABASE_URL}")
    _reconcile_task = asyncio.create_task(_reconcile_loop())
    print("[gateway] Scheduled reconciliation background task started")
    _docker_events_task = asyncio.create_task(_docker_events_monitor())
    print("[gateway] Docker events monitor started (watching provision-nginx)")
    yield
    # Shutdown
    if _reconcile_task:
        _reconcile_task.cancel()
    if _docker_events_task:
        _docker_events_task.cancel()
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

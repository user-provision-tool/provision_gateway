"""Provision Gateway — FastAPI application entry point."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Startup
    init_db()
    print(f"[gateway] Database initialized at {settings.DATABASE_URL}")
    yield
    # Shutdown
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

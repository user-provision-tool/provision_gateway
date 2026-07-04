"""System router — /api/system/* endpoints with real Docker monitoring."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..services import docker_service
from ..services.provision_service import provision_service
from ..services.reconciliation import reconciliation_service

router = APIRouter(prefix="/api/system", tags=["system"])

_start_time = time.time()


@router.get("/status")
async def system_status(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get system health status including Docker host stats."""
    provision_api_status = {"status": "unknown", "latency_ms": 0, "version": "unknown"}
    try:
        health = await provision_service.health()
        provision_api_status["status"] = health.get("status", "unknown")
    except Exception:
        provision_api_status["status"] = "unreachable"

    nginx_status = {"status": "unknown"}
    try:
        if docker_service.container_running("provision-nginx"):
            nginx_status["status"] = "running"
            nginx_status["container_id"] = "provision-nginx"
        else:
            nginx_status["status"] = "stopped"
    except Exception:
        nginx_status["status"] = "error"

    total, running = docker_service.get_container_count()
    host_stats = docker_service.get_host_stats()

    docker_host = {
        "containers_total": total,
        "containers_running": running,
        "disk_percent": host_stats.get("disk_percent", 0),
    }

    gateway = {
        "version": "1.0.0",
        "uptime_sec": int(time.time() - _start_time),
    }

    return {
        "provision_api": provision_api_status,
        "provision_nginx": nginx_status,
        "docker_host": docker_host,
        "gateway": gateway,
    }


@router.get("/stats")
async def system_stats(
    detail: bool = Query(False),
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get detailed system stats including per-container metrics."""
    containers = docker_service.docker_ps()
    stats = docker_service.docker_stats_snapshot()

    stats_map = {s["name"]: s for s in stats}
    merged = []
    for c in containers:
        name = c["name"]
        s = stats_map.get(name, {})
        merged.append({
            "name": name,
            "status": c["status"],
            "image": c["image"],
            "cpu_percent": s.get("cpu_percent", "N/A"),
            "mem_usage": s.get("mem_usage", "N/A"),
            "mem_percent": s.get("mem_percent", "N/A"),
            "running_for": c.get("running_for", ""),
        })

    result = {"containers": merged}

    if detail:
        host_stats = docker_service.get_host_stats()
        prov_size = docker_service.get_provision_dir_size()
        result["host"] = host_stats
        result["provision_dir"] = prov_size

    return result


@router.post("/reconcile")
async def trigger_reconciliation(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Run a full nginx upstream reconciliation."""
    from ..services.audit_service import log_action

    try:
        report = await reconciliation_service.run_reconciliation()
    except Exception as e:
        log_action(
            db, action="reconcile", admin_id=current_admin.id,
            status="failure", error_message=str(e),
        )
        raise HTTPException(500, f"Reconciliation failed: {e}")

    log_action(
        db, action="reconcile", admin_id=current_admin.id,
        status="success",
        detail={
            "total_upstreams": report["total_upstreams"],
            "reachable": report["reachable"],
            "unreachable": report["unreachable"],
        },
    )

    return {"message": "Reconciliation completed.", "report": report}


@router.get("/reconcile/status")
async def reconciliation_status(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the last reconciliation status from the state file."""
    state = await reconciliation_service.get_state()
    last_run = state.get("last_updated")
    upstreams = state.get("upstreams", [])

    reachable = sum(1 for u in upstreams if u.get("reachable") is True)
    unreachable = sum(1 for u in upstreams if u.get("reachable") is False)

    return {
        "last_run": last_run,
        "result": {
            "total_upstreams": len(upstreams),
            "reachable": reachable,
            "unreachable": unreachable,
            "unreachable_details": [
                u for u in upstreams if u.get("reachable") is False
            ],
        },
    }


@router.get("/nginx-state")
async def get_nginx_state(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the full nginx state JSON."""
    return await reconciliation_service.get_state()

"""System router — /api/system/* endpoints with real Docker monitoring."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import require_gateway_token, require_admin
from ..models.system_config import SystemConfig
from ..services.acl_state import acl_state
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/system", tags=["system"])

_start_time = time.time()


# T3/R5 — the status page must render in seconds even when the -api is down.
# Each probe has a short per-call bound and the whole -api-dependent half has
# ONE overall deadline; a timeout returns a degraded payload instead of holding
# the request for 15 × 300 s. The registry-backed queue counts are computed
# BEFORE this deadline and always survive.
STATUS_PROBE_TIMEOUT_S = 2.0
STATUS_DEGRADED_DEADLINE_S = 5.0
STATUS_COMPONENT_NAMES = [
    "subnet-acl-provision-api", "subnet-acl-nginx", "subnet-acl-gateway",
    "subnet-acl-dashboard", "subnet-acl-nginx-acl",
]


def _degraded_api_status() -> dict:
    """The payload used when the -api does not answer within the deadline."""
    return {
        "provision_api": {"status": "unreachable", "latency_ms": 0,
                          "version": "unknown"},
        "subnet_acl_nginx": {"status": "unknown"},
        "docker_host": {
            "containers_total": 0, "containers_running": 0,
            "cpu_percent": 0, "mem_percent": 0, "disk_percent": 0,
        },
        "components": {
            name: {"running": False, "exists": False, "status": "unavailable"}
            for name in STATUS_COMPONENT_NAMES
        },
        "services_count": 0,
        "users_count": 0,
        "service_stats": {},
        "container_stats": {},
        "acl": {**acl_state(None), "edge": None, "enabled": False},
        "degraded": True,
        "unavailable_sections": [
            "provision_api", "components", "docker_host", "service_stats",
            "container_stats", "users_count", "acl",
        ],
    }


async def _collect_api_status() -> dict:
    """The -api-dependent half of the status payload, within probe bounds."""
    timeout = STATUS_PROBE_TIMEOUT_S
    unavailable: list[str] = []

    provision_api_status = {"status": "unknown", "latency_ms": 0, "version": "unknown"}
    try:
        health = await provision_service.health()
        provision_api_status["status"] = health.get("status", "unknown")
        if health.get("status") != "ok":
            unavailable.append("provision_api")
    except Exception:
        provision_api_status["status"] = "unreachable"
        unavailable.append("provision_api")

    components = {}
    components_failed = False
    for name in STATUS_COMPONENT_NAMES:
        probe_failed = False
        try:
            running = await provision_service.container_running(name, timeout=timeout)
            exists = await provision_service.container_exists(name, timeout=timeout)
        except Exception:
            running = False
            exists = False
            probe_failed = True
            components_failed = True
        components[name] = {
            "running": running,
            "exists": exists,
            # A failed PROBE is not "not found": the container may exist and the
            # -api may simply be down. Say unavailable (R5) rather than lie.
            "status": ("unavailable" if probe_failed
                       else ("running" if running else ("stopped" if exists else "not found"))),
        }
    if components_failed:
        unavailable.append("components")

    try:
        host_stats = await provision_service.host_stats(timeout=timeout)
    except Exception:
        host_stats = {}
        unavailable.append("docker_host")
    try:
        docker_info_data = await provision_service.docker_info(timeout=timeout)
    except Exception:
        docker_info_data = {}
        if "docker_host" not in unavailable:
            unavailable.append("docker_host")

    docker_host = {
        "containers_total": docker_info_data.get("containers_total", 0),
        "containers_running": docker_info_data.get("containers_running", 0),
        "cpu_percent": host_stats.get("cpu_percent", 0),
        "mem_percent": host_stats.get("mem_percent", 0),
        "disk_percent": host_stats.get("disk_percent", 0),
    }

    services_count = 0
    users_count = 0
    service_stats: dict = {}
    container_stats: dict = {}

    try:
        cs = await provision_service.get_container_stats(timeout=timeout)
        container_stats = cs.get("container_stats", {})
    except Exception:
        unavailable.append("container_stats")

    try:
        ss = await provision_service.get_service_stats(timeout=timeout)
        service_stats = ss.get("service_stats", {})
        services_count = service_stats.get("expected", 0)
    except Exception:
        unavailable.append("service_stats")

    try:
        users_result = await provision_service.list_users(timeout=timeout)
        users_list = users_result.get("user_status", [])
        users_count = len(users_list)
    except Exception:
        unavailable.append("users_count")

    # ACL state: the gateway setting AND the edge container env must BOTH be on
    # for ACL to be effective — a gateway-on/edge-off drift (the 2026-08-28 G3
    # outage) silently exposes native Basic on the internal nginx. G80 removed
    # the switch from the deployment, so an ABSENT value means on (see
    # acl_state); only an explicit edge-off is a mismatch. An unreadable edge
    # env is unknown, and marked unavailable.
    try:
        edge_env = await provision_service.container_env(
            "subnet-acl-nginx-acl", timeout=timeout)
    except Exception:
        edge_env = None
        unavailable.append("acl")
    acl = acl_state(edge_env)

    return {
        "provision_api": provision_api_status,
        "subnet_acl_nginx": {"status": components["subnet-acl-nginx"]["status"]},
        "docker_host": docker_host,
        "components": components,
        "services_count": services_count,
        "users_count": users_count,
        "service_stats": service_stats,
        "container_stats": container_stats,
        "acl": acl,
        "degraded": bool(unavailable),
        "unavailable_sections": unavailable,
    }


@router.get("/status")
async def system_status(
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Get system health status including per-component Docker stats.

    R5 — the -api-dependent probes run under ONE short deadline. Locally
    available values (queue counts, proxy state, gateway uptime) are computed
    first and never lost to an -api outage.
    """
    gateway = {
        "version": "1.0.0",
        "uptime_sec": int(time.time() - _start_time),
    }

    # Proxy status (local DB)
    from ..services.proxy_service import has_active_proxy, get_active_config
    proxy_active = has_active_proxy(db)
    proxy_info = None
    if proxy_active:
        cfg = get_active_config(db)
        if cfg:
            proxy_info = {"url": cfg.get("url", ""), "reachable": cfg.get("reachable")}

    # Queue counts come from the task rows (R9.2), not from an -api HTTP poll.
    # `tasks_*` are the OPERATOR lane (the cards' counts, R8); the maintenance
    # lane is reported separately (G94 R5.2).
    tasks_running = tasks_queued = tasks_planned = 0
    tasks_queued_maintenance = tasks_maintenance = 0
    try:
        from ..services.task_queue import gateway_queue
        counts = await asyncio.to_thread(gateway_queue.counts)
        tasks_running = counts["operator"]["running"]
        tasks_queued = counts["operator"]["queued"] + counts["operator"]["planned"]
        tasks_planned = counts["operator"]["planned"]
        maint = counts["maintenance"]
        tasks_queued_maintenance = maint["queued"] + maint["planned"]
        tasks_maintenance = maint["queued"] + maint["planned"] + maint["running"]
    except Exception:
        pass

    try:
        api_payload = await asyncio.wait_for(
            _collect_api_status(), timeout=STATUS_DEGRADED_DEADLINE_S)
    except asyncio.TimeoutError:
        api_payload = _degraded_api_status()
    except Exception:  # noqa: BLE001 — an unexpected probe bug must not blank the page
        api_payload = _degraded_api_status()

    return {
        **api_payload,
        "gateway": gateway,
        "proxy": {"active": proxy_active, "info": proxy_info},
        "tasks_running": tasks_running,
        "tasks_queued": tasks_queued,
        "tasks_planned": tasks_planned,
        "tasks_queued_maintenance": tasks_queued_maintenance,
        "tasks_maintenance": tasks_maintenance,
        "nginx_http_port": settings.NGINX_HTTP_PORT,
        "nginx_https_port": settings.NGINX_HTTPS_PORT,
    }


@router.get("/queue")
async def system_queue(
    current_admin: dict = Depends(require_admin),
):
    """Both queues' telemetry: counts, positions, ETA, heartbeat, settings.

    Admin-only (task doc §3.1). The gateway half is its own store; the
    registry half is read read-only through ``gateway_reader`` — the -api owns
    it and never reads back (R9.1/R9.2).
    """
    from ..services.task_queue import gateway_queue
    return await asyncio.to_thread(gateway_queue.stats)


@router.get("/stats")
async def system_stats(
    detail: bool = Query(False),
    current_user: dict = Depends(require_gateway_token),
):
    """Get detailed system stats including per-container metrics (via provision-api)."""
    try:
        containers = await provision_service.docker_ps()
    except Exception:
        containers = []
    try:
        stats = await provision_service.docker_stats()
    except Exception:
        stats = []

    stats_map = {s["name"]: s for s in stats}
    merged = []
    for c in containers:
        name = c["name"]
        s = stats_map.get(name, {})
        # provision-api returns "cpu" and "mem" not "cpu_percent"/"mem_usage"
        cpu_val = s.get("cpu", s.get("cpu_percent", "N/A"))
        mem_val = s.get("mem", s.get("mem_usage", "N/A"))
        # Parse memory: "10.5MiB / 1.94GiB" → extract RSS
        mem_rss = mem_val.split(" / ")[0] if " / " in str(mem_val) else str(mem_val)
        merged.append({
            "name": name,
            "status": c["status"],
            "image": c["image"],
            "cpu_percent": cpu_val,
            "mem_usage": mem_val,
            "mem_usage_mb": mem_rss,
            "running_for": c.get("running_for", ""),
        })

    result = {"containers": merged}

    if detail:
        try:
            host_stats = await provision_service.host_stats()
        except Exception:
            host_stats = {}
        result["host"] = host_stats

    return result


@router.post("/reconcile")
async def trigger_reconciliation(
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Run a full nginx upstream reconciliation (proxied to provision-api)."""
    from ..services.audit_service import log_action

    try:
        result = await provision_service.reconcile()
    except Exception as e:
        log_action(
            db, action="reconcile", admin_id=current_user["id"],
            status="failure", error_message=str(e),
        )
        raise HTTPException(500, f"Reconciliation failed: {e}")

    report = result.get("report", {})
    log_action(
        db, action="reconcile", admin_id=current_user["id"],
        status="success",
        detail={
            "total_upstreams": report.get("total_upstreams", 0),
            "reachable": report.get("reachable", 0),
            "unreachable": report.get("unreachable", 0),
        },
    )

    return result


@router.get("/reconcile/status")
async def reconciliation_status(
    current_user: dict = Depends(require_gateway_token),
):
    """Get the last reconciliation status (proxied to provision-api)."""
    return await provision_service.reconciliation_status()


@router.get("/nginx-state")
async def get_nginx_state(
    current_user: dict = Depends(require_gateway_token),
):
    """Get the full nginx state JSON (proxied to provision-api)."""
    return await provision_service.nginx_state()


# ---------------------------------------------------------------------------
# Global Proxy endpoints (multi-config)
# ---------------------------------------------------------------------------

from ..services.proxy_service import (
    list_configs, create_config, update_config, delete_config,
    activate_config, deactivate_all, has_active_proxy,
    test_config_reachability, test_all_configs,
    get_active_config, get_proxy_env,
)


@router.get("/proxy")
async def get_proxy_list(
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get all proxy configs plus active state.

    Admin-only (Gap G5): proxy config may contain credentials.
    """
    configs = list_configs(db)
    active = get_active_config(db)
    return {
        "configs": configs,
        "active": active,
        "has_active": active is not None,
    }


@router.post("/proxy")
async def add_proxy_config(
    req: dict[str, Any] = Body(...),
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Add a new proxy config. Auto-tests reachability after save.

    The DB connection is released before the reachability probe and re-opened
    briefly afterwards, so the (up to 5s) connect never holds a pool connection.
    """
    config = create_config(db, req)
    db.close()  # release the connection before the probe

    reachability = await test_config_reachability(config["id"])

    from ..database import SessionLocal
    from ..services.audit_service import log_action
    audit_db = SessionLocal()
    try:
        row = audit_db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config["id"]).first()
        config = row.to_dict() if row else config
        log_action(audit_db, action="proxy_config_create", admin_id=current_user["id"],
                   status="success", detail={"host": config["host"]})
    finally:
        audit_db.close()

    return {"config": config, "reachability": reachability}


@router.put("/proxy/{config_id}")
async def update_proxy_config(
    config_id: int,
    req: dict[str, Any] = Body(...),
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a proxy config. Re-tests reachability.

    The DB connection is released before the reachability probe and re-opened
    briefly afterwards, so the (up to 5s) connect never holds a pool connection.
    """
    config = update_config(db, config_id, req)
    if not config:
        raise HTTPException(404, "Config not found")
    db.close()  # release the connection before the probe

    reachability = await test_config_reachability(config_id)

    from ..database import SessionLocal
    audit_db = SessionLocal()
    try:
        row = audit_db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
        config = row.to_dict() if row else config
    finally:
        audit_db.close()

    return {"config": config, "reachability": reachability}


@router.delete("/proxy/{config_id}")
async def delete_proxy_config(
    config_id: int,
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a proxy config."""
    if not delete_config(db, config_id):
        raise HTTPException(404, "Config not found")
    return {"deleted": True}


@router.put("/proxy/{config_id}/activate")
def activate_proxy_config(
    config_id: int,
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Activate a proxy config. Only works if reachable."""
    config = activate_config(db, config_id)
    if not config:
        # Check if exists but unreachable
        exists = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
        if exists:
            raise HTTPException(400, "Cannot activate — proxy is not reachable")
        raise HTTPException(404, "Config not found")

    from ..services.audit_service import log_action
    log_action(db, action="proxy_config_activate", admin_id=current_user["id"],
               status="success", detail={"host": config["host"]})

    return {"activated": True, "config": config}


@router.post("/proxy/deactivate")
async def deactivate_proxy(
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Deactivate all proxy configs."""
    deactivate_all(db)
    return {"deactivated": True}


@router.post("/proxy/test")
async def test_proxy_reachability_endpoint(
    current_user: dict = Depends(require_gateway_token),
):
    """Test reachability of all proxy configs."""
    results = await test_all_configs()
    return {"results": results}


# ---------------------------------------------------------------------------
# System Configuration (key-value store for special_users, etc.)
# ---------------------------------------------------------------------------

@router.get("/config")
def get_system_config(
    key: str = Query(None, description="Config key to fetch, or all if omitted"),
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get system configuration value(s).

    Admin-only (Gap G1): config may hold sensitive values (e.g. proxy
    credentials). Every other ``system.py`` route is gated; this one was not.
    """
    if key:
        cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return {"key": key, "value": cfg.value if cfg else ""}
    cfgs = db.query(SystemConfig).all()
    return {"configs": {c.key: c.value for c in cfgs}}


@router.put("/config")
def set_system_config(
    key: str = Query(...),
    value: str = Body(..., embed=True),
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set a system configuration value."""
    cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if cfg:
        cfg.value = value
    else:
        cfg = SystemConfig(key=key, value=value)
        db.add(cfg)
    db.commit()
    return {"key": key, "value": value}


from ..models.proxy_config import ProxyConfig as ProxyConfigModel


# ---------------------------------------------------------------------------
# SSL Certificate endpoints (proxied to provision-api)
# ---------------------------------------------------------------------------

from fastapi import UploadFile, File, Form

@router.get("/ssl-certs")
async def list_ssl_certs(
    current_user: dict = Depends(require_admin),
):
    """List available SSL certificate domains (proxied to provision-api).

    Admin-only (Gap G5): SSL cert inventory is an admin-page concern.
    """
    return await provision_service.list_ssl_certs()


@router.post("/ssl-certs", status_code=201)
async def upload_ssl_cert(
    domain: str = Form(...),
    fullchain: str = Form(""),
    privkey: str = Form(""),
    ssl_path: str = Form(""),
    current_user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upload SSL certificates for a domain (proxied to provision-api).

    Supports two modes:
    - Paste mode: provide ``fullchain`` and ``privkey`` PEM content directly.
    - Path mode: provide ``ssl_path`` to a directory containing fullchain.pem
      and privkey.pem (e.g. /etc/letsencrypt/live/example.com). The
      provision-api reads the files from that path.
    """
    from ..services.audit_service import log_action

    try:
        result = await provision_service.upload_ssl_cert(
            domain=domain,
            fullchain=fullchain,
            privkey=privkey,
            ssl_path=ssl_path or None,
        )
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    log_action(db, action="ssl_upload", admin_id=current_user["id"],
        detail={"domain": domain, "mode": "path" if ssl_path else "paste"}, status="success")
    return result


@router.post("/ssl-certs/{domain}/refresh")
async def refresh_ssl_cert(
    domain: str,
    current_user: dict = Depends(require_admin),
):
    """Refresh SSL certificates for a domain from its original source path."""
    try:
        result = await provision_service.refresh_ssl_cert(domain)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")
    return result


@router.delete("/ssl-certs/{domain}")
async def delete_ssl_cert(
    domain: str,
    current_user: dict = Depends(require_admin),
):
    """Delete SSL certificates for a domain (proxied to provision-api)."""
    return await provision_service.delete_ssl_cert(domain)


# ---------------------------------------------------------------------------
# GET /api/system/subnet-pool  —  subnet pool usage statistics
# ---------------------------------------------------------------------------

@router.get("/subnet-pool")
async def get_subnet_pool(
    current_user: dict = Depends(require_admin),
):
    """Get subnet pool usage statistics for the dashboard (proxied to provision-api).

    Admin-only (Gap G5): pool CIDRs/usage are an admin-dashboard concern.
    """
    try:
        return await provision_service.subnet_pool()
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")

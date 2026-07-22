"""System router — /api/system/* endpoints with real Docker monitoring."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import get_current_admin
from ..models.admin import AdminUser
from ..models.system_config import SystemConfig
from ..services.provision_service import provision_service

router = APIRouter(prefix="/api/system", tags=["system"])

_start_time = time.time()


@router.get("/status")
async def system_status(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Get system health status including per-component Docker stats."""
    provision_api_status = {"status": "unknown", "latency_ms": 0, "version": "unknown"}
    try:
        health = await provision_service.health()
        provision_api_status["status"] = health.get("status", "unknown")
    except Exception:
        provision_api_status["status"] = "unreachable"

    # Per-component status from Docker (via provision-api)
    components = {}
    for name in ["provision-api", "provision-nginx", "provision-gateway", "provision-dashboard"]:
        try:
            running = await provision_service.container_running(name)
            exists = await provision_service.container_exists(name)
        except Exception:
            running = False; exists = False
        components[name] = {
            "running": running,
            "exists": exists,
            "status": "running" if running else ("stopped" if exists else "not found"),
        }

    # Docker info + host stats (via provision-api)
    try:
        host_stats = await provision_service.host_stats()
    except Exception:
        host_stats = {}
    try:
        docker_info_data = await provision_service.docker_info()
    except Exception:
        docker_info_data = {}

    docker_host = {
        "containers_total": docker_info_data.get("containers_total", 0),
        "containers_running": docker_info_data.get("containers_running", 0),
        "cpu_percent": host_stats.get("cpu_percent", 0),
        "mem_percent": host_stats.get("mem_percent", 0),
        "disk_percent": host_stats.get("disk_percent", 0),
    }

    gateway = {
        "version": "1.0.0",
        "uptime_sec": int(time.time() - _start_time),
    }

    # Proxy status
    from ..services.proxy_service import has_active_proxy, get_active_config
    proxy_active = has_active_proxy(db)
    proxy_info = None
    if proxy_active:
        cfg = get_active_config(db)
        if cfg:
            proxy_info = {"url": cfg.get("url", ""), "reachable": cfg.get("reachable")}

    # Compute counts for stat cards
    services_count = 0
    users_count = 0
    tasks_running = 0
    service_stats = {}
    container_stats = {}

    # Service & container stats from provision-api (registry-based, not docker ps)
    try:
        cs = await provision_service.get_container_stats()
        container_stats = cs.get("container_stats", {})
    except Exception:
        pass

    try:
        ss = await provision_service.get_service_stats()
        service_stats = ss.get("service_stats", {})
        services_count = service_stats.get("expected", 0)
    except Exception:
        pass

    try:
        users_result = await provision_service.list_users()
        users_list = users_result.get("user_status", [])
        users_count = len(users_list)
    except Exception:
        pass

    try:
        tasks_result = await provision_service.list_tasks()
        tasks_list = tasks_result if isinstance(tasks_result, list) else tasks_result.get("tasks", [])
        tasks_running = sum(1 for t in tasks_list if t.get("status") in ("pending", "running"))
    except Exception:
        pass

    return {
        "provision_api": provision_api_status,
        "provision_nginx": {"status": components["provision-nginx"]["status"]},
        "docker_host": docker_host,
        "gateway": gateway,
        "components": components,
        "proxy": {"active": proxy_active, "info": proxy_info},
        "services_count": services_count,
        "users_count": users_count,
        "tasks_running": tasks_running,
        "service_stats": service_stats,
        "container_stats": container_stats,
    }


@router.get("/stats")
async def system_stats(
    detail: bool = Query(False),
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Run a full nginx upstream reconciliation (proxied to provision-api)."""
    from ..services.audit_service import log_action

    try:
        result = await provision_service.reconcile()
    except Exception as e:
        log_action(
            db, action="reconcile", admin_id=current_admin.id,
            status="failure", error_message=str(e),
        )
        raise HTTPException(500, f"Reconciliation failed: {e}")

    report = result.get("report", {})
    log_action(
        db, action="reconcile", admin_id=current_admin.id,
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
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get the last reconciliation status (proxied to provision-api)."""
    return await provision_service.reconciliation_status()


@router.get("/nginx-state")
async def get_nginx_state(
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Get all proxy configs plus active state."""
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
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Add a new proxy config. Auto-tests reachability after save."""
    config = create_config(db, req)
    reachability = await test_config_reachability(db, config["id"])
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config["id"]).first().to_dict()

    from ..services.audit_service import log_action
    log_action(db, action="proxy_config_create", admin_id=current_admin.id,
               status="success", detail={"host": config["host"]})

    return {"config": config, "reachability": reachability}


@router.put("/proxy/{config_id}")
async def update_proxy_config(
    config_id: int,
    req: dict[str, Any] = Body(...),
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Update a proxy config. Re-tests reachability."""
    config = update_config(db, config_id, req)
    if not config:
        raise HTTPException(404, "Config not found")
    reachability = await test_config_reachability(db, config_id)
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first().to_dict()
    return {"config": config, "reachability": reachability}


@router.delete("/proxy/{config_id}")
async def delete_proxy_config(
    config_id: int,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Delete a proxy config."""
    if not delete_config(db, config_id):
        raise HTTPException(404, "Config not found")
    return {"deleted": True}


@router.put("/proxy/{config_id}/activate")
async def activate_proxy_config(
    config_id: int,
    current_admin: AdminUser = Depends(get_current_admin),
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
    log_action(db, action="proxy_config_activate", admin_id=current_admin.id,
               status="success", detail={"host": config["host"]})

    return {"activated": True, "config": config}


@router.post("/proxy/deactivate")
async def deactivate_proxy(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Deactivate all proxy configs."""
    deactivate_all(db)
    return {"deactivated": True}


@router.post("/proxy/test")
async def test_proxy_reachability_endpoint(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Test reachability of all proxy configs."""
    results = await test_all_configs(db)
    return {"results": results}


# ---------------------------------------------------------------------------
# System Configuration (key-value store for special_users, etc.)
# ---------------------------------------------------------------------------

@router.get("/config")
def get_system_config(
    key: str = Query(None, description="Config key to fetch, or all if omitted"),
    db: Session = Depends(get_db),
):
    """Get system configuration value(s)."""
    if key:
        cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return {"key": key, "value": cfg.value if cfg else ""}
    cfgs = db.query(SystemConfig).all()
    return {"configs": {c.key: c.value for c in cfgs}}


@router.put("/config")
def set_system_config(
    key: str = Query(...),
    value: str = Body(..., embed=True),
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
):
    """List available SSL certificate domains (proxied to provision-api)."""
    return await provision_service.list_ssl_certs()


@router.post("/ssl-certs", status_code=201)
async def upload_ssl_cert(
    domain: str = Form(...),
    fullchain: str = Form(""),
    privkey: str = Form(""),
    ssl_path: str = Form(""),
    current_admin: AdminUser = Depends(get_current_admin),
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
    log_action(db, action="ssl_upload", admin_id=current_admin.id,
        detail={"domain": domain, "mode": "path" if ssl_path else "paste"}, status="success")
    return result


@router.post("/ssl-certs/{domain}/refresh")
async def refresh_ssl_cert(
    domain: str,
    current_admin: AdminUser = Depends(get_current_admin),
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
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Delete SSL certificates for a domain (proxied to provision-api)."""
    return await provision_service.delete_ssl_cert(domain)

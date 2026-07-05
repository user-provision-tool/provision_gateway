"""Proxy service — multi-config proxy management."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models.proxy_config import ProxyConfig as ProxyConfigModel
from ..utils.crypto import encrypt_api_key, decrypt_api_key


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

def list_configs(db: Session) -> list[dict]:
    configs = db.query(ProxyConfigModel).order_by(ProxyConfigModel.created_at).all()
    return [c.to_dict() for c in configs]


def get_active_config(db: Session) -> dict | None:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.is_active == True).first()
    return config.to_dict(mask_password=False) if config else None


def has_active_proxy(db: Session) -> bool:
    return db.query(ProxyConfigModel).filter(ProxyConfigModel.is_active == True).first() is not None


def create_config(db: Session, data: dict) -> dict:
    config = ProxyConfigModel(
        name=data.get("name", ""),
        protocol=data.get("protocol", "http"),
        host=data.get("host", ""),
        port=int(data.get("port", 8080)),
        username_enc=encrypt_api_key(data.get("username", "")) if data.get("username") else "",
        password_enc=encrypt_api_key(data.get("password", "")) if data.get("password") else "",
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config.to_dict()


def update_config(db: Session, config_id: int, data: dict) -> dict | None:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
    if not config:
        return None
    for field in ("protocol", "host", "name"):
        if field in data:
            setattr(config, field, data[field])
    if "port" in data:
        config.port = int(data["port"])
    if "username" in data:
        config.username_enc = encrypt_api_key(data["username"]) if data["username"] else ""
    if "password" in data:
        config.password_enc = encrypt_api_key(data["password"]) if data["password"] else ""
    config.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(config)
    return config.to_dict()


def delete_config(db: Session, config_id: int) -> bool:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
    if not config:
        return False
    db.delete(config)
    db.commit()
    return True


def activate_config(db: Session, config_id: int) -> dict | None:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
    if not config:
        return None
    if config.reachable != "true":
        return None
    db.query(ProxyConfigModel).filter(ProxyConfigModel.is_active == True).update({"is_active": False})
    config.is_active = True
    db.commit()
    db.refresh(config)
    return config.to_dict()


def deactivate_all(db: Session) -> None:
    db.query(ProxyConfigModel).filter(ProxyConfigModel.is_active == True).update({"is_active": False})
    db.commit()


# ---------------------------------------------------------------------------
# Reachability testing
# ---------------------------------------------------------------------------

async def test_config_reachability(db: Session, config_id: int) -> dict:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.id == config_id).first()
    if not config:
        return {"reachable": None, "error": "Config not found"}
    checked_at = datetime.now(timezone.utc)
    try:
        start = time.monotonic()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(config.host, config.port), timeout=5.0)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        config.reachable = "true"
        config.last_checked_at = checked_at
        config.last_error = None
        db.commit()
        return {"reachable": True, "latency_ms": latency_ms, "error": None, "checked_at": checked_at.isoformat()}
    except asyncio.TimeoutError:
        config.reachable = "false"; config.last_checked_at = checked_at
        config.last_error = "Connection timed out"; db.commit()
        return {"reachable": False, "latency_ms": 0, "error": "Connection timed out", "checked_at": checked_at.isoformat()}
    except ConnectionRefusedError:
        config.reachable = "false"; config.last_checked_at = checked_at
        config.last_error = "Connection refused"; db.commit()
        return {"reachable": False, "latency_ms": 0, "error": "Connection refused", "checked_at": checked_at.isoformat()}
    except OSError as e:
        config.reachable = "false"; config.last_checked_at = checked_at
        config.last_error = str(e); db.commit()
        return {"reachable": False, "latency_ms": 0, "error": str(e), "checked_at": checked_at.isoformat()}


async def test_all_configs(db: Session) -> list[dict]:
    configs = db.query(ProxyConfigModel).all()
    return [await test_config_reachability(db, c.id) for c in configs]


# ---------------------------------------------------------------------------
# Proxy env vars for docker / git
# ---------------------------------------------------------------------------

def _get_active_url(db: Session) -> str:
    config = db.query(ProxyConfigModel).filter(ProxyConfigModel.is_active == True).first()
    if not config:
        return ""
    d = config.to_dict(mask_password=False)
    return d.get("url", "")


def get_proxy_env(db: Session) -> dict[str, str]:
    url = _get_active_url(db)
    if not url:
        return {}
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url}


def inject_proxy_build_args(db: Session, build_args: dict | None, use_global_proxy: bool) -> dict:
    if not use_global_proxy or not has_active_proxy(db):
        return build_args or {}
    proxy_env = get_proxy_env(db)
    if not proxy_env:
        return build_args or {}
    merged = dict(proxy_env)
    if build_args:
        merged.update(build_args)
    return merged


def configure_git_proxy(db: Session) -> None:
    import subprocess
    url = _get_active_url(db)
    if url:
        subprocess.run(["git", "config", "--global", "http.proxy", url], check=False, capture_output=True)
        subprocess.run(["git", "config", "--global", "https.proxy", url], check=False, capture_output=True)
    else:
        subprocess.run(["git", "config", "--global", "--unset", "http.proxy"], check=False, capture_output=True)
        subprocess.run(["git", "config", "--global", "--unset", "https.proxy"], check=False, capture_output=True)

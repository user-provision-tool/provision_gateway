"""Pydantic schemas for system status, stats, config, and reconciliation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── System Status ──

class ComponentStatus(BaseModel):
    running: bool = False
    exists: bool = False
    status: str = "unknown"


class SystemStatusResponse(BaseModel):
    provision_api: dict[str, Any] = Field(default_factory=dict)
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    docker_host: dict[str, Any] = Field(default_factory=dict)
    gateway: dict[str, Any] = Field(default_factory=dict)
    service_stats: dict[str, Any] | None = None
    container_stats: dict[str, Any] | None = None


# ── System Stats ──

class ContainerStatItem(BaseModel):
    name: str = ""
    cpu_percent: float = 0.0
    mem_usage_mb: float = 0.0
    status: str = "unknown"


class SystemStatsResponse(BaseModel):
    containers: list[dict[str, Any]] = Field(default_factory=list)
    disk: dict[str, Any] | None = None
    host: dict[str, Any] | None = None


# ── Reconciliation ──

class ReconcileStatusResponse(BaseModel):
    last_run: str | None = None
    result: dict[str, Any] | None = None


# ── System Config ──

class SystemConfigUpdateRequest(BaseModel):
    key: str
    value: str


class SystemConfigResponse(BaseModel):
    key: str
    value: str

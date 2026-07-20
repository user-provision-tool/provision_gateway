"""Pydantic schemas for proxy configuration endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Proxy Config ──

class ProxyConfigCreate(BaseModel):
    """Create a new proxy configuration."""
    name: str = Field(default="", description="Display name, e.g. 'Host Proxy'")
    protocol: str = Field(default="http", description="'http' | 'https' | 'socks5'")
    host: str = Field(description="Proxy hostname or IP")
    port: int = Field(default=8080, ge=1, le=65535, description="Proxy port")
    username: str = Field(default="", description="Optional — encrypted at rest")
    password: str = Field(default="", description="Optional — encrypted at rest")


class ProxyConfigUpdate(BaseModel):
    """Update an existing proxy configuration (all fields optional)."""
    name: str | None = None
    protocol: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None


class ProxyConfigResponse(BaseModel):
    """Proxy configuration as returned by the API."""
    id: int
    name: str | None = None
    protocol: str = "http"
    host: str = ""
    port: int = 8080
    username: str = ""
    password_masked: str = ""
    url: str = ""
    is_active: bool = False
    reachable: bool | None = None
    last_checked_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class ProxyConfigListResponse(BaseModel):
    """List of all proxy configurations."""
    configs: list[ProxyConfigResponse]


class ProxyConfigUpdateResponse(BaseModel):
    """Response after updating a proxy config."""
    updated: bool = True
    config: ProxyConfigResponse


class ProxyConfigDeleteResponse(BaseModel):
    """Response after deleting a proxy config."""
    deleted: bool = True


class ProxyConfigActivateResponse(BaseModel):
    """Response after activating a proxy config."""
    activated: bool = True
    config_id: int


class ProxyConfigDeactivateResponse(BaseModel):
    """Response after deactivating all proxy configs."""
    deactivated: bool = True


class ProxyConfigCreateResponse(BaseModel):
    """Response after creating a proxy config."""
    id: int
    name: str | None = None
    protocol: str
    host: str
    port: int
    username: str
    password_masked: str
    url: str
    is_active: bool
    reachable: bool | None = None
    last_checked_at: str | None = None


# ── Proxy Test ──

class ProxyTestResult(BaseModel):
    """Reachability test result for a single proxy config."""
    config_id: int | None = None
    reachable: bool
    latency_ms: float = 0
    error: str | None = None
    checked_at: str | None = None


class ProxyTestResponse(BaseModel):
    """Response from testing all proxy configs."""
    results: list[ProxyTestResult] = Field(default_factory=list)

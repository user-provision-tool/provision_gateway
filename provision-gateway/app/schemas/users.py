"""Pydantic schemas for user provisioning endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DeployRequest(BaseModel):
    """Request to deploy a service to a user."""
    user_name: str
    service_name: str
    project_root: str
    compose_template_path: str = Field(alias="compose_file_path", default="")
    nginx_conf_template_path: str = Field(alias="nginx_conf_file_path", default="")
    env_file_path: str = ""
    label: str = "0"
    domain: str = "localhost"
    passwd: str = ""
    volumes: dict[str, str] = Field(default_factory=dict)
    build_args: dict[str, str] = Field(default_factory=dict)
    use_global_proxy: bool = False
    https: bool = False
    fullchain: str | None = None
    privkey: str | None = None

    model_config = {"populate_by_name": True}


class CloneRequest(BaseModel):
    """Request to clone all services from one user to another."""
    source_user: str
    target_user: str
    domain: str = "localhost"
    passwd: str = ""
    volume_base_override: str | None = None


class RebuildRequest(BaseModel):
    """Request to rebuild a service."""
    no_cache: bool = True
    build_args: dict[str, str] = Field(default_factory=dict)


class PasswordChangeRequest(BaseModel):
    """Request to change a service's password."""
    passwd: str


class CurlTestRequest(BaseModel):
    """Request to test a service URL with curl."""
    include_auth: bool = False
    follow_redirect: bool = True


class DeploymentFileUpdateRequest(BaseModel):
    """Request to update a deployment file's content."""
    content: str


class TaskResponse(BaseModel):
    """Async task status."""
    task_id: str = ""
    status: str = "pending"
    type: str = ""
    message: str = ""


class CloneResponse(BaseModel):
    """Response from cloning all services."""
    tasks: list[dict[str, str]] = Field(default_factory=list)
    total: int = 0


class ServiceURLResponse(BaseModel):
    """Service URL information."""
    url: str = ""
    http_url: str = ""
    https_enabled: bool = False
    auth_enabled: bool = False
    nginx_http_port: int = 80
    nginx_https_port: int = 443


class CurlTestResponse(BaseModel):
    """Result from testing a service URL with curl."""
    url: str = ""
    http_code: int = 0
    headers: dict[str, str] = Field(default_factory=dict)
    body_preview: str = ""
    time_total_ms: float = 0
    error: str | None = None


class ContainerLogsResponse(BaseModel):
    """Container logs response."""
    logs: str = ""


class RegistrationTimeResponse(BaseModel):
    """Service registration timestamp."""
    user_name: str = ""
    service_name: str = ""
    label: str = ""
    registration_time: float | None = None


class DeploymentFilesResponse(BaseModel):
    """List of deployment files and their metadata."""
    files: list[dict[str, Any]] = Field(default_factory=list)


class DeploymentFileResponse(BaseModel):
    """Single deployment file content."""
    content: str = ""
    path: str = ""

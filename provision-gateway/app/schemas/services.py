"""Pydantic schemas for service project endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ServiceCreateGit(BaseModel):
    """Create service from a Git repository."""
    mode: str = "git"
    repo_url: str
    branch: str = "main"
    name: str
    use_proxy: bool = False


class ServiceCreateUpload(BaseModel):
    """Create service from file upload."""
    mode: str = "upload"
    name: str


class ServiceCreateTemplate(BaseModel):
    """Create service from a template."""
    mode: str = "template"
    template_id: int
    name: str


class ServiceFileUpdate(BaseModel):
    """Update a service file's content."""
    content: str


class ServiceConvertRequest(BaseModel):
    """Request to convert compose/nginx files to Jinja2 templates."""
    compose_file: str = "docker-compose.yml"
    nginx_file: str = "nginx.conf"


class ServiceCheckDeployRequest(BaseModel):
    """Check if a service is ready to deploy."""
    name: str


class ServiceSaveGeneratedRequest(BaseModel):
    """Save LLM-generated files for a service."""
    name: str
    files: dict[str, str] = Field(default_factory=dict, description="filename → content mapping")


class ServiceResponse(BaseModel):
    """Service project as returned by the API."""
    name: str
    path: str = ""
    files: list[str] = Field(default_factory=list)
    has_compose_template: bool = False
    has_nginx_template: bool = False
    active_users: int = 0
    active_instances: list[str] = Field(default_factory=list)
    created_at: str | None = None


class ServiceListResponse(BaseModel):
    """List of service projects."""
    services: list[ServiceResponse]


class ServiceConvertResponse(BaseModel):
    """Response after converting compose/nginx to templates."""
    compose_template: str = ""
    nginx_template: str = ""


class ServiceDeleteResponse(BaseModel):
    """Response after deleting a service."""
    deleted: bool = True


class GitStatusResponse(BaseModel):
    """Git status for a service project."""
    status: str = ""
    files: list[dict[str, str]] = Field(default_factory=list)


class GitDiffResponse(BaseModel):
    """Git diff for a service project."""
    diff: str = ""


class GitHeadFileResponse(BaseModel):
    """File content from HEAD revision."""
    content: str = ""
    filename: str = ""

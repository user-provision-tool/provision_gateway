"""Pydantic schemas for LLM configuration and generation endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── LLM Config ──

class LLMConfigBase(BaseModel):
    """Shared fields for LLM configuration."""
    mode: str = Field(default="byok", description="'local_agent' | 'byok'")
    agent_url: str = Field(default="", description="Local agent URL, e.g. http://localhost:11434/v1")
    agent_model: str = Field(default="", description="Local agent model name, e.g. llama3.1:8b")
    byok_api_key: str | None = Field(default=None, description="BYOK API key (write-only)")
    byok_base_url: str = Field(default="", description="BYOK base URL, e.g. https://api.deepseek.com/v1")
    byok_model: str = Field(default="", description="BYOK model name, e.g. deepseek-chat")
    system_prompt: str = Field(default="", description="Custom system prompt for config generation")


class LLMConfigCreate(LLMConfigBase):
    """Create a new LLM configuration."""
    pass


class LLMConfigUpdate(BaseModel):
    """Update an existing LLM configuration (all fields optional)."""
    mode: str | None = None
    agent_url: str | None = None
    agent_model: str | None = None
    byok_api_key: str | None = None
    byok_base_url: str | None = None
    byok_model: str | None = None
    system_prompt: str | None = None


class LLMConfigResponse(BaseModel):
    """LLM configuration as returned by the API."""
    id: int
    mode: str
    agent_url: str | None = None
    agent_model: str | None = None
    byok_configured: bool = False
    byok_model: str | None = None
    byok_api_key_masked: str | None = None
    byok_base_url: str | None = None
    is_active: bool = False
    system_prompt: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class LLMConfigListResponse(BaseModel):
    """List of LLM configurations."""
    configs: list[LLMConfigResponse]


class LLMConfigActivateResponse(BaseModel):
    """Response after activating an LLM config."""
    activated: bool
    config_id: int


# ── LLM Test ──

class LLMTestResponse(BaseModel):
    """Response from testing LLM connection."""
    success: bool
    latency_ms: float = 0
    model: str = ""
    response_preview: str = ""


# ── LLM Generate ──

class GenerateContext(BaseModel):
    """Context for LLM config generation (repo analysis results)."""
    repo_description: str = Field(default="", description="Description of the repository")
    repo_files: list[str] = Field(default_factory=list, description="Files found in the repo")
    port: int | None = Field(default=None, description="Detected application port")
    needs_db: bool = Field(default=False, description="Whether a database is needed")
    needs_cache: bool = Field(default=False, description="Whether a cache (Redis/etc) is needed")


class GenerateRequest(BaseModel):
    """Request to generate a config file via LLM."""
    type: str = Field(description="File type: 'docker_compose' | 'nginx_conf' | 'env_file' | 'dockerfile' | 'service_config'")
    context: GenerateContext = Field(default_factory=GenerateContext)


class GenerateResponse(BaseModel):
    """Response from LLM config generation."""
    generated_content: str
    filename_suggestion: str = ""
    warnings: list[str] = Field(default_factory=list)

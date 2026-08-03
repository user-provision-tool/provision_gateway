"""LLMConfig ORM model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base


class LLMConfig(Base):
    __tablename__ = "llm_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(50), nullable=False, default="byok")  # 'byok' only; 'local_agent' is a future feature (GAP-2)
    agent_url = Column(String(500), nullable=True)
    agent_model = Column(String(255), nullable=True)
    byok_api_key_enc = Column(Text, nullable=True)  # AES-256-GCM encrypted
    byok_base_url = Column(String(500), nullable=True)
    byok_model = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)  # only one at a time
    system_prompt = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self, mask_key: bool = True, include_agent_fields: bool = False) -> dict:
        """Serialize config to dict.

        Agent fields (agent_url, agent_model, system_prompt) are excluded by default
        because they are for future use (local_agent/provision_agent modes) and are
        not exposed in the current BYOK-only UI. Pass include_agent_fields=True to
        include them (used internally when needed).
        """
        result = {
            "id": self.id,
            "mode": self.mode,
            "byok_configured": bool(self.byok_api_key_enc),
            "byok_base_url": self.byok_base_url,
            "byok_model": self.byok_model,
            "is_active": self.is_active,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_agent_fields:
            result["agent_url"] = self.agent_url
            result["agent_model"] = self.agent_model
            result["system_prompt"] = self.system_prompt
        if mask_key and self.byok_api_key_enc:
            result["byok_api_key_masked"] = "sk-...xxxx"
        return result

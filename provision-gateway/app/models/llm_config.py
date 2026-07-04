"""LLMConfig ORM model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base


class LLMConfig(Base):
    __tablename__ = "llm_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(50), nullable=False, default="local_agent")  # 'local_agent' | 'byok'
    agent_url = Column(String(500), nullable=True)
    agent_model = Column(String(255), nullable=True)
    byok_api_key_enc = Column(Text, nullable=True)  # AES-256-GCM encrypted
    byok_base_url = Column(String(500), nullable=True)
    byok_model = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    system_prompt = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self, mask_key: bool = True) -> dict:
        result = {
            "id": self.id,
            "mode": self.mode,
            "agent_url": self.agent_url,
            "agent_model": self.agent_model,
            "byok_configured": bool(self.byok_api_key_enc),
            "byok_base_url": self.byok_base_url,
            "byok_model": self.byok_model,
            "is_active": self.is_active,
            "system_prompt": self.system_prompt,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if mask_key and self.byok_api_key_enc:
            result["byok_api_key_masked"] = "sk-...xxxx"
        return result

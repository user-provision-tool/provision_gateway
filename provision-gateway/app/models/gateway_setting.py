"""GatewaySetting ORM model (key-value store)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text

from ..database import Base


class GatewaySetting(Base):
    __tablename__ = "gateway_settings"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

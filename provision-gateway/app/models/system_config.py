"""SystemConfig ORM model — key-value store for gateway settings."""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text

from ..database import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(255), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=False, default="")

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value}

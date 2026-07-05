"""ProxyConfig ORM model — multiple saved proxy configurations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base


class ProxyConfig(Base):
    """A saved proxy configuration. Only one row can have is_active=True at a time."""

    __tablename__ = "proxy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=True)  # optional display name
    protocol = Column(String(50), nullable=False, default="http")
    host = Column(String(500), nullable=False)
    port = Column(Integer, nullable=False, default=8080)
    username_enc = Column(Text, nullable=True)  # AES-256-GCM encrypted
    password_enc = Column(Text, nullable=True)  # AES-256-GCM encrypted
    is_active = Column(Boolean, nullable=False, default=False)  # only one at a time
    reachable = Column(String(20), nullable=True)  # "true" | "false" | null
    last_checked_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self, mask_password: bool = True) -> dict:
        from ..utils.crypto import decrypt_api_key
        username = decrypt_api_key(self.username_enc or "")
        password = decrypt_api_key(self.password_enc or "")
        if not self.host:
            url = ""
        elif username and password:
            url = f"{self.protocol}://{username}:{'****' if mask_password else password}@{self.host}:{self.port}"
        else:
            url = f"{self.protocol}://{self.host}:{self.port}"

        reachable = None
        if self.reachable == "true":
            reachable = True
        elif self.reachable == "false":
            reachable = False

        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "username": username if not mask_password else "",
            "password_masked": "****" if password and mask_password else "",
            "url": url,
            "is_active": self.is_active,
            "reachable": reachable,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

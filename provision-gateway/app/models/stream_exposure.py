"""Stream exposure handles — service_id and users.id keyed (T6)."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID

from ..database import Base
from . import registry_ref  # noqa: F401 — registers the FK target tables


class StreamExposure(Base):
    __tablename__ = "stream_exposures"
    __table_args__ = (
        Index(
            "uq_stream_exposures_one_live_per_row_py",
            "service_id", "sub_service", "port",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": "gateway"},
    )

    handle_hash = Column(Text, primary_key=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=False, index=True)
    service_id = Column(UUID(as_uuid=True), ForeignKey("registry.services.id"), nullable=False, index=True)
    sub_service = Column(Text, nullable=False)
    port = Column(Integer, nullable=False)
    canonical_name = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "service_id": str(self.service_id),
            "owner_user_id": str(self.owner_user_id),
            "sub_service": self.sub_service,
            "port": self.port,
            "canonical_name": self.canonical_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "live": self.revoked_at is None,
        }

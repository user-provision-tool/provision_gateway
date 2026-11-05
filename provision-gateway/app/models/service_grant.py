"""Directional service grants (G83)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from ..database import Base


class ServiceGrant(Base):
    __tablename__ = "service_grants"
    __table_args__ = (
        UniqueConstraint("source_service_id", "target_service_id",
                         name="service_grants_pair"),
        Index("service_grants_by_source", "source_service_id"),
        Index("service_grants_by_target", "target_service_id"),
        {"schema": "gateway"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_service_id = Column(
        UUID(as_uuid=True), ForeignKey("registry.services.id"), nullable=False, index=True,
    )
    target_service_id = Column(
        UUID(as_uuid=True), ForeignKey("registry.services.id"), nullable=False, index=True,
    )
    created_by = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_service_id": str(self.source_service_id),
            "target_service_id": str(self.target_service_id),
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

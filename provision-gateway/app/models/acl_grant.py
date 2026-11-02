"""ACL grants — user-id keyed (T6)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from ..database import Base

STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
GRANT_STATES = (STATE_ACTIVE, STATE_INACTIVE)


class AclGrant(Base):
    __tablename__ = "acl_grants"
    __table_args__ = (
        UniqueConstraint("grantee_user_id", "shared_user_id", name="uq_acl_grants_pair"),
        Index("ix_acl_grants_grantee_state", "grantee_user_id", "state"),
        Index("ix_acl_grants_shared_state", "shared_user_id", "state"),
        {"schema": "gateway"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    grantee_user_id = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=False, index=True)
    shared_user_id = Column(UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=False, index=True)
    state = Column(String(16), nullable=False, default=STATE_ACTIVE)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "grantee_user_id": str(self.grantee_user_id),
            "shared_user_id": str(self.shared_user_id),
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

"""EndUser ORM model — registered end-users for service deployment."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from ..database import Base


class EndUser(Base):
    """A registered end-user who can have services deployed for them."""

    __tablename__ = "end_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="viewer")  # 'viewer' | 'special'
    is_approved = Column(Boolean, nullable=False, default=False)  # admin must approve
    is_active = Column(Boolean, nullable=False, default=True)
    # A public functional user: every HTTP endpoint this account owns is
    # reachable without a platform token. The flag is target-owned; it is not a
    # grant and it does not publish stream rows (GRILLING G77).
    is_public_shared = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Soft delete (GRILLING G78): the row remains, the username stays reserved,
    # and the id stays stable. The effective "deleted" test is this column, not
    # a missing row.
    deleted_at = Column(DateTime, nullable=True, index=True)
    # LEGACY — migrated to acl_grants on startup and never read by the ACL.
    # Kept only so an upgraded database can be read by the one-time migration;
    # new rows never populate it and the migration clears it after conversion.
    allowed_special_users = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    approved_at = Column(DateTime, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "is_approved": self.is_approved,
            "is_active": self.is_active,
            "is_public_shared": bool(self.is_public_shared),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
        }

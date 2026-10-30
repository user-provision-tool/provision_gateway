"""AclGrant ORM model — one ID-keyed grantee/shared pair (GRILLING G78).

The ACL matrix used to be a comma-separated ``allowed_special_users`` string on
each ``EndUser``. A string cannot carry a per-row lifecycle (active/inactive),
cannot be cleaned up when either account is deleted, and a reused username
silently inherits the old grant.

This table replaces it:

* one row per ``(grantee_user_id, shared_user_id)`` pair, unique;
* both columns are ``end_users.id`` — the stable account id, never a username;
* ``state`` is ``active`` or ``inactive``; an inactive row is kept (the admin
  sees a greyed tag and toggles it), while a deleted account's rows are removed;
* the ACL query reads active rows only, and additionally requires both accounts
  to be active and not deleted (``acl_service.allows``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from ..database import Base

STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
GRANT_STATES = (STATE_ACTIVE, STATE_INACTIVE)


class AclGrant(Base):
    """A grant from a grantee account to a shared account."""

    __tablename__ = "acl_grants"
    __table_args__ = (
        UniqueConstraint("grantee_user_id", "shared_user_id",
                         name="uq_acl_grants_pair"),
        Index("ix_acl_grants_grantee_state", "grantee_user_id", "state"),
        Index("ix_acl_grants_shared_state", "shared_user_id", "state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    grantee_user_id = Column(
        Integer, ForeignKey("end_users.id"), nullable=False, index=True
    )
    shared_user_id = Column(
        Integer, ForeignKey("end_users.id"), nullable=False, index=True
    )
    state = Column(
        String(16), nullable=False, default=STATE_ACTIVE,
        server_default=STATE_ACTIVE,
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "grantee_user_id": self.grantee_user_id,
            "shared_user_id": self.shared_user_id,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

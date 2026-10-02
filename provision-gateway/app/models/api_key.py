"""ApiKey ORM model — API tokens for end-user service access."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, ForeignKey, Index, text

from ..database import Base


class ApiKey(Base):
    """A long-lived JWT API key for an end-user **or an admin**.

    Multiple named keys per user with user-defined labels.
    Keys are revocable individually.
    Default expiry: 1 year from creation.

    A key's target is a **prefixed reference** — ``end_user:<id>`` / ``admin:<id>``
    (a bare ``<id>`` means ``end_user``; see ``auth_service.parse_target_ref``).
    It is stored as the pair ``(user_id, target_type)`` because ``admins`` and
    ``end_users`` are separate id spaces: id 1 exists in BOTH, so an unprefixed id
    is ambiguous — ``_target_identity`` used to resolve ``EndUser`` first, which
    silently turned an admin's "own" key into one belonging to whichever end user
    held that id. Filters must carry both halves too, or one id space's keys are
    listed to the other.

    ``user_id`` keeps its legacy ``end_users.id`` foreign-key declaration; for a
    ``target_type='admin'`` row it holds an ``admins.id`` instead.

    Exactly one default key per *target* is enforced at the DB level by a partial
    unique index on ``(user_id, target_type)`` WHERE ``is_default`` (v4
    §6.1.3/§6.1.5).
    """

    __tablename__ = "api_keys"

    __table_args__ = (
        Index(
            "uq_api_keys_one_default",
            "user_id",
            "target_type",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("end_users.id"), nullable=False, index=True)
    # Which id space ``user_id`` belongs to: 'end_user' | 'admin'.
    target_type = Column(String(16), nullable=False, default="end_user",
                         server_default="end_user")
    label = Column(String(255), nullable=False)
    # The key itself is a 1-year JWT provision token carrying its own api_key_id.
    # Only the hash + a short mask are stored (v4 §6.1.1-6.1.3).
    token_hash = Column(String(255), nullable=False, unique=True)
    mask = Column(String(255), nullable=True)
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
    is_revoked = Column(Boolean, default=False, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "target_type": self.target_type,
            "label": self.label,
            "mask": self.mask,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_revoked": self.is_revoked,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }

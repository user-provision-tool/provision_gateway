"""ApiKey ORM model — API tokens for end-user service access."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, ForeignKey, Index, text

from ..database import Base


class ApiKey(Base):
    """A long-lived JWT API key for an end-user.

    Multiple named keys per user with user-defined labels.
    Keys are revocable individually.
    Default expiry: 1 year from creation.

    Exactly one default key per user is enforced at the DB level by a partial
    unique index on ``user_id`` WHERE ``is_default`` (v4 §6.1.3/§6.1.5).
    """

    __tablename__ = "api_keys"

    __table_args__ = (
        Index(
            "uq_api_keys_one_default",
            "user_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("end_users.id"), nullable=False, index=True)
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
            "label": self.label,
            "mask": self.mask,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_revoked": self.is_revoked,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }

"""Login identities — one row per unique login string (T6/R5.4)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import CITEXT, UUID

from ..database import Base


class UserLogin(Base):
    __tablename__ = "user_logins"
    __table_args__ = {"schema": "gateway"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("gateway.users.id"), nullable=False, index=True
    )
    login = Column(CITEXT, nullable=False, unique=True)
    kind = Column(String(8), nullable=False)  # username | email

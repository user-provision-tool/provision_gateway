"""Read-only registry references used only to resolve cross-schema FKs.

The -api owns and migrates these tables; the gateway never creates them and
never writes them through these mappings (`Base.metadata.create_all` is not
used). They exist so SQLAlchemy can order/validate the gateway FKs.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import CITEXT, UUID

from ..database import Base


class ApiUser(Base):
    __tablename__ = "api_users"
    __table_args__ = {"schema": "registry"}

    id = Column(UUID(as_uuid=True), primary_key=True)
    username = Column(CITEXT, nullable=False, unique=True)
    state = Column(Text, nullable=False)
    pending_since = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class Service(Base):
    __tablename__ = "services"
    __table_args__ = {"schema": "registry"}

    id = Column(UUID(as_uuid=True), primary_key=True)
    owner_api_user_id = Column(UUID(as_uuid=True), nullable=False)
    service_name = Column(Text, nullable=False)
    label = Column(Text, nullable=False)
    recipe_path = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

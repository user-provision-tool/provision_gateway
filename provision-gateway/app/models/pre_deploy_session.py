"""Pre-deploy session — the gateway-side projection of one service_id (T8)."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ..database import Base

STATE_GENERATING = "generating"
STATE_PAUSED = "paused"
STATE_REFUSED = "refused"
STATE_GENERATED = "generated"
STATE_DEPLOYING = "deploying"
STATE_FAILED = "failed"
STATES = (STATE_GENERATING, STATE_PAUSED, STATE_REFUSED, STATE_GENERATED,
          STATE_DEPLOYING, STATE_FAILED)
OPERATOR_HELD = (STATE_PAUSED, STATE_REFUSED, STATE_GENERATED, STATE_FAILED)

OPERATION_DEPLOY = "deploy"
OPERATION_GENERATE = "generate"
OPERATIONS = (OPERATION_DEPLOY, OPERATION_GENERATE)


class PreDeploySession(Base):
    __tablename__ = "pre_deploy_sessions"
    __table_args__ = {"schema": "gateway"}

    service_id = Column(UUID(as_uuid=True), primary_key=True)
    procedure_id = Column(UUID(as_uuid=True), nullable=False)
    owner_api_user_id = Column(UUID(as_uuid=True), ForeignKey("registry.api_users.id"), nullable=False)
    service_name = Column(Text, nullable=False)
    service_label = Column(Text, nullable=False)
    recipe_path = Column(Text, nullable=False, default="")
    state = Column(Text, nullable=False, default=STATE_GENERATING, index=True)
    # The intake operation travels with the record across its whole async life.
    operation = Column(Text, nullable=False, default=OPERATION_DEPLOY)
    reason = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)
    options = Column(JSONB, nullable=True)
    options_secret_enc = Column(LargeBinary, nullable=True)
    options_key_version = Column(Integer, nullable=True)
    # The deploy intent (non-secret) and its encrypted credentials; the
    # effective selection and env name (R2/R3/R8).
    deploy_request = Column(JSONB, nullable=True)
    deploy_secret_enc = Column(LargeBinary, nullable=True)
    deploy_key_version = Column(Integer, nullable=True)
    selection = Column(JSONB, nullable=True)
    env_name = Column(Text, nullable=True)
    generation_task_id = Column(
        Text, ForeignKey("gateway.tasks.task_id", ondelete="SET NULL"), nullable=True
    )
    deploy_task_id = Column(Text, nullable=True)
    guide = Column(Text, nullable=True)
    slot = Column(Text, nullable=True)
    use_global_proxy = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, username: str | None = None) -> dict:
        return {
            "service_id": str(self.service_id),
            "procedure_id": str(self.procedure_id),
            "owner_api_user_id": str(self.owner_api_user_id),
            "user_name": username,
            "service_name": self.service_name,
            "label": self.service_label,
            "recipe_path": self.recipe_path or "",
            "state": self.state,
            "operation": self.operation or OPERATION_DEPLOY,
            "reason": self.reason or "",
            "detail": self.detail or "",
            "options": self.options,
            "selection": self.selection,
            "env_name": self.env_name or "",
            "generation_task_id": self.generation_task_id,
            "deploy_task_id": self.deploy_task_id,
            "guide": self.guide or "",
            "slot": self.slot or "",
            "use_global_proxy": bool(self.use_global_proxy),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

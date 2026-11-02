"""Stream exposure handles — owner/admin surface and the edge verdict (T9)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import require_gateway_token
from ..models.user import User
from ..services import stream_service

router = APIRouter(prefix="/api/stream", tags=["stream"])


class MintRequest(BaseModel):
    service_id: str = Field(..., min_length=1)
    sub_service: str = Field(..., min_length=1)
    port: int | None = Field(None, ge=1, le=65535)


class VerifyRequest(BaseModel):
    handle: str = ""
    sub_service: str | None = None
    port: int | None = Field(None, ge=1, le=65535)


def _entry(request: Request, service_id: str) -> dict:
    registry = getattr(request.app.state, "registry", None)
    entry = registry.get_by_id(service_id) if registry else None
    if entry is None or entry.get("service_deleted_at") is not None:
        raise HTTPException(404, f"No such service: {service_id}")
    return entry


def _owner_user(db: Session, entry: dict) -> User | None:
    return db.query(User).filter(User.api_user_id == entry["owner_api_user_id"]).first()


def _require_owner_or_admin(db: Session, entry: dict, requester: dict) -> User:
    owner = _owner_user(db, entry)
    if requester["role"] == "admin":
        if owner is None:
            raise HTTPException(409, "Instance owner has no gateway account")
        return owner
    if owner is None or str(owner.id) != requester["id"]:
        raise HTTPException(403, "Only the service owner or an admin may manage exposures")
    return owner


def _stream_row(entry: dict, sub_service: str, port: int | None) -> int:
    for row in entry.get("stream_rows") or []:
        if row.get("sub_service") == sub_service:
            resolved = int(row["port"]) if row.get("port") is not None else None
            if resolved is None:
                raise HTTPException(409, "stream row has no recorded port")
            if port is not None and int(port) != resolved:
                raise HTTPException(409, "port disagrees with the recorded stream row")
            return resolved
    raise HTTPException(409, f"sub-service {sub_service!r} is not a stream endpoint of this service")


@router.post("/exposures")
async def mint_exposure(request: Request, payload: MintRequest,
                        current_user: dict = Depends(require_gateway_token),
                        db: Session = Depends(get_db)):
    """Mint (or ROTATE) the handle of one stream row; the new URL is shown once."""
    entry = _entry(request, payload.service_id)
    owner = _require_owner_or_admin(db, entry, current_user)
    resolved_port = _stream_row(entry, payload.sub_service, payload.port)
    host, row = stream_service.mint(
        db, owner_user_id=owner.id, entry=entry,
        sub_service=payload.sub_service, port=resolved_port,
    )
    return {
        "host": host,
        "stream_port": settings.STREAM_PORT,
        "address": f"{host}:{settings.STREAM_PORT}",
        "canonical_name": row.canonical_name,
        "row": {"sub_service": row.sub_service, "port": row.port},
        "shown_once": True,
    }


@router.get("/exposures")
async def list_exposures(request: Request,
                         service_id: str = Query(...),
                         current_user: dict = Depends(require_gateway_token),
                         db: Session = Depends(get_db)):
    entry = _entry(request, service_id)
    _require_owner_or_admin(db, entry, current_user)
    rows = stream_service.list_for_instance(db, service_id)
    return {"rows": [r.to_dict() for r in rows]}


@router.delete("/exposures")
async def revoke_exposure(request: Request,
                          service_id: str = Query(...),
                          sub_service: str = Query(...),
                          port: int = Query(..., ge=1, le=65535),
                          current_user: dict = Depends(require_gateway_token),
                          db: Session = Depends(get_db)):
    # The service may already be tombstoned: the delete sweep revokes after.
    entry = request.app.state.registry.get_by_id(service_id)
    if entry is not None:
        _require_owner_or_admin(db, entry, current_user)
    elif current_user["role"] != "admin":
        raise HTTPException(403, "Only an admin may revoke exposures of a deleted service")
    return {"revoked": stream_service.revoke_row(db, service_id, sub_service, port)}


@router.post("/verify")
async def verify_stream(request: Request, payload: VerifyRequest,
                        db: Session = Depends(get_db)):
    registry = getattr(request.app.state, "registry", None)
    return stream_service.verify(
        db, registry, payload.handle,
        requested_sub_service=payload.sub_service, requested_port=payload.port,
    )

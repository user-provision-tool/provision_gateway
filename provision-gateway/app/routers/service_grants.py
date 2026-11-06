"""Service-grant admin API and the read-only card view (GRILLING G83).

The admin assigns; the owner sees read-only; a granted viewer sees neither
grants nor keys. Every change is audited and triggers the grant-driven refresh
(resolver config + the source's resolv.conf/membership).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..database import SessionLocal
from ..middleware import require_admin, require_gateway_token
from ..services import audit_service, service_grants
from ..services.cross_orchestrator import cross_orchestrator
from ..services.registry import Registry

router = APIRouter(prefix="/api/service-grants", tags=["service-grants"])


def _entry_or_404(service_id: str) -> dict:
    entry = Registry().get_by_id(service_id)
    if entry is None:
        raise HTTPException(404, "service not found")
    return entry


@router.get("/candidates")
def get_account_candidates(user_name: str,
                           current_user: dict = Depends(require_gateway_token)):
    """Accessible-account groups for a deploying user (R10).

    Declared before `/{service_id}` so the literal path wins.
    """
    db = SessionLocal()
    try:
        return service_grants.account_candidates(db, user_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        db.close()


@router.get("/{service_id}")
def get_service_grants(service_id: str,
                       current_user: dict = Depends(require_gateway_token)):
    entry = _entry_or_404(service_id)
    is_owner = str(entry["owner_api_user_id"]) == str(current_user["api_user_id"])
    if current_user["role"] != "admin" and not is_owner:
        # A granted viewer sees neither keys nor grants.
        raise HTTPException(403, "Only the service owner or an admin may see the grants")
    db = SessionLocal()
    try:
        return service_grants.list_for_source(db, entry)
    finally:
        db.close()


@router.get("/{service_id}/candidates")
def get_service_grant_candidates(service_id: str,
                                 current_admin: dict = Depends(require_admin)):
    entry = _entry_or_404(service_id)
    db = SessionLocal()
    try:
        return {"service_id": service_id,
                "candidates": service_grants.candidates(db, entry)}
    finally:
        db.close()


@router.put("/{source_id}")
async def apply_service_grant_set(source_id: str, body: dict,
                                  current_admin: dict = Depends(require_admin)):
    """Replace the source's explicit target set in one transaction (R10)."""
    targets = body.get("target_service_ids")
    if targets is None:
        targets = body.get("targets")
    if not isinstance(targets, list):
        raise HTTPException(422, "'target_service_ids' must be a list")
    db = SessionLocal()
    try:
        try:
            result = service_grants.apply_set(
                db, source_id, targets, actor_user_id=current_admin["id"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit_service.log_action(
            db, action="service_grant_set", actor_user_id=current_admin["id"],
            target_service_id=source_id,
            detail={"source_service_id": source_id,
                    "target_service_ids": [str(t) for t in targets],
                    "mode": "set"},
            status="success",
        )
    finally:
        db.close()
    await cross_orchestrator.sync_all()
    return {"updated": True, **result}


@router.put("/{source_id}/{target_id}")
async def set_service_grant(source_id: str, target_id: str,
                            current_admin: dict = Depends(require_admin)):
    db = SessionLocal()
    try:
        try:
            row = service_grants.set_grant(db, source_id, target_id,
                                           actor_user_id=current_admin["id"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit_service.log_action(
            db, action="service_grant_set", actor_user_id=current_admin["id"],
            target_service_id=source_id,
            detail={"source_service_id": source_id, "target_service_id": target_id},
            status="success",
        )
        entry = Registry().get_by_id(source_id)
        result = service_grants.list_for_source(db, entry) if entry else {}
        result["grant"] = row
    finally:
        db.close()
    await cross_orchestrator.sync_all()
    return {"updated": True, **result}


@router.delete("/{source_id}/{target_id}")
async def remove_service_grant(source_id: str, target_id: str,
                               current_admin: dict = Depends(require_admin)):
    db = SessionLocal()
    try:
        removed = service_grants.remove_grant(db, source_id, target_id)
        audit_service.log_action(
            db, action="service_grant_remove", actor_user_id=current_admin["id"],
            target_service_id=source_id,
            detail={"source_service_id": source_id, "target_service_id": target_id},
            status="success" if removed else "noop",
        )
        entry = Registry().get_by_id(source_id)
        result = service_grants.list_for_source(db, entry) if entry else {}
    finally:
        db.close()
    await cross_orchestrator.sync_all()
    return {"removed": removed, **result}

"""Service grants and the effective cross-path set (GRILLING G83/G84).

A **service grant** is one directional admin-assigned row: the source service
may call the target service. Same-owner targets are implicit. Every
**cross-owner** target — public or private — needs an explicit row (G84); the
account grant is its prerequisite for a private target and is waived for a
public one. There is no transitivity.

The effective set is what drives everything else: the dnsmasq wildcard, each
source's resolv.conf/membership, and the gateway's `cross-verify`. It is
**same-owner + explicit grants only** (G84): a public target enters no source's
set unless it is explicitly granted, so a public target never makes a service a
source.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.service_grant import ServiceGrant
from ..models.user import User
from . import acl_service
from .registry import Registry

_log = logging.getLogger("gateway.service_grants")

REASON_SAME_OWNER = "same_owner"
REASON_GRANT = "grant"


def _now():
    return datetime.now(timezone.utc)


def live_entries() -> list[dict]:
    """Every live registry service (the reader already excludes tombstones)."""
    return Registry().get_all_entries()


def entries_by_id() -> dict[str, dict]:
    return {str(e["service_id"]): e for e in live_entries()}


def live(entry: dict | None) -> bool:
    """A service is live when its instance is running (targets must be reachable)."""
    return bool(entry is not None and str(entry.get("status") or "") == "running")


def same_owner(a: dict, b: dict) -> bool:
    return str(a.get("owner_api_user_id")) == str(b.get("owner_api_user_id"))


def domain_of(entry: dict) -> str:
    hostname = str(entry.get("hostname") or "")
    return hostname.partition(".")[2]


def user_for(db: Session, entry: dict | None) -> User | None:
    if not entry:
        return None
    return db.query(User).filter(User.api_user_id == entry["owner_api_user_id"]).first()


def is_public_target(db: Session, target: dict) -> bool:
    return acl_service.is_public_owner(user_for(db, target))


def explicit_row(db: Session, source_id: str, target_id: str) -> ServiceGrant | None:
    return db.query(ServiceGrant).filter(
        ServiceGrant.source_service_id == source_id,
        ServiceGrant.target_service_id == target_id,
    ).first()


def explicit_rows(db: Session, source_id: str) -> list[ServiceGrant]:
    return db.query(ServiceGrant).filter(
        ServiceGrant.source_service_id == source_id,
    ).order_by(ServiceGrant.id).all()


def _target_row(entry: dict, reason: str) -> dict:
    return {
        "service_id": str(entry["service_id"]),
        "owner_api_user_id": str(entry["owner_api_user_id"]),
        "user_name": entry.get("user_name"),
        "service_name": entry.get("service_name"),
        "label": str(entry.get("label")),
        "hostname": entry.get("hostname"),
        "domain": domain_of(entry),
        "reason": reason,
    }


def targets_for(db: Session, source: dict, entries: list[dict] | None = None) -> list[dict]:
    """The effective target set for *source*: same-owner + explicit only (G84).

    A public target is NOT implicit: it appears only through an explicit row,
    whose account prerequisite is waived while the target owner is public/live.
    """
    if not live(source):
        return []
    all_entries = entries if entries is not None else live_entries()
    explicit = {str(r.target_service_id) for r in explicit_rows(db, str(source["service_id"]))}
    source_user = user_for(db, source)
    out: list[dict] = []
    for target in all_entries:
        if str(target["service_id"]) == str(source["service_id"]):
            continue
        if not live(target):
            continue
        if same_owner(source, target):
            out.append(_target_row(target, REASON_SAME_OWNER))
            continue
        if str(target["service_id"]) not in explicit:
            continue
        target_user = user_for(db, target)
        if is_public_target(db, target) or (
                source_user is not None and target_user is not None
                and acl_service.allows(db, source_user, target_user.id)):
            out.append(_target_row(target, REASON_GRANT))
    return out


def sources_with_targets(db: Session) -> dict[str, list[dict]]:
    """Every live source → its effective targets (empty sets omitted)."""
    entries = live_entries()
    out: dict[str, list[dict]] = {}
    for entry in entries:
        if not live(entry):
            continue
        targets = targets_for(db, entry, entries)
        if targets:
            out[str(entry["service_id"])] = targets
    return out


def reachable_domains(db: Session) -> set[str]:
    """The service domains that currently contain at least one reachable target.

    A domain is present while SOME live source has a **same-owner or explicitly
    granted** target in it (both services live). Publicness is not an input: an
    ungranted public target contributes nothing, a granted one contributes
    exactly like a granted private target (G84). This is what the dnsmasq
    wildcard is written from.
    """
    return {t["domain"] for targets in sources_with_targets(db).values() for t in targets
            if t.get("domain")}


def set_grant(db: Session, source_id: str, target_id: str,
              actor_user_id=None) -> dict:
    """Create the explicit row; refuse what the matrix makes unnecessary."""
    entries = entries_by_id()
    source = entries.get(str(source_id))
    target = entries.get(str(target_id))
    if source is None or target is None:
        raise ValueError("source or target service not found")
    if str(source_id) == str(target_id):
        raise ValueError("a service cannot call itself")
    if not live(source) or not live(target):
        raise ValueError("both services must be running to grant the call")
    if same_owner(source, target):
        raise ValueError("same-owner targets are implicit — no grant row is needed")
    target_user = user_for(db, target)
    # G84 — a public target is grantable without the account prerequisite; a
    # private target still needs the active account relation.
    if not is_public_target(db, target):
        source_user = user_for(db, source)
        if source_user is None or target_user is None:
            raise ValueError("both owner accounts must have a gateway account")
        if not acl_service.allows(db, source_user, target_user.id):
            raise ValueError(
                "the source owner has no active account grant for the target owner — "
                "assign that account grant first"
            )
    row = explicit_row(db, str(source_id), str(target_id))
    if row is None:
        row = ServiceGrant(
            source_service_id=source_id, target_service_id=target_id,
            created_by=actor_user_id, created_at=_now(), updated_at=_now(),
        )
        db.add(row)
    else:
        row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row.to_dict()


def remove_grant(db: Session, source_id: str, target_id: str) -> bool:
    row = explicit_row(db, str(source_id), str(target_id))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_for_source(db: Session, source: dict) -> dict:
    """The explicit rows plus the full effective set, for the card/panel UI."""
    entries = entries_by_id()
    explicit = []
    for row in explicit_rows(db, str(source["service_id"])):
        target = entries.get(str(row.target_service_id))
        explicit.append({
            **row.to_dict(),
            "target": _target_row(target, REASON_GRANT) if target else None,
        })
    effective = targets_for(db, source)
    return {
        "service_id": str(source["service_id"]),
        "explicit": explicit,
        "effective": effective,
    }


def candidates(db: Session, source: dict) -> list[dict]:
    """Every live service the source may target, for the card's picker (G86).

    Only **accessible accounts** are listed: the source owner's own account
    (same-owner, implicit), accounts with an active account grant, and public
    accounts (reachable at the user level; the service grant is still needed).
    Public is an account property here, not an implicit allow (G84).
    """
    entries = entries_by_id()
    explicit = {str(r.target_service_id) for r in explicit_rows(db, str(source["service_id"]))}
    source_user = user_for(db, source)
    accessible_shared = (
        acl_service.allowed_user_ids(db, source_user.id) if source_user is not None else set()
    )
    out: list[dict] = []
    for target in entries.values():
        if str(target["service_id"]) == str(source["service_id"]):
            continue
        if not live(target):
            continue
        target_user = user_for(db, target)
        same = same_owner(source, target)
        public = is_public_target(db, target)
        if not (same or public or (
                target_user is not None and str(target_user.id) in accessible_shared)):
            continue
        out.append({
            **_target_row(target, REASON_SAME_OWNER if same else ""),
            "explicit": str(target["service_id"]) in explicit,
            # Same-owner is the ONLY implicit case now; a public target is a
            # grantable candidate like a private one.
            "implicit": same,
            "is_public": public,
        })
    out.sort(key=lambda r: (r.get("user_name") or "", r.get("service_name") or "",
                            r.get("label") or ""))
    return out


def annotate_services(db: Session, services: list[dict]) -> None:
    """Attach the effective/explicit sets to service-card dicts, in place."""
    entries = entries_by_id()
    for svc in services:
        sid = str(svc.get("service_id") or "")
        source = entries.get(sid)
        if source is None:
            continue
        effective = targets_for(db, source, list(entries.values()))
        explicit_ids = {str(r.target_service_id) for r in explicit_rows(db, sid)}
        svc["cross_effective_targets"] = effective
        svc["cross_explicit_target_ids"] = sorted(explicit_ids)
        svc["cross_has_path"] = bool(effective)

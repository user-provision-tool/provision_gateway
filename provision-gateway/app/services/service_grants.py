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

G95 R7 — the set has TWO consumers with DIFFERENT predicates, computed
separately:

* the **relationship set** (`targets_for`) drives the source's resolver pair and
  cross-network membership. A target counts while an effective grant exists to
  a **non-tombstoned** service; its instance status, and whether it has an
  instance at all (instance-only removal keeps the service), do not matter.
* the **reachable set** (`reachable_targets_for`) drives the dnsmasq wildcard:
  both services must be live (`running`). This is unchanged from G83/G91.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.service_grant import ServiceGrant
from ..models.user import User
from . import acl_service
from .registry import Registry

_log = logging.getLogger("gateway.service_grants")

REASON_SAME_OWNER = "same_owner"
REASON_GRANT = "grant"

# T0/R6 — the stale-row reasons the editor shows. A stale row is an explicit
# grant whose permission no longer holds; it is MARKED and removable in one
# action, never silently dropped.
STALE_TARGET_DELETED = "target_deleted"
STALE_ACCOUNT_MISSING = "account_grant_missing"
STALE_IMPLICIT = "same_owner_implicit"
STALE_NOT_EFFECTIVE = "not_effective"


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
        "status": entry.get("status"),
        "reason": reason,
    }


def targets_for(db: Session, source: dict, entries: list[dict] | None = None) -> list[dict]:
    """The effective grant RELATIONSHIP set for *source* (G84 + G95 R7).

    Same-owner is implicit; a public target enters only through an explicit
    row, whose account prerequisite is waived while the target owner is public.
    Membership depends on the GRANT RELATIONSHIP, never on the target's runtime
    state: a `building`, `failed` or `stopped` target — or one whose instance
    was removed while the service and its grant remain — is still a target. The
    tombstone is the one exclusion (the reader already filters it out).

    This set drives the resolver pair; the dnsmasq wildcard uses
    :func:`reachable_targets_for`, which additionally requires both services to
    be live.
    """
    all_entries = entries if entries is not None else live_entries()
    explicit = {str(r.target_service_id) for r in explicit_rows(db, str(source["service_id"]))}
    source_user = user_for(db, source)
    out: list[dict] = []
    for target in all_entries:
        if str(target["service_id"]) == str(source["service_id"]):
            continue
        if same_owner(source, target):
            # G102 — same-owner is no longer implicit: it enters the effective
            # set only through an explicit row, exactly like a cross-owner one.
            if str(target["service_id"]) in explicit:
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


def reachable_targets_for(db: Session, source: dict,
                          entries: list[dict] | None = None) -> list[dict]:
    """The WILDCARD set: relationship targets that are also reachable (G95 R7.6).

    Both services must be live (`running`) — the unchanged G83/G91 rule. A
    relationship target that is stopped/building/failed keeps the source's pair
    but contributes no dnsmasq wildcard.
    """
    if not live(source):
        return []
    return [t for t in targets_for(db, source, entries) if live(t)]


def sources_with_targets(db: Session) -> dict[str, list[dict]]:
    """Every source with a relationship → its targets (empty sets omitted).

    The source's own runtime state is NOT an input: a stopped source still has
    its relationships and its stored pair. A service with no instance at all
    carries no pair to apply (the caller skips it).
    """
    entries = live_entries()
    out: dict[str, list[dict]] = {}
    for entry in entries:
        targets = targets_for(db, entry, entries)
        if targets:
            out[str(entry["service_id"])] = targets
    return out


def reachable_domains(db: Session) -> set[str]:
    """The service domains that currently contain at least one reachable target.

    A domain is present while SOME live source has a **same-owner or explicitly
    granted** target in it and BOTH services are live. Publicness is not an
    input: an ungranted public target contributes nothing, a granted one
    contributes exactly like a granted private target (G84). This is what the
    dnsmasq wildcard is written from — the reachable set, not the relationship
    set (G95 R7.6).
    """
    entries = live_entries()
    return {t["domain"] for entry in entries if live(entry)
            for t in reachable_targets_for(db, entry, entries) if t.get("domain")}


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
    target_user = user_for(db, target)
    # G84 — a public target is grantable without the account prerequisite; a
    # private target still needs the active account relation.  G102 — a
    # same-owner target is an ordinary row and needs neither check.
    if not same_owner(source, target) and not is_public_target(db, target):
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


def ensure_grant(db: Session, source_id: str, target_id: str,
                 actor_user_id=None) -> dict:
    """The deploy's pre-start, add-only grant row (G102 R3.1/R3.6).

    Unlike :func:`set_grant` this path has **no running check**: it is called
    after registration and before the source's app containers start, when the
    source is still ``building``.  The relationship follows the services, not
    their runtime state, so a stopped target is still grantable.  It never
    removes a row and is idempotent by the unique pair.

    Accessibility is checked at write time: the target must exist (the reader
    excludes tombstones) and be public or have an active account grant —
    same-owner needs neither.  A refusal raises ``ValueError``; the caller
    reports it without failing the deploy.
    """
    entries = entries_by_id()
    source = entries.get(str(source_id))
    target = entries.get(str(target_id))
    if source is None:
        raise ValueError("source service not found")
    if target is None:
        raise ValueError("target service not found")
    if str(source_id) == str(target_id):
        raise ValueError("a service cannot call itself")
    if not same_owner(source, target) and not is_public_target(db, target):
        source_user = user_for(db, source)
        target_user = user_for(db, target)
        if source_user is None or target_user is None:
            raise ValueError("both owner accounts must have a gateway account")
        if not acl_service.allows(db, source_user, target_user.id):
            raise ValueError(
                "the source owner has no active account grant for the target owner — "
                "assign that account grant first"
            )
    row = explicit_row(db, str(source_id), str(target_id))
    created = False
    if row is None:
        row = ServiceGrant(
            source_service_id=source_id, target_service_id=target_id,
            created_by=actor_user_id, created_at=_now(), updated_at=_now(),
        )
        db.add(row)
        created = True
    else:
        row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return {"created": created, "row": row.to_dict()}


def remove_grant(db: Session, source_id: str, target_id: str) -> bool:
    row = explicit_row(db, str(source_id), str(target_id))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _stale_reason(db: Session, source: dict, target: dict | None,
                  source_user: User | None) -> str | None:
    """Why an explicit row no longer holds a permission, or ``None`` when it does.

    G95 R6 — a row is stale when its target is tombstoned, its account
    prerequisite fails closed, or it is otherwise excluded from the effective
    relationship set. The editor marks it; it is never silently dropped.
    """
    if target is None:
        return STALE_TARGET_DELETED
    # G102 — a same-owner row is an ordinary, effective row: target existence
    # is the only condition.  (The old STALE_IMPLICIT state no longer exists.)
    if same_owner(source, target):
        return None
    if is_public_target(db, target):
        return None
    target_user = user_for(db, target)
    if (source_user is None or target_user is None
            or not acl_service.allows(db, source_user, target_user.id)):
        return STALE_ACCOUNT_MISSING
    return None


_STALE_MESSAGES = {
    STALE_TARGET_DELETED: "no longer permitted: the target service was deleted",
    STALE_ACCOUNT_MISSING: "no longer permitted: the source owner has no active account grant",
    STALE_NOT_EFFECTIVE: "no longer permitted: the grant is not effective",
}


def list_for_source(db: Session, source: dict) -> dict:
    """The explicit rows (stale-marked) plus the effective relationship set."""
    entries = entries_by_id()
    source_user = user_for(db, source)
    effective = targets_for(db, source)
    effective_ids = {str(t["service_id"]) for t in effective}
    explicit = []
    for row in explicit_rows(db, str(source["service_id"])):
        target_id = str(row.target_service_id)
        target = entries.get(target_id)
        reason = _stale_reason(db, source, target, source_user)
        if reason is None and target_id not in effective_ids:
            reason = STALE_NOT_EFFECTIVE
        explicit.append({
            **row.to_dict(),
            "target": _target_row(target, REASON_GRANT) if target else None,
            "stale": reason is not None,
            "stale_reason": reason,
            "stale_message": _STALE_MESSAGES.get(reason) if reason else None,
        })
    return {
        "service_id": str(source["service_id"]),
        "explicit": explicit,
        "effective": effective,
    }


def _api_user_id(db: Session, gateway_user_id) -> str | None:
    """The registry api-user id behind a gateway user id, or ``None``."""
    import uuid as _uuid
    try:
        uid = _uuid.UUID(str(gateway_user_id))
    except (ValueError, TypeError, AttributeError):
        return None
    user = db.query(User).filter(User.id == uid).first()
    return str(user.api_user_id) if user else None


def affected_sources_for_account_change(
    db: Session, *, account_grant: tuple[str, str] | None = None,
    changed_user_id: str | None = None,
) -> list[str]:
    """The sources whose pair depends on an account-level change (R14/S10).

    ``account_grant=(grantee, shared)``: services owned by *grantee* that have
    an explicit service grant to a service owned by *shared* — those rows' account
    prerequisite is the grant being toggled.

    ``changed_user_id``: an activation toggle or deletion of that account. The
    sources are services owned by the account with at least one explicit row,
    plus services with an explicit row to one of the account's services. Rows
    whose two ends are same-owner are excluded: same-owner relationships do not
    depend on the account prerequisite. Rows with a tombstoned/absent end are
    excluded (they are not relationships).

    The set is deduplicated and sorted; callers must not fall back to
    `sync_all` when it is empty.
    """
    entries = entries_by_id()
    out: set[str] = set()

    if account_grant is not None:
        grantee_id, shared_id = account_grant
        grantee_api = _api_user_id(db, grantee_id)
        shared_api = _api_user_id(db, shared_id)
        if grantee_api and shared_api:
            own = {sid for sid, e in entries.items()
                   if str(e.get("owner_api_user_id")) == grantee_api}
            targets = {sid for sid, e in entries.items()
                       if str(e.get("owner_api_user_id")) == shared_api}
            if own and targets:
                import uuid as _uuid
                rows = db.query(ServiceGrant).filter(
                    ServiceGrant.source_service_id.in_(
                        [_uuid.UUID(s) for s in own])
                ).all()
                for row in rows:
                    src, tgt = str(row.source_service_id), str(row.target_service_id)
                    if tgt not in targets or src not in entries:
                        continue
                    if same_owner(entries[src], entries[tgt]):
                        continue
                    out.add(src)

    if changed_user_id is not None:
        api = _api_user_id(db, changed_user_id)
        if api:
            own = {sid for sid, e in entries.items()
                   if str(e.get("owner_api_user_id")) == api}
            if own:
                import uuid as _uuid
                ids = [_uuid.UUID(s) for s in own]
                rows = db.query(ServiceGrant).filter(
                    or_(ServiceGrant.source_service_id.in_(ids),
                        ServiceGrant.target_service_id.in_(ids))
                ).all()
                for row in rows:
                    src, tgt = str(row.source_service_id), str(row.target_service_id)
                    if src not in entries or tgt not in entries:
                        continue
                    if same_owner(entries[src], entries[tgt]):
                        continue
                    if src in own or tgt in own:
                        out.add(src)

    return sorted(out)


def remove_grants_for_service(db: Session, service_id: str) -> int:
    """Delete every grant row that names *service_id* in EITHER direction (R7).

    A service delete ends its relationships: outbound rows (it as source) and
    inbound rows (it as target) both go in one transaction. Existing
    ``remove_grant`` only removes one direction.
    """
    sid = str(service_id)
    rows = db.query(ServiceGrant).filter(
        or_(ServiceGrant.source_service_id == sid,
            ServiceGrant.target_service_id == sid)
    ).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


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
            # G102 — no target is implicit any more; same-owner is an ordinary
            # grantable candidate like a cross-owner one.
            "implicit": False,
            "same_owner": same,
            "is_public": public,
        })
    out.sort(key=lambda r: (r.get("user_name") or "", r.get("service_name") or "",
                            r.get("label") or ""))
    return out


def account_candidates(db: Session, username: str) -> dict:
    """Accessible-account groups for a deploying user (R10).

    Own, active account grant and public accounts are grouped by owner; the
    `Public` marker is on the GROUP (an account property), never on a service.
    """
    user = acl_service.user_by_username(db, username)
    if user is None or not acl_service.account_live(user):
        raise ValueError(f"'{username}' is not a live gateway account")
    accessible = acl_service.allowed_user_ids(db, user.id)
    entries = entries_by_id()
    groups: dict[str, dict] = {}
    for target in entries.values():
        if not live(target):
            continue
        owner_user = user_for(db, target)
        if owner_user is None or not acl_service.account_live(owner_user):
            continue
        same = str(owner_user.id) == str(user.id)
        public = acl_service.is_public_owner(owner_user)
        if not (same or public or str(owner_user.id) in accessible):
            continue
        reason = "own" if same else ("public" if public else "account_grant")
        key = str(owner_user.id)
        group = groups.setdefault(key, {
            "owner_gateway_user_id": key,
            "user_name": target.get("user_name"),
            "is_public": public,
            "reason": reason,
            "services": [],
        })
        if public and not same:
            group["reason"] = "public"
        group["services"].append({
            **_target_row(target, REASON_SAME_OWNER if same else ""),
            # G102 — same-owner is shown as grantable, not implicit.
            "implicit": False,
            "same_owner": same,
            "is_public": public,
        })
    ordered = sorted(groups.values(),
                     key=lambda g: (g.get("user_name") or ""))
    for group in ordered:
        group["services"].sort(key=lambda s: (s.get("service_name") or "",
                                              s.get("label") or ""))
    return {"user_name": username, "groups": ordered}


def apply_set(db: Session, source_id: str, target_ids: list[str],
              actor_user_id=None) -> dict:
    """Apply the desired target set in ONE transaction (R10).

    Validation runs first and raises before any row is touched; same-owner
    targets are implicit and are skipped (no row). All-or-nothing: a failure
    mid-apply rolls back and the previous set remains.
    """
    entries = entries_by_id()
    source = entries.get(str(source_id))
    if source is None:
        raise ValueError("source service not found")
    if not live(source):
        raise ValueError("the source service must be running")

    desired: set[str] = set()
    for raw in target_ids or []:
        target_id = str(raw)
        if target_id == str(source_id):
            continue
        target = entries.get(target_id)
        if target is None:
            raise ValueError(f"target service not found: {target_id}")
        if not live(target):
            raise ValueError(f"target service is not running: {target_id}")
        if same_owner(source, target):
            # G102 — an ordinary row, written like any other.
            desired.add(target_id)
            continue
        if not is_public_target(db, target):
            target_user = user_for(db, target)
            source_user = user_for(db, source)
            if source_user is None or target_user is None:
                raise ValueError("both owner accounts must have a gateway account")
            if not acl_service.allows(db, source_user, target_user.id):
                raise ValueError(
                    "the source owner has no active account grant for "
                    f"{target.get('user_name')} — assign that account grant first"
                )
        desired.add(target_id)

    current = {str(r.target_service_id): r
               for r in explicit_rows(db, str(source_id))}
    try:
        for target_id in desired - set(current):
            db.add(ServiceGrant(
                source_service_id=source_id, target_service_id=target_id,
                created_by=actor_user_id, created_at=_now(), updated_at=_now(),
            ))
        for target_id in set(current) - desired:
            db.delete(current[target_id])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return list_for_source(db, source)


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

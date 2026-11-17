"""The four condition sources — read-only adapters (R9–R13).

Each adapter decides whether its condition holds and returns facts. A fact is
the input to one active alert row; the evaluator in ``alert_service`` owns the
upsert and the resolution. An adapter that cannot read its source reports
``readable=False``: the evaluator then opens nothing and resolves nothing for
that source, so an outage never masquerades as recovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .acl_state import acl_state

_log = logging.getLogger("gateway.conditions")

TYPE_ORPHAN = "orphan_user_services"
TYPE_ACL = "acl_drift"
TYPE_GRANT_SYNC = "grant_sync_failed"
TYPE_SERVICE = "service_failed"

PREFIX_ORPHAN = "orphan-user:"
KEY_ACL = "acl:drift"
PREFIX_GRANT = "grant-sync:"
PREFIX_SERVICE = "service:"

SOURCE_ORPHAN = "orphan"
SOURCE_ACL = "acl"
SOURCE_GRANT = "grant-sync"
SOURCE_SERVICE = "service"

EDGE_CONTAINER = "subnet-acl-nginx-acl"


@dataclass
class SourceResult:
    """One source's facts plus whether it could be read at all."""

    readable: bool = True
    facts: list[dict[str, Any]] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fact(*, condition_key: str, type: str, severity: str, title: str,
          message: str, context: dict, service_id: str | None = None,
          user_name: str | None = None, service_name: str | None = None,
          label: str | None = None) -> dict[str, Any]:
    return {
        "condition_key": condition_key,
        "type": type,
        "severity": severity,
        "title": title,
        "message": message,
        "context": context,
        "service_id": service_id,
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
    }


def _orphan_facts(gateway_db: Session, reader_db: Session) -> SourceResult:
    """R10 — one fact per username whose services have a gone owner.

    "Gone" is the existing owner test: the registry api_users row is deleted,
    or the gateway users row for that api_user is deleted. One key per user,
    not per service: the operator's remedy is per user, so the context lists
    every affected service.
    """
    try:
        deleted_api_users = {
            str(row[0])
            for row in gateway_db.execute(
                text("SELECT api_user_id FROM gateway.users "
                     "WHERE deleted_at IS NOT NULL")
            )
        }
        rows = reader_db.execute(text(
            "SELECT s.id AS service_id, s.service_name, s.label, "
            "       a.id AS api_user_id, a.username, a.deleted_at "
            "FROM registry.services s "
            "JOIN registry.api_users a ON a.id = s.owner_api_user_id "
            "WHERE s.deleted_at IS NULL"
        )).mappings().all()
    except Exception as exc:  # noqa: BLE001 — unreadable is unknown
        _log.warning("orphan-user condition source unreadable: %s", exc)
        return SourceResult(readable=False)

    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        gone = (
            row["deleted_at"] is not None
            or str(row["api_user_id"]) in deleted_api_users
        )
        if not gone:
            continue
        by_user.setdefault(str(row["username"]), []).append({
            "service_id": str(row["service_id"]),
            "service_name": row["service_name"],
            "label": row["label"],
        })

    facts = [
        _fact(
            condition_key=f"{PREFIX_ORPHAN}{username}",
            type=TYPE_ORPHAN,
            severity="warning",
            title=f"Services owned by a deleted user: {username}",
            message=(
                f"{len(services)} service(s) owned by {username} remain, but the "
                "owner account is gone."
            ),
            context={"username": username, "services": services},
            user_name=username,
        )
        for username, services in sorted(by_user.items())
    ]
    return SourceResult(readable=True, facts=facts)


async def _acl_facts() -> SourceResult:
    """R11 — a known gateway-vs-edge disagreement, and only that.

    A deliberately disabled ACL (both sides off, `enabled=false`,
    `consistent=true`) is a configuration decision, not drift. An unreadable
    edge env (`edge is None`) is unknown: it neither opens nor resolves.
    """
    from .provision_service import provision_service

    try:
        edge_env = await provision_service.container_env(EDGE_CONTAINER, timeout=2.0)
    except Exception as exc:  # noqa: BLE001 — unknown, not cleared
        _log.info("ACL condition source unknown: edge env unreadable (%s)", exc)
        return SourceResult(readable=False)

    state = acl_state(edge_env)
    if state["edge"] is None or state["consistent"]:
        # Unknown edge: the evaluator must not resolve an existing drift row.
        return SourceResult(readable=state["edge"] is not None)

    return SourceResult(readable=True, facts=[_fact(
        condition_key=KEY_ACL,
        type=TYPE_ACL,
        severity="error",
        title="ACL drift between gateway and edge",
        message=(
            "The gateway and the edge ACL settings disagree "
            f"(gateway={state['gateway']}, edge={state['edge']})."
        ),
        context={
            "gateway": state["gateway"],
            "edge": state["edge"],
            "enabled": state["enabled"],
            "consistent": state["consistent"],
        },
    )])


def _grant_sync_facts(gateway_db: Session, reader_db: Session) -> SourceResult:
    """R12 — the durable targeted-sync outcome per source.

    Only `deferred`/`error` rows are facts; every `sync_source` completion
    overwrites the row, so a later `ok` removes the key and resolves the alert.
    """
    try:
        rows = gateway_db.execute(text(
            "SELECT source_service_id, last_outcome, reason, task_id "
            "FROM gateway.grant_sync_state "
            "WHERE last_outcome IN ('deferred', 'error')"
        )).mappings().all()
    except Exception as exc:  # noqa: BLE001 — own store unreadable
        _log.warning("grant-sync condition source unreadable: %s", exc)
        return SourceResult(readable=False)

    names: dict[str, dict[str, Any]] = {}
    if rows:
        try:
            ids = [str(row["source_service_id"]) for row in rows]
            name_rows = reader_db.execute(
                text(
                    "SELECT s.id, s.service_name, s.label, a.username "
                    "FROM registry.services s "
                    "LEFT JOIN registry.api_users a ON a.id = s.owner_api_user_id "
                    "WHERE s.id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            ).mappings().all()
            names = {str(r["id"]): dict(r) for r in name_rows}
        except Exception as exc:  # noqa: BLE001 — names are optional
            _log.info("grant-sync condition names unavailable: %s", exc)

    facts: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row["source_service_id"])
        named = names.get(sid) or {}
        service_name = named.get("service_name")
        facts.append(_fact(
            condition_key=f"{PREFIX_GRANT}{sid}",
            type=TYPE_GRANT_SYNC,
            severity="warning",
            title=f"Grant sync failed for {service_name or sid}",
            message=(
                f"The last targeted sync for source {sid} ended "
                f"{row['last_outcome']}"
                + (f": {row['reason']}" if row["reason"] else ".")
            ),
            context={
                "source_service_id": sid,
                "outcome": row["last_outcome"],
                "reason": row["reason"],
                "task_id": row["task_id"],
            },
            service_id=sid,
            user_name=named.get("username"),
            service_name=service_name,
            label=named.get("label"),
        ))
    return SourceResult(readable=True, facts=facts)


def _service_failed_facts(gateway_db: Session, reader_db: Session) -> SourceResult:
    """R13 — a failed deploy attempt or a failed registered instance.

    Two gateway-readable records carry the operator-visible fact:
      * the gateway's own `pre_deploy_sessions` row in `state='failed'` with
        `operation='deploy'` — the failed deploy attempt the card renders as
        "Failed (deploy_failed)"; this is the case an initial register failure
        produces because no registry instance row is written;
      * a `registry.service_instances` row in `status='failed'` — the durable
        registered state, read through `gateway_reader`.
    Both are read in the gateway; the `-api` is never written or changed. One
    `service:<id>` key; when both records exist the instance is the source. A
    failed preparation-only (`operation='generate'`) session is not a service
    failure. Either store unreadable makes the source unknown for the pass.
    """
    try:
        session_rows = gateway_db.execute(text(
            "SELECT service_id, service_name, service_label, "
            "       owner_api_user_id, reason, detail "
            "FROM gateway.pre_deploy_sessions "
            "WHERE state = 'failed' AND operation = 'deploy'"
        )).mappings().all()
        instance_rows = reader_db.execute(text(
            "SELECT i.service_id, i.failure, s.service_name, s.label, "
            "       a.username "
            "FROM registry.service_instances i "
            "JOIN registry.services s ON s.id = i.service_id "
            "LEFT JOIN registry.api_users a ON a.id = s.owner_api_user_id "
            "WHERE i.status = 'failed' AND s.deleted_at IS NULL"
        )).mappings().all()
    except Exception as exc:  # noqa: BLE001 — either store unreadable is unknown
        _log.warning("service-failed condition source unreadable: %s", exc)
        return SourceResult(readable=False)

    # Resolve the session owners' usernames through the read-only registry path;
    # a failed name lookup only leaves the name null, never hides the fact.
    usernames: dict[str, str] = {}
    owner_ids = sorted({
        str(row["owner_api_user_id"]) for row in session_rows
        if row["owner_api_user_id"]
    })
    if owner_ids:
        try:
            user_rows = reader_db.execute(
                text("SELECT id, username FROM registry.api_users "
                     "WHERE id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": owner_ids},
            ).mappings().all()
            usernames = {str(r["id"]): r["username"] for r in user_rows}
        except Exception as exc:  # noqa: BLE001 — names are optional
            _log.info("service-failed session names unavailable: %s", exc)

    facts_by_key: dict[str, dict[str, Any]] = {}
    for row in session_rows:
        sid = str(row["service_id"])
        name = row["service_name"]
        detail = row["detail"] or row["reason"] or "the deploy failed"
        username = usernames.get(str(row["owner_api_user_id"]))
        facts_by_key[f"{PREFIX_SERVICE}{sid}"] = _fact(
            condition_key=f"{PREFIX_SERVICE}{sid}",
            type=TYPE_SERVICE,
            severity="error",
            title=f"Service deploy failed: {name}",
            message=f"The last deploy attempt for {name} failed: {detail}",
            context={
                "source": "pre_deploy_session",
                "service_id": sid,
                "service_name": name,
                "label": row["service_label"],
                "user_name": username,
                "reason": row["reason"],
                "detail": row["detail"],
            },
            service_id=sid,
            user_name=username,
            service_name=name,
            label=row["service_label"],
        )

    # The registered instance is authoritative when both records exist.
    for row in instance_rows:
        sid = str(row["service_id"])
        name = row["service_name"]
        facts_by_key[f"{PREFIX_SERVICE}{sid}"] = _fact(
            condition_key=f"{PREFIX_SERVICE}{sid}",
            type=TYPE_SERVICE,
            severity="error",
            title=f"Service failed: {name}",
            message=f"The instance for {name} is in the failed state.",
            context={
                "source": "instance",
                "service_id": sid,
                "service_name": name,
                "label": row["label"],
                "user_name": row["username"],
                "failure": row["failure"],
            },
            service_id=sid,
            user_name=row["username"],
            service_name=name,
            label=row["label"],
        )

    return SourceResult(readable=True, facts=list(facts_by_key.values()))


async def collect(gateway_db: Session, reader_db: Session) -> dict[str, SourceResult]:
    """Read every source once; one unreadable adapter never hides another."""
    results = {
        SOURCE_ORPHAN: _orphan_facts(gateway_db, reader_db),
        SOURCE_SERVICE: _service_failed_facts(gateway_db, reader_db),
        SOURCE_GRANT: _grant_sync_facts(gateway_db, reader_db),
    }
    try:
        results[SOURCE_ACL] = await _acl_facts()
    except Exception as exc:  # noqa: BLE001 — never fail the pass
        _log.warning("ACL condition source raised: %s", exc)
        results[SOURCE_ACL] = SourceResult(readable=False)
    return results

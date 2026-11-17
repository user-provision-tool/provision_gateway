"""The Alerts Center service — ingestion, condition evaluation, list/read.

One durable row per alert:
  * event rows are ingested from the registry task result's `hold` block
    (`source_key = hold:<task_id>`), stay active indefinitely and never resolve;
  * condition rows are keyed by `condition_key`, active while the state holds
    and resolved (never deleted) when it clears; a recurrence inserts a new row
    with fresh read state.

Ingest and evaluation run at gateway startup and before every alert read.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Text, bindparam, cast, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import ReaderSessionLocal, SessionLocal
from ..models.alert import Alert, GrantSyncState
from . import condition_sources as cs

_log = logging.getLogger("gateway.alerts")

TYPE_HOLD = "stream_conf_hold_collision"
TITLE_HOLD = "Stream conf .paused collision warning"

SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

KIND_EVENT = "event"
KIND_CONDITION = "condition"
STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"

# source name → (mode, key) for the resolution scope of its active rows
_SOURCE_SCOPE = {
    cs.SOURCE_ORPHAN: ("startswith", cs.PREFIX_ORPHAN),
    cs.SOURCE_ACL: ("equals", cs.KEY_ACL),
    cs.SOURCE_GRANT: ("startswith", cs.PREFIX_GRANT),
    cs.SOURCE_SERVICE: ("startswith", cs.PREFIX_SERVICE),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Ingestion — the event half
# ---------------------------------------------------------------------------

def ingest_events(db: Session, reader_db: Session) -> int:
    """Project every indexed hold task into one durable event row.

    Re-ingest refreshes the content and `last_seen_at` but never touches
    `read_at`/`read_by` or `status`; a task row without `result.hold.alert`
    produces nothing, and a normal hold records no alert.
    """
    try:
        rows = reader_db.execute(text(
            "SELECT t.task_id, t.service_id, t.finished_at, t.result, "
            "       s.service_name, s.label, a.username "
            "FROM registry.tasks t "
            "LEFT JOIN registry.services s ON s.id = t.service_id "
            "LEFT JOIN registry.api_users a ON a.id = s.owner_api_user_id "
            "WHERE t.deleted_at IS NULL "
            "  AND t.result ? 'hold' "
            "  AND t.result->'hold'->>'alert' IS NOT NULL"
        )).mappings().all()
    except Exception as exc:  # noqa: BLE001 — the list still serves old rows
        _log.warning("alert event ingest skipped: registry unreadable (%s)", exc)
        return 0

    touched = 0
    for row in rows:
        hold = (row["result"] or {}).get("hold") or {}
        source_key = f"hold:{row['task_id']}"
        finished = row["finished_at"] or _now()
        message = str(hold.get("alert") or "A stream-conf hold collision occurred.")
        fields = {
            "kind": KIND_EVENT,
            "status": STATUS_ACTIVE,
            "type": TYPE_HOLD,
            "severity": SEVERITY_WARNING,
            "title": TITLE_HOLD,
            "message": message,
            "task_id": str(row["task_id"]),
            "service_id": _uuid(row["service_id"]),
            "user_name": row["username"],
            "service_name": row["service_name"],
            "label": row["label"],
            "context": hold,
            "last_seen_at": finished,
            "updated_at": _now(),
        }
        existing = db.query(Alert).filter(Alert.source_key == source_key).first()
        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            touched += 1
            continue
        db.add(Alert(
            source_key=source_key,
            created_at=finished,
            **fields,
        ))
        touched += 1
    try:
        db.commit()
    except IntegrityError:
        # Two workers ingested the same row: the first wins, the second is a
        # no-op (the unique source_key is the dedupe).
        db.rollback()
        _log.info("alert event ingest raced with another worker; no-op")
        return touched
    return touched


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def _scope_filter(source: str):
    mode, key = _SOURCE_SCOPE[source]
    if mode == "equals":
        return Alert.condition_key == key
    return Alert.condition_key.like(key + "%")


def _apply_fact(row: Alert, fact: dict[str, Any]) -> None:
    row.type = fact["type"]
    row.status = STATUS_ACTIVE
    row.severity = fact["severity"]
    row.title = fact["title"]
    row.message = fact["message"]
    row.context = fact.get("context") or {}
    row.service_id = _uuid(fact.get("service_id")) or row.service_id
    row.user_name = fact.get("user_name")
    row.service_name = fact.get("service_name")
    row.label = fact.get("label")
    row.last_seen_at = _now()
    row.updated_at = _now()


def _new_fact_row(fact: dict[str, Any]) -> Alert:
    now = _now()
    return Alert(
        source_key=None,
        kind=KIND_CONDITION,
        condition_key=fact["condition_key"],
        status=STATUS_ACTIVE,
        type=fact["type"],
        severity=fact["severity"],
        title=fact["title"],
        message=fact["message"],
        task_id=None,
        service_id=_uuid(fact.get("service_id")),
        user_name=fact.get("user_name"),
        service_name=fact.get("service_name"),
        label=fact.get("label"),
        context=fact.get("context") or {},
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )


def evaluate_conditions(db: Session, results: dict[str, cs.SourceResult]) -> int:
    """Upsert each readable source's facts, then resolve the keys that cleared.

    An unreadable source is skipped in BOTH directions: it opens nothing and
    resolves nothing, so an outage never looks like recovery. Event rows are
    never touched here.
    """
    touched = 0
    for source, result in results.items():
        if not result.readable:
            continue
        keys = {fact["condition_key"] for fact in result.facts}
        for fact in result.facts:
            existing = (
                db.query(Alert)
                .filter(Alert.condition_key == fact["condition_key"],
                        Alert.status == STATUS_ACTIVE)
                .first()
            )
            if existing is not None:
                _apply_fact(existing, fact)
                touched += 1
                continue
            try:
                with db.begin_nested():
                    db.add(_new_fact_row(fact))
                touched += 1
            except IntegrityError:
                # Another evaluator inserted the same active key; the row now
                # exists, so advance it instead of creating a duplicate.
                winner = (
                    db.query(Alert)
                    .filter(Alert.condition_key == fact["condition_key"],
                            Alert.status == STATUS_ACTIVE)
                    .first()
                )
                if winner is not None:
                    _apply_fact(winner, fact)
                    touched += 1

        # Resolve active rows of THIS source whose key is no longer a fact.
        active = (
            db.query(Alert)
            .filter(Alert.kind == KIND_CONDITION,
                    Alert.status == STATUS_ACTIVE,
                    _scope_filter(source))
            .all()
        )
        for row in active:
            if row.condition_key in keys:
                continue
            row.status = STATUS_RESOLVED
            row.resolved_at = _now()
            row.updated_at = _now()
            touched += 1
    db.commit()
    return touched


# ---------------------------------------------------------------------------
# Refresh — startup and before each read
# ---------------------------------------------------------------------------

async def refresh(db: Session) -> None:
    """Ingest events and evaluate every condition once, best effort.

    A failed ingest or a single unreadable source never fails the read: the
    alert list must still render the rows the store already holds.
    """
    reader = ReaderSessionLocal()
    try:
        try:
            ingest_events(db, reader)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _log.warning("alert event ingest failed: %s", exc)
        try:
            results = await cs.collect(db, reader)
            evaluate_conditions(db, results)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _log.warning("condition evaluation failed: %s", exc)
    finally:
        reader.close()


async def refresh_once() -> None:
    """Startup entry point: one pass on its own session."""
    db = SessionLocal()
    try:
        await refresh(db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Queries and read state
# ---------------------------------------------------------------------------

def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_query(db: Session, *, q: str | None = None,
                type: str | None = None, kind: str | None = None,
                status: str | None = None, read: str | None = None):
    query = db.query(Alert)
    if q:
        pattern = f"%{_escape_like(q)}%"
        query = query.filter(or_(
            Alert.type.ilike(pattern, escape="\\"),
            Alert.title.ilike(pattern, escape="\\"),
            Alert.message.ilike(pattern, escape="\\"),
            Alert.user_name.ilike(pattern, escape="\\"),
            Alert.service_name.ilike(pattern, escape="\\"),
            Alert.label.ilike(pattern, escape="\\"),
            Alert.task_id.ilike(pattern, escape="\\"),
            Alert.condition_key.ilike(pattern, escape="\\"),
            cast(Alert.context, Text).ilike(pattern, escape="\\"),
        ))
    if type:
        query = query.filter(Alert.type == type)
    if kind:
        query = query.filter(Alert.kind == kind)
    if status in (STATUS_ACTIVE, STATUS_RESOLVED):
        query = query.filter(Alert.status == status)
    if read == "read":
        query = query.filter(Alert.read_at.isnot(None))
    elif read == "unread":
        query = query.filter(Alert.read_at.is_(None))
    return query


def _read_by_names(db: Session, rows: list[Alert]) -> dict[str, str]:
    """Resolve the gateway user ids recorded in `read_by` to usernames."""
    ids = {str(row.read_by) for row in rows if row.read_by}
    if not ids:
        return {}
    try:
        gateway_rows = db.execute(
            text("SELECT id, api_user_id FROM gateway.users WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": tuple(ids)},
        ).mappings().all()
    except Exception:  # noqa: BLE001 — the display name is optional
        return {}
    by_api = {str(r["api_user_id"]): str(r["id"]) for r in gateway_rows}
    if not by_api:
        return {}
    reader = ReaderSessionLocal()
    try:
        user_rows = reader.execute(
            text("SELECT id, username FROM registry.api_users WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": tuple(by_api.keys())},
        ).mappings().all()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        reader.close()
    username_by_api = {str(r["id"]): r["username"] for r in user_rows}
    return {
        gateway_id: username_by_api.get(api_id)
        for api_id, gateway_id in by_api.items()
        if username_by_api.get(api_id)
    }


def list_alerts(db: Session, *, q: str | None = None, type: str | None = None,
                kind: str | None = None, status: str | None = None,
                read: str | None = None, limit: int = 50,
                offset: int = 0) -> dict[str, Any]:
    query = _base_query(db, q=q, type=type, kind=kind, status=status, read=read)
    total = query.count()
    rows = (
        query.order_by(Alert.created_at.desc(), Alert.id.desc())
        .offset(max(0, offset))
        .limit(max(0, limit))
        .all()
    )
    names = _read_by_names(db, rows)
    alerts = []
    for row in rows:
        item = row.to_dict()
        item["read_by_name"] = names.get(str(row.read_by)) if row.read_by else None
        alerts.append(item)
    return {
        "alerts": alerts,
        "total": total,
        "unread": unread_active_count(db),
        "limit": limit,
        "offset": offset,
    }


def counts(db: Session) -> dict[str, int]:
    total = db.query(func.count(Alert.id)).scalar() or 0
    active = (
        db.query(func.count(Alert.id))
        .filter(Alert.status == STATUS_ACTIVE)
        .scalar() or 0
    )
    return {"unread": unread_active_count(db), "total": int(total),
            "active": int(active)}


def unread_active_count(db: Session) -> int:
    return int(
        db.query(func.count(Alert.id))
        .filter(Alert.status == STATUS_ACTIVE, Alert.read_at.is_(None))
        .scalar() or 0
    )


def mark(db: Session, ids: list[str], *, read: bool, actor_id: str | None) -> dict:
    """Mark a batch read/unread. Unknown ids are reported, never fatal.

    Marking read sets the pair only when the row is unread, so a repeated mark
    keeps the first acknowledger; marking unread clears both. Status is never
    changed by a read-state operation.
    """
    valid = {raw: _uuid(raw) for raw in ids}
    wanted = [uid for uid in valid.values() if uid is not None]
    rows = db.query(Alert).filter(Alert.id.in_(wanted)).all() if wanted else []
    found = {str(row.id) for row in rows}
    missing = [raw for raw, uid in valid.items()
               if uid is None or str(uid) not in found]
    updated = 0
    now = _now()
    for row in rows:
        if read:
            if row.read_at is None:
                row.read_at = now
                row.read_by = _uuid(actor_id)
                row.updated_at = now
                updated += 1
        else:
            if row.read_at is not None:
                row.read_at = None
                row.read_by = None
                row.updated_at = now
                updated += 1
    db.commit()
    return {"updated": updated, "missing": missing}


def read_all(db: Session, actor_id: str | None) -> dict:
    rows = db.query(Alert).filter(Alert.read_at.is_(None)).all()
    now = _now()
    for row in rows:
        row.read_at = now
        row.read_by = _uuid(actor_id)
        row.updated_at = now
    db.commit()
    return {"updated": len(rows)}


# ---------------------------------------------------------------------------
# Grant-sync outcome recording (R12)
# ---------------------------------------------------------------------------

def record_grant_sync_outcome(source_id: str, result: dict[str, Any]) -> None:
    """Upsert the durable per-source outcome every `sync_source` returns.

    The row is the grant-sync condition's source: deferred/error is active, and
    a later ok (including the absent-source skip) overwrites it, which resolves
    the alert. A store failure is logged, never propagated into the sync.
    """
    uid = _uuid(source_id)
    if uid is None:
        return
    db = SessionLocal()
    try:
        row = db.get(GrantSyncState, uid)
        if row is None:
            row = GrantSyncState(source_service_id=uid,
                                 updated_at=_now())
            db.add(row)
        row.last_outcome = str(result.get("outcome") or "error")
        row.reason = result.get("reason")
        row.task_id = result.get("task_id")
        row.updated_at = _now()
        db.commit()
    except Exception as exc:  # noqa: BLE001 — the sync already succeeded/failed
        db.rollback()
        _log.warning("grant-sync outcome not recorded for %s: %s", source_id, exc)
    finally:
        db.close()

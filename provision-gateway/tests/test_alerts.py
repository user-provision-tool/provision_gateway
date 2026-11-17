"""Alerts Center — store, lifecycle, read state and condition sources.

DB-backed tests use the dedicated PostgreSQL test database (``pg_test_db``);
the ACL adapter is monkeypatched because the live edge read is an HTTP hop.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

import pg_test_db

pg_test_db.provision()

from app.database import ReaderSessionLocal, SessionLocal, init_db  # noqa: E402
from app.models.alert import Alert, GrantSyncState  # noqa: E402
from app.services import alert_service  # noqa: E402
from app.services import condition_sources as cs  # noqa: E402
from app.services.acl_state import acl_state  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _gateway_schema():
    init_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _registry_execute(sql: str, params: tuple):
    with psycopg.connect(pg_test_db.registry_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _make_api_user(username: str, *, deleted: bool = False) -> str:
    uid = str(uuid.uuid4())
    _registry_execute(
        "INSERT INTO registry.api_users (id, username, state, created_at, deleted_at) "
        "VALUES (%s, %s, 'live', %s, %s)",
        (uid, username, _now(), _now() if deleted else None),
    )
    return uid


def _make_service(api_user_id: str, name: str, label: str = "0",
                  sid: str | None = None) -> str:
    sid = sid or str(uuid.uuid4())
    _registry_execute(
        "INSERT INTO registry.services (id, owner_api_user_id, service_name, "
        "label, recipe_path, created_at) VALUES (%s, %s, %s, %s, '', %s)",
        (sid, api_user_id, name, label, _now()),
    )
    return sid


def _make_instance(service_id: str, status: str, failure: dict | None = None):
    import json
    _registry_execute(
        "INSERT INTO registry.service_instances (service_id, status, failure) "
        "VALUES (%s, %s, %s::jsonb)",
        (service_id, status,
         json.dumps(failure) if failure is not None else None),
    )


def _make_task(task_id: str, service_id: str | None, result: dict | None,
               *, finished: datetime | None = None):
    import json
    with psycopg.connect(pg_test_db.registry_url()) as conn:
        conn.execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
            "kind, operation, status, result, created_at, updated_at, finished_at) "
            "VALUES (%s, %s, %s, 'deploy', 'generate', 'completed', %s::jsonb, "
            "%s, %s, %s)",
            (task_id, str(uuid.uuid4()), service_id,
             json.dumps(result) if result is not None else None,
             _now(), _now(), finished or _now()),
        )
        conn.commit()


def _hold(index: int = 1) -> dict:
    return {"hold": {
        "index": index,
        "previous_paused_present": True,
        "alert": "previous paused conf was not restored",
        "active": "/gen/svc.stream.conf",
        "held_as": "/gen/svc.stream.conf.paused.1",
        "existing": [{"path": "/gen/svc.stream.conf.paused",
                      "mtime": "2026-11-17T10:00:00", "size": 12}],
    }}


def _fact(key: str, **kw) -> dict:
    base = dict(
        condition_key=key, type=cs.TYPE_ORPHAN, severity="warning",
        title="t", message="m", context={"k": 1}, service_id=None,
        user_name=None, service_name=None, label=None,
    )
    base.update(kw)
    return base


def test_event_ingest_is_idempotent_and_keeps_read_state():
    api = _make_api_user("holder")
    sid = _make_service(api, "svc")
    _make_task("api-hold-1", sid, _hold())
    _make_task("api-normal", sid, {"ok": True})

    db, reader = SessionLocal(), ReaderSessionLocal()
    try:
        alert_service.ingest_events(db, reader)
        rows = db.query(Alert).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "event" and row.type == "stream_conf_hold_collision"
        assert row.source_key == "hold:api-hold-1"
        assert row.context["held_as"].endswith(".paused.1")
        assert row.user_name == "holder" and row.service_name == "svc"

        alert_service.mark(db, [str(row.id)], read=True, actor_id=None)
        first_read = row.read_at
        alert_service.ingest_events(db, reader)
        db.expire_all()
        row = db.query(Alert).one()
        assert row.read_at == first_read, "re-ingest must preserve read state"
    finally:
        db.close()
        reader.close()


def test_condition_upsert_resolve_and_recurrence():
    db = SessionLocal()
    try:
        fact = _fact("orphan-user:alice", user_name="alice")
        results = {cs.SOURCE_ORPHAN: cs.SourceResult(True, [fact])}
        alert_service.evaluate_conditions(db, results)
        active = db.query(Alert).filter(Alert.status == "active").all()
        assert len(active) == 1
        first_id = active[0].id
        first_seen = active[0].last_seen_at

        alert_service.evaluate_conditions(db, results)
        db.expire_all()
        active = db.query(Alert).filter(Alert.status == "active").all()
        assert len(active) == 1 and active[0].id == first_id
        assert active[0].last_seen_at >= first_seen

        # Cleared → resolved, never deleted.
        alert_service.evaluate_conditions(
            db, {cs.SOURCE_ORPHAN: cs.SourceResult(True, [])})
        db.expire_all()
        resolved = db.query(Alert).filter(Alert.status == "resolved").all()
        assert len(resolved) == 1 and resolved[0].resolved_at is not None
        assert resolved[0].read_at is None

        # Recurrence → a NEW active row with fresh read state.
        alert_service.evaluate_conditions(db, results)
        db.expire_all()
        rows = db.query(Alert).order_by(Alert.created_at).all()
        assert len(rows) == 2
        assert sum(1 for r in rows if r.status == "active") == 1
        assert sum(1 for r in rows if r.status == "resolved") == 1
        active_row = next(r for r in rows if r.status == "active")
        assert active_row.read_at is None
    finally:
        db.close()


def test_unreadable_source_opens_and_resolves_nothing():
    db = SessionLocal()
    try:
        fact = _fact("service:" + str(uuid.uuid4()))
        alert_service.evaluate_conditions(
            db, {cs.SOURCE_SERVICE: cs.SourceResult(True, [fact])})
        row = db.query(Alert).one()
        before = row.last_seen_at

        # Unreadable with an empty fact list must not resolve the row.
        alert_service.evaluate_conditions(
            db, {cs.SOURCE_SERVICE: cs.SourceResult(False, [])})
        db.expire_all()
        row = db.query(Alert).one()
        assert row.status == "active"
        assert row.last_seen_at == before

        # Unreadable with a fact must not open or update anything.
        alert_service.evaluate_conditions(
            db, {cs.SOURCE_SERVICE: cs.SourceResult(False, [fact])})
        db.expire_all()
        assert db.query(Alert).one().last_seen_at == before

        # A readable empty pass resolves it.
        alert_service.evaluate_conditions(
            db, {cs.SOURCE_SERVICE: cs.SourceResult(True, [])})
        db.expire_all()
        assert db.query(Alert).one().status == "resolved"
    finally:
        db.close()


def test_read_state_mark_unread_missing_and_read_all():
    db = SessionLocal()
    try:
        now = _now()
        a = Alert(id=uuid.uuid4(), kind="event", source_key="hold:a",
                  status="active", type="t", severity="warning", title="a",
                  message="a", created_at=now, updated_at=now)
        b = Alert(id=uuid.uuid4(), kind="event", source_key="hold:b",
                  status="active", type="t", severity="warning", title="b",
                  message="b", created_at=now, updated_at=now)
        db.add_all([a, b])
        db.commit()
        actor1, actor2 = str(uuid.uuid4()), str(uuid.uuid4())

        out = alert_service.mark(db, [str(a.id)], read=True, actor_id=actor1)
        assert out == {"updated": 1, "missing": []}
        db.expire_all()
        assert str(db.get(Alert, a.id).read_by) == actor1

        # A repeated mark keeps the FIRST acknowledger.
        out = alert_service.mark(db, [str(a.id)], read=True, actor_id=actor2)
        assert out["updated"] == 0
        db.expire_all()
        assert str(db.get(Alert, a.id).read_by) == actor1

        out = alert_service.mark(db, [str(a.id), "not-a-uuid"], read=False,
                                 actor_id=actor2)
        assert out["updated"] == 1 and out["missing"] == ["not-a-uuid"]
        db.expire_all()
        assert db.get(Alert, a.id).read_at is None

        out = alert_service.read_all(db, actor2)
        assert out == {"updated": 2}
        db.expire_all()
        assert all(r.read_at is not None for r in db.query(Alert).all())
        assert alert_service.counts(db)["unread"] == 0
    finally:
        db.close()


def test_list_filters_search_escaping_and_counts():
    db = SessionLocal()
    try:
        now = _now()
        db.add_all([
            Alert(id=uuid.uuid4(), kind="event", source_key="hold:1",
                  status="active", type="stream_conf_hold_collision",
                  severity="warning", title="Hold", message="100% done",
                  created_at=now, updated_at=now),
            Alert(id=uuid.uuid4(), kind="condition", condition_key="acl:drift",
                  status="resolved", type="acl_drift", severity="error",
                  title="ACL", message="100 done",
                  created_at=now, updated_at=now,
                  resolved_at=now, last_seen_at=now),
        ])
        db.commit()
        hits = alert_service.list_alerts(db, q="100%")
        assert hits["total"] == 1, "% is literal, not a wildcard"
        assert hits["alerts"][0]["type"] == "stream_conf_hold_collision"

        active = alert_service.list_alerts(db, status="active", read="unread")
        assert active["total"] == 1 and active["unread"] == 1
        resolved = alert_service.list_alerts(db, status="resolved")
        assert resolved["total"] == 1
        assert alert_service.counts(db) == {"unread": 1, "total": 2, "active": 1}
        assert alert_service.list_alerts(db, limit=0)["alerts"] == []
    finally:
        db.close()


def test_sync_source_records_its_outcome(monkeypatch):
    """T6 — every sync_source completion overwrites the durable state row."""
    from app.services.cross_orchestrator import cross_orchestrator

    sid = str(uuid.uuid4())

    async def deferred(_sid):
        return {"ok": False, "outcome": "deferred",
                "reason": "queue_full", "task_id": "api-9"}

    monkeypatch.setattr(cross_orchestrator, "_sync_source_once", deferred)
    result = asyncio.run(cross_orchestrator.sync_source(sid))
    assert result["outcome"] == "deferred"

    db = SessionLocal()
    try:
        row = db.get(GrantSyncState, uuid.UUID(sid))
        assert row is not None
        assert row.last_outcome == "deferred"
        assert row.reason == "queue_full" and row.task_id == "api-9"
    finally:
        db.close()


def test_alert_api_contract(monkeypatch):
    """T2 — list/count/read shapes, escaping, limit bounds and admin gate."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.middleware import require_admin
    from app.services import provision_service as ps_mod

    async def edge_on(_name, timeout=2.0):
        return {}

    monkeypatch.setattr(ps_mod.provision_service, "container_env", edge_on)

    db = SessionLocal()
    try:
        now = _now()
        row = Alert(id=uuid.uuid4(), kind="event", source_key="hold:api",
                    status="active", type="stream_conf_hold_collision",
                    severity="warning", title="Hold", message="100% done",
                    created_at=now, updated_at=now)
        db.add(row)
        db.commit()
        alert_id = str(row.id)
    finally:
        db.close()

    admin = {"id": str(uuid.uuid4()), "username": "admin", "role": "admin",
             "api_user_id": str(uuid.uuid4())}
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        client = TestClient(app)
        got = client.get("/api/alerts", params={"limit": 50})
        assert got.status_code == 200
        body = got.json()
        assert set(body) == {"alerts", "total", "unread", "limit", "offset"}
        assert body["alerts"][0]["read"] is False

        assert client.get("/api/alerts", params={"q": "100%"}).json()["total"] == 1
        assert client.get("/api/alerts", params={"q": "%"}).json()["total"] == 1
        assert client.get("/api/alerts", params={"limit": 201}).status_code == 422

        got = client.get("/api/alerts/count")
        assert got.status_code == 200
        assert got.json() == {"unread": 1, "total": 1, "active": 1}

        got = client.post("/api/alerts/read",
                          json={"ids": [alert_id, "not-a-uuid"]})
        assert got.json() == {"updated": 1, "missing": ["not-a-uuid"]}
        assert client.get("/api/alerts/count").json()["unread"] == 0
        listed = client.get("/api/alerts").json()["alerts"][0]
        assert listed["read"] is True and listed["read_by"] == admin["id"]

        got = client.post("/api/alerts/read-all")
        assert got.status_code == 200 and "updated" in got.json()
    finally:
        app.dependency_overrides.pop(require_admin, None)




def test_service_failed_reads_failed_deploy_sessions(monkeypatch):
    """CH16 — a failed deploy session is a service failure, gateway-only.

    A deploy-only failed session opens the fact; a generate-only failure does
    not; when both a session and a registered instance are failed for the same
    service id, the instance is the source; deleting the session clears the
    session-only fact. No `-api` change is involved.
    """
    from app.models.pre_deploy_session import PreDeploySession
    from app.services import provision_service as ps_mod

    api = _make_api_user("deployer")
    session_id = str(uuid.uuid4())
    gen_id = str(uuid.uuid4())
    both_id = str(uuid.uuid4())
    _make_service(api, "both-svc", sid=both_id)
    _make_instance(both_id, "failed", {"code": "build_failed"})

    db, reader = SessionLocal(), ReaderSessionLocal()
    try:
        for sid, operation, name in (
            (session_id, "deploy", "session-svc"),
            (gen_id, "generate", "gen-svc"),
            (both_id, "deploy", "both-svc"),
        ):
            db.add(PreDeploySession(
                service_id=uuid.UUID(sid), procedure_id=uuid.uuid4(),
                owner_api_user_id=uuid.UUID(api), service_name=name,
                service_label="0", recipe_path="", state="failed",
                operation=operation, reason="deploy_failed",
                detail="build failed", created_at=_now(), updated_at=_now(),
            ))
        db.commit()

        async def edge_on(_name, timeout=2.0):
            return {}

        monkeypatch.setattr(ps_mod.provision_service, "container_env", edge_on)
        results = asyncio.run(cs.collect(db, reader))
        facts = {f["condition_key"]: f for f in results[cs.SOURCE_SERVICE].facts}

        assert f"service:{session_id}" in facts
        assert facts[f"service:{session_id}"]["context"]["source"] == "pre_deploy_session"
        assert facts[f"service:{session_id}"]["context"]["detail"] == "build failed"
        assert f"service:{gen_id}" not in facts, (
            "a generate-only failure is not a service failure")
        assert facts[f"service:{both_id}"]["context"]["source"] == "instance", (
            "the registered instance wins when both records exist")

        db.query(PreDeploySession).filter_by(
            service_id=uuid.UUID(session_id)).delete()
        db.commit()
        db.expire_all()
        results = asyncio.run(cs.collect(db, reader))
        facts = {f["condition_key"] for f in results[cs.SOURCE_SERVICE].facts}
        assert f"service:{session_id}" not in facts
        assert f"service:{both_id}" in facts
    finally:
        db.close()
        reader.close()


def test_acl_state_rule_excludes_disabled_and_unknown(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ENABLE_ACL", True)
    assert acl_state(None)["edge"] is None
    assert acl_state({"ENABLE_ACL": "false"})["consistent"] is False
    assert acl_state({})["consistent"] is True  # absent edge value means on

    monkeypatch.setattr(settings, "ENABLE_ACL", False)
    both_off = acl_state({"ENABLE_ACL": "false"})
    assert both_off["enabled"] is False and both_off["consistent"] is True


def test_condition_sources_collect_orphan_service_and_grant(monkeypatch):
    from app.services import provision_service as ps_mod

    gone_api = _make_api_user("gone", deleted=True)
    gone_svc = _make_service(gone_api, "orphan-svc", "3")
    live_api = _make_api_user("live")
    live_svc = _make_service(live_api, "ok-svc")
    _make_instance(live_svc, "failed", {"code": "build_failed"})

    db, reader = SessionLocal(), ReaderSessionLocal()
    try:
        state = GrantSyncState(source_service_id=uuid.UUID(live_svc),
                               last_outcome="deferred",
                               reason="queue_full", task_id="api-1",
                               updated_at=_now())
        db.add(state)
        db.commit()

        async def edge_off(_name, timeout=2.0):
            return {"ENABLE_ACL": "false"}

        monkeypatch.setattr(ps_mod.provision_service, "container_env", edge_off)
        results = asyncio.run(cs.collect(db, reader))

        assert results[cs.SOURCE_ORPHAN].readable
        assert [f["condition_key"] for f in results[cs.SOURCE_ORPHAN].facts] == [
            "orphan-user:gone"]
        assert results[cs.SOURCE_ORPHAN].facts[0]["context"]["services"][0][
            "service_id"] == gone_svc
        assert [f["condition_key"] for f in results[cs.SOURCE_SERVICE].facts] == [
            f"service:{live_svc}"]
        assert results[cs.SOURCE_SERVICE].facts[0]["context"]["failure"] == {
            "code": "build_failed"}
        assert [f["condition_key"] for f in results[cs.SOURCE_GRANT].facts] == [
            f"grant-sync:{live_svc}"]
        assert results[cs.SOURCE_ACL].facts[0]["condition_key"] == "acl:drift"

        # A successful later sync overwrites the durable row → no fact.
        alert_service.record_grant_sync_outcome(
            live_svc, {"outcome": "ok", "reason": None, "task_id": "api-2"})
        db.expire_all()
        results = asyncio.run(cs.collect(db, reader))
        assert results[cs.SOURCE_GRANT].facts == []
        assert db.get(GrantSyncState, uuid.UUID(live_svc)).last_outcome == "ok"

        # A tombstoned/absent instance removes the failed fact.
        _registry_execute(
            "UPDATE registry.service_instances SET status='running' "
            "WHERE service_id=%s", (live_svc,))
        results = asyncio.run(cs.collect(db, reader))
        assert results[cs.SOURCE_SERVICE].facts == []

        # Unreadable edge is unknown, not a drift.
        async def edge_down(_name, timeout=2.0):
            raise RuntimeError("edge down")

        monkeypatch.setattr(ps_mod.provision_service, "container_env", edge_down)
        results = asyncio.run(cs.collect(db, reader))
        assert results[cs.SOURCE_ACL].readable is False
        assert results[cs.SOURCE_ACL].facts == []
    finally:
        db.close()
        reader.close()

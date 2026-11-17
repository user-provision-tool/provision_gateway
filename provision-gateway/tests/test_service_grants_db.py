"""G83/G84 — the service-grant matrix against the real (test) PostgreSQL store.

Same-owner is implicit. Every cross-owner target — public or private — needs an
explicit grant (G84); a private one also needs an active account grant, a public
one is waived. There is no transitivity; both services must be live. The
effective/path set is same-owner + explicit only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

import pg_test_db

pg_test_db.provision()

from app.database import SessionLocal, init_db  # noqa: E402 — URLs set by conftest
from app.models.acl_grant import STATE_ACTIVE, AclGrant  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_login import UserLogin  # noqa: E402
from app.services import service_grants  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _gateway_schema():
    init_db()


@pytest.fixture(autouse=True)
def _clean():
    pg_test_db.truncate()
    yield


def _now():
    return datetime.now(timezone.utc)


def _registry_execute(sql: str, params: tuple):
    """Registry writes are owned by registry_app; the gateway role only reads."""
    with psycopg.connect(pg_test_db.registry_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _make_owner(db, username: str, role: str = "viewer", public: bool = False):
    api_user_id = uuid.uuid4()
    _registry_execute(
        "INSERT INTO registry.api_users (id, username, state, created_at) "
        "VALUES (%s, %s, 'live', %s)",
        (api_user_id, username, _now()),
    )
    user = User(
        id=uuid.uuid4(), api_user_id=api_user_id, role=role,
        password_hash="x", is_approved=True, is_active=True,
        is_public_shared=public, deletion_state="none", created_at=_now(),
    )
    db.add(user)
    db.add(UserLogin(user_id=user.id, login=username, kind="username"))
    db.commit()
    return user


def _make_service(db, owner, name: str, label: str = "0", status: str = "running",
                  domain: str = "localhost"):
    sid = uuid.uuid4()
    owner_api_id = owner.api_user_id
    _registry_execute(
        "INSERT INTO registry.services (id, owner_api_user_id, service_name, label, "
        "recipe_path, created_at) VALUES (%s, %s, %s, %s, '.', %s)",
        (sid, owner_api_id, name, label, _now()),
    )
    _registry_execute(
        "INSERT INTO registry.service_instances (service_id, status, hostname, "
        "container_names, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (sid, status, f"{name}-{label}.{domain}", [f"{name}-{label}-app"],
         _now(), _now()),
    )
    _registry_execute(
        "INSERT INTO registry.service_endpoints (service_id, served_name, kind) "
        "VALUES (%s, %s, 'http')",
        (sid, f"{name}-{label}--app.{domain}"),
    )
    return str(sid)


def _account_grant(db, grantee: User, shared: User):
    db.add(AclGrant(grantee_user_id=grantee.id, shared_user_id=shared.id,
                    state=STATE_ACTIVE, created_at=_now(), updated_at=_now()))
    db.commit()


def test_same_owner_is_implicit():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        _make_service(db, alice, "svc-a", "0")
        _make_service(db, alice, "svc-b", "0")
        source = service_grants.entries_by_id()
        sid = next(s for s, e in source.items() if e["service_name"] == "svc-a")
        targets = service_grants.targets_for(db, source[sid])
        assert [t["service_name"] for t in targets] == ["svc-b"]
        assert targets[0]["reason"] == service_grants.REASON_SAME_OWNER
    finally:
        db.close()


def test_public_target_is_not_implicit_but_is_grantable_without_an_account_grant():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        pub = _make_owner(db, "public-owner", role="special", public=True)
        a = _make_service(db, alice, "svc-a", "0")
        p = _make_service(db, pub, "public-svc", "0")
        entries = service_grants.entries_by_id()

        # G84 — no row: the public target is not in the source's path set.
        assert service_grants.targets_for(db, entries[a]) == []
        assert service_grants.reachable_domains(db) == set()

        # The public row is accepted WITHOUT an account grant, and only then
        # does the target enter the effective set.
        service_grants.set_grant(db, a, p)
        targets = service_grants.targets_for(db, entries[a])
        assert [(t["service_name"], t["reason"]) for t in targets] == [
            ("public-svc", service_grants.REASON_GRANT)
        ]
        assert service_grants.reachable_domains(db) == {"localhost"}
    finally:
        db.close()


def test_cross_owner_requires_both_grants():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        entries = service_grants.entries_by_id()

        # No account grant: the explicit row is refused.
        with pytest.raises(ValueError, match="account grant"):
            service_grants.set_grant(db, src, tgt)

        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
        targets = service_grants.targets_for(db, entries[src])
        assert [(t["service_id"], t["reason"]) for t in targets] == [
            (tgt, service_grants.REASON_GRANT)
        ]
    finally:
        db.close()


def test_no_transitivity_and_dead_target():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        carol = _make_owner(db, "carol")
        a = _make_service(db, alice, "svc-a", "0")
        b = _make_service(db, bob, "svc-b", "0")
        c = _make_service(db, carol, "svc-c", "0")
        dead = _make_service(db, carol, "svc-dead", "0", status="failed")
        _account_grant(db, alice, bob)
        _account_grant(db, bob, carol)
        service_grants.set_grant(db, a, b)
        service_grants.set_grant(db, b, c)

        entries = service_grants.entries_by_id()
        targets = service_grants.targets_for(db, entries[a])
        names = {t["service_id"] for t in targets}
        assert c not in names and dead not in names  # no transitivity, dead excluded
        assert b in names
    finally:
        db.close()


def test_same_owner_never_takes_a_row_and_a_public_target_takes_one_without_account_grant():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        pub = _make_owner(db, "public-owner", role="special", public=True)
        a = _make_service(db, alice, "svc-a", "0")
        a2 = _make_service(db, alice, "svc-a2", "0")
        p = _make_service(db, pub, "public-svc", "0")
        with pytest.raises(ValueError, match="same-owner"):
            service_grants.set_grant(db, a, a2)
        # G84 — no ValueError for the public target; the row is created.
        row = service_grants.set_grant(db, a, p)
        assert row["target_service_id"] == p
    finally:
        db.close()


def test_reachable_domains_follow_the_effective_set():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        _make_service(db, alice, "svc-a", "0", domain="localhost")
        _make_service(db, alice, "svc-b", "0", domain="snaprovision.com")
        assert service_grants.reachable_domains(db) == {"localhost", "snaprovision.com"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# G95 R7 — the relationship set vs the reachable (wildcard) set
# ---------------------------------------------------------------------------


def _set_instance_status(sid: str, status: str | None) -> None:
    if status is None:
        _registry_execute(
            "DELETE FROM registry.service_instances WHERE service_id=%s", (sid,))
    else:
        _registry_execute(
            "UPDATE registry.service_instances SET status=%s WHERE service_id=%s",
            (status, sid))


def _tombstone(sid: str) -> None:
    _registry_execute(
        "UPDATE registry.services SET deleted_at=%s WHERE id=%s", (_now(), sid))


def test_relationship_set_ignores_target_runtime_state():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)

        # A failed/building/stopped target still holds the relationship.
        for status in ("failed", "building", "unknown"):
            _set_instance_status(tgt, status)
            entries = service_grants.entries_by_id()
            assert [t["service_id"] for t in service_grants.targets_for(db, entries[src])] == [tgt]
            # ...but it contributes no dnsmasq wildcard.
            assert service_grants.reachable_domains(db) == set()

        # A stopped SOURCE keeps its relationship too.
        _set_instance_status(tgt, "running")
        _set_instance_status(src, "unknown")
        entries = service_grants.entries_by_id()
        assert [t["service_id"] for t in service_grants.targets_for(db, entries[src])] == [tgt]
        assert service_grants.reachable_domains(db) == set()
    finally:
        db.close()


def test_instance_only_removal_keeps_the_relationship():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
        # scope=instance: the service row AND the grant remain.
        _set_instance_status(tgt, None)
        entries = service_grants.entries_by_id()
        assert [t["service_id"] for t in service_grants.targets_for(db, entries[src])] == [tgt]
        assert service_grants.reachable_domains(db) == set()
    finally:
        db.close()


def test_tombstoned_target_ends_the_relationship():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
        _tombstone(tgt)
        entries = service_grants.entries_by_id()
        assert tgt not in entries
        assert service_grants.targets_for(db, entries[src]) == []
    finally:
        db.close()


def test_service_delete_removes_grants_in_both_directions():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        carol = _make_owner(db, "carol")
        a = _make_service(db, alice, "svc-a", "0")
        b = _make_service(db, bob, "svc-b", "0")
        c = _make_service(db, carol, "svc-c", "0")
        _account_grant(db, alice, bob)
        _account_grant(db, bob, carol)
        service_grants.set_grant(db, a, b)   # inbound to b
        service_grants.set_grant(db, b, c)   # outbound from b
        # c -> a is unrelated to b and must survive the delete.
        _account_grant(db, carol, alice)
        service_grants.set_grant(db, c, a)

        removed = service_grants.remove_grants_for_service(db, b)
        assert removed == 2, "both the outbound and inbound rows go"
        unrelated = {(str(r.source_service_id), str(r.target_service_id))
                     for r in service_grants.explicit_rows(db, c)}
        assert unrelated == {(c, a)}, "the unrelated c -> a row stays"
        assert service_grants.explicit_rows(db, a) == []
    finally:
        db.close()


def test_stale_row_is_marked_and_removable():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
        assert service_grants.list_for_source(db, service_grants.entries_by_id()[src])[
            "explicit"][0]["stale"] is False

        # Revoke the account prerequisite: the row is stale, not silently gone.
        db.query(AclGrant).delete()
        db.commit()
        body = service_grants.list_for_source(db, service_grants.entries_by_id()[src])
        row = body["explicit"][0]
        assert row["stale"] is True
        assert row["stale_reason"] == service_grants.STALE_ACCOUNT_MISSING
        assert body["effective"] == []

        # One action removes it.
        assert service_grants.remove_grant(db, src, tgt) is True
        assert service_grants.list_for_source(
            db, service_grants.entries_by_id()[src])["explicit"] == []
    finally:
        db.close()


def test_a_tombstoned_target_marks_the_row_stale():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
        _tombstone(tgt)
        body = service_grants.list_for_source(db, service_grants.entries_by_id()[src])
        assert body["explicit"][0]["stale_reason"] == service_grants.STALE_TARGET_DELETED
    finally:
        db.close()


def test_hold_alerts_come_from_the_task_rows():
    import json
    from app.database import ReaderSessionLocal
    from app.services.task_queue import GatewayQueue

    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        sid = _make_service(db, alice, "svc-a", "0")
        task_id = f"api-{uuid.uuid4()}"
        _registry_execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
            "kind, operation, status, created_at, updated_at, result) "
            "VALUES (%s, %s, %s, 'remove', 'remove', 'completed', %s, %s, %s::jsonb)",
            (task_id, uuid.uuid4(), sid, _now(), _now(), json.dumps({
                "hold": {
                    "index": 2, "previous_paused_present": True,
                    "alert": "previous paused conf was not restored",
                    "existing": [{"path": "svc.stream.conf.paused",
                                  "mtime": "2026-11-17T00:00:00+00:00",
                                  "size": 1234}],
                    "held_as": "svc.stream.conf.paused.2",
                    "active": "svc.stream.conf",
                },
                "comment": "stream-conf hold used index 2",
            })),
        )
        # A normal hold (no index) writes no `hold` record and must not appear.
        normal_id = f"api-{uuid.uuid4()}"
        _registry_execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
            "kind, operation, status, created_at, updated_at, result) "
            "VALUES (%s, %s, %s, 'remove', 'remove', 'completed', %s, %s, %s::jsonb)",
            (normal_id, uuid.uuid4(), sid, _now(), _now(),
             json.dumps({"comment": None})),
        )
    finally:
        db.close()

    rd = ReaderSessionLocal()
    try:
        alerts = GatewayQueue()._hold_alerts(rd)
    finally:
        rd.close()
    assert [a["task_id"] for a in alerts] == [task_id]
    assert alerts[0]["index"] == 2
    assert alerts[0]["service_name"] == "svc-a"
    assert alerts[0]["user_name"] == "alice"
    # §15.3 — the hold evidence fields are exposed for the dashboard.
    assert alerts[0]["held_as"] == "svc.stream.conf.paused.2"
    assert alerts[0]["active"] == "svc.stream.conf"
    assert alerts[0]["existing"] == [{
        "path": "svc.stream.conf.paused",
        "mtime": "2026-11-17T00:00:00+00:00",
        "size": 1234,
    }]


def test_hold_alerts_render_old_and_malformed_rows_defensively():
    """An older -api row has no evidence fields; nothing raises."""
    import json
    from app.database import ReaderSessionLocal
    from app.services.task_queue import GatewayQueue

    db = SessionLocal()
    try:
        alice = _make_owner(db, "old-hold")
        sid = _make_service(db, alice, "svc-old", "0")
        old_id = f"api-{uuid.uuid4()}"
        _registry_execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
            "kind, operation, status, created_at, updated_at, result) "
            "VALUES (%s, %s, %s, 'remove', 'remove', 'completed', %s, %s, %s::jsonb)",
            (old_id, uuid.uuid4(), sid, _now(), _now(), json.dumps({
                "hold": {"index": 1, "alert": "held"}  # no existing/held_as/active
            })),
        )
        malformed_id = f"api-{uuid.uuid4()}"
        _registry_execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, "
            "kind, operation, status, created_at, updated_at, result) "
            "VALUES (%s, %s, %s, 'remove', 'remove', 'completed', %s, %s, %s::jsonb)",
            (malformed_id, uuid.uuid4(), sid, _now(), _now(), json.dumps({
                "hold": {"alert": "held", "existing": "not-a-list"}
            })),
        )
    finally:
        db.close()

    rd = ReaderSessionLocal()
    try:
        alerts = GatewayQueue()._hold_alerts(rd)
    finally:
        rd.close()
    by_id = {a["task_id"]: a for a in alerts}
    assert by_id[old_id]["existing"] == []
    assert by_id[old_id]["held_as"] is None
    assert by_id[old_id]["active"] is None
    assert by_id[malformed_id]["existing"] == []


def test_grant_cleanup_runs_only_after_the_tombstone():
    """R3/R7 — a failed pre-tombstone remove must NOT lose the grants."""
    from app.routers.users import _remove_service_grants

    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        src = _make_service(db, alice, "svc-a", "0")
        tgt = _make_service(db, bob, "svc-b", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, src, tgt)
    finally:
        db.close()

    # Live service: the rows survive a failed remove.
    assert _remove_service_grants(tgt) == 0
    remaining = service_grants.entries_by_id()
    db = SessionLocal()
    try:
        assert service_grants.explicit_rows(db, src), "the live target's grant stays"
    finally:
        db.close()

    _tombstone(tgt)
    assert _remove_service_grants(tgt) == 1
    db = SessionLocal()
    try:
        assert service_grants.explicit_rows(db, src) == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# R14/S10 — the affected-source set for account-level changes
# ---------------------------------------------------------------------------


def test_account_grant_affected_sources_are_directional():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        carol = _make_owner(db, "carol")
        a1 = _make_service(db, alice, "a1", "0")
        a2 = _make_service(db, alice, "a2", "0")
        b1 = _make_service(db, bob, "b1", "0")
        b2 = _make_service(db, bob, "b2", "0")
        c1 = _make_service(db, carol, "c1", "0")
        _account_grant(db, alice, bob)
        _account_grant(db, alice, carol)
        service_grants.set_grant(db, a1, b1)
        service_grants.set_grant(db, a2, b2)
        service_grants.set_grant(db, a1, c1)

        affected = service_grants.affected_sources_for_account_change(
            db, account_grant=(str(alice.id), str(bob.id)))
        assert affected == sorted([a1, a2])
        # The account grant is directional: B → A has no rows.
        assert service_grants.affected_sources_for_account_change(
            db, account_grant=(str(bob.id), str(alice.id))) == []
    finally:
        db.close()


def test_account_toggle_affected_sources_include_own_and_inbound_rows():
    db = SessionLocal()
    try:
        alice = _make_owner(db, "alice")
        bob = _make_owner(db, "bob")
        a1 = _make_service(db, alice, "a1", "0")
        b1 = _make_service(db, bob, "b1", "0")
        b2 = _make_service(db, bob, "b2", "0")
        _account_grant(db, alice, bob)
        _account_grant(db, bob, alice)
        service_grants.set_grant(db, a1, b1)   # inbound to bob
        service_grants.set_grant(db, b2, a1)   # own outbound from bob

        affected = service_grants.affected_sources_for_account_change(
            db, changed_user_id=str(bob.id))
        assert affected == sorted([a1, b2])
        # Alice's toggle sees her own outbound row and the inbound row b2 -> a1.
        affected_a = service_grants.affected_sources_for_account_change(
            db, changed_user_id=str(alice.id))
        assert affected_a == sorted([a1, b2])
    finally:
        db.close()


def test_account_toggle_excludes_same_owner_and_absent_ends():
    db = SessionLocal()
    try:
        bob = _make_owner(db, "bob")
        ba = _make_service(db, bob, "ba", "0")
        bb = _make_service(db, bob, "bb", "0")
        # A same-owner row never exists through the API, but a stale/hand-made
        # row must not make the account change sweep a same-owner source.
        db.add(service_grants.ServiceGrant(
            source_service_id=uuid.UUID(ba), target_service_id=uuid.UUID(bb),
            created_at=_now(), updated_at=_now()))
        db.commit()
        assert service_grants.affected_sources_for_account_change(
            db, changed_user_id=str(bob.id)) == []

        # A row whose target is tombstoned is not a relationship: excluded.
        alice = _make_owner(db, "alice")
        a1 = _make_service(db, alice, "a1", "0")
        b1 = _make_service(db, bob, "b1", "0")
        _account_grant(db, alice, bob)
        service_grants.set_grant(db, a1, b1)
        _tombstone(b1)
        assert service_grants.affected_sources_for_account_change(
            db, changed_user_id=str(bob.id)) == []
    finally:
        db.close()

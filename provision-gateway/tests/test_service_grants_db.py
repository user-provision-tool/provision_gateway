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

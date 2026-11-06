"""Thin dashboard/CLI migration — workflow unit + integration tests (T1–T10).

DB-backed tests use the dedicated PostgreSQL test database (``pg_test_db``);
pure mapping/policy tests run with no gateway and no store.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

import pg_test_db

pg_test_db.provision()

from app.database import SessionLocal, init_db  # noqa: E402 — URLs set by conftest
from app.models.pre_deploy_session import (  # noqa: E402
    STATE_DEPLOYING, STATE_GENERATED, STATE_GENERATING, STATE_PAUSED,
)
from app.models.user import User  # noqa: E402
from app.models.user_login import UserLogin  # noqa: E402
from app.services import (  # noqa: E402
    deploy_workflow, label_reservations, pre_deploy_registry as pdr, service_grants,
)


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
    with psycopg.connect(pg_test_db.registry_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _make_api_user(username: str) -> str:
    api_user_id = uuid.uuid4()
    _registry_execute(
        "INSERT INTO registry.api_users (id, username, state, created_at) "
        "VALUES (%s, %s, 'live', %s)",
        (api_user_id, username, _now()),
    )
    return str(api_user_id)


def _make_owner(db, username: str, *, public: bool = False):
    api_user_id = _make_api_user(username)
    user = User(
        id=uuid.uuid4(), api_user_id=uuid.UUID(api_user_id),
        role="special" if public else "viewer",
        password_hash="x", is_approved=True, is_active=True,
        is_public_shared=public, deletion_state="none", created_at=_now(),
    )
    db.add(user)
    db.add(UserLogin(user_id=user.id, login=username, kind="username"))
    db.commit()
    return user


def _make_task(task_id: str):
    """A gateway task row — the session FK requires a real task id."""
    with psycopg.connect(pg_test_db.gateway_url()) as conn:
        conn.execute(
            "INSERT INTO gateway.tasks (task_id, collision_token, procedure_id, "
            "executor, kind, operation, origin, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'gateway', 'generate', 'generate_missing', "
            "'deploy', 'paused', now(), now())",
            (task_id, uuid.uuid4().hex[:12], uuid.uuid4()),
        )
        conn.commit()


def _make_service(owner_api_user_id: str, name: str, label: str = "0",
                  status: str = "running"):
    sid = uuid.uuid4()
    _registry_execute(
        "INSERT INTO registry.services "
        "(id, owner_api_user_id, service_name, label, recipe_path, created_at) "
        "VALUES (%s, %s, %s, %s, '', %s)",
        (sid, owner_api_user_id, name, label, _now()),
    )
    _registry_execute(
        "INSERT INTO registry.service_instances "
        "(service_id, status, hostname, tls, created_at, updated_at) "
        "VALUES (%s, %s, %s, false, %s, %s)",
        (sid, status, f"{name}.localhost", _now(), _now()),
    )
    return str(sid)


def _session(service_id, owner_api_user_id, *, state=STATE_GENERATING,
             operation="deploy", label="0", **kw):
    return pdr.create_or_replace(
        service_id=service_id, procedure_id=str(uuid.uuid4()),
        owner_api_user_id=owner_api_user_id, service_name="svc",
        service_label=label, recipe_path="", state=state, operation=operation, **kw,
    )


# ---------------------------------------------------------------------------
# R5 — label reservations
# ---------------------------------------------------------------------------

def test_auto_label_is_next_and_released_labels_are_reusable():
    owner = _make_api_user("label-owner")
    first_id, second_id, third_id = (str(uuid.uuid4()) for _ in range(3))
    assert label_reservations.reserve(
        owner_api_user_id=owner, service_name="svc", service_id=first_id) == "0"
    assert label_reservations.reserve(
        owner_api_user_id=owner, service_name="svc", service_id=second_id) == "1"
    label_reservations.release(first_id)
    assert label_reservations.reserve(
        owner_api_user_id=owner, service_name="svc", service_id=third_id) == "0"


def test_pinned_label_conflict_is_structured():
    owner = _make_api_user("label-owner-2")
    first = str(uuid.uuid4())
    assert label_reservations.reserve(
        owner_api_user_id=owner, service_name="svc", service_id=first,
        requested_label="7") == "7"
    with pytest.raises(label_reservations.LabelConflict):
        label_reservations.reserve(
            owner_api_user_id=owner, service_name="svc",
            service_id=str(uuid.uuid4()), requested_label="7")


def test_history_reserves_the_label_even_after_service_delete():
    owner = _make_api_user("label-owner-3")
    _make_service(owner, "svc", "4")
    label = label_reservations.reserve(
        owner_api_user_id=owner, service_name="svc", service_id=str(uuid.uuid4()))
    assert label == "5"


# ---------------------------------------------------------------------------
# R1/R2 — the record keeps its operation; the claim is exactly-once
# ---------------------------------------------------------------------------

def test_new_generation_leg_preserves_the_operation():
    owner = _make_api_user("leg-owner")
    sid = str(uuid.uuid4())
    _make_task("gt-old")
    _make_task("gt-new")
    _session(sid, owner, state=STATE_PAUSED, operation="deploy",
             generation_task_id="gt-old")
    pdr.new_generation_leg(
        service_id=sid, procedure_id=str(uuid.uuid4()),
        owner_api_user_id=owner, service_name="svc", service_label="0",
        recipe_path="", generation_task_id="gt-new",
    )
    record = pdr.get_session(sid)
    assert record["operation"] == "deploy"
    assert record["generation_task_id"] == "gt-new"


def test_claim_is_exactly_once():
    owner = _make_api_user("claim-owner")
    sid = str(uuid.uuid4())
    _session(sid, owner, state=STATE_GENERATED, operation="deploy")
    assert pdr.claim_for_deploy(sid) is True
    assert pdr.claim_for_deploy(sid) is False
    assert pdr.get_session(sid)["state"] == STATE_DEPLOYING


# ---------------------------------------------------------------------------
# R13 — the instance wins, even over an operator-held session
# ---------------------------------------------------------------------------

def test_reconcile_drops_an_operator_held_session_when_an_instance_exists():
    owner = _make_api_user("reconcile-owner")
    sid = _make_service(owner, "svc")
    _session(sid, owner, state=STATE_GENERATED, operation="deploy")
    report = pdr.reconcile(set())
    assert sid in report["dropped_stale"]
    assert pdr.get_session(sid) is None


def test_reconcile_keeps_an_operator_held_session_without_an_instance():
    owner = _make_api_user("reconcile-owner-2")
    sid = str(uuid.uuid4())
    _session(sid, owner, state=STATE_PAUSED, operation="deploy")
    report = pdr.reconcile(set())
    assert sid in report["kept"]


# ---------------------------------------------------------------------------
# R10 — the set apply is atomic; the account candidates are grouped
# ---------------------------------------------------------------------------

def test_apply_set_replaces_the_whole_set():
    db = SessionLocal()
    try:
        source_owner = _make_owner(db, "set-source")
        target_owner = _make_owner(db, "set-target", public=True)
        source = _make_service(str(source_owner.api_user_id), "set-source-svc")
        target = _make_service(str(target_owner.api_user_id), "set-target-svc")
        result = service_grants.apply_set(db, source, [target])
        assert [r["target_service_id"] for r in result["explicit"]] == [target]
        empty = service_grants.apply_set(db, source, [])
        assert empty["explicit"] == []
    finally:
        db.close()


def test_apply_set_is_all_or_nothing():
    db = SessionLocal()
    try:
        source_owner = _make_owner(db, "set2-source")
        target_owner = _make_owner(db, "set2-target", public=True)
        source = _make_service(str(source_owner.api_user_id), "set2-source-svc")
        target = _make_service(str(target_owner.api_user_id), "set2-target-svc")
        service_grants.apply_set(db, source, [target])
        with pytest.raises(ValueError):
            service_grants.apply_set(db, source, [target, str(uuid.uuid4())])
        assert [r["target_service_id"]
                for r in service_grants.list_for_source(
                    db, service_grants.entries_by_id()[source])["explicit"]] == [target]
    finally:
        db.close()


def test_account_candidates_group_own_and_public_accounts():
    db = SessionLocal()
    try:
        me = _make_owner(db, "cand-me")
        public = _make_owner(db, "cand-public", public=True)
        _make_service(str(me.api_user_id), "cand-mine")
        _make_service(str(public.api_user_id), "cand-public-svc")
        result = service_grants.account_candidates(db, "cand-me")
        by_name = {g["user_name"]: g for g in result["groups"]}
        assert by_name["cand-me"]["reason"] == "own"
        assert by_name["cand-public"]["is_public"] is True
        assert by_name["cand-public"]["reason"] == "public"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# R1/R6 — the -api payload and the policy
# ---------------------------------------------------------------------------

def test_api_payload_uses_the_final_selection_and_generated_env():
    intent = {
        "service_name": "svc", "user_name": "u", "owner_api_user_id": "o",
        "service_id": "s", "procedure_id": "p", "recipe_path": "",
        "label": "3", "domain": "localhost", "passwd": "secret",
        "tls": True, "prompt": "make it", "allowed_target_service_ids": ["t"],
        "use_global_proxy": True, "selection": {"compose": ["c.yml"]},
    }
    selection = {
        "compose": ["docker-compose.generated-1.yml"], "nginx": "nginx.conf",
        "stream_conf": "svc.stream.conf", "mail_conf": None,
        "env": ["env.raw"], "profiles": ["p1"],
    }
    payload = deploy_workflow._api_payload(intent, selection, "env.raw.u.3")
    assert payload["compose_file_paths"] == ["docker-compose.generated-1.yml"]
    assert payload["nginx_confs"] == {"http": "nginx.conf", "stream": "svc.stream.conf"}
    assert payload["env_files"] == ["env.raw.u.3"]
    assert payload["profiles"] == ["p1"]
    assert payload["label"] == "3"
    assert "prompt" not in payload and "allowed_target_service_ids" not in payload
    assert "passwd" in payload


def test_policy_refuses_missing_nginx_and_stream_without_tls():
    with pytest.raises(deploy_workflow.WorkflowError) as exc:
        deploy_workflow._validate_policy(
            {"service_name": "svc"}, {"compose": ["c.yml"]}, require_ready=True)
    assert exc.value.code == "nginx_required"
    with pytest.raises(deploy_workflow.WorkflowError) as exc:
        deploy_workflow._validate_policy(
            {"service_name": "svc", "https": False},
            {"compose": ["c.yml"], "nginx": "n.conf", "stream_conf": "s.conf"},
            require_ready=True,
        )
    assert exc.value.code == "stream_requires_tls"

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


# ---------------------------------------------------------------------------
# S11/S17 — the env payload precedence and `env_none`
# ---------------------------------------------------------------------------

def _payload_intent():
    return {
        "service_name": "svc", "user_name": "u", "owner_api_user_id": "o",
        "service_id": "s", "procedure_id": "p", "recipe_path": "",
        "label": "3", "domain": "localhost", "tls": True,
        "env_reference": ["reference.env"],
        "llm_validate_env": True,
    }


def test_env_payload_prefers_the_generated_raw():
    selection = {"compose": ["c.yml"], "nginx": "n.conf",
                 "env": ["one.env", "two.env"], "env_none": False}
    payload = deploy_workflow._api_payload(
        _payload_intent(), selection, ".env.raw.u.3")
    assert payload["env_files"] == [".env.raw.u.3"]


def test_env_payload_sends_the_selected_ordered_list_verbatim():
    selection = {"compose": ["c.yml"], "nginx": "n.conf",
                 "env": ["first.env", "second.env"], "env_none": False}
    payload = deploy_workflow._api_payload(_payload_intent(), selection, None)
    assert payload["env_files"] == ["first.env", "second.env"]


def test_env_payload_is_absent_for_env_none_and_for_an_empty_list():
    payload = deploy_workflow._api_payload(
        _payload_intent(),
        {"compose": ["c.yml"], "nginx": "n.conf",
         "env": ["ignored.env"], "env_none": True},
        None)
    assert "env_files" not in payload
    payload = deploy_workflow._api_payload(
        _payload_intent(),
        {"compose": ["c.yml"], "nginx": "n.conf", "env": [], "env_none": False},
        None)
    assert "env_files" not in payload


def test_env_reference_and_guard_flag_never_reach_the_api_payload():
    selection = {"compose": ["c.yml"], "nginx": "n.conf",
                 "env": ["selected.env"], "env_none": False}
    payload = deploy_workflow._api_payload(_payload_intent(), selection, None)
    assert payload["env_files"] == ["selected.env"]
    assert "env_reference" not in payload
    assert "llm_validate_env" not in payload
    assert "env_none" not in payload


def test_env_none_suppresses_env_generation_and_satisfies_readiness(monkeypatch):
    from app.services.generation_jobs import generation_jobs as generation_manager

    monkeypatch.setattr(generation_manager, "_recipe_missing", lambda req: [])
    base = {"service_name": "svc", "recipe_path": "", "selection": {
        "compose": ["c.yml"], "nginx": "n.conf",
        "serving_ports": "ports.yml", "env": [], "env_none": True,
    }}
    assert deploy_workflow.generation_needed(base) is False
    without_none = {"service_name": "svc", "recipe_path": "", "selection": {
        "compose": ["c.yml"], "nginx": "n.conf",
        "serving_ports": "ports.yml", "env": [], "env_none": False,
    }}
    assert deploy_workflow.generation_needed(without_none) is True

    existing = {"selection": {
        "compose": ["c.yml"], "nginx": "n.conf",
        "serving_ports": "ports.yml", "env": [], "env_none": True,
    }, "env_name": ""}
    assert deploy_workflow._session_ready(existing, {"tls": False}) is True


def test_recipe_missing_phase_does_not_override_a_filled_slot(monkeypatch):
    """A recipe without serving-ports must still allow an explicit None.

    §14.1/§14.2: `serving_ports_none` fills the slot, so the deploy is
    validation-only. The recipe-presence scan must not force a generation run
    over the operator's explicit choice (measured live: the S22 guard trace
    entered a generation job for exactly this reason).
    """
    from app.services.generation_jobs import generation_jobs as generation_manager

    monkeypatch.setattr(generation_manager, "_recipe_missing",
                        lambda req: ["serving_ports"])
    filled = {"service_name": "svc", "recipe_path": "", "selection": {
        "compose": ["c.yml"], "nginx": "n.conf", "env": ["e.env"],
        "serving_ports": None, "serving_ports_none": True,
    }}
    assert deploy_workflow.generation_needed(filled) is False

    monkeypatch.setattr(generation_manager, "_recipe_missing",
                        lambda req: ["docker-compose"])
    no_compose = {"service_name": "svc", "recipe_path": "", "selection": {
        "compose": [], "nginx": "n.conf", "env": ["e.env"],
        "serving_ports": None, "serving_ports_none": True,
    }}
    assert deploy_workflow.generation_needed(no_compose) is True


# ---------------------------------------------------------------------------
# S12 — the validation-only mechanical pass
# ---------------------------------------------------------------------------

def _recipe(tmp_path, monkeypatch, *, env_files=("selected.env",)):
    from app.config import settings
    project = tmp_path / "source_projects" / "svc"
    project.mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path / "source_projects")
    (project / "compose.yml").write_text("services:\n  app:\n    image: x\n")
    (project / "nginx.conf").write_text(
        "server {\n  server_name {{ hostname_stem }}--app.localhost;\n"
        "  location / { proxy_pass http://app:8080; }\n}\n")
    (project / "ports.yml").write_text(
        "ports:\n  - sub_service: app\n    port: 8080\n    kind: http\n")
    for name in env_files:
        (project / name).write_text("TOKEN=value\n")
    return project


def _patch_validators(monkeypatch, *, env_result=None):
    from app.services import config_validation, file_sets
    from app.services import serving_ports as serving_ports_mod

    calls: dict[str, object] = {}

    def fake_compose(paths):
        calls["compose"] = [str(p) for p in paths]
        return {"valid": True, "errors": [], "skipped": False}

    def fake_ports(content, paths):
        calls["ports"] = content
        return {"valid": True, "errors": [], "warnings": []}

    async def fake_nginx(path, names):
        calls.setdefault("nginx", []).append(str(path))
        return {"valid": True, "errors": []}

    def fake_env(content, paths):
        calls["env"] = content
        return env_result or {"valid": True, "errors": [], "missing": []}

    def fake_agreement(*args, **kwargs):
        calls.setdefault("agreement", 0)
        calls["agreement"] += 1
        return {"valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(config_validation, "validate_compose", fake_compose)
    monkeypatch.setattr(config_validation, "validate_serving_ports", fake_ports)
    monkeypatch.setattr(config_validation, "validate_nginx", fake_nginx)
    monkeypatch.setattr(config_validation, "validate_env", fake_env)
    monkeypatch.setattr(file_sets, "compose_service_names", lambda paths: ["app"])
    monkeypatch.setattr(serving_ports_mod, "validate_conf_agreement", fake_agreement)
    return calls


def _ready_selection(env=("selected.env",), env_none=False):
    return {
        "compose": ["compose.yml"], "nginx": "nginx.conf",
        "serving_ports": "ports.yml", "env": list(env),
        "env_none": env_none, "profiles": [],
    }


def test_validation_only_pass_runs_every_validator(tmp_path, monkeypatch):
    import asyncio
    _recipe(tmp_path, monkeypatch)
    calls = _patch_validators(monkeypatch)
    asyncio.run(deploy_workflow._validate_selected_artifacts(
        {"service_name": "svc", "recipe_path": "", "tls": False},
        _ready_selection(), None))
    assert calls["compose"] and calls["ports"] and calls["nginx"]
    assert calls["agreement"] == 1, "serving-port/nginx agreement ran"
    assert "TOKEN=value" in calls["env"]


def test_validation_only_failure_is_a_workflow_error(tmp_path, monkeypatch):
    import asyncio
    _recipe(tmp_path, monkeypatch)
    _patch_validators(
        monkeypatch,
        env_result={"valid": False, "errors": ["missing TOKEN"], "missing": ["TOKEN"]},
    )
    with pytest.raises(deploy_workflow.WorkflowError) as exc:
        asyncio.run(deploy_workflow._validate_selected_artifacts(
            {"service_name": "svc", "recipe_path": "", "tls": False},
            _ready_selection(), None))
    assert exc.value.code == "env_invalid"
    assert "missing TOKEN" in exc.value.detail


def test_validation_only_env_none_skips_env_validation(tmp_path, monkeypatch):
    import asyncio
    _recipe(tmp_path, monkeypatch, env_files=())
    calls = _patch_validators(monkeypatch)
    asyncio.run(deploy_workflow._validate_selected_artifacts(
        {"service_name": "svc", "recipe_path": "", "tls": False},
        _ready_selection(env=(), env_none=True), None))
    assert "env" not in calls, "env_none has nothing to validate"


def test_validation_only_pass_happens_without_a_generation_job(tmp_path, monkeypatch):
    """`_start` validates and goes straight to claim when every slot is filled."""
    import asyncio
    from app.services.generation_jobs import generation_jobs as generation_manager

    _recipe(tmp_path, monkeypatch)
    _patch_validators(monkeypatch)
    monkeypatch.setattr(generation_manager, "_recipe_missing", lambda req: [])

    def no_generation(*args, **kwargs):
        raise AssertionError("a validation-only deploy must not create a job")

    monkeypatch.setattr(generation_manager, "create_job", no_generation)

    seen = {}

    async def fake_claim(service_id, intent, selection, env_name):
        seen.update(service_id=service_id, selection=selection, env_name=env_name)
        return {"stage": "deploying"}

    monkeypatch.setattr(deploy_workflow, "_claim_and_register", fake_claim)
    owner = _make_api_user("validation-owner")
    intent = {
        "service_name": "svc", "recipe_path": "", "owner_api_user_id": owner,
        "selection": _ready_selection(), "tls": False,
    }
    result = asyncio.run(deploy_workflow.start_deploy(intent))
    assert result["stage"] == "deploying"
    assert seen["env_name"] is None
    assert seen["selection"]["env"] == ["selected.env"]


# ---------------------------------------------------------------------------
# S13 — the LLM env guard blocks on findings and call failures
# ---------------------------------------------------------------------------

def _guard_selection(env=("selected.env",), env_none=False):
    return {"compose": ["compose.yml"], "nginx": "nginx.conf",
            "env": list(env), "env_none": env_none}


def test_llm_guard_blocks_on_findings(tmp_path, monkeypatch):
    import asyncio
    from app.services import llm_service as llm_mod

    _recipe(tmp_path, monkeypatch)

    async def fake_validate(db, **kwargs):
        return {"valid": False, "findings": ["TOKEN is empty"],
                "guide": "Set TOKEN in selected.env", "error": None}

    monkeypatch.setattr(llm_mod.llm_service, "validate_env_selection", fake_validate)
    with pytest.raises(deploy_workflow.WorkflowError) as exc:
        asyncio.run(deploy_workflow._run_llm_env_guard(
            {"service_name": "svc", "recipe_path": "", "llm_validate_env": True},
            _guard_selection()))
    assert exc.value.code == "llm_env_validation_blocked"
    assert exc.value.findings == ["TOKEN is empty"]
    assert "Set TOKEN" in exc.value.detail


def test_llm_guard_blocks_when_the_llm_call_fails(tmp_path, monkeypatch):
    import asyncio
    from app.services import llm_service as llm_mod

    _recipe(tmp_path, monkeypatch)

    async def fake_validate(db, **kwargs):
        return {"valid": False, "findings": [], "guide": "",
                "error": "HTTP 500"}

    monkeypatch.setattr(llm_mod.llm_service, "validate_env_selection", fake_validate)
    with pytest.raises(deploy_workflow.WorkflowError) as exc:
        asyncio.run(deploy_workflow._run_llm_env_guard(
            {"service_name": "svc", "recipe_path": "", "llm_validate_env": True},
            _guard_selection()))
    assert exc.value.code == "llm_env_validation_blocked"
    assert exc.value.llm_error == "HTTP 500"


def test_llm_guard_never_sends_the_env_reference(tmp_path, monkeypatch):
    import asyncio
    from app.services import llm_service as llm_mod

    _recipe(tmp_path, monkeypatch)
    seen = {}

    async def fake_validate(db, **kwargs):
        seen.update(kwargs)
        return {"valid": True, "findings": [], "guide": "", "error": None}

    monkeypatch.setattr(llm_mod.llm_service, "validate_env_selection", fake_validate)
    asyncio.run(deploy_workflow._run_llm_env_guard(
        {"service_name": "svc", "recipe_path": "", "llm_validate_env": True,
         "env_reference": ["reference.env"]},
        _guard_selection()))
    assert [name for name, _ in seen["env_files"]] == ["selected.env"]


def test_llm_guard_is_skipped_for_env_none_and_unchecked(tmp_path, monkeypatch):
    import asyncio
    from app.services import llm_service as llm_mod

    _recipe(tmp_path, monkeypatch)
    calls = []

    async def fake_validate(db, **kwargs):
        calls.append(kwargs)
        return {"valid": True, "findings": [], "guide": "", "error": None}

    monkeypatch.setattr(llm_mod.llm_service, "validate_env_selection", fake_validate)
    asyncio.run(deploy_workflow._run_llm_env_guard(
        {"service_name": "svc", "recipe_path": "", "llm_validate_env": True},
        _guard_selection(env=(), env_none=True)))
    asyncio.run(deploy_workflow._run_llm_env_guard(
        {"service_name": "svc", "recipe_path": "", "llm_validate_env": False},
        _guard_selection()))
    # A generated `.raw` is what will be sent; the generation path guards it.
    asyncio.run(deploy_workflow._run_llm_env_guard(
        {"service_name": "svc", "recipe_path": "", "llm_validate_env": True},
        _guard_selection(), ".env.raw.u.0"))
    assert calls == []


def test_claim_and_register_pauses_on_a_guard_finding(monkeypatch):
    """S22 — a finding pauses the session through the LLM-fix flow."""
    import asyncio
    from app.services.provision_service import provision_service

    owner = _make_api_user("guard-owner")
    sid = str(uuid.uuid4())
    _session(sid, owner, state=STATE_GENERATED, operation="deploy",
             selection=_guard_selection())
    registered = []

    async def fake_guard(intent, selection, env_name=None):
        exc = deploy_workflow.WorkflowError(
            "llm_env_validation_blocked", "TOKEN is empty")
        exc.findings = ["TOKEN is empty"]
        exc.guide = "Set TOKEN in selected.env"
        raise exc

    async def fake_register(**kwargs):
        registered.append(kwargs)
        return {"task_id": "t"}

    monkeypatch.setattr(deploy_workflow, "_run_llm_env_guard", fake_guard)
    monkeypatch.setattr(provision_service, "register_user", fake_register)
    with pytest.raises(deploy_workflow.WorkflowError) as excinfo:
        asyncio.run(deploy_workflow._claim_and_register(
            sid, {"llm_validate_env": True}, _guard_selection(), None))
    assert excinfo.value.code == "workflow_paused"
    assert registered == [], "nothing may be registered after a guard pause"
    sess = pdr.get_session(sid)
    assert sess["state"] == "paused"
    assert sess["reason"] == "llm_fix"
    assert sess["slot"] == "env"
    assert "Set TOKEN in selected.env" in sess["guide"]
    assert "TOKEN is empty" in sess["guide"]


def test_claim_and_register_fails_closed_on_a_guard_call_failure(monkeypatch):
    """S22 — an LLM-call failure has no fixing prompt, so it fails closed."""
    import asyncio
    from app.services.provision_service import provision_service

    owner = _make_api_user("guard-owner-2")
    sid = str(uuid.uuid4())
    _session(sid, owner, state=STATE_GENERATED, operation="deploy",
             selection=_guard_selection())
    registered = []

    async def fake_guard(intent, selection, env_name=None):
        raise deploy_workflow.WorkflowError(
            "llm_env_validation_blocked", "LLM call failed: HTTP 500")

    async def fake_register(**kwargs):
        registered.append(kwargs)
        return {"task_id": "t"}

    monkeypatch.setattr(deploy_workflow, "_run_llm_env_guard", fake_guard)
    monkeypatch.setattr(provision_service, "register_user", fake_register)
    with pytest.raises(deploy_workflow.WorkflowError) as excinfo:
        asyncio.run(deploy_workflow._claim_and_register(
            sid, {"llm_validate_env": True}, _guard_selection(), None))
    assert excinfo.value.code == "llm_env_validation_blocked"
    assert registered == [], "nothing may be registered after a guard block"
    assert pdr.get_session(sid)["state"] == "failed"


def test_llm_env_validation_parses_findings_and_call_failures(monkeypatch):
    """The reviewer method: findings block, transport/shape failures block."""
    import asyncio
    from app.services import llm_service as llm_mod

    service = llm_mod.llm_service
    monkeypatch.setattr(service, "_resolve_endpoint",
                        lambda db: ("http://llm", "model", {}))

    async def reply(content):
        async def fake_chat(*args, **kwargs):
            return {"choices": [{"message": {"content": content}}]}
        return fake_chat

    async def run(content):
        monkeypatch.setattr(service, "_chat", await reply(content))
        return await service.validate_env_selection(
            None, env_files=[("a.env", "TOKEN=")], compose_files=[],
            service_name="svc")

    blocked = asyncio.run(run(
        '{"valid": false, "findings": ["TOKEN is blank"], "guide": "set TOKEN"}'))
    assert blocked["valid"] is False
    assert blocked["findings"] == ["TOKEN is blank"]
    assert blocked["guide"] == "set TOKEN"

    # The model saying valid:true does not override a finding.
    assert asyncio.run(run(
        '{"valid": true, "findings": ["still wrong"]}'))["valid"] is False

    clean = asyncio.run(run('{"valid": true, "findings": []}'))
    assert clean == {"valid": True, "findings": [], "guide": "", "error": None}

    no_json = asyncio.run(run("I cannot answer in JSON"))
    assert no_json["valid"] is False and no_json["error"]

    async def failing_chat(*args, **kwargs):
        return {"_error": "HTTP 503"}

    monkeypatch.setattr(service, "_chat", failing_chat)
    failed = asyncio.run(service.validate_env_selection(
        None, env_files=[("a.env", "TOKEN=")], compose_files=[],
        service_name="svc"))
    assert failed["valid"] is False and failed["error"] == "HTTP 503"

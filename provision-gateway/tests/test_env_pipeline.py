"""T4/T5/T8/T10/T14/T16/T17 — intake, assembly, relation and snapshot rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services import deploy_workflow, generation_jobs
from app.services.deploy_workflow import WorkflowError, _api_payload
from app.services.dependency_model import (
    read_relation, relation_filename, relation_is_current, write_relation)
from app.services.env_classification import (
    structural_fingerprint, write_artifact)


def test_a_per_user_env_with_a_non_numeric_label_is_not_a_candidate():
    from app.services.file_sets import _is_env_candidate
    # Measured live 2026-11-20: `.env.raw.s2sshared.None` was offered as a base
    # and would have deployed an empty handoff as the template.
    assert not _is_env_candidate(".env.raw.s2sshared.None")
    assert not _is_env_candidate(".env.fix.s2sshared.None")
    assert _is_env_candidate(".env.example")
    assert _is_env_candidate(".env")


def _intent(**extra):
    intent = {
        "service_name": "graphiti-mcp",
        "user_name": "alice",
        "label": "30",
        "recipe_path": "",
        "tls": False,
        "selection": {"compose": ["docker-compose.yml"], "env": [],
                      "env_none": False, "profiles": ["dev"]},
    }
    intent.update(extra)
    return intent


def _selection(**extra):
    selection = {"compose": ["docker-compose.yml"], "env": [],
                 "env_none": False, "profiles": ["dev"]}
    selection.update(extra)
    return selection


def _dep(service="graphiti-local", label="0", user="alice"):
    return {"user_name": user, "service_name": service, "service": service,
            "label": label, "key": f"{user}/{service}/{label}",
            "hostname": f"{service}.localhost",
            "http_endpoints": [f"{service}--app.localhost"],
            "serving_url": f"http://{service}--app.localhost:8767"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "source_projects"
    rdir = root / "graphiti-mcp"
    rdir.mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
    (rdir / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: x\n    environment:\n"
        "      - REQUIRED_VAR=${REQUIRED_VAR}\n", encoding="utf-8")
    (rdir / ".env").write_text(
        "# template comment\nKEEP=1\nREQUIRED_VAR=from-template\n", encoding="utf-8")
    (rdir / ".env.fix").write_text("ONLY_IN_REFERENCE=2\n", encoding="utf-8")
    return rdir


# ---------------------------------------------------------------------------
# T4 — intake validation (base refs, context)
# ---------------------------------------------------------------------------

def test_a_missing_base_ref_is_refused_by_name(project):
    intent = _intent(base_refs={"env": [".env.missing"]})
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_base_refs(intent, _selection())
    assert exc.value.code == "base_ref_missing"
    assert ".env.missing" in exc.value.detail


def test_a_base_ref_on_a_filled_slot_is_refused(project):
    intent = _intent(base_refs={"env": [".env.fix"]})
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_base_refs(
            intent, _selection(env=[".env"]))
    assert exc.value.code == "base_ref_on_selected_slot"


def test_a_context_file_outside_the_project_root_is_refused(project):
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_context(_intent(context={"files": ["../x"], "links": []}))
    assert exc.value.code == "context_file_invalid"


def test_a_missing_context_file_is_named(project):
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_context(_intent(context={"files": ["missing.md"], "links": []}))
    assert exc.value.code == "context_file_missing"


def test_a_context_link_must_be_http(project):
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_context(_intent(context={"files": [], "links": ["ftp://x"]}))
    assert exc.value.code == "context_link_invalid"


def test_the_fingerprint_hashes_the_env_base_refs_and_profiles(project):
    import hashlib
    fp = deploy_workflow._classification_fingerprint(
        _intent(base_refs={"env": [".env"]}),
        _selection(compose=["docker-compose.yml"], profiles=["dev"]))
    assert fp["profiles"] == ["dev"]
    expected = "sha256:" + hashlib.sha256(
        (project / ".env").read_bytes()).hexdigest()
    assert fp["env_bases"] == [expected]
    assert fp["compose"] and fp["compose"][0].startswith("sha256:")


# ---------------------------------------------------------------------------
# T14 — the register payload boundary
# ---------------------------------------------------------------------------

def test_the_register_payload_carries_no_generation_context():
    intent = _intent(
        base_refs={"compose": [], "serving_ports": None, "nginx": None, "env": [".env"]},
        context={"files": ["auth.md"], "links": ["https://x"]},
        force=True, classification_fingerprint={"compose": []})
    payload = _api_payload(intent, _selection(env=[".env"]), None)
    for key in ("base_refs", "context", "force", "classification_fingerprint"):
        assert key not in payload
    # Profiles remain the deployment option, never dropped.
    assert payload["profiles"] == ["dev"]


def test_the_generation_request_carries_refs_context_profiles_and_force():
    intent = _intent(force=True, base_refs={"env": [".env"]},
                     context={"files": ["auth.md"], "links": []})
    req = deploy_workflow._generation_request(intent, _selection())
    assert req["base_refs"]["env"] == [".env"]
    assert req["context"]["files"] == ["auth.md"]
    assert req["profiles"] == ["dev"]
    assert req["force"] is True


def test_force_is_ignored_on_a_selected_env_and_on_a_fix_job():
    assert deploy_workflow._force_effective(
        _intent(force=True), _selection(env=[".env"])) is False
    assert deploy_workflow._force_effective(
        _intent(force=True), _selection(env_none=True)) is False
    assert deploy_workflow._force_effective(
        _intent(force=True, fix=True), _selection()) is False
    assert deploy_workflow._force_effective(_intent(force=True), _selection()) is True


# ---------------------------------------------------------------------------
# T17 — the relation fingerprint and re-fill
# ---------------------------------------------------------------------------

def _fingerprint(project, compose=("docker-compose.yml",), env=(".env",), profiles=("dev",)):
    return structural_fingerprint(
        project, compose_names=list(compose), profiles=list(profiles),
        env_base_names=list(env))


def test_a_relation_records_its_structural_fingerprint(project):
    fp = _fingerprint(project)
    write_relation(project, "graphiti-mcp", "graphiti-local",
                   {"GRAPHITI_API_URL": "graphiti-local"}, fp)
    data = read_relation(project, "graphiti-mcp", "graphiti-local")
    assert data["fingerprint"] == fp
    for key in ("version", "source_service", "target_service", "env", "generated_at"):
        assert key in data
    assert relation_is_current(data, fp)
    changed = _fingerprint(project, compose=())
    assert not relation_is_current(data, changed)


def test_fill_relations_reuses_current_and_rewrites_stale_or_missing(project):
    fp = _fingerprint(project)
    write_relation(project, "graphiti-mcp", "current", {"A": "current"}, fp)
    write_relation(project, "graphiti-mcp", "stale", {"OLD": "stale"},
                   _fingerprint(project, compose=()))
    mapping = {"current": ["A"], "stale": ["NEW"], "missing": ["B"]}
    entries = [_dep("current"), _dep("stale"), _dep("missing")]
    written = generation_jobs._fill_relations(
        project, source="graphiti-mcp", entries=entries,
        mapping=mapping, fingerprint=fp, force=False)
    assert sorted(written) == ["missing", "stale"]
    assert read_relation(project, "graphiti-mcp", "current")["fingerprint"] == fp
    assert read_relation(project, "graphiti-mcp", "stale")["env"] == {
        "NEW": {"depends_on": "stale"}}
    assert read_relation(project, "graphiti-mcp", "missing")["env"] == {
        "B": {"depends_on": "missing"}}


def test_force_rewrites_every_relation_in_the_env_context(project):
    fp = _fingerprint(project)
    write_relation(project, "graphiti-mcp", "a", {"A": "a"}, fp)
    write_relation(project, "graphiti-mcp", "b", {"B": "b"}, fp)
    before = (project / relation_filename("graphiti-mcp", "a")).read_text()
    written = generation_jobs._fill_relations(
        project, source="graphiti-mcp", entries=[_dep("a"), _dep("b")],
        mapping={"a": ["A"], "b": ["B"]}, fingerprint=fp, force=True)
    assert sorted(written) == ["a", "b"]
    assert (project / relation_filename("graphiti-mcp", "a")).read_text() != before


def test_a_stale_relation_refuses_a_selected_env(project, monkeypatch):
    fp = _fingerprint(project)
    write_relation(project, "graphiti-mcp", "graphiti-local", {"A": "graphiti-local"},
                   _fingerprint(project, compose=()))
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids",
                        lambda ids: [_dep("graphiti-local")])
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._dependency_precheck(
            _intent(dependency_service_ids=["a"]),
            {"env": ["x.env"]}, fp)
    assert exc.value.code == "dependency_relations_missing"


# ---------------------------------------------------------------------------
# T8 — template copy, assembly and the one mechanical check
# ---------------------------------------------------------------------------

def _artifact(**variables):
    return {"version": 1, "source_service": "graphiti-mcp", "recipe_path": "",
            "fingerprint": {"compose": [], "profiles": [], "env_bases": []},
            "variables": variables, "dependencies": {},
            "generated_at": "2026-11-20T00:00:00Z"}


def test_assembly_copies_the_template_and_changes_only_essentials(project):
    template = (project / ".env").read_text()
    artifact = _artifact(**{
        "REQUIRED_VAR": {"category": "defaulted",
                         "source": {"kind": "file"}, "value": "from-template"},
        "URL": {"category": "deploy_value", "source": {"kind": "metadata_key"},
                "template": "http://{{ hostname }}:{{ http_port }}"},
    })
    text, assembled, missing, externals = generation_jobs._assemble_classified_env(
        template, values={}, artifact=artifact,
        base_metadata={"hostname": "h", "http_port": 8767},
        relation_values={}, external_values=None, defaults={})
    assert "# template comment" in text
    assert "KEEP=1" in text
    assert assembled["URL"] == "http://h:8767"
    assert "ONLY_IN_REFERENCE" not in text
    assert missing == [] and externals == []


def test_assembly_missing_required_blocks_by_name(project):
    template = "KEEP=1\n"
    artifact = _artifact(**{
        "REQUIRED_VAR": {"category": "required", "source": {"kind": "file"}},
    })
    _, _, missing, externals = generation_jobs._assemble_classified_env(
        template, values={}, artifact=artifact, base_metadata={},
        relation_values={}, external_values=None, defaults={})
    assert missing == ["REQUIRED_VAR"]
    assert externals == []


def test_assembly_unsupplied_external_routes_to_the_pause(project):
    artifact = _artifact(**{
        "API_KEY": {"category": "external", "source": {"kind": "operator"}},
    })
    _, _, missing, externals = generation_jobs._assemble_classified_env(
        "KEEP=1\n", values={}, artifact=artifact, base_metadata={},
        relation_values={}, external_values=None, defaults={})
    assert externals == ["API_KEY"]
    assert missing == []


# ---------------------------------------------------------------------------
# T12 — the used-or-written snapshot matrix
# ---------------------------------------------------------------------------

def test_snapshot_matrix(tmp_path, monkeypatch):
    rdir = tmp_path / "s" / "recipe"
    rdir.mkdir(parents=True)
    (rdir / "serving_ports.yml").write_text("rows: []\n")
    write_relation(rdir, "s", "dep", {"A": "dep"},
                   {"compose": [], "profiles": [], "env_bases": []})
    write_artifact(rdir, source_service="s", recipe_path="",
                   fingerprint={"compose": [], "profiles": [], "env_bases": []},
                   variables={"A": {"category": "defaulted",
                                    "source": {"kind": "file"}}})
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(deploy_workflow, "_dependency_entries",
                        lambda sid: [{"service_name": "dep"}])

    def run(label, env_meta, selection):
        before = {p.name for p in rdir.iterdir()}
        deploy_workflow._snapshot_for_deploy(
            {"service_name": "s", "user_name": "alice", "recipe_path": "recipe"},
            selection, label, env_meta)
        return {p.name for p in rdir.iterdir()} - before

    # Row 1 — Env None, no deps: nothing.
    created = run("30", {"relations": {}, "classification_read": False,
                         "classification_written": False},
                  {"env_none": True, "serving_ports_none": True})
    assert created == set()

    # Row 4 — a relation read (sent env + deps): dep snapshot only.
    created = run("31", {"relations": {"dep": "read"}, "classification_read": False,
                         "classification_written": False},
                  {"env": ["e.env"], "serving_ports_none": True})
    assert created == {"dependency.s.dep.user-alice.31.json",
                       "dependency.s.dep.user-alice.31.json.generated"}

    # Row 6 — artefact read: env-class snapshot only.
    created = run("32", {"relations": {}, "classification_read": True,
                         "classification_written": False},
                  {"env_none": True, "serving_ports_none": True})
    assert created == {"env.classification.user-alice.32.json",
                       "env.classification.user-alice.32.json.generated"}

    # Row 8 — no artefact and no relation: nothing.
    created = run("33", {"relations": {}, "classification_read": False,
                         "classification_written": False},
                  {"env_none": True, "serving_ports_none": True})
    assert created == set()

    # Row 10 — a relation written: both snapshots.
    created = run("34", {"relations": {"dep": "written"}, "classification_read": True,
                         "classification_written": True},
                  {"env_none": True, "serving_ports_none": True})
    assert "dependency.s.dep.user-alice.34.json" in created
    assert "env.classification.user-alice.34.json" in created

    # The card rows read the same files, exact names only.
    rows = deploy_workflow._snapshot_rows({
        "service_name": "s", "user_name": "alice", "label": "34",
        "service_id": "sid", "recipe_path": "recipe"})
    assert rows["env_classification_snapshot"] == \
        "env.classification.user-alice.34.json"
    assert rows["dependency_snapshots"] == [
        {"target_service": "dep",
         "path": "dependency.s.dep.user-alice.34.json"}]

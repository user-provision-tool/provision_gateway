"""T4/T5/T8/T10/T14/T16/T17 — intake, assembly, relation and snapshot rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services import deploy_workflow, generation_jobs
from app.services.deploy_workflow import WorkflowError, _api_payload
from app.services.dependency_model import (
    matched_relation_variant, read_relation_variant, relation_is_current,
    relation_variant_filename, write_relation)
from app.services.env_classification import (
    matched_variant, structural_fingerprint, variant_filename, write_artifact)


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


def test_the_nginx_base_ref_is_per_category(project):
    """C15 — one file per category, each validated against its own predicate."""
    (project / "nginx.conf").write_text("server {}\n")
    (project / "app.stream.conf").write_text("stream {}\n")
    (project / "app.mail.conf").write_text("mail {}\n")
    deploy_workflow._validate_base_refs(
        _intent(base_refs={"nginx": {"http": "nginx.conf",
                                     "stream": "app.stream.conf",
                                     "mail": "app.mail.conf"}}),
        _selection(),
    )
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_base_refs(
            _intent(base_refs={"nginx": {"http": "app.stream.conf"}}),
            _selection())
    assert exc.value.code == "base_ref_not_a_candidate"
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_base_refs(
            _intent(base_refs={"nginx": {"smtp": "nginx.conf"}}),
            _selection())
    assert exc.value.code == "invalid_base_ref"
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_base_refs(
            _intent(base_refs={"nginx": {"http": "nginx.conf"}}),
            _selection(nginx="n.conf"))
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
        classification="generate_new",
        relations={"graphiti-local": "generate_new"},
        classification_fingerprint={"compose": []})
    payload = _api_payload(intent, _selection(env=[".env"]), None)
    for key in ("base_refs", "context", "force", "classification", "relations",
                "classification_fingerprint"):
        assert key not in payload
    # Profiles remain the deployment option, never dropped.
    assert payload["profiles"] == ["dev"]


def test_the_generation_request_carries_refs_context_profiles_and_selections():
    intent = _intent(classification="generate_new",
                     relations={"graphiti-local": "none"},
                     base_refs={"env": [".env"]},
                     context={"files": ["auth.md"], "links": []})
    req = deploy_workflow._generation_request(intent, _selection())
    assert req["base_refs"]["env"] == [".env"]
    assert req["context"]["files"] == ["auth.md"]
    assert req["profiles"] == ["dev"]
    assert req["classification"] == "generate_new"
    assert req["relations"] == {"graphiti-local": "none"}
    # C21 — the force control is retired from the request entirely.
    assert "force" not in req


def test_env_none_pins_the_classification_and_relations_to_none():
    intent = _intent(classification="generate_new",
                     relations={"graphiti-local": "generate_new"})
    deploy_workflow._validate_env_selection(intent, _selection(env_none=True))
    assert intent["classification"] == "none"
    assert intent["relations"] == {}


def test_an_unknown_classification_mode_is_refused():
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_env_selection(
            _intent(classification="some-file.yaml"), _selection())
    assert exc.value.code == "unknown_classification_mode"


def test_relation_none_is_refused_over_a_non_empty_projection(project, monkeypatch):
    fp = structural_fingerprint(
        project, compose_names=["docker-compose.yml"], profiles=["dev"],
        env_base_names=[".env"])
    name = write_artifact(
        project, source_service="graphiti-mcp", recipe_path="", fingerprint=fp,
        variables={"DEP": {"category": "dependency",
                           "source": {"kind": "dependency_target",
                                      "target": "graphiti-local"}}}).name
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids",
                        lambda ids: [_dep("graphiti-local")])
    intent = _intent(classification=name,
                     relations={"graphiti-local": "none"},
                     dependency_service_ids=["a"])
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_env_selection(intent, _selection(env=[".env"]))
    assert exc.value.code == "relation_none_not_allowed"
    assert "DEP" in exc.value.detail


def test_an_unknown_relation_target_is_refused(project, monkeypatch):
    fp = structural_fingerprint(
        project, compose_names=["docker-compose.yml"], profiles=["dev"],
        env_base_names=[".env"])
    name = write_artifact(
        project, source_service="graphiti-mcp", recipe_path="", fingerprint=fp,
        variables={"A": {"category": "defaulted", "source": {"kind": "file"}}}).name
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids",
                        lambda ids: [_dep("graphiti-local")])
    intent = _intent(classification=name, relations={"other": "none"},
                     dependency_service_ids=["a"])
    with pytest.raises(WorkflowError) as exc:
        deploy_workflow._validate_env_selection(intent, _selection())
    assert exc.value.code == "unknown_relation_target"


# ---------------------------------------------------------------------------
# T17 — the relation fingerprint and re-fill
# ---------------------------------------------------------------------------

def _fingerprint(project, compose=("docker-compose.yml",), env=(".env",), profiles=("dev",)):
    return structural_fingerprint(
        project, compose_names=list(compose), profiles=list(profiles),
        env_base_names=list(env))


def test_a_relation_is_a_fingerprint_variant_with_its_fingerprint(project):
    fp = _fingerprint(project)
    path = write_relation(project, "graphiti-mcp", "graphiti-local",
                          {"GRAPHITI_API_URL": "graphiti-local"}, fp)
    assert path.name == relation_variant_filename(
        "graphiti-mcp", "graphiti-local", fp)
    assert (project / f"{path.name}.generated").is_file()
    data = read_relation_variant(
        project, "graphiti-mcp", "graphiti-local", path.name)
    assert data["fingerprint"] == fp
    for key in ("version", "source_service", "target_service", "env", "generated_at"):
        assert key in data
    assert relation_is_current(data, fp)
    changed = _fingerprint(project, compose=())
    assert not relation_is_current(data, changed)
    # Only the matched fingerprint's variant is a candidate.
    assert matched_relation_variant(
        project, "graphiti-mcp", "graphiti-local", fp)[0] == path.name
    assert matched_relation_variant(
        project, "graphiti-mcp", "graphiti-local", changed) is None


def test_fill_relations_writes_the_current_variant_for_every_target(project):
    fp = _fingerprint(project)
    mapping = {"current": ["A"], "stale": ["NEW"], "missing": ["B"]}
    entries = [_dep("current"), _dep("stale"), _dep("missing")]
    written = generation_jobs._fill_relations(
        project, source="graphiti-mcp", entries=entries,
        mapping=mapping, fingerprint=fp)
    assert sorted(written) == ["current", "missing", "stale"]
    for target, expected in (("current", {"A": "current"}),
                             ("stale", {"NEW": "stale"}),
                             ("missing", {"B": "missing"})):
        name = relation_variant_filename("graphiti-mcp", target, fp)
        data = read_relation_variant(project, "graphiti-mcp", target, name)
        assert data["env"] == {v: {"depends_on": target} for v in expected}


def test_the_1to1_projection_names_missing_and_extra_variables(project):
    from app.services.dependency_model import relation_projection_errors
    name = write_relation(project, "s", "t", {"A": "t", "B": "t"},
                          {"compose": [], "profiles": [], "env_bases": []}).name
    data = read_relation_variant(project, "s", "t", name)
    assert relation_projection_errors(data, ["A", "B"]) == ([], [])
    assert relation_projection_errors(data, ["A"]) == ([], ["B"])
    assert relation_projection_errors(data, ["A", "B", "C"]) == (["C"], [])


def test_the_coverage_fill_names_every_selected_env_key():
    artifact = {"version": 1, "fingerprint": {}, "variables": {
        "KEEP": {"category": "defaulted", "source": {"kind": "file"},
                 "value": "1"}}}
    filled = generation_jobs.ensure_classification_coverage(
        artifact, ["KEEP", "NEW"], {"KEEP": "1", "NEW": "2"})
    assert set(filled["variables"]) == {"KEEP", "NEW"}
    assert filled["variables"]["NEW"]["value"] == "2"
    assert filled["variables"]["NEW"]["category"] == "defaulted"
    # An already-covered classification is returned unchanged.
    same = generation_jobs.ensure_classification_coverage(
        artifact, ["KEEP"], {"KEEP": "1"})
    assert same is artifact


def test_generation_needed_for_a_classified_selected_env(monkeypatch, project):
    from app.services.generation_jobs import generation_jobs as gm
    monkeypatch.setattr(gm, "_recipe_missing", lambda req: [])
    selection = {"compose": ["docker-compose.yml"], "nginx": "n.conf",
                 "serving_ports": "p.yml", "env": [".env"], "env_none": False}
    intent = {"service_name": "graphiti-mcp", "recipe_path": "",
              "classification": "none", "selection": selection}
    assert deploy_workflow.generation_needed(intent) is False
    intent["classification"] = "generate_new"
    assert deploy_workflow.generation_needed(intent) is True
    intent["classification"] = "env.classification.0123456789abcdef.yaml"
    assert deploy_workflow.generation_needed(intent) is True
    intent["selection"] = {**selection, "env": [], "env_none": True}
    assert deploy_workflow.generation_needed(intent) is False


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


def test_compose_defaults_fill_absent_and_empty_template_variables(project):
    """F7 follow-up — the defaults are applied after inject_values."""
    artifact = _artifact(**{
        "REQ": {"category": "required", "source": {"kind": "file"}},
        "DEF": {"category": "defaulted", "source": {"kind": "file"}},
    })
    text, assembled, missing, externals = generation_jobs._assemble_classified_env(
        "KEEP=1\n", values={}, artifact=artifact, base_metadata={},
        relation_values={}, external_values=None,
        defaults={"REQ": "req-default", "DEF": "def-default"},
        active_vars={"REQ", "DEF"})
    assert missing == [] and externals == []
    assert assembled["REQ"] == "req-default"
    assert assembled["DEF"] == "def-default"


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
    fp = {"compose": [], "profiles": [], "env_bases": []}
    rel_name = write_relation(rdir, "s", "dep", {"A": "dep"}, fp).name
    cls_name = write_artifact(
        rdir, source_service="s", recipe_path="", fingerprint=fp,
        variables={"A": {"category": "defaulted",
                         "source": {"kind": "file"}}}).name
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

    # A relation read (sent env + deps): dep snapshot only, exact source.
    created = run("31", {"relations": {"dep": "read"}, "classification_read": False,
                         "classification_written": False,
                         "relation_variants": {"dep": rel_name}},
                  {"env": ["e.env"], "serving_ports_none": True})
    assert created == {"dependency.s.dep.user-alice.31.json",
                       "dependency.s.dep.user-alice.31.json.generated"}

    # The artefact read: env-class snapshot only, copied from the used variant.
    created = run("32", {"relations": {}, "classification_read": True,
                         "classification_written": False,
                         "classification_name": cls_name},
                  {"env_none": True, "serving_ports_none": True})
    assert created == {"env.classification.user-alice.32.yaml",
                       "env.classification.user-alice.32.yaml.generated"}

    # No artefact and no relation: nothing.
    created = run("33", {"relations": {}, "classification_read": False,
                         "classification_written": False},
                  {"env_none": True, "serving_ports_none": True})
    assert created == set()

    # A relation written: both snapshots.
    created = run("34", {"relations": {"dep": "written"}, "classification_read": True,
                         "classification_written": True,
                         "relation_variants": {"dep": rel_name},
                         "classification_name": cls_name},
                  {"env_none": True, "serving_ports_none": True})
    assert "dependency.s.dep.user-alice.34.json" in created
    assert "env.classification.user-alice.34.yaml" in created

    # The card rows read the same files, exact names only.
    rows = deploy_workflow._snapshot_rows({
        "service_name": "s", "user_name": "alice", "label": "34",
        "service_id": "sid", "recipe_path": "recipe"})
    assert rows["env_classification_snapshot"] == \
        "env.classification.user-alice.34.yaml"
    assert rows["dependency_snapshots"] == [
        {"target_service": "dep",
         "path": "dependency.s.dep.user-alice.34.json"}]


def test_snapshot_matrix_rev7_rows(tmp_path, monkeypatch):
    """Rows 18–22 of the snapshot matrix: the used-or-written set decides."""
    rdir = tmp_path / "s" / "recipe"
    rdir.mkdir(parents=True)
    fp = {"compose": [], "profiles": [], "env_bases": []}
    rel_name = write_relation(rdir, "s", "dep", {"A": "dep"}, fp).name
    cls_name = write_artifact(
        rdir, source_service="s", recipe_path="", fingerprint=fp,
        variables={"A": {"category": "defaulted",
                         "source": {"kind": "file"}}}).name
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(deploy_workflow, "_dependency_entries",
                        lambda sid: [{"service_name": "dep"}])

    def run(label, env_meta, selection):
        before = {p.name for p in rdir.iterdir()}
        deploy_workflow._snapshot_for_deploy(
            {"service_name": "s", "user_name": "alice", "recipe_path": "recipe"},
            selection, label, env_meta)
        return {p.name for p in rdir.iterdir()} - before

    # Row 18 — sent env + cls existing + relation read: both snapshots.
    created = run("40", {"relations": {"dep": "read"},
                         "classification_read": True,
                         "classification_name": cls_name,
                         "relation_variants": {"dep": rel_name}},
                  {"env": ["e.env"], "serving_ports_none": True})
    assert "env.classification.user-alice.40.yaml" in created
    assert "dependency.s.dep.user-alice.40.json" in created

    # Row 19 — sent env + cls None: nothing.
    created = run("41", {"relations": {}, "classification_read": False,
                         "classification_written": False},
                  {"env": ["e.env"], "serving_ports_none": True})
    assert created == set()

    # Row 20 — generate new + cls None: nothing.
    created = run("42", {"relations": {}, "classification_read": False,
                         "classification_written": False},
                  {"env": [], "env_none": False, "serving_ports_none": True})
    assert created == set()

    # Rows 21/22 — cls generate new + relation written: both snapshots, for a
    # sent env and for generate new.
    for label, selection in (("43", {"env": ["e.env"], "serving_ports_none": True}),
                             ("44", {"env": [], "env_none": False,
                                     "serving_ports_none": True})):
        created = run(label, {"relations": {"dep": "written"},
                              "classification_read": True,
                              "classification_written": True,
                              "classification_name": cls_name,
                              "relation_variants": {"dep": rel_name}},
                      selection)
        assert f"env.classification.user-alice.{label}.yaml" in created
        assert f"dependency.s.dep.user-alice.{label}.json" in created

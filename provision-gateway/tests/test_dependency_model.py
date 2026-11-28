"""G103 — the dependency-aware env model (replaces the G102 per-instance artifact).

Per-relation files (user/label-agnostic), the env-picker modes, the selected-env
substitution, and the per-instance snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services import deploy_workflow
from app.services.deploy_workflow import WorkflowError, _api_payload
from app.services.dependency_model import (
    duplicate_target_services,
    read_relation_variant,
    relation_env,
    serving_ports_snapshot_name,
    snapshot_used_artifacts,
    write_relation,
)
from app.services.generation_jobs import _write_relation_files


def _intent(**extra):
    intent = {
        "service_name": "graphiti-mcp",
        "user_name": "alice",
        "label": "0",
        "recipe_path": "",
        "tls": False,
    }
    intent.update(extra)
    return intent


def _dep(service, label="0", user="alice", sid=None):
    return {"service_id": sid or f"id-{service}-{user}", "user_name": user,
            "service_name": service, "service": service, "label": label,
            "key": f"{user}/{service}/{label}",
            "hostname": f"{service}.localhost",
            "http_endpoints": [f"{service}-{user}-{label}--app.localhost"]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "source_projects"
    rdir = root / "graphiti-mcp"
    rdir.mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
    return rdir


# ---------------------------------------------------------------------------
# The deploy payload still prepares the resolver when dependencies exist
# ---------------------------------------------------------------------------

def test_a_dependency_deploy_is_prepared_with_the_resolver_name():
    payload = _api_payload(
        _intent(dependency_service_ids=["target-1"]), {"compose": []}, None)
    assert payload["prepare"] is True
    assert payload["extra_resolver"] == settings.CROSS_RESOLVER_NAME
    assert payload["extra_network"] == settings.CROSS_NETWORK
    assert "dependency_service_ids" not in payload


def test_a_deploy_without_dependencies_starts_immediately():
    payload = _api_payload(_intent(), {"compose": []}, None)
    assert payload["prepare"] is False
    assert "extra_resolver" not in payload


# ---------------------------------------------------------------------------
# T14 — one fingerprint-scoped variant per relation
# ---------------------------------------------------------------------------

def test_one_variant_per_relation_and_unselected_keys_are_dropped(project):
    deps = [_dep("graphiti-local"), _dep("other")]
    written = _write_relation_files(
        project, service="graphiti-mcp", deps=deps,
        refs={"GRAPHITI_API_URL": "alice/graphiti-local/0",
              "OTHER_URL": "alice/other/0",
              "NOT_SELECTED": "alice/not-selected/0"})
    names = sorted(p.name for p in written)
    assert all(name.startswith("dependency.graphiti-mcp.") for name in names)
    rel = read_relation_variant(
        project, "graphiti-mcp", "graphiti-local", names[0])
    assert relation_env(rel) == {"GRAPHITI_API_URL": "graphiti-local"}
    from app.services.generated_marker import find_marker
    assert find_marker(project / names[0]) is not None
    # The unselected key appears in neither file.
    assert "NOT_SELECTED" not in relation_env(rel)
    assert "NOT_SELECTED" not in relation_env(read_relation_variant(
        project, "graphiti-mcp", "other", names[1]))


def test_a_target_with_no_classified_vars_still_gets_its_variant(project):
    written = _write_relation_files(project, service="graphiti-mcp",
                                    deps=[_dep("graphiti-local")], refs={})
    assert len(written) == 1
    rel = read_relation_variant(
        project, "graphiti-mcp", "graphiti-local", written[0].name)
    assert rel is not None and relation_env(rel) == {}


def test_the_same_target_under_different_owners_is_one_variant(project):
    deps = [_dep("graphiti-local", user="alice", sid="a"),
            _dep("graphiti-local", user="bob", sid="b")]
    written = _write_relation_files(project, service="graphiti-mcp",
                                    deps=deps, refs={})
    assert len(written) == 1


# ---------------------------------------------------------------------------
# T15/T22 — the classification state pins the relation pickers
# ---------------------------------------------------------------------------

def test_duplicate_target_service_names_remove_the_deps_from_the_context(
        monkeypatch, project):
    entries = [_dep("graphiti-local", user="alice"),
               _dep("graphiti-local", user="bob")]
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids", lambda ids: entries)
    intent = _intent(dependency_service_ids=["a", "b"])
    deploy_workflow._validate_env_selection(intent, {"env": ["e.env"]})
    assert intent.get("_dependency_env_disabled") is True
    assert intent["relations"] == {}


def test_none_removes_the_deps_from_the_context(monkeypatch, project):
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids",
                        lambda ids: [_dep("graphiti-local")])
    intent = _intent(dependency_service_ids=["a"],
                     classification="generate_new")
    deploy_workflow._validate_env_selection(
        intent, {"env": [], "env_none": True})
    assert intent["classification"] == "none"
    assert intent["relations"] == {}
    assert intent["_dependency_env_disabled"] is True


def test_cls_none_and_generate_new_pin_every_relation(monkeypatch, project):
    monkeypatch.setattr(deploy_workflow, "_entries_for_ids",
                        lambda ids: [_dep("graphiti-local")])
    a = _intent(dependency_service_ids=["a"], classification="none",
                relations={"graphiti-local": "generate_new"})
    deploy_workflow._validate_env_selection(a, {"env": ["e.env"]})
    assert a["relations"] == {}
    b = _intent(dependency_service_ids=["a"], classification="generate_new",
                relations={"graphiti-local": "none"})
    deploy_workflow._validate_env_selection(b, {"env": []})
    assert b["relations"] == {}


# ---------------------------------------------------------------------------
# T9 — the per-instance snapshots and the picker exclusion
# ---------------------------------------------------------------------------

def _write_relation(project, target, env=None):
    return write_relation(
        project, "graphiti-mcp", target,
        {k: target for k in (env or {})},
        {"compose": [], "profiles": [], "env_bases": []}).name


def test_snapshots_copy_the_used_artifacts(project):
    (project / "serving_ports.yml").write_text("ports: []\n")
    rel_name = _write_relation(project, "graphiti-local", env=["GRAPHITI_API_URL"])
    out = snapshot_used_artifacts(
        project, source_service="graphiti-mcp", user="alice", label="0",
        serving_ports="serving_ports.yml", target_services=["graphiti-local"],
        relation_variants={"graphiti-local": rel_name})
    assert out["serving_ports"] == serving_ports_snapshot_name("alice", "0")
    assert (project / out["serving_ports"]).is_file()
    assert out["dependencies"] == [{
        "target_service": "graphiti-local",
        "path": "dependency.graphiti-mcp.graphiti-local.user-alice.0.json",
    }]
    assert (project / out["dependencies"][0]["path"]).is_file()


def test_a_later_shared_edit_does_not_change_the_snapshot(project):
    (project / "serving_ports.yml").write_text("ports: [one]\n")
    out = snapshot_used_artifacts(
        project, source_service="graphiti-mcp", user="alice", label="0",
        serving_ports="serving_ports.yml", target_services=[])
    (project / "serving_ports.yml").write_text("ports: [changed]\n")
    assert "one" in (project / out["serving_ports"]).read_text()


def test_the_serving_ports_snapshot_is_not_a_picker_candidate():
    from app.services.file_sets import _is_serving_ports_candidate
    assert _is_serving_ports_candidate("serving_ports.yml") is True
    assert _is_serving_ports_candidate("serving_ports.user-alice.0.yml") is False

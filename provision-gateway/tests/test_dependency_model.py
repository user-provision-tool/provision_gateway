"""G102 — the dependency deploy payload and the persisted artifact."""

from __future__ import annotations

import json

from app.config import settings
from app.services.deploy_workflow import _api_payload
from app.services.generation_jobs import _write_dependencies_artifact


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


def test_a_dependency_deploy_is_prepared_with_the_resolver_name():
    payload = _api_payload(
        _intent(dependency_service_ids=["target-1"]), {"compose": []}, None)
    assert payload["prepare"] is True
    assert payload["extra_resolver"] == settings.CROSS_RESOLVER_NAME
    assert payload["extra_network"] == settings.CROSS_NETWORK
    # The selection is gateway context, never a raw -api field.
    assert "dependency_service_ids" not in payload


def test_a_deploy_without_dependencies_starts_immediately():
    payload = _api_payload(_intent(), {"compose": []}, None)
    assert payload["prepare"] is False
    assert "extra_resolver" not in payload
    assert "extra_network" not in payload


def test_the_artifact_records_the_source_and_every_selected_dependency(tmp_path):
    deps = [
        {"key": "alice/graphiti-local/0", "user": "alice",
         "service": "graphiti-local", "label": "0", "hostname": "gl.localhost"},
        {"key": "alice/other/0", "user": "alice",
         "service": "other", "label": "0", "hostname": "other.localhost"},
    ]
    path = _write_dependencies_artifact(
        tmp_path, user="alice", service="graphiti-mcp", label="0", deps=deps,
        refs={"GRAPHITI_API_URL": "alice/graphiti-local/0",
              # An unselected key is dropped.
              "OTHER_URL": "alice/not-selected/0"},
    )
    data = json.loads(path.read_text())
    assert data["source"] == "alice/graphiti-mcp/0"
    assert set(data["services"]) == {
        "alice/graphiti-mcp/0", "alice/graphiti-local/0", "alice/other/0"}
    assert data["services"]["alice/graphiti-mcp/0"]["env"] == {
        "GRAPHITI_API_URL": {"depends_on": "alice/graphiti-local/0"}}
    assert data["services"]["alice/graphiti-local/0"]["env"] == {}
    assert "generated_at" in data


def test_no_selection_writes_no_artifact(tmp_path):
    path = _write_dependencies_artifact(
        tmp_path, user="alice", service="svc", label="0", deps=[], refs={})
    assert path is None

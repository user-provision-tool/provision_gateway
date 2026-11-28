"""T13 — per-instance copies, the deployment record, and teardown."""

from __future__ import annotations

import json

from app.config import settings
from app.services import deploy_workflow as wf
from app.services import generated_marker as gm


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_copies_are_recorded_and_card_rows_read_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    rdir = tmp_path / "s" / "recipe"
    _write(rdir / "serving_ports.yml", "ports: []\n")
    _write(rdir / "dependency.s.dep.1.json", '{"env": {}}\n')
    _write(rdir / "env.classification.1.yaml", "variables: {}\n")
    _write(rdir / "docker-compose.fix.77.yml", "services: {}\n")
    _write(rdir / "docker-compose.fix.88.yml", "services: {}\n")

    intent = {"service_name": "s", "user_name": "alice", "recipe_path": "recipe"}
    selection = {
        "compose": ["docker-compose.fix.77.yml", "docker-compose.fix.88.yml"],
        "serving_ports": "serving_ports.yml",
        "serving_ports_none": False,
    }
    env_meta = {
        "relations": {"dep": "read"},
        "classification_read": True,
        "classification_written": False,
        "relation_variants": {"dep": "dependency.s.dep.1.json"},
        "classification_name": "env.classification.1.yaml",
        "job_id": "77",
    }
    wf._snapshot_for_deploy(intent, selection, "0", env_meta)

    record_path = rdir / "deploy.user-alice.0.json"
    record = json.loads(record_path.read_text())
    kinds = sorted(c["kind"] for c in record["copies"])
    assert kinds == ["dependency", "env_classification", "fix", "serving_ports"]
    paths = {c["path"] for c in record["copies"]}
    assert "docker-compose.fix.user-alice.0.yml" in paths
    assert "docker-compose.fix.user-alice.0.88.yml" not in paths
    for path in paths:
        assert (rdir / path).is_file()
        assert gm.find_marker(rdir / path) is not None

    rows = wf._snapshot_rows({
        "service_name": "s", "user_name": "alice", "label": "0",
        "recipe_path": "recipe"})
    assert rows["serving_ports_snapshot"] == "serving_ports.user-alice.0.yml"
    assert rows["env_classification_snapshot"] == \
        "env.classification.user-alice.0.yaml"
    assert rows["dependency_snapshots"] == [{
        "target_service": "dep",
        "path": "dependency.s.dep.user-alice.0.json"}]
    assert rows["fix_copies"] == ["docker-compose.fix.user-alice.0.yml"]


def test_redeploy_without_an_artefact_omits_the_row(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    rdir = tmp_path / "s" / "recipe"
    _write(rdir / "serving_ports.yml", "ports: []\n")
    intent = {"service_name": "s", "user_name": "alice", "recipe_path": "recipe"}
    wf._snapshot_for_deploy(
        intent, {"serving_ports": "serving_ports.yml", "serving_ports_none": False},
        "0", {})
    assert (rdir / "serving_ports.user-alice.0.yml").is_file()

    # The next deploy does not use serving ports: its record omits the row,
    # while the old copy remains on disk (never auto-deleted).
    wf._snapshot_for_deploy(
        intent, {"serving_ports_none": True}, "0", {})
    rows = wf._snapshot_rows({
        "service_name": "s", "user_name": "alice", "label": "0",
        "recipe_path": "recipe"})
    assert "serving_ports_snapshot" not in rows
    assert (rdir / "serving_ports.user-alice.0.yml").is_file()


def test_teardown_deletes_copies_and_record(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    rdir = tmp_path / "s" / "recipe"
    _write(rdir / "serving_ports.yml", "ports: []\n")
    intent = {"service_name": "s", "user_name": "alice", "recipe_path": "recipe"}
    wf._snapshot_for_deploy(
        intent, {"serving_ports": "serving_ports.yml", "serving_ports_none": False},
        "0", {})
    assert (rdir / "deploy.user-alice.0.json").is_file()

    wf.teardown_deploy_copies({
        "service_name": "s", "user_name": "alice", "label": "0",
        "recipe_path": "recipe"})
    assert not (rdir / "deploy.user-alice.0.json").exists()
    assert not (rdir / "serving_ports.user-alice.0.yml").exists()
    assert gm.find_marker(rdir / "serving_ports.user-alice.0.yml") is None


def test_no_record_means_no_intermediate_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    rdir = tmp_path / "s" / "recipe"
    rdir.mkdir(parents=True)
    # A legacy snapshot on disk without a record must NOT be shown (R11.2).
    _write(rdir / "serving_ports.user-alice.0.yml", "ports: []\n")
    rows = wf._snapshot_rows({
        "service_name": "s", "user_name": "alice", "label": "0",
        "recipe_path": "recipe"})
    assert rows == {}


def test_a_selected_env_is_copied_into_the_per_user_raw(tmp_path, monkeypatch):
    """R7.2 — nothing to tune is still sent as a per-user `.env.raw`."""
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    rdir = tmp_path / "s" / "recipe"
    _write(rdir / ".env", "A=1\n")
    intent = {"service_name": "s", "user_name": "alice", "label": "2",
              "recipe_path": "recipe"}
    raw = wf._ensure_env_raw(
        intent, {"env": [".env"], "env_none": False})
    assert raw == ".env.raw.alice.2"
    assert (rdir / raw).read_text() == "A=1\n"
    assert gm.find_marker(rdir / raw) is not None
    payload = wf._api_payload(intent, {"env": [".env"]}, raw)
    assert payload["env_files"] == [raw]
    assert wf._ensure_env_raw(intent, {"env_none": True}) is None

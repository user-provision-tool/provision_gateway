"""T6 — derived evidence: exact match, deploy join, duplicates, staleness."""

from __future__ import annotations

import json

import pytest

from app.services import artifact_provenance as prov
from app.services import generated_marker as gm

FP = {"compose": ["sha256:a"], "profiles": [], "env_bases": []}


def _pair(tmp_path, name, text, *, cls="base", fingerprint=None):
    path = tmp_path / name
    gm.write_marker_pair(path, text, cls=cls, description=f"{cls} artifact",
                         fingerprint=fingerprint or {})
    return path


def test_exact_match_true_and_false(tmp_path):
    _pair(tmp_path, "a.conf", "a\n", fingerprint=FP)
    exact = prov.summarize(tmp_path / "a.conf", fingerprint=FP)
    assert exact["exact"] is True and exact["description"] == "base artifact"
    other = prov.summarize(
        tmp_path / "a.conf",
        fingerprint={"compose": ["sha256:b"], "profiles": [], "env_bases": []})
    assert other["exact"] is False


def test_legacy_and_unmarked_degrade(tmp_path):
    (tmp_path / "old.conf").write_text("x")
    (tmp_path / "old.conf.generated").write_text("")
    legacy = prov.summarize(tmp_path / "old.conf", fingerprint=FP)
    assert legacy["class"] == "legacy" and legacy["description"] == ""
    assert legacy["exact"] is None
    (tmp_path / "repo.conf").write_text("x")
    assert prov.summarize(tmp_path / "repo.conf")["class"] is None


def test_history_unknown_on_edited_file(tmp_path):
    path = _pair(tmp_path, "a.conf", "original\n")
    path.write_text("edited by hand\n")
    assert prov.summarize(path, fingerprint=FP)["history_unknown"] is True


def test_duplicate_count(tmp_path):
    _pair(tmp_path, "a.conf", "same\n")
    _pair(tmp_path, "b.conf", "same\n")
    _pair(tmp_path, "c.conf", "different\n")
    dups = prov.duplicate_counts(tmp_path, ["a.conf", "b.conf", "c.conf"])
    assert dups == {"a.conf": 1, "b.conf": 1, "c.conf": 0}
    info = prov.summarize(tmp_path / "a.conf", duplicates=dups["a.conf"])
    assert info["duplicates"] == 1


def test_deploy_counts_join(monkeypatch):
    class FakeRegistry:
        def get_all_entries(self):
            return [
                {"service_name": "s", "service_deleted_at": None,
                 "selected_compose_paths": ["a.yml", "b.yml"]},
                {"service_name": "s", "service_deleted_at": None,
                 "selected_nginx_paths": {"http": "a.yml"}},
                {"service_name": "s", "service_deleted_at": "2026-01-01",
                 "selected_compose_paths": ["b.yml"]},
                {"service_name": "other", "selected_compose_paths": ["a.yml"]},
            ]

    monkeypatch.setattr("app.services.registry.Registry", FakeRegistry)
    assert prov.deploy_counts("s") == {"a.yml": 2, "b.yml": 1}


def test_registry_outage_degrades(monkeypatch):
    class Broken:
        def get_all_entries(self):
            raise RuntimeError("registry down")

    monkeypatch.setattr("app.services.registry.Registry", Broken)
    assert prov.deploy_counts("s") == {}


def test_build_provenance_map(tmp_path):
    _pair(tmp_path, "a.conf", "a\n", fingerprint=FP)
    out = prov.build_provenance(tmp_path, ["a.conf", "missing.conf"],
                                fingerprint=FP)
    assert set(out) == {"a.conf"}  # only existing files get a row
    assert out["a.conf"]["exact"] is True


def test_ins_files_are_not_sent_file_candidates(tmp_path, monkeypatch):
    """R3.1 — only the `ins` class is filtered from the pickers."""
    from app.config import settings
    from app.services.file_sets import list_file_sets
    from app.services.project_state import ProjectState

    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    project = tmp_path / "svc"
    project.mkdir()
    (project / "docker-compose.repo.yml").write_text("services: {}\n")
    gm.write_marker_pair(project / "docker-compose.user-alice.0.yml",
                         "services: {}\n", cls="ins")
    ProjectState(project).save()

    out = list_file_sets("svc")
    candidates = out["candidates"]["."]["compose"]
    assert "docker-compose.repo.yml" in candidates
    assert "docker-compose.user-alice.0.yml" not in candidates
    assert out["provenance"]["."]["docker-compose.repo.yml"]["class"] is None

"""Mechanical rows of the consolidated scenario matrix (M-A/M-B/M-C).

Only the rows whose rule is a pure function live here. Rows that need the LLM,
the full job or the browser remain integration/ui/live rows (§0).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import env_classification as ec
from app.services import dependency_model as dm

FP = {"compose": ["sha256:a"], "profiles": [], "env_bases": ["sha256:b"]}


def _artifact(**variables) -> dict:
    return {"version": 1, "fingerprint": FP, "variables": variables,
            "dependencies": {}, "generated_at": "2026-11-24T00:00:00Z"}


# ---------------------------------------------------------------- M-A rows

def test_m_a6_an_invalid_schema_variant_is_red():
    data = _artifact(X={"category": "nonsense", "source": {"kind": "file"}})
    assert ec.artifact_errors(data)


def test_m_a10_a_partial_classification_names_the_missing_env_key():
    data = _artifact(KEEP={"category": "defaulted", "source": {"kind": "file"}})
    assert ec.coverage_missing(data, ["KEEP", "MISSING"]) == ["MISSING"]


def test_m_a12_and_m_a17_extra_classification_keys_are_allowed():
    data = _artifact(KEEP={"category": "defaulted", "source": {"kind": "file"}},
                     EXTRA={"category": "defaulted", "source": {"kind": "file"}})
    assert ec.coverage_missing(data, ["KEEP"]) == []


def test_m_a14_a_target_outside_the_selected_set_is_a_violation():
    data = _artifact(DEP={"category": "dependency",
                          "source": {"kind": "dependency_target",
                                     "target": "graphiti-local"}})
    assert ec.target_allowlist_violations(data, ["other"]) == [
        {"variable": "DEP", "target": "graphiti-local"}]


def test_m_a21_a_legacy_json_matches_by_fingerprint_and_yaml_is_canonical(tmp_path):
    legacy = {"version": 1, "fingerprint": FP, "variables": {
        "A": {"category": "defaulted", "source": {"kind": "file"}}},
        "dependencies": {}}
    (tmp_path / ec.ARTIFACT_NAME).write_text(json.dumps(legacy), encoding="utf-8")
    yaml_path = ec.write_artifact(
        tmp_path, source_service="s", recipe_path="", fingerprint=FP,
        variables={"A": {"category": "defaulted", "source": {"kind": "file"}}})
    assert ec.matched_variant(tmp_path, FP)[0] == yaml_path.name


def test_m_a22_a_duplicate_legacy_and_yaml_pair_is_one_canonical_variant(tmp_path):
    (tmp_path / ec.ARTIFACT_NAME).write_text(json.dumps({
        "version": 1, "fingerprint": FP, "variables": {
            "A": {"category": "defaulted", "source": {"kind": "file"}}},
        "dependencies": {}}), encoding="utf-8")
    yaml_path = ec.write_artifact(
        tmp_path, source_service="s", recipe_path="", fingerprint=FP,
        variables={"A": {"category": "defaulted", "source": {"kind": "file"}}})
    names = [v["name"] for v in ec.list_variants(tmp_path)]
    assert ec.ARTIFACT_NAME in names and yaml_path.name in names
    assert ec.matched_variant(tmp_path, FP)[0] == yaml_path.name


# ---------------------------------------------------------------- M-B rows

def test_m_b4_an_empty_projection_allows_none():
    data = _artifact(KEEP={"category": "defaulted", "source": {"kind": "file"}})
    assert ec.projection(data, "graphiti-local") == []


def test_m_b5_m_b7_m_b12_the_1to1_projection_catches_missing_and_extra(tmp_path):
    name = dm.write_relation(tmp_path, "s", "t", {"A": "t", "B": "t"}, FP).name
    data = dm.read_relation_variant(tmp_path, "s", "t", name)
    assert dm.relation_projection_errors(data, ["A"]) == ([], ["B"])
    assert dm.relation_projection_errors(data, ["A", "B", "C"]) == (["C"], [])
    # Regeneration writes exactly the projection.
    fixed = dm.write_relation(tmp_path, "s", "t", {"A": "t"}, FP).name
    assert dm.relation_projection_errors(
        dm.read_relation_variant(tmp_path, "s", "t", fixed), ["A"]) == ([], [])


def test_m_b14_m_c10_a_deleted_relation_reads_as_absent(tmp_path):
    name = dm.write_relation(tmp_path, "s", "t", {"A": "t"}, FP).name
    (tmp_path / name).unlink()
    assert dm.read_relation_variant(tmp_path, "s", "t", name) is None


def test_m_b18_a_missing_projected_var_is_appended():
    text = ec.apply_env_values("KEEP=1\n", {"NEW": "http://t:1"})
    assert "KEEP=1" in text and "NEW=http://t:1" in text


def test_m_b19_a_stale_selected_env_value_is_replaced():
    text = ec.apply_env_values("URL=http://old\n", {"URL": "http://new"})
    assert "URL=http://new" in text and "http://old" not in text


# ---------------------------------------------------------------- M-C rows

def test_m_c6_a_none_classification_keeps_the_validation_only_path(monkeypatch):
    from app.services import deploy_workflow
    from app.services.generation_jobs import generation_jobs as gm
    monkeypatch.setattr(gm, "_recipe_missing", lambda req: [])
    intent = {"service_name": "s", "recipe_path": "", "classification": "none",
              "selection": {"compose": ["c.yml"], "nginx": "n.conf",
                            "serving_ports": "p.yml", "env": ["e.env"],
                            "env_none": False}}
    assert deploy_workflow.generation_needed(intent) is False


def test_m_c7_an_existing_classification_injects_for_a_selected_env(monkeypatch):
    from app.services import deploy_workflow
    from app.services.generation_jobs import generation_jobs as gm
    monkeypatch.setattr(gm, "_recipe_missing", lambda req: [])
    intent = {"service_name": "s", "recipe_path": "",
              "classification": "env.classification.0123456789abcdef.yaml",
              "selection": {"compose": ["c.yml"], "nginx": "n.conf",
                            "serving_ports": "p.yml", "env": ["e.env"],
                            "env_none": False}}
    assert deploy_workflow.generation_needed(intent) is True


def test_m_c13_editing_both_artefacts_rechecks_the_same_rules():
    data = _artifact(DEP={"category": "dependency",
                          "source": {"kind": "dependency_target",
                                     "target": "t"}})
    assert ec.projection(data, "t") == ["DEP"]
    data["variables"]["DEP"]["source"]["target"] = "other"
    assert ec.projection(data, "t") == []
    assert ec.target_allowlist_violations(data, ["t"]) == [
        {"variable": "DEP", "target": "other"}]

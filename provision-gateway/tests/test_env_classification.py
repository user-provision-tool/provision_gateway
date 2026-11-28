"""T0/T8/T9 — the env classification contract, artefact, renderer and check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import env_classification as ec


def _classification(**variables):
    return {"variables": {
        name: {"category": "defaulted", "source": {"kind": "file"}, "reason": "r"}
        for name in variables
    }, "dependencies": {}}


def test_the_six_categories_are_the_contract():
    assert set(ec.CATEGORIES) == {
        "required", "external", "secret", "deploy_value", "dependency", "defaulted"}
    assert set(ec.SOURCE_KINDS) == {
        "file", "compose_default", "metadata_key", "dependency_target",
        "operator", "minted"}


def test_a_classification_normalizes_and_derives_the_dependency_map():
    raw = {"variables": {
        "API_URL": {"category": "deploy_value", "source": {"kind": "metadata_key", "key": "serving_url"},
                    "template": "http://{{ hostname }}:{{ http_port }}", "reason": "URL"},
        "DEP_URL": {"category": "dependency", "source": {"kind": "dependency_target",
                                                         "target": "graphiti-local"}, "reason": "dep"},
        "EXT": {"category": "external", "source": {"kind": "operator"}},
        "S": {"category": "secret", "source": {"kind": "minted"}},
        "D": {"category": "defaulted", "source": {"kind": "compose_default"}, "value": "8000"},
        "R": {"category": "required", "source": {"kind": "file"}},
    }}
    out = ec.normalize_classification(raw)
    assert set(out["variables"]) == {"API_URL", "DEP_URL", "EXT", "S", "D", "R"}
    assert out["dependencies"] == {"graphiti-local": ["DEP_URL"]}


def test_an_unknown_category_is_rejected():
    with pytest.raises(ec.ClassificationError):
        ec.normalize_classification({"variables": {
            "X": {"category": "deploy_url", "source": {"kind": "file"}}}})


def test_an_unknown_source_kind_is_rejected_but_a_missing_one_is_derived():
    with pytest.raises(ec.ClassificationError):
        ec.normalize_classification({"variables": {
            "X": {"category": "secret", "source": {"kind": "nonsense"}}}})
    out = ec.normalize_classification({"variables": {
        "X": {"category": "secret", "source": {}}}})
    assert out["variables"]["X"]["source"]["kind"] == "minted"


def test_the_defaulted_variable_is_never_dropped_and_external_is_null():
    variables = {"D": {"category": "defaulted"}, "E": {"category": "external"}}
    out = ec.validate_values({"D": {"value": "1", "source": "file"},
                              "E": {"value": None, "source": "operator"}}, variables)
    assert out["D"]["value"] == "1"
    assert out["E"]["value"] is None


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_the_fingerprint_is_order_insensitive_and_empty_profiles_is_a_list(tmp_path):
    _write(tmp_path / "a.yml", "services: {}\n")
    _write(tmp_path / "b.yml", "services: {x: {}}\n")
    _write(tmp_path / ".env", "A=1\n")
    left = ec.structural_fingerprint(
        tmp_path, compose_names=["a.yml", "b.yml"], profiles=["b", "a"],
        env_base_names=[".env"])
    right = ec.structural_fingerprint(
        tmp_path, compose_names=["b.yml", "a.yml"], profiles=["a", "b"],
        env_base_names=[".env"])
    assert left == right
    assert left["profiles"] == ["a", "b"]
    empty = ec.structural_fingerprint(
        tmp_path, compose_names=[], profiles=[], env_base_names=[])
    assert empty["profiles"] == []
    assert ec.fingerprint_matches(left, right)
    assert not ec.fingerprint_matches(left, empty)


def test_the_artefact_round_trips_as_a_yaml_fingerprint_variant(tmp_path):
    fingerprint = {"compose": [], "profiles": [], "env_bases": []}
    path = ec.write_artifact(
        tmp_path, source_service="graphiti-mcp", recipe_path="",
        fingerprint=fingerprint,
        variables={"A": {"category": "defaulted", "source": {"kind": "file"}}},
        dependencies={"graphiti-local": ["A"]})
    assert path.name == ec.variant_filename(fingerprint)
    assert path.name.startswith("env.classification.")
    assert path.name.endswith(".yaml")
    from app.services.generated_marker import find_marker
    assert find_marker(path) is not None
    data = ec.read_variant(tmp_path, path.name)
    assert data["version"] == 1
    assert data["dependencies"] == {"graphiti-local": ["A"]}
    assert ec.matched_variant(tmp_path, fingerprint)[0] == path.name
    assert ec.matched_variant(
        tmp_path, {"compose": ["sha256:x"], "profiles": [], "env_bases": []}) is None
    # The legacy JSON name is read but never written by a generation.
    _write(tmp_path / ec.ARTIFACT_NAME, "{not json")
    assert ec.read_artifact(tmp_path) is None


def test_two_fingerprint_variants_coexist_and_only_the_matched_one_lists(tmp_path):
    fp_a = {"compose": ["sha256:a"], "profiles": [], "env_bases": []}
    fp_b = {"compose": ["sha256:b"], "profiles": [], "env_bases": []}
    ec.write_artifact(tmp_path, source_service="s", recipe_path="",
                      fingerprint=fp_a,
                      variables={"A": {"category": "defaulted",
                                       "source": {"kind": "file"}}})
    name_b = ec.write_artifact(
        tmp_path, source_service="s", recipe_path="", fingerprint=fp_b,
        variables={"B": {"category": "defaulted", "source": {"kind": "file"}}}).name
    before = (tmp_path / name_b).read_bytes()
    ec.write_artifact(tmp_path, source_service="s", recipe_path="",
                      fingerprint=fp_b,
                      variables={"B": {"category": "required",
                                       "source": {"kind": "file"}}})
    # Only the matched variant is a candidate; the other fingerprint's bytes
    # are untouched by a generation for this one (R29).
    assert ec.matched_variant(tmp_path, fp_b)[0] == name_b
    assert ec.matched_variant(tmp_path, fp_a)[0] == ec.variant_filename(fp_a)
    assert (tmp_path / ec.variant_filename(fp_a)).is_file()
    assert before != (tmp_path / name_b).read_bytes()


def test_coverage_and_the_target_allowlist_make_a_variant_red(tmp_path):
    artifact = {"version": 1,
                "fingerprint": {"compose": [], "profiles": [], "env_bases": []},
                "variables": {
                    "KEYS": {"category": "defaulted", "source": {"kind": "file"}},
                    "DEP": {"category": "dependency",
                            "source": {"kind": "dependency_target",
                                       "target": "target-a"}}}}
    assert ec.coverage_missing(artifact, ["KEYS", "MISSING"]) == ["MISSING"]
    assert ec.coverage_missing(artifact, ["KEYS"]) == []
    assert ec.projection(artifact, "target-a") == ["DEP"]
    assert ec.projection(artifact, "target-b") == []
    assert ec.target_allowlist_violations(artifact, ["target-a"]) == []
    assert ec.target_allowlist_violations(artifact, ["target-b"]) == [
        {"variable": "DEP", "target": "target-a"}]
    assert ec.artifact_errors(artifact) == []
    broken = {"version": 1, "fingerprint": {}, "variables": {
        "X": {"category": "nonsense", "source": {"kind": "file"}}}}
    assert any("unknown category" in e for e in ec.artifact_errors(broken))


def test_a_template_renders_metadata_and_a_missing_key_is_an_error():
    meta = {"hostname": "svc-a-1.localhost", "http_port": 8767}
    assert ec.render_template("http://{{ hostname }}:{{ http_port }}", meta) == \
        "http://svc-a-1.localhost:8767"
    with pytest.raises(ec.MissingMetadataKey):
        ec.render_template("http://{{ hostname }}:{{ nope }}", meta)


def test_injection_covers_every_category():
    artifact = {"variables": {
        "REQ": {"category": "required", "source": {"kind": "file"}},
        "EXT": {"category": "external", "source": {"kind": "operator"}},
        "SEC": {"category": "secret", "source": {"kind": "minted"}},
        "URL": {"category": "deploy_value", "source": {"kind": "metadata_key"},
                "template": "http://{{ hostname }}:{{ http_port }}"},
        "DEP": {"category": "dependency", "source": {"kind": "dependency_target"}},
        "DEF": {"category": "defaulted", "source": {"kind": "compose_default"},
                "value": "8000"},
    }}
    values, missing, externals = ec.inject_values(
        artifact,
        base_values={"REQ": "present", "SEC": "", "DEF": ""},
        metadata={"hostname": "h", "http_port": 8767},
        relation_values={"DEP": "http://target:8767"},
        external_values={"EXT": "secret-from-operator"},
    )
    assert values["URL"] == "http://h:8767"
    assert values["DEP"] == "http://target:8767"
    assert values["EXT"] == "secret-from-operator"
    assert values["DEF"] == "8000"
    assert values["SEC"] and values["SEC"] != ""
    assert missing == [] and externals == []


def test_an_unsupplied_external_routes_to_the_pause_and_an_unmapped_dependency_is_missing():
    artifact = {"variables": {
        "EXT": {"category": "external", "source": {"kind": "operator"}},
        "DEP": {"category": "dependency", "source": {"kind": "dependency_target"}},
    }}
    values, missing, externals = ec.inject_values(
        artifact, base_values={}, metadata={}, relation_values={},
        external_values={})
    assert externals == ["EXT"]
    assert missing == ["DEP"]
    assert values == {}


def test_a_dependency_blocked_by_no_relation_is_missing_even_with_a_stale_base_value():
    artifact = {"variables": {
        "DEP": {"category": "dependency", "source": {"kind": "dependency_target"}},
    }}
    missing, externals = ec.check_assembled(
        artifact, {"DEP": "http://old:8767"}, relation_values={},
        external_values={})
    assert missing == ["DEP"]
    assert externals == []


def test_the_template_copy_is_verbatim_apart_from_the_essential_values(tmp_path):
    template = (
        "# keep me\n"
        "TEMPLATE_ONLY=untouched\n"
        "EMPTY=\"\"\n"
        "\n"
        "API_URL=http://placeholder\n"
        "KEEP=1\n")
    _write(tmp_path / ".env", template)
    defaults, required = {"EMPTY": "fallback"}, ["API_URL"]
    text = ec.apply_compose_defaults(template, defaults)
    text = ec.apply_env_values(text, {"API_URL": "http://new"})
    assert "# keep me" in text
    assert "TEMPLATE_ONLY=untouched" in text
    assert "EMPTY=fallback" in text
    assert "API_URL=http://new" in text
    assert "KEEP=1" in text
    # The base file itself is untouched.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == template
    assert required == ["API_URL"]


def test_identical_template_files_contribute_one_copy(tmp_path):
    same = "KEEP=1\n# comment\n"
    _write(tmp_path / ".env", same)
    _write(tmp_path / ".env.example", same)
    text = ec.read_env_template(tmp_path, [".env", ".env.example"])
    assert text.count("KEEP=1") == 1
    assert text.count("# comment") == 1


def test_a_variable_only_in_a_reference_is_not_copied(tmp_path):
    _write(tmp_path / ".env", "TEMPLATE=1\n")
    _write(tmp_path / ".env.fix", "ONLY_IN_FIX=2\n")
    template_names, reference_names, text = (
        [".env"], [".env.fix"], (tmp_path / ".env").read_text())
    assert reference_names == [".env.fix"]
    assert "ONLY_IN_FIX" not in text
    assert "TEMPLATE=1" in text


def test_an_internal_service_target_is_demoted_and_wins_over_a_selected_dependency():
    """C22 — same-compose services are internal; never dependencies."""
    artifact = {"version": 1, "fingerprint": {}, "variables": {
        "NEO4J_URI": {"category": "dependency",
                      "source": {"kind": "dependency_target", "target": "neo4j"},
                      "reason": "looks like a dependency"},
        "OTHER": {"category": "dependency",
                  "source": {"kind": "dependency_target", "target": "graphiti-local"}},
    }}
    fixed, rewrites = ec.enforce_internal_targets(
        artifact, {"neo4j"}, {"NEO4J_URI": "http://neo4j:7687", "OTHER": "x"})
    assert rewrites == [{"variable": "NEO4J_URI", "target": "neo4j"}]
    assert fixed["variables"]["NEO4J_URI"]["category"] == "defaulted"
    assert fixed["variables"]["NEO4J_URI"]["value"] == "http://neo4j:7687"
    assert fixed["variables"]["NEO4J_URI"]["source"] == {"kind": "file"}
    assert fixed["dependencies"] == {"graphiti-local": ["OTHER"]}
    # A selected dependency with the same name does not override the internal
    # match: the caller passes only the compose services here.
    assert ec.internal_target_violations(fixed, {"neo4j"}) == []
    assert ec.internal_target_violations(artifact, {"neo4j"}) == [
        {"variable": "NEO4J_URI", "target": "neo4j"}]


def test_an_empty_internal_service_value_becomes_required():
    artifact = {"version": 1, "fingerprint": {}, "variables": {
        "NEO4J_URI": {"category": "dependency",
                      "source": {"kind": "dependency_target", "target": "neo4j"}},
    }}
    fixed, _ = ec.enforce_internal_targets(artifact, {"neo4j"}, {})
    assert fixed["variables"]["NEO4J_URI"]["category"] == "required"
    assert "value" not in fixed["variables"]["NEO4J_URI"]
    assert fixed["dependencies"] == {}


def test_the_classification_snapshot_copies_the_used_variant_with_a_marker(tmp_path):
    fingerprint = {"compose": [], "profiles": [], "env_bases": []}
    source = ec.write_artifact(
        tmp_path, source_service="s", recipe_path="", fingerprint=fingerprint,
        variables={"A": {"category": "defaulted", "source": {"kind": "file"}}})
    name = ec.snapshot_classification(
        tmp_path, user="alice", label="30", source_name=source.name)
    assert name == "env.classification.user-alice.30.yaml"
    assert (tmp_path / name).is_file()
    from app.services.generated_marker import find_marker
    assert find_marker(tmp_path / name) is not None
    assert ec.snapshot_exists(tmp_path, user="alice", label="30")
    assert ec.classification_snapshot_name("alice", "30") == name
    # Nothing to copy ⇒ no snapshot, never a stale one.
    assert ec.snapshot_classification(
        tmp_path, user="bob", label="1", source_name="missing.yaml") is None

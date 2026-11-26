"""F7 — profile-gated compose services do not require their variables."""

from __future__ import annotations

from app.services import env_classification as ec
from app.services.config_validation import validate_env
from app.utils.var_scan import scan_compose_files_active

COMPOSE = """services:
  app:
    image: x
    environment:
      - A=${A}
  opt:
    profiles: [opt]
    image: y
    environment:
      - B=${B}
"""


def _compose(tmp_path):
    p = tmp_path / "compose.yml"
    p.write_text(COMPOSE, encoding="utf-8")
    return p


def test_the_surface_excludes_an_inactive_profile_service(tmp_path):
    p = _compose(tmp_path)
    inactive = scan_compose_files_active([p], [])
    assert [r.name for r in inactive.refs] == ["A"]
    active = scan_compose_files_active([p], ["opt"])
    assert {r.name for r in active.refs} == {"A", "B"}


def _artifact():
    return {"variables": {
        "A": {"category": "defaulted", "source": {"kind": "file"}, "value": "1"},
        "B": {"category": "defaulted", "source": {"kind": "file"}},
    }}


def test_an_empty_defaulted_var_of_an_inactive_profile_is_not_missing():
    artifact = _artifact()
    _, missing, _ = ec.inject_values(
        artifact, base_values={"A": "1"}, metadata={}, relation_values={},
        active_vars={"A"})
    assert missing == []
    _, missing_active, _ = ec.inject_values(
        artifact, base_values={"A": "1"}, metadata={}, relation_values={},
        active_vars={"A", "B"})
    assert missing_active == ["B"]
    missing, _ = ec.check_assembled(
        artifact, {"A": "1", "B": ""}, relation_values={}, active_vars={"A"})
    assert missing == []
    missing_active, _ = ec.check_assembled(
        artifact, {"A": "1", "B": ""}, relation_values={}, active_vars={"A", "B"})
    assert missing_active == ["B"]


def test_validate_env_ignores_an_inactive_profile_var(tmp_path):
    p = _compose(tmp_path)
    assert validate_env("A=1\n", [p], [])["valid"] is True
    result = validate_env("A=1\n", [p], ["opt"])
    assert result["valid"] is False
    assert "B" in result["missing"]

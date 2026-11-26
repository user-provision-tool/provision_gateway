"""R6/F1 — every base ref reaches its OWN phase, and only its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services.generation_jobs import GenerationJobManager


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "source_projects"
    rdir = root / "svc"
    rdir.mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
    (rdir / "compose.yml").write_text(
        "services:\n  app:\n    image: x\n", encoding="utf-8")
    (rdir / "ports.yml").write_text(
        "ports:\n  - sub_service: app\n    port: 8000\n    kind: http\n",
        encoding="utf-8")
    (rdir / "nginx.conf").write_text("server { listen 80; }\n", encoding="utf-8")
    (rdir / ".env").write_text("A=1\n", encoding="utf-8")
    (rdir / "base-compose.yml").write_text(
        "services:\n  app:\n    image: base\n", encoding="utf-8")
    (rdir / "base-ports.yml").write_text(
        "ports:\n  - sub_service: app\n    port: 9000\n    kind: http\n",
        encoding="utf-8")
    (rdir / "base-nginx.conf").write_text(
        "server { listen 8080; }\n", encoding="utf-8")
    return rdir


def _request(project: Path) -> dict:
    return {
        "service_name": "svc",
        "recipe_path": "",
        "selection": {
            "compose": ["compose.yml"],
            "serving_ports": "ports.yml",
            "nginx": "nginx.conf",
            "env": [".env"],
            "profiles": [],
        },
        "base_refs": {
            "compose": ["base-compose.yml"],
            "serving_ports": "base-ports.yml",
            "nginx": {"http": "base-nginx.conf", "stream": None, "mail": None},
            "env": [],
        },
        "deploy_metadata": {"service_name": "svc", "user_name": "u", "label": "0"},
        "profiles": [],
    }


@pytest.fixture
def manager():
    return GenerationJobManager()


def test_the_compose_ref_reaches_only_the_compose_phase(project, manager):
    ctx = manager._build_context("svc", "", "compose", _request(project))
    assert "base-compose.yml" in ctx["base_files"]
    assert "base-ports.yml" not in ctx["base_files"]
    other = manager._build_context("svc", "", "serving_ports", _request(project))
    assert "base-compose.yml" not in other["base_files"]


def test_the_serving_ports_ref_reaches_only_the_serving_ports_phase(project, manager):
    ctx = manager._build_context("svc", "", "serving_ports", _request(project))
    assert "base-ports.yml" in ctx["base_files"]
    assert "base-compose.yml" not in ctx["base_files"]
    other = manager._build_context("svc", "", "compose", _request(project))
    assert "base-ports.yml" not in other["base_files"]


def test_the_nginx_ref_reaches_the_nginx_phase_tagged_with_its_category(project, manager):
    ctx = manager._build_context("svc", "", "nginx", _request(project))
    assert "http:base-nginx.conf" in ctx["base_files"]
    other = manager._build_context("svc", "", "compose", _request(project))
    assert "http:base-nginx.conf" not in other["base_files"]

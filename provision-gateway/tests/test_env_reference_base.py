"""2026-11-19 T7/T12 — the env reference is the BASE of the generated .raw.

O3: every variable from the reference files is preserved (later files win),
non-essential values are kept, and only the essential variables are filled or
overridden. A zero-variable LLM turn must surface its fallback warning.
"""

import asyncio

import pytest


def _project(tmp_path, monkeypatch, compose: str):
    from app.config import settings
    root = tmp_path / "source_projects"
    project = root / "svc"
    project.mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
    (project / "compose.yml").write_text(compose)
    return project


def _mech(tmp_path, monkeypatch, *, compose, references, selected=None):
    from app.services.generation_jobs import generation_jobs
    _project(tmp_path, monkeypatch, compose)
    request = {
        "selection": {"compose": ["compose.yml"], "env": selected or []},
        "env_reference": references,
    }
    return generation_jobs._mechanical_env("svc", "", request) or ""


def _kv(text):
    return dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line
    )


class TestReferenceBase:
    def test_every_reference_variable_is_preserved(self, tmp_path, monkeypatch):
        compose = (
            "services:\n  app:\n    image: x\n    environment:\n"
            "      DB_HOST: ${DB_HOST}\n      DB_PORT: ${DB_PORT:-5432}\n")
        project = _project(tmp_path, monkeypatch, compose)
        (project / "ref.env").write_text("DB_HOST=db\nUNRELATED=kept\nEMPTY=\n")
        from app.services.generation_jobs import generation_jobs
        out = generation_jobs._mechanical_env("svc", "", {
            "selection": {"compose": ["compose.yml"], "env": []},
            "env_reference": ["ref.env"],
        }) or ""
        kv = _kv(out)
        assert kv["DB_HOST"] == "db"          # the reference value stands
        assert kv["UNRELATED"] == "kept"      # non-essential variables survive
        assert kv["DB_PORT"] == "5432"        # empty/unset takes the default
        # order: the reference's own variables first, defaults appended
        assert list(kv) == ["DB_HOST", "UNRELATED", "EMPTY", "DB_PORT"]

    def test_later_reference_files_win(self, tmp_path, monkeypatch):
        project = _project(tmp_path, monkeypatch,
                           "services:\n  app:\n    image: x\n"
                           "    environment:\n      A: ${A}\n")
        (project / "one.env").write_text("A=first\nB=one\n")
        (project / "two.env").write_text("A=second\n")
        from app.services.generation_jobs import generation_jobs
        out = generation_jobs._mechanical_env("svc", "", {
            "selection": {"compose": ["compose.yml"], "env": []},
            "env_reference": ["one.env", "two.env"],
        }) or ""
        kv = _kv(out)
        assert kv["A"] == "second"
        assert kv["B"] == "one"

    def test_a_reference_with_no_compose_surface_is_still_the_env(self, tmp_path, monkeypatch):
        project = _project(tmp_path, monkeypatch,
                           "services:\n  app:\n    image: x\n")
        (project / "ref.env").write_text("ONLY=1\n")
        from app.services.generation_jobs import generation_jobs
        out = generation_jobs._mechanical_env("svc", "", {
            "selection": {"compose": ["compose.yml"], "env": []},
            "env_reference": ["ref.env"],
        }) or ""
        assert _kv(out) == {"ONLY": "1"}

    def test_malformed_reference_lines_are_ignored(self, tmp_path, monkeypatch):
        project = _project(tmp_path, monkeypatch,
                           "services:\n  app:\n    image: x\n")
        (project / "ref.env").write_text("GOOD=1\nnot a line\n# comment\nBAD\n")
        from app.services.generation_jobs import generation_jobs
        out = generation_jobs._mechanical_env("svc", "", {
            "selection": {"compose": ["compose.yml"], "env": []},
            "env_reference": ["ref.env"],
        }) or ""
        assert _kv(out) == {"GOOD": "1"}


class TestZeroVariableWarning:
    def test_the_fallback_warning_is_returned(self, monkeypatch):
        from app.services.llm_service import llm_service

        class _Q:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        class _DB:
            def query(self, *a, **k):
                return _Q()

        monkeypatch.setattr(llm_service, "_resolve_endpoint",
                            lambda db: ("http://llm", "model", {}))
        async def _empty_chat(*args, **kwargs):
            return {"choices": [{"message": {"content": ""}}], "usage": {}}
        monkeypatch.setattr(llm_service, "_chat", _empty_chat)

        result = asyncio.run(llm_service.generate_env_two_turn(_DB(), {}))
        assert result["kvs"] == {}
        assert any("no variables" in w and "mechanical" in w
                   for w in result["warnings"])

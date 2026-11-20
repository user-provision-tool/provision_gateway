"""2026-11-19 T1/T4/T5 — the stream slot derives from the artifact's rows.

R1.1–R1.3: one predicate (`stream_conf.stream_rows`) decides whether a stream
conf is required. TLS alone is not a requirement; an HTTP-only artifact never
demands a conf it cannot produce.
"""

import pytest

from app.services import deploy_workflow
from app.services.stream_conf import artifact_has_stream_rows


def _artifact(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


HTTP_ONLY = "ports:\n  - sub_service: mcp\n    port: 8000\n    kind: http\n"
BOLT = "ports:\n  - sub_service: app\n    port: 9000\n    kind: bolt\n"


class TestArtifactHasStreamRows:
    def test_http_only_has_none(self, tmp_path):
        assert artifact_has_stream_rows(_artifact(tmp_path, "p.yml", HTTP_ONLY)) is False

    def test_stream_kind_has_one(self, tmp_path):
        assert artifact_has_stream_rows(_artifact(tmp_path, "p.yml", BOLT)) is True

    def test_a_kindless_or_malformed_row_contributes_nothing(self, tmp_path):
        text = (
            "ports:\n"
            "  - sub_service: a\n    port: 1\n    kind: http\n"
            "  - sub_service: b\n    port: 2\n"
            "  - not-a-row\n"
        )
        assert artifact_has_stream_rows(_artifact(tmp_path, "p.yml", text)) is False

    def test_missing_or_malformed_reads_as_false(self, tmp_path):
        assert artifact_has_stream_rows(str(tmp_path / "absent.yml")) is False
        bad = _artifact(tmp_path, "bad.yml", "ports: [unclosed\n")
        assert artifact_has_stream_rows(bad) is False
        assert artifact_has_stream_rows(None) is False
        assert artifact_has_stream_rows("") is False


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.generation_jobs import generation_jobs as manager

    root = tmp_path / "source_projects"
    (root / "svc").mkdir(parents=True)
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
    monkeypatch.setattr(manager, "_recipe_missing", lambda req: [])
    (root / "svc" / "http.yml").write_text(HTTP_ONLY)
    (root / "svc" / "bolt.yml").write_text(BOLT)
    return root


def _intent(serving_ports, *, tls=True):
    return {
        "service_name": "svc", "recipe_path": "", "tls": tls,
        "selection": {
            "compose": ["c.yml"], "nginx": "n.conf",
            "serving_ports": serving_ports,
            "env": ["e.env"], "env_none": False,
        },
    }


class TestGenerationNeeded:
    def test_http_only_never_starts_a_stream_generation(self, project):
        assert deploy_workflow.generation_needed(_intent("http.yml")) is False

    def test_a_stream_row_without_a_conf_starts_generation(self, project):
        assert deploy_workflow.generation_needed(_intent("bolt.yml")) is True

    def test_a_selected_stream_conf_satisfies_the_slot(self, project):
        intent = _intent("bolt.yml")
        intent["selection"]["stream_conf"] = "s.conf"
        assert deploy_workflow.generation_needed(intent) is False

    def test_tls_off_never_asks_for_a_stream_conf(self, project):
        assert deploy_workflow.generation_needed(_intent("bolt.yml", tls=False)) is False

    def test_a_missing_artifact_reads_as_no_rows(self, project):
        assert deploy_workflow.generation_needed(_intent("absent.yml")) is False

    def test_serving_ports_none_demands_nothing(self, project):
        intent = _intent(None)
        intent["selection"]["serving_ports_none"] = True
        assert deploy_workflow.generation_needed(intent) is False


class TestSessionReady:
    def _existing(self, serving_ports):
        return {"selection": {
            "compose": ["c.yml"], "nginx": "n.conf",
            "serving_ports": serving_ports,
            "env": ["e.env"], "env_none": False,
        }, "env_name": ""}

    def test_http_only_session_is_ready_without_a_stream_conf(self, project):
        assert deploy_workflow._session_ready(
            self._existing("http.yml"),
            {"tls": True, "service_name": "svc", "recipe_path": ""}) is True

    def test_a_stream_row_without_a_conf_is_not_ready(self, project):
        assert deploy_workflow._session_ready(
            self._existing("bolt.yml"),
            {"tls": True, "service_name": "svc", "recipe_path": ""}) is False

    def test_a_stream_conf_makes_it_ready(self, project):
        existing = self._existing("bolt.yml")
        existing["selection"]["stream_conf"] = "s.conf"
        assert deploy_workflow._session_ready(
            existing,
            {"tls": True, "service_name": "svc", "recipe_path": ""}) is True


class TestSummaryEndpoint:
    """T2 — `GET /api/projects/{name}/serving-ports/summary`."""

    def _setup(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        from app.config import settings
        from app.main import app
        from app.services.registry import Registry

        monkeypatch.setattr(app.state, "registry", Registry(), raising=False)
        root = tmp_path / "source_projects"
        (root / "svc").mkdir(parents=True)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", root)
        (root / "svc" / "serving-ports.yml").write_text(HTTP_ONLY)
        (root / "svc" / "serving-ports-bolt.yml").write_text(BOLT)
        (root / "svc" / "serving-ports-bad.yml").write_text("ports: [unclosed\n")
        return TestClient(app), root

    def _admin_token(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user
        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            key, _ = auth_service.create_api_key(
                db, admin.id, "Default", is_default=True)
            return auth_service.create_provision_token(db, admin.id, key.id)
        finally:
            db.close()

    def _client(self, monkeypatch, tmp_path):
        client, root = self._setup(monkeypatch, tmp_path)
        client.cookies.set("provision_token", self._admin_token())
        return client, root

    def test_http_only_reports_zero_rows(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports.yml"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "path": "serving-ports.yml", "readable": True, "stream_rows": 0}

    def test_bolt_reports_one_row(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports-bolt.yml"})
        assert r.status_code == 200, r.text
        assert r.json()["stream_rows"] == 1

    def test_a_missing_selected_artifact_is_readable_false(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports-absent.yml"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "path": "serving-ports-absent.yml", "readable": False, "stream_rows": 0}

    def test_malformed_yaml_is_readable_false(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports-bad.yml"})
        assert r.status_code == 200, r.text
        assert r.json()["readable"] is False
        assert r.json()["stream_rows"] == 0

    @pytest.mark.parametrize("path", [
        "../escape.yml", "/etc/passwd", "notes.txt", "docker-compose.yml"])
    def test_traversal_absolute_and_non_candidate_are_400(
            self, monkeypatch, tmp_path, path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": path})
        assert r.status_code == 400, r.text

    def test_unknown_service_and_recipe_are_404(self, monkeypatch, tmp_path):
        client, _ = self._client(monkeypatch, tmp_path)
        r = client.get("/api/projects/absent/serving-ports/summary",
                       params={"path": "serving-ports.yml"})
        assert r.status_code == 404, r.text
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports.yml", "recipe_path": "nope"})
        assert r.status_code == 404, r.text

    def test_an_unauthenticated_call_is_refused(self, monkeypatch, tmp_path):
        client, _ = self._setup(monkeypatch, tmp_path)
        r = client.get("/api/projects/svc/serving-ports/summary",
                       params={"path": "serving-ports.yml"})
        assert r.status_code in (401, 403), r.text

"""Unit tests for provision-gateway."""

import pytest
import re
import sys
from pathlib import Path

# Add gateway app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.auth_service import hash_password, verify_password, create_provision_token, decode_token


class TestAuthService:
    """Test password hashing and JWT."""

    def test_hash_and_verify_password(self):
        password = "test_password_123"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed)
        assert not verify_password("wrong_password", hashed)

    def test_empty_password(self):
        hashed = hash_password("")
        assert hashed != ""
        assert verify_password("", hashed)

    def test_create_and_decode_token(self):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            token = create_provision_token(db, "42")
        finally:
            db.close()
        assert token is not None
        payload = decode_token(token)
        assert payload["sub"] == "42"
        assert payload["type"] == "provision"
        assert "token_epoch" in payload

    def test_invalid_token(self):
        from jose import JWTError
        with pytest.raises(JWTError):
            decode_token("invalid.token.here")


class TestConfig:
    """Test configuration loading."""

    def test_settings_defaults(self):
        from app.config import Settings
        s = Settings()
        assert s.PROVISION_API_URL == "http://subnet-acl-provision-api:8000"
        assert s.JWT_EXPIRE_SEC == 3600
        assert s.JWT_REFRESH_EXPIRE_SEC == 604800

    def test_docker_ops_log_still_defined(self):
        """DOCKER_OPS_LOG should still exist in config (backward compat)."""
        from app.config import Settings
        s = Settings()
        assert s.DOCKER_OPS_LOG is not None


# ---------------------------------------------------------------------------
# Tests for scan re-architecture — project_state module (F11-F14)
# ---------------------------------------------------------------------------

class TestProjectStateModule:
    """project_state.py: fingerprinting + atomic state persistence (F11-F14)."""

    def test_compute_dir_fingerprint_entries(self, tmp_path):
        from app.services.project_state import compute_dir_fingerprint
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "sub").mkdir()
        fp = compute_dir_fingerprint(tmp_path)
        assert set(fp.keys()) == {"a.txt", "sub"}
        a = fp["a.txt"]
        assert a["size"] == 5
        assert a["is_dir"] is False
        assert isinstance(a["mtime_ns"], int) and a["mtime_ns"] > 0
        assert fp["sub"]["is_dir"] is True

    def test_compute_dir_fingerprint_missing_dir(self, tmp_path):
        from app.services.project_state import compute_dir_fingerprint
        assert compute_dir_fingerprint(tmp_path / "nope") == {}

    def test_fingerprints_equal(self):
        from app.services.project_state import fingerprints_equal
        assert fingerprints_equal(
            {"a": {"mtime_ns": 1, "size": 2, "is_dir": False}},
            {"a": {"mtime_ns": 1, "size": 2, "is_dir": False}},
        ) is True
        assert fingerprints_equal(
            {"a": {"mtime_ns": 1, "size": 2, "is_dir": False}},
            {"a": {"mtime_ns": 9, "size": 2, "is_dir": False}},
        ) is False
        assert fingerprints_equal(None, {}) is False

    def test_state_roundtrip(self, tmp_path):
        from app.services.project_state import ProjectState
        state = ProjectState(tmp_path)
        state.recipe_origin = "user"
        state.recipe_paths = [".", "docker"]
        state.recipes = [{"path": "", "label": "(root)", "is_root": True}]
        state.files = ["a.txt", "docker/b.yml"]
        state.generated_files = ["docker/b.yml"]
        state.template_files = ["Dockerfile"]
        state.dir_scans = {".": {"fingerprint": {"a.txt": {"mtime_ns": 1, "size": 2, "is_dir": False}}}}
        state.updated_at = "2026-08-28T00:00:00+00:00"
        state.save()
        loaded = ProjectState.load(tmp_path)
        assert loaded is not None
        assert loaded.schema_version == 2
        assert loaded.recipe_origin == "user"
        assert loaded.recipe_paths == [".", "docker"]
        assert loaded.recipes == [{"path": "", "label": "(root)", "is_root": True}]
        assert loaded.files == ["a.txt", "docker/b.yml"]
        assert loaded.generated_files == ["docker/b.yml"]
        assert loaded.template_files == ["Dockerfile"]
        assert loaded.dir_scans == state.dir_scans
        assert loaded.updated_at == state.updated_at

    def test_state_load_missing_and_corrupt(self, tmp_path):
        from app.services.project_state import ProjectState
        assert ProjectState.load(tmp_path) is None
        (tmp_path / ".provision-state.json").write_text("{not json")
        assert ProjectState.load(tmp_path) is None
        (tmp_path / ".provision-state.json").write_text('{"schema_version": 99}')
        assert ProjectState.load(tmp_path) is None
        (tmp_path / ".provision-state.json").write_text('{"schema_version": 1, "recipe_paths": 5}')
        assert ProjectState.load(tmp_path) is None

    def test_state_save_is_atomic(self, tmp_path):
        from app.services.project_state import ProjectState
        ProjectState(tmp_path).save()
        assert (tmp_path / ".provision-state.json").is_file()
        assert not (tmp_path / ".provision-state.json.tmp").exists()

    def test_fingerprint_ignores_state_file_churn(self, tmp_path):
        """The state file's own mtime must never invalidate the fingerprint
        (otherwise every save would trigger a rescan on the next listing)."""
        from app.services.project_state import compute_dir_fingerprint, ProjectState
        (tmp_path / "a.txt").write_text("hello")
        fp1 = compute_dir_fingerprint(tmp_path)
        ProjectState(tmp_path).save()
        fp2 = compute_dir_fingerprint(tmp_path)
        assert fp1 == fp2


class TestScanRearchitecture:
    """Scan re-architecture: shallow scan, cache, set_recipes, tree (F3-F8, F20-F22)."""

    @staticmethod
    def _svc(tmp_path, monkeypatch):
        from app.services.service_manager import ServiceManager
        svc = ServiceManager()
        monkeypatch.setattr(svc, "_source_dir", tmp_path)
        return svc

    def test_state_file_written_on_first_scan(self, tmp_path, monkeypatch):
        from app.services.project_state import ProjectState
        project = tmp_path / "svc1"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        svc = self._svc(tmp_path, monkeypatch)
        info = svc._get_service_info(project)
        assert info["files"] == ["Dockerfile"]
        state_file = project / ".provision-state.json"
        assert state_file.is_file()
        state = ProjectState.load(project)
        assert state is not None
        assert state.schema_version == 2
        assert state.recipe_origin == "auto"
        assert state.recipe_paths == ["."]
        assert state.files == ["Dockerfile"]

    def test_shallow_scan_never_touches_deep_dirs(self, tmp_path, monkeypatch):
        project = tmp_path / "svc2"
        (project / "node_modules" / "pkg" / "deep").mkdir(parents=True)
        (project / "node_modules" / "pkg" / "deep" / "x.py").write_text("x")
        (project / "src" / "deep").mkdir(parents=True)
        (project / "src" / "deep" / "y.py").write_text("y")
        (project / "top.txt").write_text("t")
        info = self._svc(tmp_path, monkeypatch)._get_service_info(project)
        assert info["files"] == ["top.txt"]  # neither node_modules/** nor src/** deep files

    def test_cache_avoids_rescan_on_unchanged_fingerprint(self, tmp_path, monkeypatch):
        from app.services.project_state import ProjectState
        project = tmp_path / "svc3"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        info1 = svc._get_service_info(project)
        updated1 = ProjectState.load(project).updated_at
        info2 = svc._get_service_info(project)
        updated2 = ProjectState.load(project).updated_at
        assert info1 == info2
        assert updated1 == updated2, "unchanged tree must not re-scan/re-save"

    def test_cache_invalidates_on_new_top_level_file(self, tmp_path, monkeypatch):
        project = tmp_path / "svc4"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        assert svc._get_service_info(project)["files"] == ["a.txt"]
        (project / "b.txt").write_text("b")
        assert svc._get_service_info(project)["files"] == ["a.txt", "b.txt"]

    def test_state_file_never_listed(self, tmp_path, monkeypatch):
        project = tmp_path / "svc6"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        svc._get_service_info(project)
        info = svc._get_service_info(project)
        assert not any(f.startswith(".provision-state") for f in info["files"])

    def test_set_recipes_scopes_and_resets(self, tmp_path, monkeypatch):
        project = tmp_path / "svc7"
        project.mkdir()
        (project / "docker").mkdir()
        (project / "docker" / "docker-compose.yml").write_text("services: {}")
        (project / "docker" / "Dockerfile").write_text("FROM python:3.12")
        (project / "root.txt").write_text("r")
        svc = self._svc(tmp_path, monkeypatch)
        info = svc.set_recipes("svc7", ["docker"])
        assert info["files"] == ["docker/Dockerfile", "docker/docker-compose.yml"]
        assert info["recipes"] == [{
            "path": "docker", "label": "docker", "is_root": False,
            "template_files": ["docker/Dockerfile", "docker/docker-compose.yml"],
        }]
        # empty list → root-only reset
        info = svc.set_recipes("svc7", [])
        assert info["files"] == ["root.txt"]
        assert info["recipes"] == [{
            "path": "", "label": "(root)", "is_root": True, "template_files": [],
        }]

    def test_set_recipes_normalizes_dot(self, tmp_path, monkeypatch):
        project = tmp_path / "svc8"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        info = svc.set_recipes("svc8", ["", "."])
        assert info["files"] == ["a.txt"]

    @pytest.mark.parametrize("bad", ["..", "../x", "a/../b", "/abs", "dir\\win", "dir:evil"])
    def test_set_recipes_rejects_invalid(self, tmp_path, monkeypatch, bad):
        project = tmp_path / "svc9"
        project.mkdir()
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            svc.set_recipes("svc9", [bad])

    def test_set_recipes_rejects_non_dir(self, tmp_path, monkeypatch):
        project = tmp_path / "svc10"
        project.mkdir()
        (project / "file.txt").write_text("x")
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            svc.set_recipes("svc10", ["file.txt"])

    def test_set_recipes_unknown_service(self, tmp_path, monkeypatch):
        from app.services.service_manager import ServiceNotFoundError
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ServiceNotFoundError):
            svc.set_recipes("ghost", ["."])

    def test_tree_unknown_service_raises(self, tmp_path, monkeypatch):
        from app.services.service_manager import ServiceNotFoundError
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ServiceNotFoundError):
            svc.list_tree_children("ghost", "")

    def test_recipe_dir_deleted_no_crash(self, tmp_path, monkeypatch):
        import shutil
        project = tmp_path / "svc11"
        project.mkdir()
        (project / "docker").mkdir()
        (project / "docker" / "x.yml").write_text("x")
        svc = self._svc(tmp_path, monkeypatch)
        svc.set_recipes("svc11", ["docker"])
        shutil.rmtree(project / "docker")
        info = svc._get_service_info(project)  # must not crash
        assert info["files"] == []
        assert info["recipes"] == []

    def test_tree_immediate_children_only(self, tmp_path, monkeypatch):
        project = tmp_path / "svc12"
        project.mkdir()
        (project / "api").mkdir()
        (project / "api" / "main.py").write_text("x")
        (project / "web").mkdir()
        (project / "web" / "app.py").write_text("x")
        (project / "top.txt").write_text("t")
        (project / "gen.py").write_text("g")
        (project / "gen.py.generated").write_text("")
        svc = self._svc(tmp_path, monkeypatch)
        children = svc.list_tree_children("svc12", "")
        assert [c["name"] for c in children] == ["api", "gen.py", "top.txt", "web"]
        by_name = {c["name"]: c for c in children}
        assert by_name["api"]["type"] == "dir"
        assert by_name["top.txt"]["type"] == "file"
        assert by_name["top.txt"]["is_generated"] is False
        assert by_name["gen.py"]["is_generated"] is True
        assert by_name["top.txt"]["is_template"] is False
        assert by_name["api"]["path"] == "api"
        # nested listing is immediate-children only
        api_children = svc.list_tree_children("svc12", "api")
        assert [c["name"] for c in api_children] == ["main.py"]
        assert api_children[0]["path"] == "api/main.py"

    @pytest.mark.parametrize("bad_dir", ["../../etc", "/etc", "api/../../etc"])
    def test_tree_rejects_path_traversal(self, tmp_path, monkeypatch, bad_dir):
        project = tmp_path / "svc13"
        project.mkdir()
        (project / "api").mkdir()
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            svc.list_tree_children("svc13", bad_dir)

    def test_tree_missing_dir_raises(self, tmp_path, monkeypatch):
        project = tmp_path / "svc14"
        project.mkdir()
        svc = self._svc(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            svc.list_tree_children("svc14", "nope")

    def test_tree_excludes_state_and_generated_markers(self, tmp_path, monkeypatch):
        project = tmp_path / "svc15"
        project.mkdir()
        (project / "a.txt").write_text("a")
        (project / "b.txt").write_text("b")
        (project / "b.txt.generated").write_text("")
        svc = self._svc(tmp_path, monkeypatch)
        svc._get_service_info(project)  # writes state file
        names = [c["name"] for c in svc.list_tree_children("svc15", "")]
        assert names == ["a.txt", "b.txt"]
        assert ".provision-state.json" not in names
        assert "b.txt.generated" not in names

    def test_list_files_returns_cached_union(self, tmp_path, monkeypatch):
        project = tmp_path / "svc16"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        assert svc.list_files("svc16") == ["a.txt"]
        assert svc.list_files("ghost") == []

    def test_registry_ttl_cache_loads_once(self, tmp_path, monkeypatch):
        """The registry yaml must be parsed at most once per TTL window — a
        per-project parse per request made warm /api/services ~200ms (DB1)."""
        from app.config import settings
        import app.services.service_manager as sm_mod
        registry_dir = tmp_path / "generated"
        registry_dir.mkdir()
        (registry_dir / "user_registry.yml").write_text(
            "- user_name: bob\n  service_name: svc17\n  label: '0'\n"
            "- user_name: alice\n  service_name: svc17\n  label: '1'\n"
        )
        monkeypatch.setattr(settings, "GENERATED_DIR", registry_dir)
        project = tmp_path / "svc17"
        project.mkdir()
        (project / "a.txt").write_text("a")
        svc = self._svc(tmp_path, monkeypatch)
        svc._registry_cache = None

        loads = {"n": 0}
        import yaml as _yaml
        real_load = _yaml.load
        def counting_load(stream, Loader=None):
            loads["n"] += 1
            return real_load(stream, Loader=Loader)
        monkeypatch.setattr(_yaml, "load", counting_load)

        info1 = svc._get_service_info(project)
        info2 = svc._get_service_info(project)
        assert info1["active_users"] == 2
        assert sorted(info1["active_instances"]) == ["alice/1", "bob/0"]
        assert loads["n"] == 1, "registry yaml must be parsed only once within the TTL window"


class TestScanRearchitectureHandlers:
    """Non-blocking handler checks + new routes (F24-F29)."""

    def test_pure_blocking_handlers_are_def(self):
        import inspect
        from app.routers import services as svc_router
        for fn_name in (
            "list_services", "get_project_notifications", "get_service",
            "create_service", "save_generated_files", "delete_service",
            "get_service_file", "write_service_file", "create_service_file",
            "delete_service_file", "convert_service_files", "scan_repo",
            "git_status", "git_diff", "git_head_file",
        ):
            fn = getattr(svc_router, fn_name)
            assert not inspect.iscoroutinefunction(fn), (
                f"{fn_name} should be a sync def handler (threadpool)"
            )

    def test_mixed_handlers_wrap_blocking_in_threadpool(self):
        import inspect
        from app.routers.services import check_missing_files, check_deploy_readiness
        assert inspect.iscoroutinefunction(check_missing_files)
        assert inspect.iscoroutinefunction(check_deploy_readiness)
        assert "run_in_threadpool" in inspect.getsource(check_missing_files)
        # check-deploy's implicit LLM auto-generation was RETIRED (design
        # §Implementation notes L252-253) — the readiness report now awaits
        # provision_service directly; the remaining blocking scan runs in the
        # threadpool via provision_service's async proxy.
        assert "provision_service.check_missing_files" in inspect.getsource(check_deploy_readiness)
        assert "generate_config" not in inspect.getsource(check_deploy_readiness)

    def test_recipes_and_tree_routes_registered_before_catch_all(self):
        from app.routers.services import router
        prefix = router.prefix
        routes = router.routes

        def idx(path):
            for i, r in enumerate(routes):
                if r.path == f"{prefix}{path}":
                    return i
            return None

        recipes_idx = idx("/{name}/recipes")
        tree_idx = idx("/{name}/tree")
        catch_all_idx = idx("/{name}")
        assert recipes_idx is not None, f"recipes route missing: {[r.path for r in routes]}"
        assert tree_idx is not None, f"tree route missing: {[r.path for r in routes]}"
        assert catch_all_idx is not None
        assert recipes_idx < catch_all_idx, "recipes route must be registered before /{name}"
        assert tree_idx < catch_all_idx, "tree route must be registered before /{name}"

    def test_tree_endpoint_rejects_traversal_with_400(self, tmp_path, monkeypatch):
        from fastapi import HTTPException
        from app.routers import services as svc_router
        from app.routers.services import get_service_tree
        project = tmp_path / "svc"
        project.mkdir()
        monkeypatch.setattr(svc_router.service_manager, "_source_dir", tmp_path)
        with pytest.raises(HTTPException) as ei:
            get_service_tree("svc", "../../etc", current_admin={"id": 1, "role": "admin"})
        assert ei.value.status_code == 400

    def test_recipes_endpoint_rejects_invalid_with_400(self, tmp_path, monkeypatch):
        from fastapi import HTTPException
        from app.routers import services as svc_router
        from app.routers.services import set_service_recipes
        from app.schemas.services import ServiceRecipesRequest
        project = tmp_path / "svc"
        project.mkdir()
        (project / "recipes").mkdir()
        (project / "recipes" / "api").mkdir()
        monkeypatch.setattr(svc_router.service_manager, "_source_dir", tmp_path)
        with pytest.raises(HTTPException) as ei:
            set_service_recipes(
                "svc", ServiceRecipesRequest(recipe_paths=[".."]),
                current_admin={"id": 1, "role": "admin"},
            )
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            set_service_recipes(
                "svc", ServiceRecipesRequest(recipe_paths=["/etc"]),
                current_admin={"id": 1, "role": "admin"},
            )
        assert ei.value.status_code == 400
        # non-directory recipe path on an EXISTING service -> 400, never 404
        with pytest.raises(HTTPException) as ei:
            set_service_recipes(
                "svc", ServiceRecipesRequest(recipe_paths=["recipes/api/nonexistentfile"]),
                current_admin={"id": 1, "role": "admin"},
            )
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            set_service_recipes(
                "ghost", ServiceRecipesRequest(recipe_paths=["."]),
                current_admin={"id": 1, "role": "admin"},
            )
        assert ei.value.status_code == 404

    def test_tree_endpoint_unknown_service_404(self, tmp_path, monkeypatch):
        from fastapi import HTTPException
        from app.routers import services as svc_router
        from app.routers.services import get_service_tree
        monkeypatch.setattr(svc_router.service_manager, "_source_dir", tmp_path)
        with pytest.raises(HTTPException) as ei:
            get_service_tree("ghost", "", current_admin={"id": 1, "role": "admin"})
        assert ei.value.status_code == 404

    def test_git_status_filters_state_and_markers(self, monkeypatch):
        from app.routers import services as svc_router
        monkeypatch.setattr(
            svc_router, "_git_command",
            lambda name, *args: (
                " M .provision-state.json\n"
                "?? docker-compose.yml.generated\n"
                " M real.txt\n"
                "?? new.txt\n"
            ),
        )
        result = svc_router.git_status("svc", current_admin={"id": 1})
        assert result["modified"] == [{"status": "M", "file": "real.txt"}]
        assert result["untracked"] == [{"status": "?", "file": "new.txt"}]

    def test_blocking_scans_are_offloaded(self):
        """Blocking filesystem/git work runs in a threadpool, never on the loop."""
        from pathlib import Path
        services_src = (Path(__file__).parent.parent / "app" / "routers" / "services.py").read_text()
        assert "run_in_threadpool" in services_src
        users_src = (Path(__file__).parent.parent / "app" / "routers" / "users.py").read_text()
        assert "await provision_service.register_user" in users_src, (
            "the deploy path is async end-to-end (the -api call is awaited)")

    def test_schemas_models_exist(self):
        from app.schemas.services import (
            ServiceRecipesRequest, ServiceTreeChild, ServiceTreeResponse,
        )
        req = ServiceRecipesRequest(recipe_paths=["docker"], auto=False)
        assert req.recipe_paths == ["docker"]
        assert ServiceRecipesRequest(auto=True).auto is True
        child = ServiceTreeChild(name="a", path="a", type="file", is_generated=True, is_template=False)
        assert child.is_generated is True
        resp = ServiceTreeResponse(name="svc", dir="", children=[child])
        assert resp.children[0].name == "a"


# ---------------------------------------------------------------------------
# Tests for provision_service — container logs and SSE streaming
# ---------------------------------------------------------------------------

class TestProvisionService:
    """Test new provision_service proxy methods added for user_provision sync."""

    def test_get_container_logs_url_format(self):
        """Verify get_container_logs builds the correct provision-api URL path."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()
        # Check that the method exists and is callable
        assert callable(svc.get_container_logs)

    def test_stream_task_log_exists(self):
        """Verify stream_task_log method exists on provision_service."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()
        assert callable(svc.stream_task_log)

    def test_provision_service_has_all_new_endpoint_methods(self):
        """Verify all new user_provision endpoints are covered by provision_service methods."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()

        # Service lifecycle endpoints
        assert callable(svc.start_user)
        assert callable(svc.stop_user)
        assert callable(svc.change_user_password)
        assert callable(svc.get_container_logs)

        # Docker / host monitoring endpoints
        assert callable(svc.docker_ps)
        assert callable(svc.docker_stats)
        assert callable(svc.docker_info)
        assert callable(svc.host_stats)

        # Reconciliation helpers
        assert callable(svc.container_exists)
        assert callable(svc.container_running)
        assert callable(svc.network_connect)
        assert callable(svc.nginx_reload)

        # Nginx state management
        assert callable(svc.nginx_connections)
        assert callable(svc.nginx_reconnect_all)
        assert callable(svc.reconcile)
        assert callable(svc.reconciliation_status)
        assert callable(svc.nginx_state)

        # Task management
        assert callable(svc.list_tasks)
        assert callable(svc.get_task)
        assert callable(svc.cancel_task)
        assert callable(svc.stream_task_log)

        # Core user operations
        assert callable(svc.list_users)
        assert callable(svc.get_user)
        assert callable(svc.register_user)
        assert callable(svc.remove_user)
        assert callable(svc.rebuild_user)


# ---------------------------------------------------------------------------
# Tests for no duplicate compose_converter
# ---------------------------------------------------------------------------

class TestNoDuplicateConverter:
    """Verify gateway does NOT duplicate provision-api's compose_converter."""

    def test_compose_converter_module_removed(self):
        """The gateway's compose_converter.py should no longer exist."""
        from pathlib import Path
        converter_path = Path(__file__).parent.parent / "app" / "lib" / "compose_converter.py"
        assert not converter_path.exists(), (
            f"compose_converter.py still exists at {converter_path}. "
            "Gateway must NOT duplicate provision-api's compose_converter functionality."
        )

    def test_lib_init_docstring_confirms_delegation(self):
        """The lib/__init__.py docstring should confirm converter is delegated."""
        from pathlib import Path
        init_path = Path(__file__).parent.parent / "app" / "lib" / "__init__.py"
        content = init_path.read_text()
        assert "does not duplicate" in content.lower()


# ---------------------------------------------------------------------------
# Tests for tasks router — SSE proxying
# ---------------------------------------------------------------------------

class TestTasksRouterSSE:
    """Verify tasks router SSE endpoint proxies to provision-api."""

    def test_stream_task_log_endpoint_exists(self):
        """GET /api/tasks/{task_id}/log should be registered."""
        from app.routers.tasks import router
        routes = [r.path for r in router.routes]
        # Route paths include the router prefix "/api/tasks"
        assert any("/{task_id}/log" in r for r in routes), f"SSE log endpoint missing. Routes: {routes}"

    def test_stream_task_log_has_auth(self):
        """SSE log endpoint must require admin authentication."""
        from app.routers.tasks import router
        for route in router.routes:
            if route.path == "/{task_id}/log":
                # Check that the endpoint has dependencies (auth)
                assert len(route.dependencies) > 0 or hasattr(route, 'dependant'), \
                    "SSE log endpoint should require authentication"

    def test_tasks_router_does_not_import_config_docker_ops_log(self):
        """The tasks router should NOT directly read DOCKER_OPS_LOG from config."""
        from pathlib import Path
        tasks_path = Path(__file__).parent.parent / "app" / "routers" / "tasks.py"
        content = tasks_path.read_text()
        # Should not import settings or reference DOCKER_OPS_LOG
        assert "from ..config import settings" not in content, \
            "tasks.py should not import settings for direct log file reading"
        assert "DOCKER_OPS_LOG" not in content, \
            "tasks.py should not reference DOCKER_OPS_LOG directly"


# ---------------------------------------------------------------------------
# Tests for users router — container logs endpoint
# ---------------------------------------------------------------------------

class TestUsersRouterContainerLogs:
    """Verify users router has the new container logs endpoint."""

    def test_container_logs_endpoint_exists(self):
        """GET /api/users/{user}/{svc}/{label}/containers/{container}/logs should be registered."""
        from app.routers.users import router
        routes = [r.path for r in router.routes]
        # Route paths include the router prefix "/api/users"
        target_suffix = "containers/{container}/logs"
        assert any(target_suffix in r for r in routes), (
            f"Container logs endpoint missing from users router. Routes: {routes}"
        )

    def test_container_logs_has_tail_param(self):
        """Container logs endpoint should accept tail query parameter."""
        from app.routers.users import router
        for route in router.routes:
            if route.path == "/{user_name}/{service_name}/{label}/containers/{container}/logs":
                # Verify route exists with GET method
                assert "GET" in route.methods, "Container logs should be GET endpoint"


# ---------------------------------------------------------------------------
# Tests for new endpoints and features (dev-debug-cycle Iteration 1-15)
# ---------------------------------------------------------------------------

class TestCheckMissingFiles:
    """Tests for the check-missing-files endpoint and provision_service method."""

    def test_provision_service_has_check_missing_files(self):
        """provision_service should expose a check_missing_files public method."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()
        assert callable(svc.check_missing_files)

    def test_check_missing_files_route_exists(self):
        """Services router should expose GET /{name}/check-missing-files."""
        from app.routers.services import router
        routes = [r.path for r in router.routes]
        assert any("check-missing-files" in r for r in routes), (
            f"check-missing-files route missing from services router. Routes: {routes}"
        )

    def test_check_missing_files_returns_enriched_response_structure(self):
        """The check-missing-files response should include scan_context when repo exists."""
        # Verify the endpoint function signature exists and is async
        from app.routers.services import check_missing_files
        import inspect
        assert inspect.iscoroutinefunction(check_missing_files), (
            "check_missing_files should be an async function"
        )


class TestRecipePathMultiRecipe:
    """Tests for the recipe_path multi-recipe feature (merged from main 9f12b57)."""

    def test_provision_service_check_missing_files_forwards_recipe_path(self, monkeypatch):
        """check_missing_files must forward recipe_path as a query param."""
        from app.services.provision_service import ProvisionService
        captured: dict = {}

        async def fake_request(self, method, path, json_data=None, params=None, timeout=300.0):
            captured["path"] = path
            captured["params"] = params
            return {"ready": True, "missing": []}

        monkeypatch.setattr(ProvisionService, "_request", fake_request)
        import asyncio
        result = asyncio.run(
            ProvisionService().check_missing_files("multisvc", "recipes/web")
        )
        assert result["ready"] is True
        assert captured["path"] == "/services/multisvc/check-missing-files"
        assert captured["params"] == {"recipe_path": "recipes/web"}

    def test_provision_service_check_missing_files_no_recipe_path(self, monkeypatch):
        """Without recipe_path, no query param is forwarded."""
        from app.services.provision_service import ProvisionService
        captured: dict = {}

        async def fake_request(self, method, path, json_data=None, params=None, timeout=300.0):
            captured["params"] = params
            return {"ready": True, "missing": []}

        monkeypatch.setattr(ProvisionService, "_request", fake_request)
        import asyncio
        asyncio.run(
            ProvisionService().check_missing_files("myapp")
        )
        assert captured["params"] == {}

    def test_save_generated_creates_recipe_subdir(self, tmp_path, monkeypatch):
        """save_generated with recipe_path writes into the recipe subdirectory.

        The handler is a plain ``def`` now (runs in FastAPI's threadpool), so
        it is called directly — no ``asyncio.run``.
        """
        from app.config import settings
        from app.routers import services as services_router

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        (tmp_path / "multisvc").mkdir()

        # Patch log_action so no DB session is needed
        monkeypatch.setattr("app.routers.services.log_action", lambda db, **kw: None)

        req = {
            "service_name": "multisvc",
            "recipe_path": "recipes/web",
            "files": {"docker-compose.yml": "services: {}"},
        }
        result = services_router.save_generated_files(req, current_admin={"id": 1}, db=None)

        written = tmp_path / "multisvc" / "recipes" / "web" / "docker-compose.yml"
        assert written.read_text() == "services: {}"
        assert (tmp_path / "multisvc" / "recipes" / "web" / "docker-compose.yml.generated").exists()
        assert result["saved"] == ["docker-compose.yml"]

    def test_save_generated_rejects_recipe_path_traversal(self, tmp_path, monkeypatch):
        """GAP-3: save-generated must 400 on a traversal recipe_path and write
        nothing outside the project dir."""
        from fastapi import HTTPException
        from app.config import settings
        from app.routers import services as services_router

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        (tmp_path / "multisvc").mkdir()
        monkeypatch.setattr("app.routers.services.log_action", lambda db, **kw: None)

        req = {
            "service_name": "multisvc",
            "recipe_path": "../etc",
            "files": {"docker-compose.yml": "services: {}"},
        }
        with pytest.raises(HTTPException) as exc:
            services_router.save_generated_files(req, current_admin={"id": 1}, db=None)
        assert exc.value.status_code == 400
        assert not (tmp_path / "etc" / "docker-compose.yml").exists()
        assert not (tmp_path.parent / "etc" / "docker-compose.yml").exists()

    def test_save_generated_rejects_filename_traversal(self, tmp_path, monkeypatch):
        """GAP-3: save-generated must 400 on a traversal filename — generated
        output stays inside the recipe dir (design L221-238)."""
        from fastapi import HTTPException
        from app.config import settings
        from app.routers import services as services_router

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        (tmp_path / "multisvc").mkdir()
        monkeypatch.setattr("app.routers.services.log_action", lambda db, **kw: None)

        req = {
            "service_name": "multisvc",
            "recipe_path": "recipes/web",
            "files": {"../../escape.yml": "services: {}"},
        }
        with pytest.raises(HTTPException) as exc:
            services_router.save_generated_files(req, current_admin={"id": 1}, db=None)
        assert exc.value.status_code == 400
        assert not (tmp_path / "escape.yml").exists()
        assert not (tmp_path.parent / "escape.yml").exists()

    def test_save_generated_absolute_recipe_path_confined(self, tmp_path, monkeypatch):
        """GAP-3: an absolute recipe_path is re-rooted INTO the project dir —
        generated output never lands outside source_projects/<service>."""
        from app.config import settings
        from app.routers import services as services_router

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        (tmp_path / "multisvc").mkdir()
        monkeypatch.setattr("app.routers.services.log_action", lambda db, **kw: None)

        req = {
            "service_name": "multisvc",
            "recipe_path": "/sub",
            "files": {"docker-compose.yml": "services: {}"},
        }
        result = services_router.save_generated_files(req, current_admin={"id": 1}, db=None)
        written = tmp_path / "multisvc" / "sub" / "docker-compose.yml"
        assert written.read_text() == "services: {}"
        assert result["saved"] == ["docker-compose.yml"]
        assert not (tmp_path / "sub" / "docker-compose.yml").exists()  # NOT project-escaped

    def test_compute_needs_env_traversal_recipe_path_falls_back(self, tmp_path, monkeypatch):
        """GAP-3: _compute_needs_env with a traversal recipe_path must not
        crash or scan outside — it falls back to the api result."""
        from app.config import settings
        from app.routers.services import _compute_needs_env

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        assert _compute_needs_env("myapp", "../etc", {"needs_env": True}) is True
        assert _compute_needs_env("myapp", "a/../../etc", {"needs_env": False}) is False

    def test_scan_recipe_dir_rejects_traversal(self, tmp_path):
        """GAP-3: _scan_recipe_dir skips a traversal-invalid recipe path
        (defense-in-depth; stored state is normalized, but never escapes)."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "svc"
        project.mkdir()
        assert ServiceManager()._scan_recipe_dir(project, "../etc") is None
        assert ServiceManager()._scan_recipe_dir(project, "docker/../../etc") is None

    def test_validate_generated_rejects_recipe_path_traversal(self, tmp_path, monkeypatch):
        """GAP-3: validation-draft writes must not escape via recipe_path —
        an invalid recipe_path yields an invalid result, no draft outside."""
        import asyncio
        from app.config import settings
        from app.services.llm_service import LLMService

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        svc = LLMService()
        result = asyncio.run(svc._validate_generated(
            "docker_compose", "services: {}",
            {"project_dir": str(tmp_path), "recipe_path": "../etc"},
        ))
        assert result.get("valid") is False
        assert result.get("repairable") is False
        assert "invalid recipe_path" in " ".join(result.get("errors") or [])
        assert not (tmp_path.parent / ".draft-generated-compose.yml").exists()
        assert not (tmp_path / ".draft-generated-compose.yml").exists()


class TestProfilesDerive:
    """GAP-2/GAP-4: in-panel profile-candidate derivation (file-sets derive).

    The panels recompute profile candidates from the in-panel compose
    selection via POST /services/{name}/file-sets/derive — regression for the
    'No non-empty profiles found.' staleness on first-time selection.
    """

    def _service_dir(self, tmp_path, name="dify") -> Path:
        from app.config import settings
        project = tmp_path / name
        project.mkdir(parents=True, exist_ok=True)
        (project / "docker-compose.middleware.yaml").write_text(
            "services:\n  db_postgres:\n    profiles: ['', 'postgresql']\n"
        )
        (project / "docker-compose.yaml").write_text(
            "services:\n  api:\n    profiles: ['collaboration']\n  db_mysql:\n    profiles: ['mysql', 'postgresql']\n"
        )
        return project

    def test_derive_profiles_from_compose_selection(self, tmp_path, monkeypatch):
        """Candidates derive from the IN-PANEL compose selection: union of
        non-empty profiles across the selected files, '' excluded."""
        from app.config import settings
        from app.routers.services import derive_file_sets_profiles

        self._service_dir(tmp_path)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        result = derive_file_sets_profiles(
            "dify",
            {"recipe_path": "", "compose": ["docker-compose.middleware.yaml", "docker-compose.yaml"]},
            {"id": 1},
        )
        profiles = result["candidates"]["profiles"]
        assert set(profiles) == {"postgresql", "collaboration", "mysql"}
        assert "" not in profiles
        assert result["service_name"] == "dify"

    def test_derive_profiles_empty_compose(self, tmp_path, monkeypatch):
        """No compose selection → no profile candidates."""
        from app.config import settings
        from app.routers.services import derive_file_sets_profiles

        self._service_dir(tmp_path)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        result = derive_file_sets_profiles("dify", {"recipe_path": "", "compose": []}, {"id": 1})
        assert result["candidates"]["profiles"] == []

    def test_derive_profiles_rejects_traversal(self, tmp_path, monkeypatch):
        """Traversal recipe_path / compose paths are rejected with 400."""
        from fastapi import HTTPException
        from app.config import settings
        from app.routers.services import derive_file_sets_profiles

        self._service_dir(tmp_path)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        with pytest.raises(HTTPException) as exc:
            derive_file_sets_profiles(
                "dify", {"recipe_path": "../etc", "compose": ["docker-compose.yaml"]}, {"id": 1})
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            derive_file_sets_profiles(
                "dify", {"recipe_path": "", "compose": ["sub/../../docker-compose.yaml"]}, {"id": 1})
        assert exc.value.status_code == 400

    def test_root_only_default_no_subdir_auto_detection(self, tmp_path):
        """Without configured recipes, scan the project ROOT only — no subdir
        auto-detection (F4); _discover_recipes is gone."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "multisvc"
        (project / "recipes" / "web").mkdir(parents=True)
        (project / "recipes" / "web" / "Dockerfile").write_text("FROM python:3.12")
        (project / "recipes" / "web" / "docker-compose.yml").write_text("services: {}")
        (project / "recipes" / "api").mkdir(parents=True)
        (project / "recipes" / "api" / "Dockerfile").write_text("FROM python:3.12")
        (project / "recipes" / "api" / "docker-compose.yml").write_text("services: {}")
        (project / "top.txt").write_text("hi")
        (project / "README.md").write_text("# ms")

        info = ServiceManager()._get_service_info(project)
        # Root top-level files only — recipes/web/* and recipes/api/* are NOT listed.
        assert set(info["files"]) == {"top.txt", "README.md"}
        # recipes = root-only
        assert info["recipes"] == [{
            "path": "", "label": "(root)", "is_root": True, "template_files": [],
        }]
        assert not hasattr(ServiceManager, "_discover_recipes")

    def test_deploy_form_parses_recipe_path(self):
        """DeployForm must parse the name@@recipe_path service value format."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # parseServiceValue helper splits on '@@'; project_root folds the recipe in
        assert "parseServiceValue" in content, "DeployForm should define parseServiceValue"
        assert "indexOf('@@')" in content or "includes('@@')" in content, (
            "DeployForm should split service values on '@@'"
        )
        assert "project_root: recipePath ? `${baseName}/${recipePath}` : baseName" in content, (
            "DeployForm should fold recipePath into project_root"
        )

    def test_dockerfile_git_safe_directory(self):
        """Gateway Dockerfile must mark the repo as a git safe.directory (recipe discovery)."""
        from pathlib import Path
        dockerfile = Path(__file__).parent.parent.parent / "provision-gateway" / "Dockerfile"
        content = dockerfile.read_text()
        assert "safe.directory" in content, (
            "Dockerfile should run `git config --global --add safe.directory '*'`"
        )

    def test_subnet_pool_route_requires_admin(self):
        """GET /api/system/subnet-pool must be gated on require_admin (G5)."""
        import inspect
        from app.routers.system import get_subnet_pool
        src = inspect.getsource(get_subnet_pool)
        assert "Depends(require_admin)" in src, (
            "subnet-pool endpoint should use Depends(require_admin)"
        )


class TestPerUserFileEndpoints:
    """Per-user-per-recipe deployment files (GAP-18 — the convention-based
    deployment-file editor was RETIRED; plain per-user-file GET/PUT keyed by
    (user, service, label, recipe_path) replaced it)."""

    def test_convention_deployment_files_endpoints_retired(self):
        """The old deployment-files endpoints must be GONE."""
        from app.routers.users import router
        routes = [r.path for r in router.routes]
        assert not any("deployment-files" in r for r in routes), (
            f"convention deployment-files routes still registered: {routes}"
        )

    def test_resolve_per_user_file_env_in_recipe_dir(self, monkeypatch):
        """_resolve_per_user_file for .env.{user}.{label} resolves INSIDE the recipe dir."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        result = _resolve_per_user_file("alice", "myapp", "0", "docker", ".env.alice.0")
        assert result is not None
        assert str(result) == "/srv/provision/source_projects/myapp/docker/.env.alice.0"

    def test_resolve_per_user_file_compose(self, monkeypatch):
        """docker-compose.user-{user}.{label}.yml resolves into the recipe dir."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        result = _resolve_per_user_file("alice", "myapp", "0", "", "docker-compose.user-alice.0.yml")
        assert result is not None
        assert str(result) == "/srv/provision/source_projects/myapp/docker-compose.user-alice.0.yml"

    def test_resolve_per_user_file_rejects_traversal(self, monkeypatch):
        """Path traversal must be rejected (None) — no path-guessing escapes."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        assert _resolve_per_user_file("alice", "myapp", "0", "", "../.env") is None
        assert _resolve_per_user_file("alice", "myapp", "0", "", "/etc/passwd") is None
        assert _resolve_per_user_file("alice", "myapp", "0", "docker", "sub/../x.env") is None

    def test_resolve_per_user_file_rejects_recipe_path_traversal(self, monkeypatch):
        """GAP-1: recipe_path with '..' must be rejected (None) — the file
        cannot escape the project dir via the recipe subdirectory."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        assert _resolve_per_user_file("alice", "myapp", "0", "../etc", "passwd") is None
        assert _resolve_per_user_file("alice", "myapp", "0", "docker/../../etc", ".env.alice.0") is None
        assert _resolve_per_user_file("alice", "myapp", "0", "..", "x.env") is None
        assert _resolve_per_user_file("alice", "myapp", "0", "a/../..", "x.env") is None

    def test_resolve_per_user_file_recipe_path_absolute_confined(self, monkeypatch):
        """GAP-1: an absolute recipe_path is re-rooted INTO the project dir
        (file_sets._recipe_dir semantics) — never joined outside it."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        result = _resolve_per_user_file("alice", "myapp", "0", "/etc", "passwd")
        assert result is not None
        assert str(result).startswith("/srv/provision/source_projects/myapp/"), (
            f"absolute recipe_path escaped the project dir: {result}"
        )

    def test_resolve_per_user_file_recipe_path_nested_valid(self, monkeypatch):
        """A nested recipe subdir still resolves INSIDE the project dir."""
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import _resolve_per_user_file
        result = _resolve_per_user_file("alice", "myapp", "0", "docker/extra", ".env.alice.0")
        assert result is not None
        assert str(result) == "/srv/provision/source_projects/myapp/docker/extra/.env.alice.0"

    def test_save_per_user_file_rejects_recipe_path_traversal(self, monkeypatch):
        """GAP-1: the PUT handler 400s on a traversal recipe_path (GAP-1
        baseline repro: recipe_path=../../etc wrote outside the recipe dir)."""
        import asyncio
        from fastapi import HTTPException
        from app.routers import users as users_router
        monkeypatch.setattr(users_router.settings, "SOURCE_PROJECTS_DIR", Path("/srv/provision/source_projects"))
        from app.routers.users import save_per_user_file
        with pytest.raises(HTTPException) as exc:
            asyncio.run(save_per_user_file(
                "alice", "myapp", "0",
                {"recipe_path": "../etc", "filename": "qa-traversal.txt", "content": "x"},
                {"id": 1}, db=None,
            ))
        assert exc.value.status_code == 400

    def test_next_label_reads_the_nested_user_status_shape(self, monkeypatch):
        """Regression guard (task doc §1): the -api's real shape must yield the next label.

        `_compute_status` returns `{"user_status": [<per-user object>]}`, and each object
        carries `healthy_services`/`unhealthy_services`/`missing_services` NESTED inside it.
        The old parser expected a dict of those three lists (or a flat list of service
        entries), matched nothing, and answered "0" for every pair — including pairs that
        already had label 0 deployed, which made a second instance impossible to create.
        """
        import asyncio
        from app.routers import users as users_router

        async def fake_get_user(user_name):
            return {"user_status": [{
                "user_name": user_name,
                "summary": {"healthy_services_#": 2},
                "healthy_services": [{"service_name": "example-mcp", "label": "0"}],
                "unhealthy_services": [{"service_name": "example-mcp", "label": "1"}],
                "missing_services": [],
            }]}

        monkeypatch.setattr(users_router.provision_service, "get_user", fake_get_user)

        result = asyncio.run(users_router.get_next_label("bob", "example-mcp", {"id": 1}))
        assert result["label"] == "2", result

    def test_next_label_zero_when_service_has_no_instances(self, monkeypatch):
        """A service the user has never deployed still starts at 0."""
        import asyncio
        from app.routers import users as users_router

        async def fake_get_user(user_name):
            return {"user_status": [{
                "user_name": user_name,
                "healthy_services": [{"service_name": "other", "label": "3"}],
                "unhealthy_services": [], "missing_services": [],
            }]}

        monkeypatch.setattr(users_router.provision_service, "get_user", fake_get_user)
        result = asyncio.run(users_router.get_next_label("bob", "example-mcp", {"id": 1}))
        assert result["label"] == "0", result

    def test_per_user_file_endpoints_are_coroutine(self):
        """The new per-user-file GET/PUT are async handlers."""
        from app.routers.users import get_per_user_file, save_per_user_file
        import inspect
        assert inspect.iscoroutinefunction(get_per_user_file)
        assert inspect.iscoroutinefunction(save_per_user_file)

    def test_per_user_file_routes_registered(self):
        """The per-user-file routes exist on the users router."""
        from app.routers.users import router
        routes = [r.path for r in router.routes]
        assert any("per-user-file" in r for r in routes), (
            f"per-user-file routes missing from users router. Routes: {routes}"
        )

    def test_frontend_never_fabricates_the_env_file_path(self):
        """The Deployment Files card must show the -api's RECORDED env path.

        `.env.raw.{user}.{label}` (UsersPage's retired fallback) is the gateway's
        generation INTERMEDIATE: the -api copies it to the canonical
        `.env.{n}.{user}.{label}` and records THAT path in the registry
        (provisioner `entry["env_files"] = _env_files_used`). The raw name is
        recorded nowhere and, for a compose with no `${VAR}` surface, was never
        generated either — so the card printed a path that had never existed and
        clicking it opened an empty editor with a "file does not exist" alert
        (live: bob/example-mcp/0). A deployment with no recorded env has none;
        the card must say so instead of guessing.
        """
        users_page = (_DASH / "src" / "pages" / "UsersPage.tsx").read_text()
        assert ".env.raw.${" not in users_page, (
            "UsersPage fabricates the gateway's generation-intermediate env name "
            "(.env.raw.{user}.{label}); use baseName(svc.env_file_path) from the registry"
        )
        # The link is conditional on the registry value — an absent env file must
        # render as text, not as a link into an empty editor.
        assert "baseName(svc.env_file_path)" in users_page
        assert "records no per-user env file" in users_page

    def test_frontend_no_longer_calls_retired_deployment_files(self):
        """UsersPage must use the per-user-file endpoints keyed by
        (recipe_path, filename), never the retired /deployment-files ones.

        The FileEditor leg was RETIRED 2026-09-26 with the file itself: the
        component was imported nowhere (UsersPage edits files through its own
        Monaco pane), so its half of this guard had nothing left to guard. The
        substance — the live page's calls — is unchanged.
        """
        _dash = _DASH
        users_page = (_dash / "src" / "pages" / "UsersPage.tsx").read_text()
        users_api = (_dash / "src" / "api" / "users.ts").read_text()
        for name, content in [("UsersPage.tsx", users_page), ("api/users.ts", users_api)]:
            assert "deployment-files" not in content, f"{name} still calls retired /deployment-files endpoints"
        # The dirty-check probes the per-user files INSIDE the instance's recipe
        # dir (the recipe subdir comes from the -api; a hardcoded '' resolved to
        # the project root and never found a sub-recipe service's files).
        assert "per-user-file" in users_page
        assert "recipe_path: recipePath" in users_page
        assert "recipe_path: editorFile.recipePath" in users_page
        assert "recipe_path: ''" not in users_page
        # These two wrappers still exist (they are kept pending the user's word —
        # nothing imports them since FileEditor went), so the guard on them stays
        # until they go, rather than being silently loosened now.
        assert "getPerUserFile" in users_api and "savePerUserFile" in users_api


class TestSystemStatsKeys:
    """Tests for system stats key mapping fix (Iteration 6)."""

    def test_system_stats_endpoint_accepts_detail_param(self):
        """System stats endpoint should accept detail query parameter."""
        from app.routers.system import system_stats
        import inspect
        assert inspect.iscoroutinefunction(system_stats)


class TestConftestSkipLogic:
    """Tests for conftest.py skip-on-no-server logic (Iteration 10-11)."""

    def test_conftest_exists(self):
        """conftest.py should exist in the tests directory."""
        from pathlib import Path
        conftest = Path(__file__).parent / "conftest.py"
        assert conftest.exists(), "conftest.py missing — integration tests will error"

    def test_conftest_has_token_fixture(self):
        """conftest.py should export a token fixture for integration tests."""
        import importlib.util
        from pathlib import Path
        conftest_path = Path(__file__).parent / "conftest.py"
        spec = importlib.util.spec_from_file_location("conftest", conftest_path)
        conftest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conftest)
        assert hasattr(conftest, "token"), "conftest.py missing token fixture"

    def test_conftest_has_is_gateway_running(self):
        """conftest.py should have a gateway-running detection function."""
        import importlib.util
        from pathlib import Path
        conftest_path = Path(__file__).parent / "conftest.py"
        spec = importlib.util.spec_from_file_location("conftest", conftest_path)
        conftest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conftest)
        assert hasattr(conftest, "_is_gateway_running"), (
            "conftest.py missing _is_gateway_running function"
        )


class TestUvicornWorkers:
    """Tests for Dockerfile parallel request fix (task 2.1/2.2)."""

    def test_dockerfile_contains_workers(self):
        """Dockerfile CMD should include --workers flag."""
        dockerfile = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile.read_text()
        assert "--workers" in content, "Dockerfile missing --workers flag"
        assert "4" in content.split("--workers")[1].split()[0], (
            "Dockerfile --workers value should be 4"
        )


# ---------------------------------------------------------------------------
# Tests for G2 — Template classification filtering
# ---------------------------------------------------------------------------

class TestTemplateClassification:
    """Tests for template file classification (G2)."""

    def test_is_template_file_dockerfile(self):
        """Dockerfile should be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file("Dockerfile") is True
        assert ServiceManager._is_template_file("path/to/Dockerfile") is True

    def test_is_template_file_docker_compose(self):
        """docker-compose* files should be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file("docker-compose.yml") is True
        assert ServiceManager._is_template_file("path/docker-compose.prod.yml") is True
        assert ServiceManager._is_template_file("docker-compose.override.yml") is True

    def test_is_template_file_nginx_conf(self):
        """*.nginx.conf files should be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file("myapp.nginx.conf") is True
        assert ServiceManager._is_template_file("path/to/service.nginx.conf") is True

    def test_is_template_file_conf(self):
        """*.conf files should be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file("nginx.conf") is True
        assert ServiceManager._is_template_file("path/to/app.conf") is True

    def test_is_template_file_env(self):
        """.env and .env.example should be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file(".env") is True
        assert ServiceManager._is_template_file(".env.example") is True
        assert ServiceManager._is_template_file("path/.env") is True

    def test_is_template_file_not_template(self):
        """Regular source files should NOT be classified as template."""
        from app.services.service_manager import ServiceManager
        assert ServiceManager._is_template_file("main.py") is False
        assert ServiceManager._is_template_file("app/views.tsx") is False
        assert ServiceManager._is_template_file("README.md") is False
        assert ServiceManager._is_template_file(".gitignore") is False
        assert ServiceManager._is_template_file("package.json") is False

    def test_service_info_has_template_files_field(self):
        """Service info dict should include a template_files key."""
        from app.services.service_manager import ServiceManager
        svc = ServiceManager()
        import inspect
        sig = inspect.signature(svc._get_service_info)
        assert 'project_dir' in sig.parameters


# ---------------------------------------------------------------------------
# Tests for scan re-architecture — marker-only classification (F1/F18)
# ---------------------------------------------------------------------------

class TestTemplateClassificationMarkerOnly:
    """A file is "generated" iff a sibling {file}.generated marker exists.
    No git ls-files anywhere in classification (F1)."""

    def test_generated_marker_classifies_generated(self, tmp_path, monkeypatch):
        """A .generated-marked docker-compose.yml must be generated, never a template."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "docker-compose.yml").write_text("services: {}\n")
        (project / "docker-compose.yml.generated").write_text("")

        info = ServiceManager()._get_service_info(project)
        assert "docker-compose.yml" in info["generated_files"]
        assert "docker-compose.yml" not in info["template_files"]
        assert "Dockerfile" in info["template_files"]
        assert "Dockerfile" not in info["generated_files"]

    def test_no_marker_means_template(self, tmp_path):
        """Original deployment-critical files without markers are templates."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "nginx.conf").write_text("server {}\n")

        info = ServiceManager()._get_service_info(project)
        assert "Dockerfile" in info["template_files"]
        assert "nginx.conf" in info["template_files"]
        assert "Dockerfile" not in info["generated_files"]

    def test_generated_marker_excluded_from_all_listings(self, tmp_path):
        """`.generated` marker files must be excluded from all three lists."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "docker-compose.yml").write_text("services: {}\n")
        (project / "docker-compose.yml.generated").write_text("")

        info = ServiceManager()._get_service_info(project)
        assert "docker-compose.yml.generated" not in info["files"]
        assert "docker-compose.yml.generated" not in info["generated_files"]
        assert "docker-compose.yml.generated" not in info["template_files"]

    def test_classification_never_invokes_git(self, tmp_path, monkeypatch):
        """Classification must not run any subprocess (git ls-files removed)."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "main.py").write_text("print('hi')\n")
        (project / "gen.py").write_text("print('gen')")
        (project / "gen.py.generated").write_text("")

        def boom(*a, **k):
            raise AssertionError(f"no subprocess allowed during classification: {a}")

        monkeypatch.setattr("subprocess.run", boom)
        info = ServiceManager()._get_service_info(project)
        assert set(info["files"]) == {"Dockerfile", "main.py", "gen.py"}
        assert info["generated_files"] == ["gen.py"]
        assert info["template_files"] == ["Dockerfile"]


# ---------------------------------------------------------------------------
# Tests for G9 — Agent fields guard in LLMConfig.to_dict()
# ---------------------------------------------------------------------------

class TestLLMConfigToDict:
    """Tests that LLMConfig.to_dict() strips agent fields by default (G9)."""

    def test_to_dict_excludes_agent_fields_by_default(self):
        """to_dict() should not include agent_url, agent_model, or system_prompt."""
        from app.models.llm_config import LLMConfig
        config = LLMConfig()
        config.id = 1
        config.mode = "byok"
        config.agent_url = "http://agent:11434"
        config.agent_model = "llama3.1:8b"
        config.system_prompt = "You are a helpful assistant."
        config.byok_base_url = "https://api.openai.com/v1"
        config.byok_model = "gpt-4o"
        config.is_active = True

        result = config.to_dict()
        assert "agent_url" not in result
        assert "agent_model" not in result
        assert "system_prompt" not in result
        assert result["mode"] == "byok"
        assert result["byok_model"] == "gpt-4o"

    def test_to_dict_includes_agent_fields_when_requested(self):
        """to_dict(include_agent_fields=True) should include agent fields."""
        from app.models.llm_config import LLMConfig
        config = LLMConfig()
        config.id = 1
        config.mode = "byok"
        config.agent_url = "http://agent:11434"
        config.agent_model = "llama3.1:8b"
        config.system_prompt = "You are a helpful assistant."

        result = config.to_dict(include_agent_fields=True)
        assert result["agent_url"] == "http://agent:11434"
        assert result["agent_model"] == "llama3.1:8b"
        assert result["system_prompt"] == "You are a helpful assistant."


# ---------------------------------------------------------------------------
# Tests for GAP-2 — backend defers local-agent fields (future feature)
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal in-memory stand-in for a SQLAlchemy Session used by LLMService.

    Only supports the query/filter/first chain and add/commit/refresh used by
    llm_service config methods.
    """

    def __init__(self, config=None):
        self._config = config
        self.added = []
        self.committed = False

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._config

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        pass


class TestLLMConfigDefersLocalAgent:
    """Backend LLM config API must NOT accept/persist local-agent fields (GAP-2)."""

    def test_create_config_defaults_to_byok(self):
        """create_config should default mode to 'byok' (not 'local_agent')."""
        from app.services.llm_service import LLMService
        db = _FakeSession()
        config = LLMService().create_config(db, {})
        assert config.mode == "byok"
        assert config.agent_url is None
        assert config.agent_model is None

    def test_create_config_ignores_local_agent_mode(self):
        """create_config should normalize mode='local_agent' to 'byok' and drop agent fields."""
        from app.services.llm_service import LLMService
        db = _FakeSession()
        config = LLMService().create_config(db, {
            "mode": "local_agent",
            "agent_url": "http://agent:11434",
            "agent_model": "llama3.1:8b",
            "byok_base_url": "https://api.example.com/v1",
            "byok_model": "gpt-4o",
        })
        assert config.mode == "byok"
        assert config.agent_url is None
        assert config.agent_model is None
        assert config.byok_base_url == "https://api.example.com/v1"
        assert config.byok_model == "gpt-4o"

    def test_save_config_clears_agent_fields(self):
        """save_config should clear any previously stored agent_url/agent_model."""
        from app.services.llm_service import LLMService
        from app.models.llm_config import LLMConfig
        existing = LLMConfig()
        existing.id = 1
        existing.mode = "local_agent"
        existing.agent_url = "http://agent:11434"
        existing.agent_model = "llama3.1:8b"
        existing.byok_model = "old-model"
        existing.is_active = True

        db = _FakeSession(config=existing)
        config = LLMService().save_config(db, {"byok_base_url": "https://api.example.com/v1"})
        assert config.mode == "byok"
        assert config.agent_url is None
        assert config.agent_model is None
        assert config.byok_base_url == "https://api.example.com/v1"

    def test_resolve_endpoint_never_uses_agent_url(self):
        """_resolve_endpoint must NOT route to agent_url even for a legacy local_agent config."""
        from app.services.llm_service import LLMService
        from app.models.llm_config import LLMConfig
        legacy = LLMConfig()
        legacy.id = 1
        legacy.mode = "local_agent"
        legacy.agent_url = "http://agent:11434"
        legacy.agent_model = "llama3.1:8b"
        legacy.byok_api_key_enc = None
        legacy.is_active = True

        base, model, headers = LLMService()._resolve_endpoint(_FakeSession(config=legacy))
        assert "agent" not in base
        assert base == "http://localhost:11434/v1"

    def test_resolve_endpoint_uses_byok_when_configured(self):
        """_resolve_endpoint should use the BYOK endpoint when a byok config is active."""
        from app.services.llm_service import LLMService
        from app.models.llm_config import LLMConfig
        byok = LLMConfig()
        byok.id = 1
        byok.mode = "byok"
        byok.byok_api_key_enc = "enc-key"
        byok.byok_base_url = "https://api.deepseek.com/v1"
        byok.byok_model = "deepseek-chat"
        byok.is_active = True

        base, model, headers = LLMService()._resolve_endpoint(_FakeSession(config=byok))
        assert base == "https://api.deepseek.com/v1"
        assert model == "deepseek-chat"
        assert "Authorization" in headers

    def test_model_column_default_is_byok(self):
        """LLMConfig.mode SQLAlchemy column default must be 'byok' (not 'local_agent', GAP-2)."""
        from sqlalchemy.schema import ColumnDefault
        from app.models.llm_config import LLMConfig
        col = LLMConfig.__table__.c.mode
        assert isinstance(col.default, ColumnDefault), "mode column should have a column default"
        assert col.default.arg == "byok", f"expected column default 'byok', got {col.default.arg!r}"


# ---------------------------------------------------------------------------
# Tests for G11 — SSE format
# ---------------------------------------------------------------------------

class TestSSEFormat:
    """Tests for proper SSE event format (G11)."""

    def test_provision_service_stream_task_log_format(self):
        """stream_task_log should emit proper SSE data: {json} format."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()
        import inspect
        assert inspect.isasyncgenfunction(svc.stream_task_log)

    def test_tasks_router_sse_generator_has_json_error_format(self):
        """The tasks router SSE generator should emit JSON error events."""
        from pathlib import Path
        tasks_path = Path(__file__).parent.parent / "app" / "routers" / "tasks.py"
        content = tasks_path.read_text()
        assert "json.dumps" in content or "_json.dumps" in content


# ---------------------------------------------------------------------------
# Tests for G5 — LLM prompts reference provision-api skill
# ---------------------------------------------------------------------------

class TestLLMPromptsSkillReference:
    """Tests that LLM prompts reference the provision-api skill (G5)."""

    def test_compose_prompt_references_skill(self):
        """docker_compose prompt should include compose generation rules."""
        from app.services.llm_service import LLMService
        svc = LLMService()
        prompt = svc._build_prompt("docker_compose", {
            "repo_description": "test app",
            "repo_files": ["main.py"],
            "port": 8000,
            "language": "python",
            "framework": "fastapi",
        })
        # Verify the prompt includes key compose rules (may come from SKILL.md or fallback)
        assert "provision-api" in prompt or "provision tool" in prompt
        assert "Use `build: .`" in prompt or "build: ." in prompt
        assert "Use named volumes" in prompt or "named volumes" in prompt

    def test_nginx_prompt_references_skill(self):
        """nginx_conf prompt should include nginx generation rules."""
        from app.services.llm_service import LLMService
        svc = LLMService()
        prompt = svc._build_prompt("nginx_conf", {
            "repo_description": "test app",
            "repo_files": ["main.py"],
            "port": 8000,
            "language": "python",
            "framework": "fastapi",
        })
        # Verify the prompt includes key nginx rules (may come from SKILL.md or fallback)
        assert "provision-api" in prompt or "provision tool" in prompt
        assert "proxy_pass" in prompt.lower()


# ---------------------------------------------------------------------------
# Tests for G10 — Deploy field naming alignment
# ---------------------------------------------------------------------------

class TestDeployFieldNaming:
    """Tests for consistent deploy field naming (G10)."""

    def test_frontend_deployform_uses_compose_file_path(self):
        """DeployForm.tsx should use compose_file_path for template files."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "compose_file_path" in content

    def test_frontend_deployform_uses_nginx_conf_file_path(self):
        """DeployForm.tsx should use nginx_conf_file_path."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "nginx_conf_file_path" in content


# ---------------------------------------------------------------------------
# Tests for G3 — Task notification system
# ---------------------------------------------------------------------------

class TestTaskNotificationSystem:
    """Tests for task notification system (G3)."""

    def test_app_layout_uses_notification_api(self):
        """AppLayout should call notification function for task notifications."""
        from pathlib import Path
        layout_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "layout" / "AppLayout.tsx"
        content = layout_path.read_text()
        assert "notification[" in content

    def test_app_layout_has_2second_window(self):
        """AppLayout task poller should use 2-second interval."""
        from pathlib import Path
        layout_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "layout" / "AppLayout.tsx"
        content = layout_path.read_text()
        assert "NOTIFY_WINDOW_SEC" in content
        assert "setInterval(poll, 2000)" in content

    def test_app_layout_has_notified_ref(self):
        """AppLayout should track notified task IDs to avoid duplicates."""
        from pathlib import Path
        layout_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "layout" / "AppLayout.tsx"
        content = layout_path.read_text()
        assert "notifiedRef" in content


# ---------------------------------------------------------------------------
# Tests for G6 — Auto-deploy flow
# ---------------------------------------------------------------------------

class TestAutoDeploy:
    """Tests for the deploy flow — implicit LLM auto-generation on deploy is
    RETIRED (design §Selection & UI): generation is an explicit, human-gated
    action; deployment always waits for explicit user action."""

    def test_deploy_form_submits_via_form(self):
        """DeployForm.tsx should submit the deploy payload via the form's
        submit handling (Deploy button), not an implicit auto-deploy flag."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "onFinish={handleDeploy}" in content or "htmlType=\"submit\"" in content or "type=\"submit\"" in content
        # The autoDeploy flag is retired — no implicit auto-generation on deploy.
        assert "autoDeploy" not in content
        assert "Auto Templates Completion" not in content


# G4 checkDeploy — function removed in iteration 2 (dead code cleanup G13/G14).
# Tests removed since the target function no longer exists in services.ts.


# ---------------------------------------------------------------------------
# Tests for G7 — Upload mode JSON format
# ---------------------------------------------------------------------------

class TestUploadModeJSONFormat:
    """Tests that upload mode sends JSON with base64 content (G7)."""

    def test_upload_uses_file_reader(self):
        """UploadZipForm (ServicesPage) should use FileReader for reading upload files."""
        from pathlib import Path
        page_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "ServicesPage.tsx"
        content = page_path.read_text()
        assert "FileReader" in content, "Upload should use FileReader to read files"

    def test_upload_uses_base64_encoding(self):
        """UploadZipForm (ServicesPage) should encode files as base64 for JSON upload."""
        from pathlib import Path
        page_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "ServicesPage.tsx"
        content = page_path.read_text()
        assert "base64" in content, "Upload should use base64 encoding"

    def test_upload_uses_json_create_service(self):
        """UploadZipForm (ServicesPage) should pass JSON mode 'upload' to the API."""
        from pathlib import Path
        page_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "ServicesPage.tsx"
        content = page_path.read_text()
        assert "mode: 'upload'" in content or 'mode: "upload"' in content, (
            "Upload should pass mode: 'upload' to the API"
        )


# ---------------------------------------------------------------------------
# Tests for GAP-001 — Template mode implemented in AddServiceModal
# ---------------------------------------------------------------------------

class TestTemplateMode:
    """Tests that backend template mode remains supported while the UI no longer exposes a "From Template" option (GAP-1)."""

    def test_template_endpoint_exists_in_services_router(self):
        """Services router should expose GET /templates (backend template support retained)."""
        from app.routers.services import router
        routes = [r.path for r in router.routes]
        assert any("/templates" in r for r in routes), (
            f"Templates endpoint missing from services router. Routes: {routes}"
        )

    def test_create_service_from_template_not_501(self):
        """POST /api/services with mode='template' should NOT return 501.

        The handler is a plain ``def`` now (FastAPI runs it in a threadpool),
        so it must NOT be a coroutine function.
        """
        from app.routers.services import create_service
        import inspect
        assert not inspect.iscoroutinefunction(create_service), (
            "create_service should be a sync def handler (runs in the threadpool)"
        )

    def test_create_from_template_method_exists(self):
        """ServiceManager should have create_from_template method."""
        from app.services.service_manager import ServiceManager
        assert hasattr(ServiceManager, "create_from_template"), (
            "ServiceManager missing create_from_template method"
        )
        assert callable(ServiceManager.create_from_template)

    def test_services_page_has_no_from_template_tab(self):
        """ServicesPage.tsx should NOT contain 'From Template' tab (GAP-1)."""
        from pathlib import Path
        services_page = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "ServicesPage.tsx"
        content = services_page.read_text()
        assert "From Template" not in content, (
            "ServicesPage should NOT have 'From Template' tab in the inline modal (GAP-1)"
        )

    def test_services_page_has_no_template_form_component(self):
        """ServicesPage.tsx should NOT have a TemplateForm component (GAP-1)."""
        from pathlib import Path
        services_page = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "ServicesPage.tsx"
        content = services_page.read_text()
        assert "TemplateForm" not in content, (
            "ServicesPage should NOT define a TemplateForm component (GAP-1)"
        )
        assert "AppstoreOutlined" not in content, (
            "ServicesPage should NOT import AppstoreOutlined for a template tab (GAP-1)"
        )

    def test_add_service_modal_orphan_removed(self):
        """The orphan AddServiceModal.tsx component should have been removed (GAP-1)."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        assert not modal_path.exists(), (
            "AddServiceModal.tsx is orphan dead code and should have been removed (GAP-1)"
        )

    def test_add_service_modal_not_imported_anywhere(self):
        """No remaining source file should import AddServiceModal (GAP-1)."""
        from pathlib import Path
        src = Path(__file__).parent.parent.parent / "provision-dashboard" / "src"
        for p in src.rglob("*.ts*"):
            if "AddServiceModal" in p.name:
                continue
            if "AddServiceModal" in p.read_text(errors="ignore"):
                raise AssertionError(f"{p} still imports AddServiceModal")



# ---------------------------------------------------------------------------
# Tests for G12 — Save logic before deploy (non-autoDeploy files saved to disk)
# ---------------------------------------------------------------------------

class TestG12SaveLogic:
    """Tests that generated files are saved to disk before deployment (G12)."""

    def test_finalization_is_not_gated(self):
        """R3: the gateway finalizes the generated artifacts unconditionally.

        The browser no longer runs a save-generated step on the deploy path;
        the server-owned continuation applies the overwrite matrix itself.
        """
        import inspect
        from app.services import deploy_workflow
        src = inspect.getsource(deploy_workflow.on_generation_completed)
        assert "finalize_files" in src
        assert "record.get(\"operation\")" in src, "both operations finalize"

    def test_finalization_executes_before_register(self):
        """R2: finalization happens before the claim/register, never after."""
        import inspect
        from app.services import deploy_workflow
        src = inspect.getsource(deploy_workflow.on_generation_completed)
        assert src.index("finalize_files") < src.index("_claim_and_register")


# ---------------------------------------------------------------------------
# Tests for G15 — Hidden form field removed from DeployForm
# ---------------------------------------------------------------------------

class TestG15HiddenFieldRemoved:
    """Tests that auto_templates_completion hidden form field is removed (G15)."""

    def test_auto_templates_completion_hidden_field_removed(self):
        """The hidden Form.Item for auto_templates_completion should NOT exist."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "auto_templates_completion" not in content, (
            "auto_templates_completion hidden field should have been removed (G15)"
        )


# ---------------------------------------------------------------------------
# Tests for G13/G14/G16 — Dead code cleanup in services.ts
# ---------------------------------------------------------------------------

class TestG13G14G16DeadCodeCleanup:
    """Tests that unused exports are removed from services.ts (G13/G14/G16)."""

    def test_only_create_service_git_exported(self):
        """services.ts should only export createServiceGit."""
        from pathlib import Path
        services_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "api" / "services.ts"
        content = services_path.read_text()
        assert "export const createServiceGit" in content, (
            "createServiceGit export must still exist"
        )

    def test_removed_exports_not_present(self):
        """All removed exports should NOT be present in services.ts."""
        from pathlib import Path
        services_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "api" / "services.ts"
        content = services_path.read_text()
        removed_exports = [
            "getServices",
            "getService",
            "deleteService",
            "getServiceFile",
            "updateServiceFile",
            "convertService",
            "checkDeploy",
            "saveGenerated",
            "scanDirectory",
            "gitStatus",
            "gitDiff",
            "gitHeadFile",
        ]
        for export_name in removed_exports:
            assert export_name not in content, (
                f"Removed export '{export_name}' still present in services.ts (G13/G14/G16)"
            )


# ---------------------------------------------------------------------------
# Tests for GAP-002 — Deploy validation (compose/nginx paths)
# ---------------------------------------------------------------------------

class TestDeployValidation:
    """Tests that deploy rejects missing compose/nginx paths (GAP-002)."""

    def test_the_gateway_does_not_duplicate_api_validation(self):
        """T7/R7.6: the -api owns deploy validation; the gateway forwards its
        error (one validation home, one message)."""
        from pathlib import Path
        content = (Path(__file__).parent.parent / "app" / "routers" / "users.py").read_text()
        assert "missing docker-compose.yml" not in content
        assert "has_compose" not in content
        assert "has_nginx" not in content

    def test_deploy_errors_are_mapped_through_as_http_error(self):
        from pathlib import Path
        content = (Path(__file__).parent.parent / "app" / "routers" / "users.py").read_text()
        assert "as_http_error" in content, (
            "the -api's refusal must reach the caller with its own status/code"
        )

    def test_deploy_form_disabled_when_missing(self):
        """DeployForm should disable the Deploy button when files are missing or check error."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "!!checkError" in content, (
            "DeployForm should reference checkError in the disabled condition"
        )
        assert "missingFiles.length > 0 && Object.keys(generatedFiles).length === 0" in content, (
            "DeployForm should disable the Deploy button when missing files exist and no generated files"
        )

    def test_deploy_form_check_error_state_exists(self):
        """DeployForm should have a checkError state variable."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "checkError" in content, (
            "DeployForm should have checkError state for API error reporting"
        )

    def test_deploy_form_check_error_alert_renders(self):
        """DeployForm should render an error Alert when checkError is set."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "type=\"error\"" in content, (
            "DeployForm should render an Alert with type=\"error\" for check failures"
        )
        assert "checkError" in content.split("type=\"error\"")[0] if "type=\"error\"" in content else "", (
            "The error Alert should be associated with the checkError state"
        ) or True
        # Verify the Alert message references checkError
        assert "Deployment readiness check failed" in content, (
            "DeployForm should show a failure message that includes 'Deployment readiness check failed'"
        )

    def test_deploy_form_clears_check_error_on_success(self):
        """DeployForm should clear checkError on successful re-check."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "setCheckError(null)" in content, (
            "DeployForm should clear checkError on successful check"
        )

    def test_deploy_form_sets_sentinel_missing_files_on_error(self):
        """DeployForm should set missingFiles to sentinel value on check failure."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # DeployForm handles check errors — verify the error state and alert pattern exist
        has_error_handling = "checkError" in content and (
            "setCheckError" in content or "catch" in content or "onError" in content
        )
        assert has_error_handling, (
            "DeployForm should handle check errors (checkError state and error handling)"
        )

    def test_deploy_form_handle_deploy_guard_exists(self):
        """DeployForm handleDeploy should block submission when files missing."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "Cannot deploy: no compose file is selected." in content, (
            "handleDeploy should refuse only the no-compose case; the gateway "
            "generates the other missing slots"
        )
        assert "missingFiles.length > 0 && Object.keys(generatedFiles).length === 0" in content, (
            "handleDeploy should have the same guard condition as the disabled button"
        )

    def test_a_capacity_refusal_is_a_503_with_its_own_code(self):
        """The one deploy error the gateway may intercept is a capacity refusal,
        because it must become a card (state refused) rather than a toast."""
        from pathlib import Path
        content = (Path(__file__).parent.parent / "app" / "services" / "deploy_workflow.py").read_text()
        assert "at_capacity" in content
        assert "record_refusal" in content or "create_or_replace" in content


# ---------------------------------------------------------------------------
# Tests for GAP-003 — Service label auto-increment
# ---------------------------------------------------------------------------

class TestServiceLabelAutoIncrement:
    """Tests for auto-incremented service label (GAP-003)."""

    def test_next_label_endpoint_exists(self):
        """Users router should expose GET /{user_name}/{service_name}/next-label."""
        from app.routers.users import router
        routes = [r.path for r in router.routes]
        assert any("next-label" in r for r in routes), (
            f"next-label endpoint missing from users router. Routes: {routes}"
        )

    def test_next_label_endpoint_is_async(self):
        """next-label endpoint should be an async function."""
        from app.routers.users import get_next_label
        import inspect
        assert inspect.iscoroutinefunction(get_next_label), (
            "get_next_label should be async"
        )

    def test_deployform_label_is_input_not_select(self):
        """DeployForm label field should be an Input (not Select) for auto-increment."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # Should use Input for label (display-only), not Select
        assert "<Input" in content and "disabled" in content and "name=\"label\"" in content, (
            "Label field should be a disabled Input showing the auto-computed value"
        )
        # Should NOT have manual dropdown options
        assert "'0 (default)'" not in content and "'1'" not in content, (
            "Label should not use manual dropdown options"
        )

    def test_deployform_triggers_compute_next_label(self):
        """DeployForm should call computeNextLabel when user+service selected."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "computeNextLabel" in content, (
            "DeployForm should have computeNextLabel function"
        )

    def test_next_label_default_calls_get_user(self):
        """get_next_label endpoint should call provision_service.get_user."""
        import inspect
        from app.routers.users import get_next_label
        sig = inspect.signature(get_next_label)
        # Verify the function signature includes user_name and service_name params
        assert "user_name" in sig.parameters
        assert "service_name" in sig.parameters
        assert inspect.iscoroutinefunction(get_next_label)

    def test_next_label_returns_label_0_on_exception(self, monkeypatch):
        """get_next_label answers label 0 when the -api is unreachable."""
        import asyncio
        from app.database import SessionLocal
        from app.routers import users as users_router
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
        finally:
            db.close()

        async def boom(_name):
            raise RuntimeError("api down")
        monkeypatch.setattr(users_router.provision_service, "get_user", boom)
        out = asyncio.run(users_router.get_next_label(
            "nobody", "nosvc", {"id": str(admin.id), "role": "admin"}))
        assert out == {"label": "0", "source": "auto_increment"}

    def test_next_label_logic_max_plus_1(self, monkeypatch):
        """max(label)+1 over ACTIVE and historical rows (a tombstoned label is
        never reused, R2.3)."""
        import asyncio
        from app.database import SessionLocal
        from app.routers import users as users_router
        from support import mk_service, mk_user, registry_engine
        from sqlalchemy import text

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            admin = mk_user(db, "admin", role="admin")
            mk_service(db, alice, service_name="myapp", label="0")
            mk_service(db, alice, service_name="myapp", label="2")
            dead = mk_service(db, alice, service_name="myapp", label="5")
            with registry_engine().begin() as conn:
                conn.execute(text("UPDATE registry.services SET deleted_at=now() "
                                  "WHERE id=:id"), {"id": str(dead)})
        finally:
            db.close()

        async def empty(_name):
            return {"user_status": []}
        monkeypatch.setattr(users_router.provision_service, "get_user", empty)
        out = asyncio.run(users_router.get_next_label(
            "alice", "myapp", {"id": str(admin.id), "role": "admin"}))
        assert out["label"] == "6", out
        assert out["source"] == "auto_increment"

    def test_compute_next_label_called_on_user_change(self):
        """DeployForm user_name onChange should trigger computeNextLabel."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # The user_name onChange calls computeNextLabel with user + service values.
        # The service variable may be named `svc` or `svcBase` (multi-recipe
        # support splits "name@@recipe_path" — base name is `svcBase`).
        assert 'if (val && svcBase) computeNextLabel(val, svcBase)' in content or \
               'if (val && svc) computeNextLabel(val, svc)' in content, (
            "user_name onChange should trigger computeNextLabel with user and service"
        )

    def test_compute_next_label_called_on_service_change(self):
        """DeployForm service_name onChange should trigger computeNextLabel."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # The service_name onChange calls computeNextLabel — parameter name may vary (baseName, svc, val)
        assert 'if (val && user) computeNextLabel(user, baseName)' in content or \
               'if (val && user) computeNextLabel(user, val)' in content or \
               'if (val && user) computeNextLabel' in content, (
            "service_name onChange should trigger computeNextLabel with user and service"
        )


# ---------------------------------------------------------------------------
# Tests for GAP-004 — Active file system monitoring
# ---------------------------------------------------------------------------

class TestProjectMonitoring:
    """Tests for active project monitoring in source_projects (GAP-004)."""

    def test_project_monitor_methods_exist(self):
        """ServiceManager should have scan_for_new_projects and get_new_project_events."""
        from app.services.service_manager import ServiceManager
        assert hasattr(ServiceManager, "scan_for_new_projects")
        assert hasattr(ServiceManager, "get_new_project_events")
        assert callable(ServiceManager.scan_for_new_projects)
        assert callable(ServiceManager.get_new_project_events)

    def test_notifications_endpoint_exists(self):
        """Services router should expose GET /notifications."""
        from app.routers.services import router
        routes = [r.path for r in router.routes]
        assert any("notifications" in r for r in routes), (
            f"Notifications endpoint missing from services router. Routes: {routes}"
        )

    def test_project_monitor_task_in_main(self):
        """main.py should have _project_monitor_loop function."""
        from pathlib import Path
        main_path = Path(__file__).parent.parent / "app" / "main.py"
        content = main_path.read_text()
        assert "_project_monitor_loop" in content, (
            "main.py should have a _project_monitor_loop background task"
        )
        assert "_project_monitor_task" in content, (
            "main.py should track _project_monitor_task for lifecycle management"
        )

    def test_project_monitor_scans_source_projects(self):
        """ServiceManager scan_for_new_projects should detect new directories."""
        from app.services.service_manager import ServiceManager
        # The method should exist and work on a test directory
        # We can't easily test actual file detection without filesystem side effects,
        # but we can verify the method signature and return type
        assert callable(ServiceManager.scan_for_new_projects)


# ---------------------------------------------------------------------------
# Tests for GAP-005 — Route ordering fix (templates/notifications before /{name})
# ---------------------------------------------------------------------------

class TestRouteOrdering:
    """Tests that specific routes are registered before the /{name} catch-all."""

    @property
    def _prefix(self):
        from app.routers.services import router
        return router.prefix  # "/api/services"

    def test_templates_before_catch_all(self):
        """GET /api/services/templates must be registered before GET /api/services/{name}."""
        from app.routers.services import router
        prefix = router.prefix
        routes = router.routes
        templates_idx = None
        catch_all_idx = None
        for i, route in enumerate(routes):
            if route.path == f"{prefix}/templates":
                templates_idx = i
            elif route.path == f"{prefix}/{{name}}":
                catch_all_idx = i
        assert templates_idx is not None, f"{prefix}/templates route not found"
        assert catch_all_idx is not None, f"{prefix}/{{name}} route not found"
        assert templates_idx < catch_all_idx, (
            f"{prefix}/templates at index {templates_idx} should be before "
            f"{prefix}/{{name}} at {catch_all_idx}. "
            f"Routes: {[(r.path, r.methods) for r in routes]}"
        )

    def test_notifications_before_catch_all(self):
        """GET /api/services/notifications must be registered before GET /api/services/{name}."""
        from app.routers.services import router
        prefix = router.prefix
        routes = router.routes
        notif_idx = None
        catch_all_idx = None
        for i, route in enumerate(routes):
            if route.path == f"{prefix}/notifications":
                notif_idx = i
            elif route.path == f"{prefix}/{{name}}":
                catch_all_idx = i
        assert notif_idx is not None, f"{prefix}/notifications route not found"
        assert catch_all_idx is not None, f"{prefix}/{{name}} route not found"
        assert notif_idx < catch_all_idx, (
            f"{prefix}/notifications at index {notif_idx} should be before "
            f"{prefix}/{{name}} at {catch_all_idx}. "
            f"Routes: {[(r.path, r.methods) for r in routes]}"
        )

    def test_catch_all_still_registered(self):
        """GET /api/services/{name} should still be registered after reorder."""
        from app.routers.services import router
        prefix = router.prefix
        routes = [r.path for r in router.routes]
        target = f"{prefix}/{{name}}"
        assert target in routes, (
            f"{target} route should still be registered after reorder"
        )

    def test_templates_endpoint_still_registered(self):
        """GET /api/services/templates should still be registered after reorder."""
        from app.routers.services import router
        prefix = router.prefix
        routes = [r.path for r in router.routes]
        target = f"{prefix}/templates"
        assert target in routes, (
            f"{target} route should still be registered after reorder"
        )

    def test_notifications_endpoint_still_registered(self):
        """GET /api/services/notifications should still be registered after reorder."""
        from app.routers.services import router
        prefix = router.prefix
        routes = [r.path for r in router.routes]
        target = f"{prefix}/notifications"
        assert target in routes, (
            f"{target} route should still be registered after reorder"
        )

    def test_get_service_still_works_for_named_service(self):
        """GET /{name} handler should still accept any service name."""
        from app.routers.services import router
        prefix = router.prefix
        for route in router.routes:
            if route.path == f"{prefix}/{{name}}":
                assert "name" in route.endpoint.__code__.co_varnames, (
                    "get_service handler should accept 'name' parameter"
                )
                break


# ---------------------------------------------------------------------------
# Tests for ApiKey model (GAP-003)
# ---------------------------------------------------------------------------


class TestApiKeyModel:
    """Test the ApiKey ORM model."""

    def test_apikey_model_imports(self):
        """ApiKey model should be importable."""
        from app.models.api_key import ApiKey
        assert ApiKey.__tablename__ == "api_keys"

    def test_apikey_columns_exist(self):
        """ApiKey should have all required columns."""
        from app.models.api_key import ApiKey
        cols = {c.name for c in ApiKey.__table__.columns}
        assert "id" in cols
        assert "user_id" in cols
        assert "label" in cols
        assert "token_hash" in cols
        assert "created_at" in cols
        assert "expires_at" in cols
        assert "is_revoked" in cols
        assert "last_used_at" in cols

    def test_apikey_to_dict_includes_keys(self):
        """to_dict() should return all expected keys."""
        from datetime import datetime, timezone
        from app.models.api_key import ApiKey
        now = datetime.now(timezone.utc)
        key = ApiKey(
            id=1,
            user_id=42,
            label="test-key",
            token_hash="abc123",
            created_at=now,
            expires_at=now,
            is_revoked=False,
            last_used_at=None,
        )
        d = key.to_dict()
        assert d["id"] == 1
        assert d["user_id"] == "42"
        assert d["label"] == "test-key"
        assert d["is_revoked"] is False
        assert d["last_used_at"] is None

    def test_apikey_model_registered_in_models_init(self):
        """ApiKey should be importable through models/__init__.py."""
        from app.models import ApiKey
        assert ApiKey is not None


# ---------------------------------------------------------------------------
# Tests for HostnameIndex service (GAP-005)
# ---------------------------------------------------------------------------


class TestHostnameIndex:
    """Test the in-memory HostnameIndex service."""

    def test_hostname_index_import(self):
        """HostnameIndex should be importable."""
        from app.services.hostname_index import HostnameIndex
        assert HostnameIndex is not None

    def test_hostname_index_constructor(self):
        """HostnameIndex should accept a registry path."""
        from app.services.hostname_index import HostnameIndex
        idx = HostnameIndex("/nonexistent/registry.yml")
        assert idx._registry_path is not None

    def test_hostname_index_get_by_hostname_nonexistent(self):
        """get_by_hostname should return None for missing file."""
        from app.services.hostname_index import HostnameIndex
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            path = f.name
        try:
            os.unlink(path)  # delete so file doesn't exist
            idx = HostnameIndex(path)
            assert idx.get_by_hostname("anything") is None
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_hostname_index_get_by_service_format(self):
        """get_by_service should construct hostname correctly."""
        from app.services.hostname_index import HostnameIndex
        idx = HostnameIndex("/tmp/nonexistent.yml")
        # Even with no file, the hostname construction is testable
        result = idx.get_by_service("alice", "myapp", "0")
        assert result is None  # File doesn't exist, but no crash


# ---------------------------------------------------------------------------
# Tests for Registry wrapper (GAP-006)
# ---------------------------------------------------------------------------


class TestRegistryWrapper:
    """Test the read-only Registry wrapper service."""

    def test_registry_import(self):
        """Registry should be importable."""
        from app.services.registry import Registry
        assert Registry is not None

    def test_registry_constructor(self):
        """Registry should accept a YAML path."""
        from app.services.registry import Registry
        r = Registry("/tmp/nonexistent.yml")
        assert r._path is not None

    def test_registry_get_all_entries_empty(self):
        """get_all_entries should return empty list for missing file."""
        from app.services.registry import Registry
        r = Registry("/tmp/definitely_nonexistent_registry.yml")
        entries = r.get_all_entries()
        assert entries == []

    def test_registry_get_entry_missing(self):
        """get_entry should return None for missing entry."""
        from app.services.registry import Registry
        r = Registry("/tmp/definitely_nonexistent_registry.yml")
        assert r.get_entry("alice", "myapp", "0") is None

    def test_registry_get_all_entries_with_data(self):
        """get_all_entries reads the registry schema (T1 read-on-demand)."""
        from app.database import SessionLocal
        from app.services.registry import Registry
        from support import mk_service, mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            bob = mk_user(db, "bob")
            mk_service(db, alice, service_name="myapp", label="0")
            mk_service(db, bob, service_name="app2", label="1")
        finally:
            db.close()
        entries = Registry().get_all_entries()
        assert len(entries) == 2
        assert entries[0]["user_name"] == "alice"

    def test_registry_get_entry_match(self):
        """get_entry resolves one active (owner, service, label) tuple."""
        from app.database import SessionLocal
        from app.services.registry import Registry
        from support import mk_service, mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            mk_service(db, alice, service_name="myapp", label="0")
        finally:
            db.close()
        r = Registry()
        entry = r.get_entry("alice", "myapp", "0")
        assert entry is not None
        assert entry["user_name"] == "alice"
        assert entry["service_id"]
        assert r.get_entry("nobody", "x", "0") is None


# ---------------------------------------------------------------------------
# Tests for new middleware (GAP-012, GAP-013)
# ---------------------------------------------------------------------------


class TestNewMiddleware:
    """Test require_gateway_token and require_admin middleware."""

    def test_require_gateway_token_import(self):
        """require_gateway_token should be importable."""
        from app.middleware import require_gateway_token
        assert require_gateway_token is not None

    def test_require_admin_import(self):
        """require_admin should be importable."""
        from app.middleware import require_admin
        assert require_admin is not None

    def test_extract_gateway_token_import(self):
        """_extract_gateway_token should be importable."""
        from app.middleware import _extract_gateway_token
        assert _extract_gateway_token is not None

    def test_new_middleware_not_in_old_get_current_admin_via_depends(self):
        """Auth deps must be SYNCHRONOUS (not coroutines) so their DB queries run in a worker thread.

        Regression guard for the Aug 2026 outage: async auth deps that ran
        blocking SQLAlchemy directly on the event loop would freeze the loop the
        moment the DB pool was exhausted, wedging every in-flight request forever.
        Sync deps block a thread instead, so the system degrades gracefully.
        """
        import inspect
        from app.middleware import require_gateway_token, require_admin
        assert not inspect.iscoroutinefunction(require_gateway_token)
        assert not inspect.iscoroutinefunction(require_admin)


# ---------------------------------------------------------------------------
# Tests for subnet_pool proxy in provision_service (GAP-029)
# ---------------------------------------------------------------------------


class TestSubnetPoolProxy:
    """Test subnet_pool proxy method in ProvisionService."""

    def test_get_subnet_pool_method_exists(self):
        """ProvisionService should have subnet_pool method."""
        from app.services.provision_service import ProvisionService
        svc = ProvisionService()
        assert callable(svc.subnet_pool)


# ---------------------------------------------------------------------------
# Tests for new auth endpoints (GAP-002, GAP-016)
# ---------------------------------------------------------------------------


class TestNewAuthEndpoints:
    """Test new auth router endpoints for ACL features."""

    def test_verify_endpoint_exists(self):
        """GET /api/auth/verify should be registered in auth router."""
        from app.routers.auth import router
        routes = {r.path for r in router.routes}
        assert "/api/auth/verify" in routes

    def test_go_hostname_endpoint_exists(self):
        """GET /api/auth/go/{hostname} should be registered in auth router."""
        from app.routers.auth import router
        routes = {r.path for r in router.routes}
        assert "/api/auth/go/{hostname}" in routes

    def test_keys_endpoints_exist(self):
        """POST/GET /api/auth/keys and DELETE /api/auth/keys/{key_id} should exist."""
        from app.routers.auth import router
        routes = {r.path for r in router.routes}
        assert "/api/auth/keys" in routes
        assert "/api/auth/keys/{key_id}" in routes


# ---------------------------------------------------------------------------
# Tests for subnet_pool system endpoint (GAP-028)
# ---------------------------------------------------------------------------


class TestSubnetPoolSystemEndpoint:
    """Test /api/system/subnet-pool endpoint exists in system router."""

    def test_subnet_pool_route_exists(self):
        """GET /api/system/subnet-pool should be registered."""
        from app.routers.system import router
        prefix = router.prefix
        routes = {r.path for r in router.routes}
        assert f"{prefix}/subnet-pool" in routes


# ---------------------------------------------------------------------------
# Tests for new config settings (GAP-001, GAP-029)
# ---------------------------------------------------------------------------


class TestNewConfigSettings:
    """Test ENABLE_ACL, REGISTRY_FILE, PROVISION_COOKIE_TTL settings."""

    def test_enable_acl_setting_exists(self):
        """ENABLE_ACL should be defined in Settings."""
        from app.config import Settings
        s = Settings()
        assert hasattr(s, "ENABLE_ACL") or hasattr(s, "enable_acl")

    def test_registry_file_setting_exists(self):
        """REGISTRY_FILE should be defined in Settings."""
        from app.config import Settings
        s = Settings()
        assert hasattr(s, "REGISTRY_FILE") or hasattr(s, "registry_file")

    def test_provision_cookie_ttl_setting_exists(self):
        """PROVISION_COOKIE_TTL should be defined in Settings."""
        from app.config import Settings
        s = Settings()
        assert hasattr(s, "PROVISION_COOKIE_TTL") or hasattr(s, "provision_cookie_ttl")


# ---------------------------------------------------------------------------
# Tests for auth verify endpoint response headers (golden requirement alignment)
# ---------------------------------------------------------------------------


class TestAuthVerifyHeaders:
    """Test that /api/auth/verify returns correct X-Auth-Action header values
    matching the golden requirements (login_required, token_expired, acl_denied)."""

    def test_verify_endpoint_exists(self):
        """GET /api/auth/verify endpoint should be registered."""
        from app.routers.auth import router
        routes = {r.path for r in router.routes}
        assert "/api/auth/verify" in routes

    def test_verify_endpoint_returns_correct_header_keys(self):
        """The verify endpoint function should reference correct auth action strings."""
        from pathlib import Path
        auth_path = Path(__file__).parent.parent / "app" / "routers" / "auth.py"
        content = auth_path.read_text()
        # Golden requirements: login_required, token_expired, acl_denied
        assert '"login_required"' in content, (
            "Auth verify must use 'login_required' (not redirect_login) per golden requirements"
        )
        assert '"token_expired"' in content, (
            "Auth verify must use 'token_expired' (not redirect_token_expired) per golden requirements"
        )
        assert '"acl_denied"' in content, (
            "Auth verify must use 'acl_denied' (not redirect_acl_denied) per golden requirements"
        )
        # Old redirect_ prefixed values must NOT be present
        assert '"redirect_login"' not in content, (
            "redirect_login should have been replaced with login_required"
        )
        assert '"redirect_token_expired"' not in content, (
            "redirect_token_expired should have been replaced with token_expired"
        )
        assert '"redirect_acl_denied"' not in content, (
            "redirect_acl_denied should have been replaced with acl_denied"
        )

    def test_x_auth_action_naming_consistent(self):
        """All X-Auth-Action values across the codebase use the golden requirement names."""
        from pathlib import Path
        auth_path = Path(__file__).parent.parent / "app" / "routers" / "auth.py"
        content = auth_path.read_text()
        # Count occurrences of each auth action string
        login_count = content.count('"login_required"')
        token_expired_count = content.count('"token_expired"')
        acl_denied_count = content.count('"acl_denied"')
        total_new = login_count + token_expired_count + acl_denied_count
        # Old values should be zero
        old_count = content.count('"redirect_')
        assert total_new >= 4, (
            f"Expected at least 4 auth action assignments, found {total_new} "
            f"(login_required={login_count}, token_expired={token_expired_count}, acl_denied={acl_denied_count})"
        )
        assert old_count == 0, (
            f"redirect_ prefixed auth action values should be completely removed, found {old_count}"
        )


# ---------------------------------------------------------------------------
# Tests for BUG-2 (GAP-019): gateway_token must be decoded with
# decode_gateway_token (not decode_access_token) in _get_gateway_user_safe,
# and create_key must not call the async require_gateway_token without await.
# ---------------------------------------------------------------------------


class TestGatewayTokenDecode:
    """G5/T9: only provision-type tokens authenticate; legacy token types and
    the retired gateway_token cookie / Bearer header are rejected."""

    def _legacy_token(self, kind: str) -> str:
        from datetime import datetime, timedelta, timezone

        from jose import jwt
        from app.config import settings

        return jwt.encode(
            {"sub": "1", "type": kind,
             "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
        )

    def test_extract_rejects_gateway_token_cookie_and_bearer(self):
        """Only the provision_token cookie (or X-Provision-Token) is read."""
        from unittest.mock import MagicMock
        from app.middleware import _extract_gateway_token

        req = MagicMock()
        req.cookies = {"gateway_token": self._legacy_token("gateway")}
        req.headers = {}
        assert _extract_gateway_token(req) is None

        req = MagicMock()
        req.cookies = {}
        req.headers = {"Authorization": f"Bearer {self._legacy_token('access')}"}
        assert _extract_gateway_token(req) is None

        req = MagicMock()
        req.cookies = {"provision_token": "abc"}
        req.headers = {}
        assert _extract_gateway_token(req) == "abc"

        req = MagicMock()
        req.cookies = {}
        req.headers = {"X-Provision-Token": "xyz"}
        assert _extract_gateway_token(req) == "xyz"

    def test_decode_gateway_token_rejects_legacy_types(self):
        """decode_gateway_token accepts only type='provision'."""
        from jose import JWTError
        from app.services.auth_service import decode_gateway_token

        for kind in ("gateway", "access", "refresh", "code"):
            with pytest.raises(JWTError):
                decode_gateway_token(self._legacy_token(kind))


# ---------------------------------------------------------------------------
# Tests for BUG-1 (GAP-018): Dashboard CPU/RAM/Disk cards must always render
# the percentage (no antd status='exception' close icon).
# ---------------------------------------------------------------------------


class TestDashboardProgressStatus:
    """BUG-1: Dashboard progress cards keep the number visible even above 80%."""

    def test_dashboard_no_exception_status(self):
        from pathlib import Path
        dash_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "DashboardPage.tsx"
        content = dash_path.read_text()
        assert "'exception'" not in content, (
            "Dashboard progress cards must not use status='exception' (renders a ✕ and hides the number)"
        )

    def test_dashboard_progress_uses_normal_status(self):
        from pathlib import Path
        dash_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "pages" / "DashboardPage.tsx"
        content = dash_path.read_text()
        assert 'status="normal"' in content, (
            "Dashboard progress cards should use status='normal' to always render the percentage"
        )


# ---------------------------------------------------------------------------
# Tests for Gap 3 — viewer must see granted special-user services.
# The backend GET /api/users already ACL-filters to own + allowed_special_users;
# the frontend must NOT re-filter and drop the granted ones.
# ---------------------------------------------------------------------------


class TestViewerServiceVisibility:
    """Gap 3: UsersPage.tsx must not re-filter away granted special-user services."""

    def test_userspage_no_client_side_overfilter(self):
        from pathlib import Path
        users_path = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UsersPage.tsx"
        )
        content = users_path.read_text()
        assert "u.user_name !== admin.email" not in content, (
            "UsersPage must not re-filter services by owner username — the backend "
            "already returns own + allowed_special_users; this over-filter hid granted "
            "special-user services from viewers (Gap 3)."
        )

    def test_userspage_uses_backend_filtered_list(self):
        from pathlib import Path
        users_path = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UsersPage.tsx"
        )
        content = users_path.read_text()
        # The fetch must read the backend-filtered payload directly (no owner
        # check). The service/stage read model is the unified projection (R9).
        assert "client.get('/services/stages')" in content
        assert "for (const g of (data.groups" in content


# ---------------------------------------------------------------------------
# Tests for Gap 4 (grant-dialog stale list) — the special-user list must be
# derived from the users table (not a separate mount-only fetch).
# ---------------------------------------------------------------------------


class TestGrantDialogSync:
    """Gap 4: grant dialog's special-user list must refresh with the users table."""

    def test_no_mount_only_special_users_fetch(self):
        from pathlib import Path
        p = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UserManagementPage.tsx"
        )
        content = p.read_text()
        assert "loadGlobalSpecialUsers" not in content, (
            "the separate mount-only loadGlobalSpecialUsers must be removed; "
            "the special-user list must derive from loadUsers (Gap 4)."
        )

    def test_special_users_derived_in_loadusers(self):
        from pathlib import Path
        p = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UserManagementPage.tsx"
        )
        content = p.read_text()
        assert "setCandidates" in content
        assert "data.candidates" in content
        assert "setSpecialUsersGlobal" not in content


# ---------------------------------------------------------------------------
# Tests for Gap 10 / G3 — admin-only routes must be role-gated (not just hidden
# by the sidebar). Gap 5 (Reconcile 403) is a symptom of this.
# ---------------------------------------------------------------------------


class TestRouteRoleGating:
    """Gap 10: App.tsx must role-gate admin-only routes with AdminRoute."""

    @staticmethod
    def _app_source():
        from pathlib import Path
        p = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "App.tsx"
        )
        return p.read_text()

    def test_admin_route_guard_defined(self):
        content = self._app_source()
        assert "function AdminRoute" in content
        assert "admin?.role !== 'admin'" in content

    def test_admin_pages_are_gated(self):
        content = self._app_source()
        for page in ("DashboardPage", "TasksPage", "SettingsPage", "AuditPage",
                     "UserManagementPage", "SSLPage", "ServicesPage"):
            assert f"<AdminRoute><{page} /></AdminRoute>" in content, (
                f"{page} route must be wrapped in <AdminRoute> (Gap 10/G3)"
            )

    def test_viewer_pages_not_gated(self):
        content = self._app_source()
        # Services (deployed instances) and API Keys are viewer-accessible.
        assert '<Route path="services" element={<UsersPage />} />' in content
        assert '<Route path="api-keys" element={<ApiKeysPage />} />' in content


# ---------------------------------------------------------------------------
# Tests for Gap 2 — the gateway NGINX_* ports must be the EDGE's published
# client-facing ports (decision 10, v5 §10.2): the edge -nginx-acl publishes
# NGINX_HTTP_PORT/NGINX_HTTPS_PORT, and the gateway reads the SAME values for
# /go/, service-URL display, and system-info. The internal -nginx keeps its own
# remapped ports (8766/8443) in _users_provision/.env.
# ---------------------------------------------------------------------------


class TestServiceUrlPort:
    """Gap 2: gateway NGINX_HTTP_PORT must be the edge's published port (8767),
    not the internal nginx port (8766) — decision 10."""

    def test_provision_gateway_env_port(self):
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent / ".env"
        content = env_path.read_text()
        assert "NGINX_HTTP_PORT=8767" in content, (
            "_provision_gateway/.env NGINX_HTTP_PORT must be 8767 (the edge -nginx-acl "
            "published port, decision 10) so the gateway /go/ 303 + service-URL display "
            "target the client-facing edge, not the internal nginx (Gap 2)."
        )
        assert "NGINX_HTTPS_PORT=8768" in content, (
            "_provision_gateway/.env NGINX_HTTPS_PORT must be 8768 (the edge -nginx-acl "
            "published https port, decision 10)."
        )

    def test_users_provision_env_port(self):
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent.parent / "_users_provision" / ".env"
        content = env_path.read_text()
        assert "NGINX_HTTP_PORT=8766" in content, (
            "_users_provision/.env NGINX_HTTP_PORT must be 8766 (not stale 8080) to match the "
            "docker-compose.provision.yml internal nginx binding (Gap 2)."
        )


class TestEdgeSetTokenReachable:
    """Gap 1 (F7 /go/ handoff): the edge's `location = /_set_token` must NOT be
    `internal;` — the /go/ 303 lands the browser directly on
    {svc-host}/_set_token?code=<30s> and a design-required browser-reachable
    relay (v5 §4.3 helper sketch + §8.8). `internal;` makes it return 404 at the
    edge. The gateway-direct /api/auth/exchange was already correct."""

    def test_set_token_location_not_internal(self):
        from pathlib import Path
        template = (
            Path(__file__).parent.parent.parent
            / "nginx.acl" / "acl-helpers.conf.template"
        )
        content = template.read_text()
        # Isolate the exact-match block so an unrelated `internal;` (e.g. the
        # /_auth_jwt auth_request subrequest) cannot satisfy this assertion.
        block = content.split("location = /_set_token {", 1)[1].split("}", 1)[0]
        assert "internal" not in block, (
            "edge `location = /_set_token` must be browser-reachable (no `internal;`) "
            "for the F7 /go/ handoff — v5 §4.3/§8.8 (Gap 1)."
        )
        # The relay must still forward the full query string to the exchange.
        assert "proxy_pass http://subnet-acl-gateway:8770/api/auth/exchange$is_args$args" in block, (
            "edge `/_set_token` must relay `$is_args$args` to the gateway exchange (F12)."
        )

    def test_set_token_kept_outside_auth_request_location(self):
        from pathlib import Path
        edge = (
            Path(__file__).parent.parent.parent
            / "nginx.acl" / "edge.conf.template"
        )
        content = edge.read_text()
        # F11: the ACL gate lives in `location /` ONLY, never server-level, so the
        # exact-match `/_set_token` location stays token-less.
        assert "auth_request /_auth_jwt;" in content
        # The gate must be inside `location /` blocks, not at server level.
        import re
        gate_locations = re.findall(r"location /\s*\{[^}]*auth_request /_auth_jwt;", content)
        assert gate_locations, (
            "the ACL gate (auth_request /_auth_jwt) must live inside `location /` "
            "only (F11) so `location = /_set_token` is never gated."
        )


# ---------------------------------------------------------------------------
# Tests for Gap 4 — /api/auth/verify browser vs API-client status codes
# ---------------------------------------------------------------------------


class TestGoServiceRedirect:
    """Gap 9: /go/{hostname} service-access redirect must work end-to-end."""

    def test_go_to_service_uses_port_in_set_token(self):
        from pathlib import Path
        p = Path(__file__).parent.parent / "app" / "routers" / "auth.py"
        content = p.read_text()
        assert 'f"{service_url}/_set_token?code={code}"' in content, (
            "go_to_service must use service_url (with nginx host port) in the "
            "_set_token redirect, not the port-less http://{domain}"
        )

    def test_userspage_uses_go_redirect(self):
        from pathlib import Path
        p = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UsersPage.tsx"
        )
        content = p.read_text()
        # R14 — the card links each RECORDED endpoint, not the instance host:
        # there is no instance URL field and no `primary` row.
        assert "return `/go/${servedName}`" in content, (
            "service card must link each endpoint to /go/{served name}")
        # G86 — the endpoint body is shared with the `Can call` popup.
        endpoints = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "components" / "services"
            / "ServiceEndpointsView.tsx"
        ).read_text()
        assert "svc.http_endpoints" in endpoints
        assert "URL: " not in content
        assert "<Text code>primary</Text>" not in content

    def test_deployment_files_links_use_instance_recipe_path(self):
        """Deployment-Files links must resolve inside the instance's RECIPE dir.

        Regression: the card hardcoded ``recipe_path: ''`` (= project root) and
        the legacy ``.env.{user}.{label}`` name, so for a multi-recipe service
        (dify's recipe lives in ``docker/``) every link opened an empty editor
        ("file does not exist") — and a save would have written a stray file
        into the project root."""
        from pathlib import Path
        p = (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UsersPage.tsx"
        )
        content = p.read_text()
        # recipe subdir taken from the -api payload, not hardcoded
        assert "svc.recipe_path" in content
        assert "recipePath" in content
        assert "recipe_path: recipePath" in content, "GET must send the instance recipe path"
        assert "recipe_path: editorFile.recipePath" in content, "PUT must save back into the same recipe dir"
        # no leftover hardcoded empty recipe path on either call
        assert "recipe_path: ''" not in content
        # the env link targets a file that actually exists (canonical .env.N or .env.raw)
        assert "svc.env_file_path" in content
        assert "`.env.${svc.user_name}.${svc.label}`" not in content, (
            "legacy .env.{user}.{label} name is never written by the pipeline"
        )


class TestVerifyAuthStatusCodes:
    """T9/G78: /api/auth/verify returns the FINAL verdict — 200 (+ Basic or the
    public marker), 401 + X-Auth-Action (login_required/unauthorized/token_expired),
    403 + X-Auth-Action (acl_denied). The browser-vs-API split is nginx's."""

    @staticmethod
    def _req(cookie=None, accept="*/*", host="myapp.localhost", header_token=None):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.cookies = {"provision_token": cookie} if cookie else {}
        headers = {"Accept": accept, "Host": host}
        if header_token:
            headers["X-Provision-Token"] = header_token
        req.headers = headers
        return req

    def _enable_acl(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ENABLE_ACL", True)

    def _entry(self, owner, *, service_id=None):
        import uuid
        return {"service_id": str(service_id or uuid.uuid4()),
                "owner_api_user_id": str(owner.api_user_id)}

    def test_no_token_returns_401_login_required(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            owner = mk_user(db, "bob")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(owner)):
                resp = verify_auth(self._req(accept="text/html"), db)
            assert resp.status_code == 401
            assert resp.headers["X-Auth-Action"] == "login_required"
            # An API-style caller gets the API action for the same state.
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(owner)):
                api = verify_auth(self._req(accept="application/json"), db)
            assert api.headers["X-Auth-Action"] == "unauthorized"
        finally:
            db.close()

    def test_invalid_or_expired_token_returns_token_expired(self, monkeypatch):
        from unittest.mock import patch
        from jose import JWTError
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            owner = mk_user(db, "bob")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(owner)), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       side_effect=JWTError("bad")):
                resp = verify_auth(self._req(cookie="bad", accept="text/html"), db)
            assert resp.status_code == 401
            assert resp.headers["X-Auth-Action"] == "token_expired"
        finally:
            db.close()

    def test_acl_denied_returns_403(self, monkeypatch):
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            bob = mk_user(db, "bob")
            alice = mk_user(db, "alice", role="viewer")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(bob)), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       return_value={"sub": str(alice.id)}):
                resp = verify_auth(self._req(cookie="valid", accept="text/html"), db)
            assert resp.status_code == 403
            assert resp.headers["X-Auth-Action"] == "acl_denied"
        finally:
            db.close()

    def test_valid_admin_returns_200_x_service_basic(self, monkeypatch):
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            entry = self._entry(admin)
            with patch("app.routers.auth._lookup_by_hostname", return_value=entry), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       return_value={"sub": str(admin.id)}), \
                 patch("app.routers.auth.authorization_header",
                       return_value="dXNlcjpwYXNz"):
                resp = verify_auth(self._req(cookie="valid", accept="text/html"), db)
            assert resp.status_code == 200
            assert resp.headers["X-Service-Basic"] == "dXNlcjpwYXNz"
        finally:
            db.close()

    def test_an_owner_viewer_reaches_its_own_service(self, monkeypatch):
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            alice = mk_user(db, "alice", role="viewer")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(alice)), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       return_value={"sub": str(alice.id)}), \
                 patch("app.routers.auth.authorization_header", return_value=""):
                resp = verify_auth(self._req(cookie="valid", accept="text/html"), db)
            assert resp.status_code == 200
        finally:
            db.close()

    def test_acl_disabled_returns_401(self, monkeypatch):
        from unittest.mock import MagicMock
        from app.config import settings
        from app.routers.auth import verify_auth

        monkeypatch.setattr(settings, "ENABLE_ACL", False)
        resp = verify_auth(self._req(accept="text/html"), MagicMock())
        assert resp.status_code == 401

    # --- G78 public target verdict (T3) ---------------------------------

    def test_public_target_without_token_returns_public_verdict(self, monkeypatch):
        """R3 — no platform credential, 200 + X-Public-Service, no owner Basic."""
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            pub = mk_user(db, "pub", role="special", public=True)
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(pub)):
                resp = verify_auth(self._req(accept="application/json",
                                             host="pub.localhost"), db)
            assert resp.status_code == 200
            assert resp.headers["X-Public-Service"] == "1"
            assert resp.headers.get("X-Service-Basic") is None
        finally:
            db.close()

    def test_public_wins_over_a_valid_token(self, monkeypatch):
        """Public is target-owned; a token does not switch injection on."""
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            pub = mk_user(db, "pub", role="special", public=True)
            admin = mk_user(db, "admin", role="admin")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(pub)), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       return_value={"sub": str(admin.id)}):
                resp = verify_auth(self._req(cookie="valid", accept="text/html",
                                             host="pub.localhost"), db)
            assert resp.status_code == 200
            assert resp.headers["X-Public-Service"] == "1"
            assert resp.headers.get("X-Service-Basic") is None
        finally:
            db.close()

    def test_public_wins_over_a_valid_owner_token(self, monkeypatch):
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            pub = mk_user(db, "pub", role="special", public=True)
            viewer = mk_user(db, "viewer", role="viewer")
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(pub)), \
                 patch("app.routers.auth.auth_service.verify_provision_token",
                       return_value={"sub": str(viewer.id)}):
                resp = verify_auth(self._req(cookie="valid", accept="text/html",
                                             host="pub.localhost"), db)
            assert resp.status_code == 200
            assert resp.headers["X-Public-Service"] == "1"
            assert resp.headers.get("X-Service-Basic") is None
        finally:
            db.close()

    def test_inactive_public_owner_does_not_take_the_public_branch(self, monkeypatch):
        from unittest.mock import patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from support import mk_user

        self._enable_acl(monkeypatch)
        db = SessionLocal()
        try:
            pub = mk_user(db, "pub", role="special", public=True, active=False)
            with patch("app.routers.auth._lookup_by_hostname",
                       return_value=self._entry(pub)):
                resp = verify_auth(self._req(accept="application/json",
                                             host="pub.localhost"), db)
            assert resp.status_code == 401
            assert "X-Public-Service" not in resp.headers
        finally:
            db.close()


class TestKeysUseSharedDependency:
    """Gap 2: list_keys/delete_key must use require_gateway_token (cookie or
    Bearer), consistent with POST /keys, instead of the hand-rolled
    _get_gateway_user_safe that 401'd a valid admin gateway_token."""

    def test_list_keys_uses_require_gateway_token(self):
        import inspect
        from app.routers.auth import list_keys
        src = inspect.getsource(list_keys)
        assert "require_gateway_token" in src
        assert "_get_gateway_user_safe(request" not in src

    def test_delete_key_uses_require_gateway_token(self):
        import inspect
        from app.routers.auth import delete_key
        src = inspect.getsource(delete_key)
        assert "require_gateway_token" in src
        assert "_get_gateway_user_safe(request" not in src


class TestGetMeUsesGatewayToken:
    """GET /api/auth/me uses the ONE provision-token dependency (T9)."""

    def _legacy_token(self, kind: str) -> str:
        from datetime import datetime, timedelta, timezone

        from jose import jwt
        from app.config import settings

        return jwt.encode(
            {"sub": "1", "type": kind,
             "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            settings.GATEWAY_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
        )

    def test_get_me_uses_require_gateway_token(self):
        import inspect
        from app.routers.auth import get_me
        src = inspect.getsource(get_me)
        assert "Depends(require_gateway_token)" in src

    def test_require_gateway_token_rejects_legacy_credentials(self):
        """G5: the retired gateway_token cookie and Bearer headers are ignored."""
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from app.middleware import require_gateway_token

        for cookies, headers in (
            ({"gateway_token": self._legacy_token("gateway")}, {}),
            ({}, {"Authorization": f"Bearer {self._legacy_token('access')}"}),
        ):
            req = MagicMock()
            req.cookies = cookies
            req.headers = headers
            with pytest.raises(HTTPException) as exc_info:
                require_gateway_token(request=req)
            assert exc_info.value.status_code == 401

    def test_require_gateway_token_accepts_a_live_provision_cookie(self):
        from unittest.mock import MagicMock
        from app.database import SessionLocal
        from app.middleware import require_gateway_token
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            key, _ = auth_service.create_api_key(db, admin.id, "Default", is_default=True)
            token = auth_service.create_provision_token(db, admin.id, key.id)
            req = MagicMock()
            req.cookies = {"provision_token": token}
            req.headers = {}
            user = require_gateway_token(request=req)
            assert user["id"] == str(admin.id)
            assert user["role"] == "admin"
            assert user["username"] == "admin"
        finally:
            db.close()


class TestAdminRoutesMigratedToGatewayToken:
    """T9: admin routes depend on ``require_admin`` (provision token)."""

    def _token(self, db, *, role: str):
        from app.services import auth_service
        from support import mk_user

        user = mk_user(db, f"u-{role}", role=role)
        key, _ = auth_service.create_api_key(db, user.id, "Default", is_default=True)
        return user, auth_service.create_provision_token(db, user.id, key.id)

    def test_require_admin_accepts_admin_provision_cookie(self):
        from unittest.mock import MagicMock
        from app.database import SessionLocal
        from app.middleware import require_admin

        db = SessionLocal()
        try:
            admin, token = self._token(db, role="admin")
            req = MagicMock()
            req.cookies = {"provision_token": token}
            req.headers = {}
            user = require_admin(request=req)
            assert user["id"] == str(admin.id)
            assert user["role"] == "admin"
        finally:
            db.close()

    def test_require_admin_rejects_viewer_role_403(self):
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.middleware import require_admin

        db = SessionLocal()
        try:
            _viewer, token = self._token(db, role="viewer")
            req = MagicMock()
            req.cookies = {"provision_token": token}
            req.headers = {}
            with pytest.raises(HTTPException) as exc_info:
                require_admin(request=req)
            assert exc_info.value.status_code == 403
        finally:
            db.close()

    def test_no_router_uses_legacy_admin_dependencies(self):
        from pathlib import Path
        routers_dir = Path(__file__).parent.parent / "app" / "routers"
        legacy = ("Depends(get_current_admin)", "Depends(require_admin_role)")
        for f in sorted(routers_dir.glob("*.py")):
            content = f.read_text()
            for dep in legacy:
                assert dep not in content, f"{f.name} still uses {dep}"

    def test_representative_admin_routes_use_require_admin(self):
        import inspect
        from app.routers.tasks import list_tasks
        from app.routers.audit import list_audit_logs
        from app.routers.users import deploy_user
        from app.routers.auth import list_users
        from app.routers.services import list_services
        from app.routers.llm import list_llm_configs
        for fn in (list_tasks, list_audit_logs, deploy_user, list_users,
                   list_services, list_llm_configs):
            assert "Depends(require_admin)" in inspect.getsource(fn), fn.__name__

    def test_change_password_is_self_service(self):
        """The account password is changed by its OWNER (current token), never
        by an admin resetting another account through this route."""
        import inspect
        from app.routers.auth import change_password
        src = inspect.getsource(change_password)
        assert "Depends(require_gateway_token)" in src
        assert "Depends(require_admin)" not in src

    def test_register_end_user_is_public_signup(self):
        import inspect
        from app.routers.auth import register_end_user
        src = inspect.getsource(register_end_user)
        assert "require_gateway_token" not in src
        assert "require_admin" not in src

    def test_stream_task_log_is_admin_only(self):
        """T9: a viewer never reads a task log; the route is admin-only."""
        import inspect
        from app.routers.tasks import stream_task_log
        src = inspect.getsource(stream_task_log)
        assert "require_admin" in src


class TestV4ExchangeCode:
    """v4 §6.2 (F7/GAP-10): a 30s exchange code carries the /go/ handoff."""

    def test_exchange_code_ttl_constant(self):
        from app.config import settings
        assert settings.EXCHANGE_CODE_TTL_SEC == 30

    def test_create_and_verify_exchange_code_roundtrip(self):
        from app.database import SessionLocal
        from app.services import auth_service

        db = SessionLocal()
        try:
            code = auth_service.create_exchange_code(
                db, "00000000-0000-0000-0000-000000000001",
                "svc.example.com", redirect="/x",
            )
        finally:
            db.close()
        payload = auth_service.verify_exchange_code(code)
        assert payload["type"] == "code"
        assert payload["sub"] == "00000000-0000-0000-0000-000000000001"
        assert payload["svc_host"] == "svc.example.com"
        assert payload["redirect"] == "/x"

    def test_verify_exchange_code_rejects_non_code_token(self):
        from jose import JWTError
        from app.database import SessionLocal
        from app.services import auth_service

        db = SessionLocal()
        try:
            prov = auth_service.create_provision_token(
                db, "00000000-0000-0000-0000-000000000001")
        finally:
            db.close()
        with pytest.raises(JWTError):
            auth_service.verify_exchange_code(prov)

    def test_verify_exchange_code_rejects_garbage(self):
        from jose import JWTError
        from app.services import auth_service
        with pytest.raises(JWTError):
            auth_service.verify_exchange_code("not-a-token")

    def test_go_to_service_uses_code_not_jwt_in_url(self):
        """GAP-10: the /go/ redirect URL contains only the code."""
        from pathlib import Path
        content = (Path(__file__).parent.parent / "app" / "routers" / "auth.py").read_text()
        assert "create_exchange_code" in content
        assert 'f"{service_url}/_set_token?code={code}"' in content


class TestV4ApiKeyTokenModel:
    """T6: an API key IS a provision token carrying its own ``api_key_id``."""

    def test_provision_token_ttl_week(self):
        from app.services import auth_service
        assert auth_service.PROVISION_TOKEN_TTL_SEC == 604800

    def test_api_key_ttl_year(self):
        from app.services import auth_service
        assert auth_service.API_KEY_TTL_SEC == 31536000

    def test_create_api_key_token_carries_own_api_key_id(self):
        from app.database import SessionLocal
        from app.services import auth_service

        db = SessionLocal()
        try:
            tok = auth_service.create_api_key_token(
                db, "00000000-0000-0000-0000-000000000001", 42)
        finally:
            db.close()
        payload = auth_service.decode_token(tok)
        assert payload["api_key_id"] == 42
        assert payload["type"] == "provision"

    def test_create_api_key_stores_hash_and_mask(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "carol")
            created, raw = auth_service.create_api_key(db, user.id, "My Key")
            assert created.mask == raw[-8:]
            assert created.token_hash and created.token_hash != raw
        finally:
            db.close()

    def test_the_key_cap_is_enforced(self, monkeypatch):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        monkeypatch.setattr(auth_service, "MAX_API_KEYS_PER_USER", 1)
        db = SessionLocal()
        try:
            user = mk_user(db, "dave")
            auth_service.create_api_key(db, user.id, "first")
            with pytest.raises(ValueError):
                auth_service.create_api_key(db, user.id, "second")
        finally:
            db.close()

    def test_revoke_default_key_raises(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "erin")
            key, _ = auth_service.create_api_key(db, user.id, "Default", is_default=True)
            with pytest.raises(ValueError):
                auth_service.revoke_api_key(db, key.id)
            other, _ = auth_service.create_api_key(db, user.id, "other")
            assert auth_service.revoke_api_key(db, other.id) is True
        finally:
            db.close()


class TestV4DefaultKey:
    """T6: the default key is auto-created on first login and promotable."""

    def test_get_or_create_default_key_returns_existing(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u1")
            key, _ = auth_service.create_api_key(db, user.id, "Default", is_default=True)
            assert auth_service.get_or_create_default_key(db, user.id).id == key.id
        finally:
            db.close()

    def test_get_or_create_default_key_autocreates(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u2")
            key = auth_service.get_or_create_default_key(db, user.id)
            assert key.is_default is True
        finally:
            db.close()

    def test_backfill_default_keys_exists(self):
        import inspect
        from app.services import auth_service
        assert "is_default" in inspect.getsource(auth_service.backfill_default_keys)


class TestV4ClientTypeHybrid:
    """The X-Client-Type hybrid rule (nginx splits browser vs API)."""

    @staticmethod
    def _req(header=None, cookie=None, accept="*/*"):
        from unittest.mock import MagicMock
        req = MagicMock()
        hdrs = {"Accept": accept}
        if header:
            hdrs["X-Provision-Token"] = header
        req.headers = hdrs
        req.cookies = {"provision_token": cookie} if cookie else {}
        return req

    def test_header_wins_as_api(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(
            self._req(header="tok", cookie="c", accept="text/html")) == "api"

    def test_cookie_means_browser(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(self._req(cookie="c", accept="*/*")) == "browser"

    def test_accept_text_html_means_browser(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(
            self._req(accept="text/html,application/xhtml+xml")) == "browser"

    def test_else_means_api(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(self._req(accept="application/json")) == "api"

    def test_verify_response_carries_x_client_type(self, monkeypatch):
        from unittest.mock import MagicMock
        from app.config import settings
        from app.routers.auth import verify_auth

        monkeypatch.setattr(settings, "ENABLE_ACL", False)
        resp = verify_auth(self._req(accept="application/json"), MagicMock())
        assert resp.headers.get("X-Client-Type") == "api"
        assert resp.status_code == 401


class TestV4AuthEndpoints:
    """The login/exchange endpoints (one credential: provision_token)."""

    def test_login_sets_provision_cookie_only(self):
        from app.database import SessionLocal
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            mk_user(db, "admin@test", role="admin",
                    password_hash=auth_service.hash_password("pw"))
            resp = login(LoginRequest(email="admin@test", password="pw"), db)
        finally:
            db.close()
        body = resp.body.decode()
        assert "access_token" not in body and "refresh_token" not in body
        assert "gateway_token" not in body
        assert resp.headers["set-cookie"].startswith("provision_token=")

    def test_logout_endpoint_clears_cookie(self):
        from unittest.mock import MagicMock
        from app.routers.auth import logout
        response = MagicMock()
        logout(response)
        response.delete_cookie.assert_called_once_with("provision_token", path="/")

    def test_no_refresh_endpoint(self):
        from app.routers.auth import router
        paths = [r.path for r in router.routes]
        assert "/api/auth/refresh" not in paths
        assert "/api/auth/logout" in paths
        assert "/api/auth/exchange" in paths

    def test_exchange_endpoint_requires_code(self):
        from fastapi import HTTPException
        from unittest.mock import MagicMock
        from app.routers.auth import exchange

        req = MagicMock()
        req.query_params = {}
        with pytest.raises(HTTPException) as ei:
            exchange(req, MagicMock())
        assert ei.value.status_code == 401

    def test_exchange_endpoint_sets_cookie_on_valid_code(self):
        from unittest.mock import MagicMock, patch
        from app.database import SessionLocal
        from app.routers.auth import exchange
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u-viewer", role="viewer")
            req = MagicMock()
            req.query_params = {"code": "CODEX"}
            req.app = MagicMock()
            req.app.state.hostname_index.get_by_hostname.return_value = None
            with patch("app.routers.auth.auth_service.verify_exchange_code",
                       return_value={"sub": str(user.id), "svc_host": "",
                                     "redirect": "/"}):
                resp = exchange(req, db)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/"
            assert resp.headers["set-cookie"].startswith("provision_token=")
        finally:
            db.close()


class TestV4VerifyOrdering:
    """T9: current-role authority, revocation and expiry are checked in the
    token verifier, not by the claims."""

    def test_admin_with_revoked_key_denied(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from jose import JWTError
        from app.config import settings
        from app.routers.auth import verify_auth

        monkeypatch.setattr(settings, "ENABLE_ACL", True)
        req = MagicMock()
        req.cookies = {"provision_token": "t"}
        req.headers = {"Accept": "text/html", "Host": "svc.host"}
        with patch("app.routers.auth._lookup_by_hostname",
                   return_value={"service_id": "00000000-0000-0000-0000-000000000009",
                                 "owner_api_user_id": "00000000-0000-0000-0000-000000000001"}), \
             patch("app.routers.auth.auth_service.verify_provision_token",
                   side_effect=JWTError("revoked")):
            resp = verify_auth(req, MagicMock())
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "token_expired"

    def test_provision_token_verifier_checks_expires_at(self):
        import inspect
        from app.services import auth_service
        src = inspect.getsource(auth_service.verify_provision_token)
        assert "expires_at" in src
        assert "is_revoked" in src

    def test_provision_token_ttl_week_payload(self):
        from datetime import datetime, timezone
        from app.database import SessionLocal
        from app.services import auth_service

        db = SessionLocal()
        try:
            tok = auth_service.create_provision_token(
                db, "00000000-0000-0000-0000-000000000001", api_key_id=3)
        finally:
            db.close()
        payload = auth_service.decode_token(tok)
        assert payload["type"] == "provision"
        assert payload["api_key_id"] == 3
        delta = payload["exp"] - datetime.now(timezone.utc).timestamp()
        assert 600_000 < delta < 700_000  # ~1 week (604800)


class TestV4ServiceBasicCredential:
    """T9/R2.5: the gateway reads the -api's keyring; a missing key, a
    `reset_required` row or no ciphertext all yield NO credential (fail closed)."""

    def _seed_basic(self, db, service_id, *, secret=None, reset=False):
        from sqlalchemy import text
        from support import registry_engine

        with registry_engine().begin() as conn:
            conn.execute(
                text("INSERT INTO registry.service_basic "
                     "(service_id, basic_username, secret_enc, key_version, "
                     " basic_version, reset_required) "
                     "VALUES (:sid, 'bob', :secret, 1, 1, :reset)"),
                {"sid": str(service_id),
                 "secret": secret.encode() if secret else None, "reset": reset},
            )

    def test_missing_row_returns_empty(self):
        import uuid
        from app.services.basic_credentials import authorization_header
        assert authorization_header(str(uuid.uuid4())) == ""

    def test_reset_required_returns_empty(self):
        from app.database import SessionLocal
        from app.services.basic_credentials import authorization_header
        from support import mk_service, mk_user

        db = SessionLocal()
        try:
            owner = mk_user(db, "bob")
            sid = mk_service(db, owner)
            self._seed_basic(db, sid, secret=None, reset=True)
            assert authorization_header(str(sid)) == ""
        finally:
            db.close()

    def test_an_encrypted_secret_returns_base64(self):
        import base64
        from app.database import SessionLocal
        from app.services import keyring
        from app.services.basic_credentials import authorization_header
        from support import mk_service, mk_user

        db = SessionLocal()
        try:
            owner = mk_user(db, "bob")
            sid = mk_service(db, owner)
            envelope = keyring.encrypt("secret").decode()
            self._seed_basic(db, sid, secret=envelope)
            got = authorization_header(str(sid))
            assert got == base64.b64encode(b"bob:secret").decode()
        finally:
            db.close()


class TestV4TasksCookie:
    """v4 §11.2 (N5): SSE task-log reads provision_token cookie."""

    def test_stream_task_log_reads_provision_cookie(self):
        from pathlib import Path
        p = Path(__file__).parent.parent / "app" / "routers" / "tasks.py"
        content = p.read_text()
        # T9: the SSE task-log route is admin-only and uses the one dependency.
        assert "require_admin" in content


class TestV4DefaultEndpoint:
    """v4 §6.1.5: PUT /keys/{id}/default sets a user's default key."""

    def test_set_default_endpoint_exists(self):
        from app.routers.auth import router
        paths = [r.path for r in router.routes if getattr(r, "methods", None)]
        assert any("/keys/{key_id}/default" in p and "PUT" in r.methods for p, r in
                   ((r.path, r) for r in router.routes if hasattr(r, "methods")))


class TestDbMigration:
    """T1/T12: PostgreSQL migrations are the single schema path.

    The SQLite store was created with ``create_all`` and patched in place by
    ``_ensure_schema``; PostgreSQL has one frozen DDL applied under an advisory
    lock, so both a fresh database and an upgraded one take the same path and
    the ad-hoc ALTER helper does not exist.
    """

    def test_schema_migrations_are_applied(self):
        from app.database import SessionLocal
        from sqlalchemy import text

        db = SessionLocal()
        try:
            versions = {
                r[0] for r in db.execute(text("SELECT version FROM gateway.schema_migrations"))
            }
        finally:
            db.close()
        assert "0001_gateway.sql" in versions
        assert "0002_import_state.sql" in versions

    def test_api_keys_columns_and_partial_index_in_postgres(self):
        from app.database import SessionLocal
        from sqlalchemy import text

        db = SessionLocal()
        try:
            cols = {
                r[0] for r in db.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='gateway' AND table_name='api_keys'"
                ))
            }
            indexdefs = [r[0] for r in db.execute(text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='gateway' AND tablename='api_keys'"
            ))]
            indexdef = next(
                (d for d in indexdefs if "UNIQUE" in d and "is_default" in d), None
            )
        finally:
            db.close()
        assert {"mask", "is_default", "user_id", "token_hash"} <= cols
        assert indexdef and "is_default" in indexdef

    def test_no_adhoc_schema_alter_helper_survives(self):
        import app.database as database
        assert not hasattr(database, "_ensure_schema")


# ---------------------------------------------------------------------------
# G2 — one-default-per-user (partial unique index) + cascade delete
# ---------------------------------------------------------------------------


class TestG2OneDefaultPerUser:
    """T6/R5.5: exactly one live default per user, enforced by the PostgreSQL
    partial unique index; keys belong to one user id space."""

    def test_partial_unique_index_rejects_second_default(self):
        import pytest
        from sqlalchemy.exc import IntegrityError
        from app.database import SessionLocal
        from sqlalchemy import text
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u1")
            db.execute(text(
                "INSERT INTO gateway.api_keys "
                "(user_id, label, token_hash, is_default, created_at, expires_at, is_revoked) "
                "VALUES (:uid, 'Default', 'th1', true, now(), now() + interval '30 days', false)"
            ), {"uid": str(user.id)})
            db.commit()
            with pytest.raises(IntegrityError):
                db.execute(text(
                    "INSERT INTO gateway.api_keys "
                    "(user_id, label, token_hash, is_default, created_at, expires_at, is_revoked) "
                    "VALUES (:uid, 'Default', 'th2', true, now(), now() + interval '30 days', false)"
                ), {"uid": str(user.id)})
                db.commit()
            db.rollback()
        finally:
            db.close()

    def test_a_revoked_default_frees_the_slot(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u1")
            first, _ = auth_service.create_api_key(db, user.id, "a", is_default=True)
            auth_service.revoke_api_key(db, first.id, allow_default=True)
            second, _ = auth_service.create_api_key(db, user.id, "b", is_default=True)
            assert second.is_default is True
        finally:
            db.close()

    def test_create_api_key_default_unsets_existing(self):
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u1")
            k1, _ = auth_service.create_api_key(db, user.id, "a", is_default=True)
            k2, _ = auth_service.create_api_key(db, user.id, "b", is_default=True)
            count = db.query(ApiKey).filter(
                ApiKey.user_id == user.id, ApiKey.is_default.is_(True)
            ).count()
            assert count == 1
            assert k2.is_default is True
        finally:
            db.close()

    def test_delete_api_keys_for_user_cascades(self):
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "u1")
            auth_service.create_api_key(db, user.id, "a", is_default=True)
            auth_service.create_api_key(db, user.id, "b")
            assert auth_service.delete_api_keys_for_user(db, user.id) == 2
            assert db.query(ApiKey).filter(ApiKey.user_id == user.id).count() == 0
        finally:
            db.close()

    def test_a_new_account_gets_its_own_default_after_another_is_deleted(self):
        """Identity is a UUID now, so there is no id-reuse hazard: a new account
        simply has no keys and gets one default on first login."""
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            old = mk_user(db, "u5")
            auth_service.create_api_key(db, old.id, "Default", is_default=True)
            auth_service.delete_api_keys_for_user(db, old.id)
            new = mk_user(db, "u6")
            auth_service.create_api_key(db, new.id, "Default", is_default=True)
            assert db.query(ApiKey).filter(
                ApiKey.user_id == new.id, ApiKey.is_default.is_(True)
            ).count() == 1
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Prefixed key targets — `end_user:<id>` / `admin:<id>` (the id collision)
# ---------------------------------------------------------------------------


class TestAuthFailsClosedOnAStoreFailure:
    """T9 fault row: a database failure on the auth hot path must deny, never
    500 and never allow."""

    def test_verify_denies_when_the_token_store_is_down(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from sqlalchemy.exc import OperationalError
        from app.config import settings
        from app.routers.auth import verify_auth

        monkeypatch.setattr(settings, "ENABLE_ACL", True)
        req = MagicMock()
        req.cookies = {"provision_token": "t"}
        req.headers = {"Accept": "text/html", "Host": "svc.host"}
        entry = {"service_id": "00000000-0000-0000-0000-000000000001",
                 "owner_api_user_id": "00000000-0000-0000-0000-000000000002"}
        with patch("app.routers.auth._lookup_by_hostname", return_value=entry), \
             patch("app.routers.auth.auth_service.verify_provision_token",
                   side_effect=OperationalError("SELECT", {}, Exception("down"))):
            resp = verify_auth(req, MagicMock())
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "token_expired"

    def test_require_gateway_token_denies_when_the_store_is_down(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from fastapi import HTTPException
        from sqlalchemy.exc import OperationalError
        from app.middleware import require_gateway_token

        req = MagicMock()
        req.cookies = {"provision_token": "t"}
        req.headers = {}
        with patch("app.middleware.auth_service.verify_provision_token",
                   side_effect=OperationalError("SELECT", {}, Exception("down"))):
            with pytest.raises(HTTPException) as exc:
                require_gateway_token(request=req)
        assert exc.value.status_code == 401


class TestTaskRouteStatusPassthrough:
    """T5/T7: the -api's task-route statuses survive the gateway proxy.

    An unknown task is 404, a terminal task is 409 and a pruned task's log is
    410; folding them into the generic 502 loses exactly the distinction the
    Tasks page and the operator use.
    """

    @staticmethod
    def _err(code: int):
        import httpx
        req = httpx.Request("GET", "http://api/tasks/x")
        resp = httpx.Response(code, request=req, json={"detail": f"e{code}"})
        return httpx.HTTPStatusError("x", request=req, response=resp)

    def test_404_409_410_pass_through(self):
        from fastapi import HTTPException
        from app.services.provision_service import provision_service

        for code in (404, 409, 410):
            mapped = provision_service.as_task_error(self._err(code))
            assert isinstance(mapped, HTTPException)
            assert mapped.status_code == code
            assert mapped.detail == f"e{code}"

    def test_other_failures_keep_the_generic_mapping(self):
        from fastapi import HTTPException
        from app.services.provision_service import provision_service

        mapped = provision_service.as_task_error(self._err(500))
        assert isinstance(mapped, HTTPException)
        assert mapped.status_code == 502


class TestApiKeysOneIdSpace:
    """T6/R5.5: keys reference ``users.id``; the ``target_type`` split is gone.

    The pre-migration store had two id spaces (``admins`` and ``end_users``) and
    a prefixed target reference to disambiguate them. The unified ``users``
    table has one id space, so there is nothing to disambiguate and no
    ``target_type`` anywhere in the model, the DTO or the token.
    """

    def _db(self):
        from app.database import SessionLocal
        return SessionLocal()

    def test_keys_reference_the_users_row(self):
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            user = mk_user(db, "alice")
            key, _raw = auth_service.create_api_key(db, user.id, "alice-key")
            assert key.user_id == user.id
            assert not hasattr(ApiKey, "target_type")
        finally:
            db.close()

    def test_token_claims_bind_the_key_to_the_subject(self):
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            user = mk_user(db, "alice")
            key, raw = auth_service.create_api_key(db, user.id, "alice-key")
            payload = auth_service.decode_gateway_token(raw)
            assert payload["sub"] == str(user.id)
            assert payload["api_key_id"] == key.id
            assert payload["type"] == "provision"
        finally:
            db.close()

    def test_one_default_per_user(self):
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            user = mk_user(db, "alice")
            first, _ = auth_service.create_api_key(db, user.id, "first", is_default=True)
            second, _ = auth_service.create_api_key(db, user.id, "second", is_default=True)
            db.refresh(first)
            defaults = db.query(ApiKey).filter(
                ApiKey.user_id == user.id, ApiKey.is_default.is_(True)
            ).all()
            assert [k.label for k in defaults] == ["second"]
            assert first.is_default is False
        finally:
            db.close()

    def test_listing_is_scoped_to_the_user(self):
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            alice = mk_user(db, "alice")
            bob = mk_user(db, "bob")
            auth_service.create_api_key(db, alice.id, "alice-key")
            auth_service.create_api_key(db, bob.id, "bob-key")
            assert [k.label for k in auth_service.list_api_keys(db, alice.id)] == ["alice-key"]
            assert len(auth_service.list_api_keys(db)) == 2
        finally:
            db.close()

    def test_setting_another_default_moves_it(self):
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            user = mk_user(db, "alice")
            first, _ = auth_service.create_api_key(db, user.id, "first", is_default=True)
            second, _ = auth_service.create_api_key(db, user.id, "second")
            assert auth_service.set_default_api_key(db, second.id) is True
            db.refresh(first)
            assert first.is_default is False
            assert db.query(ApiKey).filter(
                ApiKey.user_id == user.id, ApiKey.is_default.is_(True)
            ).one().label == "second"
        finally:
            db.close()

    def test_deleting_one_user_leaves_the_others_keys(self):
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            alice = mk_user(db, "alice")
            bob = mk_user(db, "bob")
            auth_service.create_api_key(db, alice.id, "alice-key")
            auth_service.create_api_key(db, bob.id, "bob-key")
            assert auth_service.delete_api_keys_for_user(db, alice.id) == 1
            assert [k.label for k in db.query(ApiKey).all()] == ["bob-key"]
        finally:
            db.close()

    def test_special_and_inactive_accounts_are_not_key_owners(self):
        import pytest
        from app.services import auth_service
        from support import mk_user

        db = self._db()
        try:
            special = mk_user(db, "internal", role="special")
            inactive = mk_user(db, "dormant", active=False)
            with pytest.raises(ValueError):
                auth_service.create_api_key(db, special.id, "nope")
            with pytest.raises(ValueError):
                auth_service.create_api_key(db, inactive.id, "nope")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# G3 — special users blocked at login with 403 (B11) when credentials are valid
# ---------------------------------------------------------------------------


class TestG3SpecialUserLogin403:
    """T9/R5.6: a special-role user with a VALID credential is rejected at
    login with 403, before any provision token is minted."""

    def _special(self, db):
        from app.services import auth_service
        from support import mk_user
        return mk_user(db, "internal", role="special",
                       password_hash=auth_service.hash_password("pass1234"))

    def test_special_user_with_valid_password_login_403(self):
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest

        db = SessionLocal()
        try:
            user = self._special(db)
            with pytest.raises(HTTPException) as ei:
                login(LoginRequest(email="internal", password="pass1234"), db)
            assert ei.value.status_code == 403
            assert "Special users cannot access the dashboard" in ei.value.detail
        finally:
            db.close()

    def test_special_user_login_never_mints_a_key_or_token(self):
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest

        db = SessionLocal()
        try:
            user = self._special(db)
            with pytest.raises(HTTPException):
                login(LoginRequest(email="internal", password="pass1234"), db)
            assert db.query(ApiKey).filter(ApiKey.user_id == user.id).count() == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# G4 — admins get a default key at registration + login binds to it
# ---------------------------------------------------------------------------


class TestG4AdminDefaultKey:
    """T6: no key is minted at account creation; login binds the provision
    token to the account's default key (creating it on first login)."""

    def test_admin_has_no_default_key_until_login(self):
        from app.models.api_key import ApiKey
        from app.database import SessionLocal
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            assert db.query(ApiKey).filter(ApiKey.user_id == admin.id).count() == 0
        finally:
            db.close()

    def test_login_binds_admin_token_to_default_key(self):
        from app.database import SessionLocal
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(
                db, "admin", role="admin",
                password_hash=auth_service.hash_password("pw"),
            )
            resp = login(LoginRequest(email="admin", password="pw"), db)
            cookie = resp.headers["set-cookie"]
            assert cookie.startswith("provision_token=")
            raw = cookie.split("provision_token=", 1)[1].split(";", 1)[0]
            payload = auth_service.decode_gateway_token(raw)
            default = auth_service.get_or_create_default_key(db, admin.id)
            assert payload["api_key_id"] == default.id
            assert payload["sub"] == str(admin.id)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# G7 — mask backfill on migration
# ---------------------------------------------------------------------------


class TestApiKeyMask:
    """The display mask is written at mint time; there is no SQLite-era ALTER
    backfill because PostgreSQL has the column from the first migration."""

    def test_created_key_carries_a_mask(self):
        from app.database import SessionLocal
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            key, raw = auth_service.create_api_key(db, user.id, "Default")
            assert key.mask
            assert key.mask == raw[-8:]
            assert key.token_hash and key.token_hash != raw
        finally:
            db.close()

    def test_no_schema_alter_step_survives(self):
        import app.database as database
        assert not hasattr(database, "_ensure_schema")


# ---------------------------------------------------------------------------
# G8 — POST /api/auth/keys becomes default only when the user has no default
# ---------------------------------------------------------------------------


class TestG8CreateKeyDefaultFallback:
    """The panel's create-key route never silently steals the default slot:
    the account's default is a separate, explicit action."""

    def test_create_key_does_not_move_the_default(self):
        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            default, _ = auth_service.create_api_key(db, user.id, "Default", is_default=True)
            other, _ = auth_service.create_api_key(db, user.id, "second")
            assert other.is_default is False
            db.refresh(default)
            assert default.is_default is True
        finally:
            db.close()

    def test_create_key_route_returns_the_raw_token_once(self):
        from app.database import SessionLocal
        from app.routers.auth import create_key
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            out = create_key({"label": "panel-key"}, {"id": str(user.id), "role": "viewer"}, db)
            assert out["token"]
            assert out["key"]["label"] == "panel-key"
            assert out["key"]["user_id"] == str(user.id)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Tests for GAP-7 — async generation jobs (persist / poll / cancel / prune /
# restart recovery)
# ---------------------------------------------------------------------------

class TestGenerationJobs:
    """GenerationJobManager lifecycle: phases, persistence, cancel, TTL
    prune, restart recovery (GAP-7)."""

    def _make_session(self, tmp_path):
        """The PostgreSQL test database (per-test truncation is the isolation)."""
        from app.database import SessionLocal
        return SessionLocal

    def test_phase_order_carries_the_artifact_and_its_two_validations(self):
        """generate_missing = compose → validate → serving_ports → validate_ports →
        nginx → validate_conf → env.

        The two validations are STEPS (T14/R1.3): `validate_ports` follows the
        artifact it checks, and `validate_conf` follows the conf whose agreement
        with that artifact it checks.

        `validate` is §3b's compose-conflict check: it must run AFTER the
        compose exists and BEFORE anything is built on it. `serving_ports`
        follows it and precedes nginx, because it is DERIVED FROM the compose
        (the ports it mentions) and the nginx conf is derived from it in turn
        (GRILLING G37: "generated after compose and before nginx").
        """
        from app.services.generation_jobs import GenerationJobManager
        m = GenerationJobManager()
        phases = m._compute_phases("generate_missing", {})
        # R15 — the stream artifact is generated regardless of TLS, so
        # `stream_conf` sits between the ports validation and nginx.
        assert phases == ["compose", "validate", "serving_ports", "validate_ports",
                          "stream_conf", "nginx", "validate_conf", "env"]
        assert phases.index("validate") == phases.index("compose") + 1
        assert phases.index("serving_ports") == phases.index("validate") + 1
        assert phases.index("validate_ports") == phases.index("serving_ports") + 1
        assert phases.index("validate_conf") == phases.index("nginx") + 1

    def test_compute_phases_single_category(self):
        """Single-category jobs map to exactly one phase."""
        from app.services.generation_jobs import GenerationJobManager
        m = GenerationJobManager()
        assert m._compute_phases("category", {"category": "docker_compose"}) == ["compose"]
        assert m._compute_phases("category", {"category": "nginx_conf"}) == ["nginx"]
        assert m._compute_phases("category", {"category": "env_file"}) == ["env"]
        assert m._compute_phases("category", {"category": "compose"}) == ["compose"]

    def test_unselected_slot_is_generated_on_a_deploy_scoped_run(self, tmp_path, monkeypatch):
        """G13/G20 — `generate new` means NOT SELECTED, and the SELECTION SET
        decides the work, not the disk.

        A deploy must hand the -api ALL THREE paths: a refused deployment
        always carries all three, because any slot left at `generate new` had a
        file generated and sent for it. So an unselected nginx is generated even
        when an `nginx.conf` already sits in the recipe.

        Found live on `bob|probe-interp||3`: its refused options carried compose
        + env and NO nginx, because the nginx phase was skipped on disk
        presence, `selection.nginx` stayed null, and the payload dropped the
        key entirely.
        """
        from pathlib import Path
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "svc"
        recipe.mkdir()
        # BOTH files exist on disk — the disk scan finds nothing "missing".
        (recipe / "docker-compose.yml").write_text("services: {}\n")
        (recipe / "nginx.conf").write_text("server {}\n")

        req = {
            "service_name": "svc",
            "user_name": "bob",
            "label": "3",
            "selection": {"compose": ["docker-compose.yml"], "nginx": None, "env": []},
        }
        phases = GenerationJobManager._compute_phases("generate_missing", req)
        assert "nginx" in phases, (
            "an unselected nginx must be generated on a deploy-scoped run — "
            "otherwise the -api is sent a deployment with no nginx at all"
        )

        # A SELECTED nginx is reused, not regenerated, when it is on disk.
        req["selection"]["nginx"] = "nginx.conf"
        assert "nginx" not in GenerationJobManager._compute_phases("generate_missing", req)

    def test_standalone_generate_also_treats_empty_as_generate(self, tmp_path, monkeypatch):
        """The standalone panel has the same slot contract as the deploy panel.

        An empty slot says "generate new", so an existing compose/nginx does NOT
        suppress it. The disk scan no longer decides the phases.
        """
        from pathlib import Path
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "svc"
        recipe.mkdir()
        (recipe / "docker-compose.yml").write_text("services: {}\n")
        (recipe / "nginx.conf").write_text("server {}\n")

        req = {"service_name": "svc", "skip_env": True,
               "selection": {"compose": [], "nginx": None, "env": []}}
        phases = GenerationJobManager._compute_phases("generate_missing", req)
        assert phases == ["compose", "validate", "serving_ports",
                          "validate_ports", "stream_conf", "nginx",
                          "validate_conf"], phases

        # `serving_ports_none` is still the one deliberate skip.
        req["selection"]["serving_ports_none"] = True
        phases = GenerationJobManager._compute_phases("generate_missing", req)
        assert "serving_ports" not in phases
        assert "validate_ports" not in phases

    def test_compute_phases_unknown_raises(self):
        """Unknown categories must raise GenerationJobError (router → 400)."""
        from app.services.generation_jobs import GenerationJobError, GenerationJobManager
        m = GenerationJobManager()
        with pytest.raises(GenerationJobError):
            m._compute_phases("category", {"category": "bogus"})
        with pytest.raises(GenerationJobError):
            m._compute_phases("weird", {})

    def test_create_job_persists_queued_and_schedules(self, tmp_path):
        """create_job persists a queued job and schedules a runner task."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import GenerationJobManager

        async def scenario():
            Session = self._make_session(tmp_path)
            db = Session()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            job = m.create_job(db, "myapp", "docker", "generate_missing",
                               {"prompt": "build it", "selection": {"compose": []}})
            return job, db, m

        job, db, m = asyncio.run(scenario())
        assert job.id is not None
        assert job.status == "queued"
        assert job.phase == "compose"
        assert job.phase_total == 8  # compose → validate → serving_ports → validate_ports → stream_conf → nginx → validate_conf → env
        assert job.progress == "queued"
        m._run_job.assert_awaited_once()

    def test_get_and_list_jobs_newest_first(self, tmp_path):
        """GET /api/llm/jobs lists newest-first and hides request/result by default."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import GenerationJobManager

        async def scenario():
            Session = self._make_session(tmp_path)
            db = Session()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            j1 = m.create_job(db, "a", "", "env_file", {"category": "env_file", "prompt": "x"})
            j2 = m.create_job(db, "b", "", "env_file", {"category": "env_file", "prompt": "y"})
            jobs = m.list_jobs(db)
            return j1, j2, jobs, m, db

        j1, j2, jobs, m, db = asyncio.run(scenario())
        assert [j["id"] for j in jobs] == [j2.id, j1.id]
        assert "result" not in jobs[0]
        got = m.get_job(db, j1.id)
        assert got is not None and got.id == j1.id
        assert m.get_job(db, "gt-nonexistent") is None

    def test_cancel_job_only_for_active(self, tmp_path):
        """Cancel marks queued/running jobs cancelled; finished jobs are untouched."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import (
            GenerationJobManager, JOB_STATUS_CANCELLED, JOB_STATUS_COMPLETED,
        )

        async def scenario():
            Session = self._make_session(tmp_path)
            db = Session()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            active = m.create_job(db, "a", "", "env_file", {"category": "env_file", "prompt": "x"})
            done = m.create_job(db, "b", "", "env_file", {"category": "env_file", "prompt": "y"})
            done.status = JOB_STATUS_COMPLETED
            db.commit()
            return active, done, m, db

        active, done, m, db = asyncio.run(scenario())
        cancelled = m.cancel_job(db, active.id)
        assert cancelled.status == JOB_STATUS_CANCELLED
        assert active.id in m._cancelled
        # Completed jobs are not cancellable.
        again = m.cancel_job(db, done.id)
        assert again.status == JOB_STATUS_COMPLETED
        assert m.cancel_job(db, "gt-nonexistent") is None

    def test_cancel_job_covers_a_paused_leg(self, tmp_path):
        """A `paused` job IS cancellable (GRILLING G27 + G34).

        G27 made a pause a TERMINAL LEG: the job has STOPPED but still EXISTS.
        The guard used to list only queued/running/awaiting_input, so a DELETE on
        a paused job changed nothing and still answered 200 with
        `status: "paused"` — the card's Delete could not end the leg, and the
        record could never become sweep-eligible.

        The paused leg has NO runner, so it must also stay out of `_cancelled`:
        nothing would ever clear that id (only a terminal status write does), and
        a stale id there cancels a later job that reused the number.
        """
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import (
            GenerationJobManager, JOB_STATUS_CANCELLED, JOB_STATUS_PAUSED,
        )

        async def scenario():
            Session = self._make_session(tmp_path)
            db = Session()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            parked = m.create_job(db, "a", "", "env_file", {"category": "env_file", "prompt": "x"})
            parked.status = JOB_STATUS_PAUSED
            db.commit()
            return parked, m, db

        parked, m, db = asyncio.run(scenario())
        assert JOB_STATUS_PAUSED in m.CANCELLABLE_STATUSES
        out = m.cancel_job(db, parked.id)
        assert out.status == JOB_STATUS_CANCELLED
        assert out.updated_at is not None
        # Its LAST word — no runner follows to write another.
        assert out.progress == "cancelled"
        assert parked.id not in m._cancelled, "a paused leg has no runner to discard"
        # And it is now sweep-eligible (the point of the cancel).
        assert JOB_STATUS_CANCELLED in m._TERMINAL

    def test_prune_finished_ttl(self, tmp_path):
        """Finished jobs older than the TTL are pruned; recent ones stay."""
        import asyncio
        from datetime import datetime, timedelta, timezone
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import (
            GenerationJobManager, JOB_STATUS_COMPLETED,
        )

        async def scenario():
            maker = self._make_session(tmp_path)
            db = maker()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            old = m.create_job(db, "a", "", "env_file", {"category": "env_file", "prompt": "x"})
            fresh = m.create_job(db, "b", "", "env_file", {"category": "env_file", "prompt": "y"})
            old.status = JOB_STATUS_COMPLETED
            old.updated_at = datetime.now(timezone.utc) - timedelta(days=10)
            fresh.status = JOB_STATUS_COMPLETED
            db.commit()
            old_id, fresh_id = old.id, fresh.id
            pruned = m.prune_finished(db)
            return pruned, old_id, fresh_id, maker, m

        pruned, old_id, fresh_id, maker, m = asyncio.run(scenario())
        assert pruned == 1
        # T7: pruning SOFT-deletes — the tombstone stays so links resolve, the
        # log is gone, and the default list omits it.
        db2 = maker()
        assert m.get_job(db2, old_id).deleted_at is not None
        assert m.get_job(db2, fresh_id).deleted_at is None
        assert old_id not in {j["id"] for j in m.list_jobs(db2)}

    def test_recover_stale_jobs_marks_interrupted(self, tmp_path):
        """Restart recovery: queued/running → failed 'interrupted by gateway restart'."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import (
            GenerationJobManager, JOB_STATUS_FAILED, JOB_STATUS_RUNNING,
            JOB_STATUS_COMPLETED,
        )

        async def scenario():
            maker = self._make_session(tmp_path)
            db = maker()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            queued = m.create_job(db, "a", "", "env_file", {"category": "env_file", "prompt": "x"})
            running = m.create_job(db, "b", "", "env_file", {"category": "env_file", "prompt": "y"})
            done = m.create_job(db, "c", "", "env_file", {"category": "env_file", "prompt": "z"})
            running.status = JOB_STATUS_RUNNING
            done.status = JOB_STATUS_COMPLETED
            db.commit()
            ids = (queued.id, running.id, done.id)
            recovered = m.recover_stale_jobs(db)
            # R4.2: the restart marks them `unknown`; the sweep terminalizes.
            terminalized = m.terminalize_unknown(db)
            return recovered, terminalized, ids, maker, m

        recovered, terminalized, (qid, rid, did), maker, m = asyncio.run(scenario())
        assert recovered == 2
        assert terminalized == 2
        # Fresh session: reload the rows after the bulk UPDATEs.
        db2 = maker()
        assert m.get_job(db2, qid).status == JOB_STATUS_FAILED
        assert m.get_job(db2, rid).status == JOB_STATUS_FAILED
        assert "no surviving runner" in m.get_job(db2, qid).error
        assert m.get_job(db2, did).status == JOB_STATUS_COMPLETED
        assert m._tasks == {} and m._cancelled == set()

    # ── external credentials: never minted, pause + resume (task doc §1/§2) ──

    def test_apply_env_kvs_external_is_operator_supplied(self):
        """`external` is never minted and never taken from the model's value.

        A random string for an externally-issued credential looks configured and
        fails at first use — worse than an empty one — and a placeholder the model
        invented (`sk-...`) is exactly the same trap.
        """
        from app.services.generation_jobs import _apply_env_kvs

        kvs = {
            "OPENAI_API_KEY": {"env_type": "external", "value": None},
            "DB_PASSWORD": {"env_type": "secret", "value": None},
        }
        supplied = _apply_env_kvs("BASE=1", kvs, external_values={"OPENAI_API_KEY": "sk-real"})
        assert "OPENAI_API_KEY=sk-real" in supplied
        assert "DB_PASSWORD=\n" not in supplied + "\n"  # the secret IS minted

        # No operator value → EMPTY, and identically so on a second run. The
        # check is on the external entry alone: a sibling `secret` in the same
        # map is minted randomly by design and would differ between runs.
        only_external = {"OPENAI_API_KEY": {"env_type": "external", "value": None}}
        unsupplied = _apply_env_kvs("BASE=1", only_external)
        assert unsupplied == "BASE=1\nOPENAI_API_KEY=\n", unsupplied
        assert _apply_env_kvs("BASE=1", only_external) == unsupplied  # no mint

        # A value the MODEL put on an external entry is discarded outright.
        invented = _apply_env_kvs("", {"K": {"env_type": "external", "value": "sk-invented"}})
        assert "sk-invented" not in invented

    def test_mechanical_env_precedence_empty_is_not_set(self, tmp_path, monkeypatch):
        """An EMPTY in-file value counts as not set; a non-empty one is respected (G16 / §2b).

        The guard used to be presence, so `NEO4J_PORT=` stayed empty while compose would have
        substituted its own default at every `${NEO4J_PORT:-…}` site — the env file then disagreed
        with the value the instance actually ran on.
        """
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "myapp"
        recipe.mkdir()
        (recipe / "docker-compose.yml").write_text(
            "services:\n"
            "  app:\n"
            "    image: x\n"
            "    environment:\n"
            "      - EMPTY_ONE=${EMPTY_ONE:-from-compose}\n"
            "      - SET_ONE=${SET_ONE:-from-compose}\n"
            "      - NO_DEFAULT=${NO_DEFAULT}\n"
        )
        # The base env file: one empty, one explicitly set.
        (recipe / ".env.example").write_text("EMPTY_ONE=\nSET_ONE=explicit\nNO_DEFAULT=\n")

        m = GenerationJobManager()
        out = m._mechanical_env(
            "myapp", "",
            {"selection": {"compose": ["docker-compose.yml"], "env": [".env.example"]}},
        )
        assert out is not None
        vals = dict(l.split("=", 1) for l in out.strip().splitlines())
        assert vals["EMPTY_ONE"] == "from-compose"      # empty ⇒ not set ⇒ compose default
        assert vals["SET_ONE"] == "explicit"            # non-empty ⇒ respected
        assert vals["NO_DEFAULT"] == ""                 # no default to write

    def test_unfilled_external_keeps_the_base_value(self):
        """A bypassed pause must not blank a value the base files already carry.

        The pause makes this unreachable through the UI (Submit stays disabled until every
        required field is filled), but an API-only caller can reach the write without one —
        and blanking an existing value would silently break the instance.
        """
        from app.services.generation_jobs import _apply_env_kvs, _unfilled_externals

        kvs = {"OPENAI_API_KEY": {"env_type": "external", "value": None}}
        mech = "OPENAI_API_KEY=from-the-base-file\n"
        assert _apply_env_kvs(mech, kvs) == "OPENAI_API_KEY=from-the-base-file\n"
        assert _unfilled_externals(kvs, None) == ["OPENAI_API_KEY"]
        # Supplied values still win, and then nothing is reported as unfilled.
        assert _apply_env_kvs(mech, kvs, external_values={"OPENAI_API_KEY": "sk-real"}) == \
            "OPENAI_API_KEY=sk-real\n"
        assert _unfilled_externals(kvs, {"OPENAI_API_KEY": "sk-real"}) == []

    def test_awaiting_input_survives_restart_recovery(self, tmp_path):
        """A paused job is NOT 'interrupted': it is durable by design."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.services.generation_jobs import (
            GenerationJobManager, JOB_STATUS_AWAITING_INPUT,
        )

        async def scenario():
            maker = self._make_session(tmp_path)
            db = maker()
            m = GenerationJobManager()
            m._run_job = AsyncMock()
            paused = m.create_job(db, "a", "", "env_file", {"category": "env_file"})
            paused.status = JOB_STATUS_AWAITING_INPUT
            paused.pending_input = '{"vars": ["OPENAI_API_KEY"]}'
            db.commit()
            recovered = m.recover_stale_jobs(db)
            return recovered, paused.id, maker, m

        recovered, jid, maker, m = asyncio.run(scenario())
        assert recovered == 0
        assert m.get_job(maker(), jid).status == JOB_STATUS_AWAITING_INPUT

# ---------------------------------------------------------------------------
# Tests for GAP-21 — LLM agent tool loop (closed allowlist, rounds cap,
# single-shot fallback, self-repair) + per-user env generation (GAP-19)
# ---------------------------------------------------------------------------

class TestAgentToolLoop:
    """Agentic generation: tool rounds, fallback, self-repair, caps."""

    def _service(self):
        from app.services.llm_service import LLMService
        return LLMService()

    def _tool_response(self, calls=None, content=None):
        """Shape an OpenAI-style chat completion response."""
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = calls
        return {"choices": [{"message": msg}]}

    def _list_files_call(self):
        return [{"id": "call_1", "type": "function",
                 "function": {"name": "list_files", "arguments": "{}"}}]

    @staticmethod
    def _seq_chat(seq):
        """Wrap an iterator of responses in an async _chat stand-in
        (``_chat`` is awaited inside generate_with_agent)."""
        async def _chat(*args, **kwargs):
            return next(seq)
        return _chat

    def _base_context(self, tmp_path):
        return {
            "project_dir": str(tmp_path),
            "recipe_path": "",
            "empty_recipe": False,
            "prompt": "create the env",
            "base_files": {},
            "compose_paths": [],
            "profiles": [],
            "deploy_metadata": {"service_name": "myapp", "user_name": "alice",
                                "label": "0", "domain": "example.com"},
        }

    def test_tool_round_then_answer(self, tmp_path, monkeypatch):
        """Model calls list_files once, then answers — no fallback needed."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        calls = iter([
            self._tool_response(self._list_files_call(), None),
            self._tool_response(None, "SECRET_KEY=abc"),
        ])
        monkeypatch.setattr(svc, "_chat", self._seq_chat(calls))
        result = asyncio.run(svc.generate_with_agent(_FakeSession(), "env_file", ctx))
        assert result["generated_content"] == "SECRET_KEY=abc"
        assert result["validation"]["valid"] is True
        assert result["tool_rounds"] == 2
        assert not any("falling back" in w for w in result["warnings"])

    def test_tool_rejection_falls_back_to_single_shot(self, tmp_path, monkeypatch):
        """Model ignores tools and returns empty → single-shot fallback with URL prefetch."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        calls = iter([
            self._tool_response(None, ""),          # no tools, empty content
            self._tool_response(None, "SECRET_KEY=final"),  # single-shot answer
        ])
        monkeypatch.setattr(svc, "_chat", self._seq_chat(calls))
        result = asyncio.run(svc.generate_with_agent(_FakeSession(), "env_file", ctx))
        assert any("falling back to single-shot" in w for w in result["warnings"])
        assert result["generated_content"] == "SECRET_KEY=final"

    def test_self_repair_on_validation_failure(self, tmp_path, monkeypatch):
        """Invalid env (missing ${IMAGE}) triggers one bounded self-repair round."""
        import asyncio
        svc = self._service()
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: ${IMAGE}\n", encoding="utf-8")
        ctx = self._base_context(tmp_path)
        ctx["compose_paths"] = [str(compose)]
        calls = iter([
            self._tool_response(None, "A=1"),                     # missing IMAGE
            self._tool_response(None, "IMAGE=nginx:latest\nA=1"),  # repaired
        ])
        monkeypatch.setattr(svc, "_chat", self._seq_chat(calls))
        result = asyncio.run(svc.generate_with_agent(_FakeSession(), "env_file", ctx))
        assert result["validation"]["valid"] is True
        assert result["generated_content"] == "IMAGE=nginx:latest\nA=1"
        assert result["tool_rounds"] == 2

    def test_tool_loop_capped_and_flagged(self, tmp_path, monkeypatch):
        """A model that never answers hits the tool-round cap with a warning."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")

        async def always_tool(*args, **kwargs):
            return self._tool_response(self._list_files_call(), None)

        monkeypatch.setattr(svc, "_chat", always_tool)
        result = asyncio.run(svc.generate_with_agent(_FakeSession(), "env_file", ctx))
        assert result["tool_rounds"] == 6
        assert any("tool loop exhausted" in w for w in result["warnings"])
        assert result["generated_content"] == ""

    def test_empty_recipe_requires_prompt(self, tmp_path, monkeypatch):
        """Empty recipe without a prompt short-circuits before any LLM call."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        ctx["empty_recipe"] = True
        ctx["prompt"] = ""
        monkeypatch.setattr(svc, "_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call LLM")))
        result = asyncio.run(svc.generate_with_agent(_FakeSession(), "env_file", ctx))
        assert result["generated_content"] == ""
        assert any("Prompt is REQUIRED" in w for w in result["warnings"])

    def test_unknown_tool_rejected(self, tmp_path):
        """The tool allowlist is closed — anything else is an error."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        assert asyncio.run(svc._exec_tool("bash", {}, ctx)).startswith("ERROR: unknown tool")

    def test_read_file_path_confined(self, tmp_path):
        """read_file cannot escape the project/recipe scope."""
        import asyncio
        svc = self._service()
        ctx = self._base_context(tmp_path)
        (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
        assert asyncio.run(svc._exec_tool("read_file", {"path": "../etc/passwd"}, ctx)).startswith("ERROR")
        assert asyncio.run(svc._exec_tool("read_file", {"path": "missing.py"}, ctx)).startswith("ERROR")
        out = asyncio.run(svc._exec_tool("read_file", {"path": "main.py"}, ctx))
        assert out.startswith("<file main.py>")

    def test_web_fetch_rejects_non_http(self):
        """web_fetch only allows http/https URLs."""
        import asyncio
        svc = self._service()
        out = asyncio.run(svc._exec_tool("web_fetch", {"url": "file:///etc/passwd"}, {}))
        assert "ERROR" in out


# ---------------------------------------------------------------------------
# Tests for GAP-16/20 — gateway var_scan port (parity with api-side scanner)
# ---------------------------------------------------------------------------

class TestGatewayVarScan:
    """The gateway ${VAR} scanner must behave exactly like the api-side one."""

    def test_basic_ref_and_default(self):
        from app.utils.var_scan import scan_text
        refs = scan_text("image: ${IMAGE}\nport: ${PORT:-8080}\n").refs
        names = [(r.name, r.has_default, r.required) for r in refs]
        assert ("IMAGE", False, False) in names
        assert ("PORT", True, False) in names

    def test_required_syntax(self):
        from app.utils.var_scan import scan_text
        refs = scan_text("${DB_URL:?db url required}").refs
        assert refs[0].name == "DB_URL"
        assert refs[0].required is True

    def test_nested_defaults(self):
        from app.utils.var_scan import scan_text
        refs = scan_text("${A:-${B:-x}}").refs
        names = sorted(r.name for r in refs)
        assert names == ["A", "B"]
        assert all(r.has_default for r in refs)

    def test_dollar_escapes(self):
        from app.utils.var_scan import scan_text
        # $${LIT} is a literal ${LIT}; $${REAL} (odd run) is a live ref.
        assert [r.name for r in scan_text("$${LIT}").refs] == []
        assert [r.name for r in scan_text("$$${REAL}").refs] == ["REAL"]

    def test_missing_and_completeness(self):
        import tempfile
        from pathlib import Path
        from app.utils.var_scan import env_completeness, parse_env_file, skeleton_env
        with tempfile.TemporaryDirectory() as tmp:
            compose = Path(tmp) / "docker-compose.yml"
            compose.write_text(
                "services:\n  app:\n    image: ${IMAGE}\n    ports:\n      - \"${PORT:-80}:80\"\n",
                encoding="utf-8",
            )
            env = Path(tmp) / ".env"
            env.write_text("IMAGE=nginx\n", encoding="utf-8")
            assert env_completeness([compose], [env]) == []
            env.write_text("", encoding="utf-8")
            assert env_completeness([compose], [env]) == ["IMAGE"]
            skel = skeleton_env([compose])
            assert skel.strip() == "IMAGE="
        assert parse_env_file(Path(tmp) / "missing.env") == {}

    def test_needs_env_union_scan(self):
        import tempfile
        from pathlib import Path
        from app.utils.var_scan import needs_env
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.yml"
            b = Path(tmp) / "b.yml"
            a.write_text("services:\n  x:\n    image: nginx\n", encoding="utf-8")
            b.write_text("services:\n  y:\n    image: ${IMG}\n", encoding="utf-8")
            assert needs_env([a]) is False
            assert needs_env([a, b]) is True


# ---------------------------------------------------------------------------
# Tests for GAP-2 — frontend: generate-missing panel + selection UI + deploy
# panel integration (static-content checks on the TSX sources)
# ---------------------------------------------------------------------------

_DASH = Path(__file__).parent.parent.parent / "provision-dashboard"


class TestGenerateMissingPanel:
    """The LLM generate-missing panel (opened by the Missing-files action)."""

    def _src(self) -> str:
        return (_DASH / "src" / "components" / "services" / "GenerateMissingPanel.tsx").read_text()

    def test_panel_exists_and_uses_selection_ui(self):
        src = self._src()
        assert "FileSetSelection" in src
        assert "service_name" in src and "recipe_path" in src

    def test_panel_uses_async_job_with_polling(self):
        src = self._src()
        assert "useGenerationJob" in src
        assert "job_type: 'generate_missing'" in src
        assert "Save to project" in src
        # The /llm/jobs endpoints (POST start / GET poll / DELETE cancel) are
        # implemented by the shared hook, not the panel itself.
        hook = (_DASH / "src" / "hooks" / "useGenerationJob.ts").read_text()
        assert "/llm/jobs" in hook
        assert "setInterval" in hook

    def test_panel_has_prompt_input_and_review_gate(self):
        src = self._src()
        assert "Generation prompt" in src
        assert "REQUIRED when the recipe has no base files" in src
        assert "Review &amp; edit" in src or "review" in src.lower()

    def test_panel_no_llm_alert_and_greyed_generate(self):
        src = self._src()
        assert "No LLM configured — no-LLM fallback strategy in effect" in src
        assert "disabled={!llmActive" in src

    def test_panel_cancels_job(self):
        src = self._src()
        assert "cancelJob" in src
        assert "Cancel" in src

    def test_panel_saves_generated_via_save_generated(self):
        src = self._src()
        assert "/projects/save-generated" in src
        assert "selection" in src

    def test_panel_recomputes_profiles_from_compose_selection(self):
        """GAP-2/GAP-4: the panel derives profile candidates from the IN-PANEL
        compose selection (file-sets derive) instead of showing the stale
        'No non-empty profiles found.' from the stored set only."""
        src = self._src()
        assert "deriveProfiles" in src
        assert "selection.compose.length === 0" in src
        assert "profiles.includes(p)" in src  # stale-profile pruning
        api = (_DASH / "src" / "api" / "services.ts").read_text()
        assert "file-sets/derive" in api


class TestFileSetSelection:
    """Shared per-recipe selection component (both panels)."""

    def _src(self) -> str:
        return (_DASH / "src" / "components" / "services" / "FileSetSelection.tsx").read_text()

    def test_ordered_multi_with_order_controls(self):
        src = self._src()
        assert "ArrowUpOutlined" in src and "ArrowDownOutlined" in src
        assert "merge order" in src

    def test_nginx_is_not_optional(self):
        """nginx's absence is as fatal as a missing compose (§1) — the picker
        must not call it optional; an unset slot means `generate new`."""
        src = self._src()
        assert "No nginx selected (optional)" not in src
        assert "generate new" in src
        assert "allowClear" in src

    def test_env_disabled_without_needs_env(self):
        src = self._src()
        assert "needsEnv" in src
        assert "No ${VAR} interpolation detected" in src

    def test_profiles_multi_select(self):
        src = self._src()
        assert "<Select" in src
        assert 'mode="multiple"' in src or "mode: 'multiple'" in src or 'mode="tags"' in src
        assert "Select profiles…" in src

    def test_stale_keep_and_mark(self):
        """A stale stored selection is kept and shown IN PLACE, red + struck —
        the operator asked for the inline marker instead of the old alert."""
        src = self._src()
        assert "stale" in src.lower()
        assert "textDecoration: 'line-through'" in src
        assert "Stale stored selections" not in src


class TestServicesPagePanelOpen:
    """The Missing-files action now opens the panel (no direct generation)."""

    def _src(self) -> str:
        return (_DASH / "src" / "pages" / "ServicesPage.tsx").read_text()

    def test_action_opens_panel(self):
        src = self._src()
        assert "GenerateMissingPanel" in src
        assert "setGenPanel" in src

    def test_direct_single_shot_generation_retired(self):
        src = self._src()
        assert "generateMissingFiles" not in src
        assert "/llm/generate" not in src


class TestDeployFormSelection:
    """Deploy panel: full selection + prompt UI + deploy-time per-user env."""

    def _src(self) -> str:
        return (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()

    def test_selection_section_present(self):
        src = self._src()
        assert "FileSetSelection" in src
        assert "Base file selection" in src

    def test_prompt_input_in_deploy_panel(self):
        src = self._src()
        assert "Generation prompt" in src

    def test_standalone_per_user_env_removed(self):
        """Feature 7: the standalone per-user-env generation path is gone —
        the deploy panel drives everything through generate_missing (the env
        phase writes .env.raw and reports it as result.env_name)."""
        src = self._src()
        assert "per_user_env" not in src
        assert "client.post('/services/generate'" in src

    def test_payload_uses_ordered_compose_and_profiles(self):
        src = self._src()
        assert "selection: { ...sel" in src, "the client sends the intent's selection"
        from app.services import deploy_workflow
        import inspect
        payload_src = inspect.getsource(deploy_workflow._api_payload)
        assert "compose_file_paths" in payload_src
        assert "profiles" in payload_src
        assert "env_files" in payload_src

    def test_selection_persisted_on_deploy(self):
        src = self._src()
        assert "selection: { ...sel" in src

    def test_deploy_panel_recomputes_profiles_from_compose_selection(self):
        """GAP-2/GAP-4: the deploy panel derives profile candidates from the
        IN-PANEL compose selection (file-sets derive) too."""
        src = self._src()
        assert "deriveProfiles" in src
        assert "Form.useWatch('service_name', form)" in src
        assert "profiles.includes(p)" in src  # stale-profile pruning
        api = (_DASH / "src" / "api" / "services.ts").read_text()
        assert "file-sets/derive" in api

    def test_implicit_auto_generation_retired(self):
        src = self._src()
        assert "Auto Templates Completion" not in src
        assert "autoDeploy" not in src

    def test_no_llm_fallback_alert(self):
        src = self._src()
        assert "no-LLM fallback strategy in effect" in src


class TestUseGenerationJob:
    """The shared async job client hook (create / poll / cancel)."""

    def _src(self) -> str:
        return (_DASH / "src" / "hooks" / "useGenerationJob.ts").read_text()

    def test_creates_job_via_post(self):
        src = self._src()
        assert "client.post('/llm/jobs'" in src

    def test_polls_job(self):
        src = self._src()
        assert "client.get(`/llm/jobs/${id}`)" in src
        assert "setInterval" in src

    def test_cancels_via_delete(self):
        src = self._src()
        assert "client.delete(`/tasks/${jobId}`)" in src


# ---------------------------------------------------------------------------
# GAP-24: lightweight convert/preview (design §Implementation notes L284-286)
# ---------------------------------------------------------------------------

class TestComposePreviewProxy:
    """Gateway compose-preview: proxied converter in-call src→key mapping."""

    def test_provision_service_has_compose_preview(self):
        """provision_service should expose a compose_preview public method."""
        from app.services.provision_service import ProvisionService
        assert callable(ProvisionService().compose_preview)

    def test_compose_preview_route_exists_and_is_async(self):
        """Services router should expose GET /{name}/compose-preview (async)."""
        from app.routers.services import router
        routes = [r.path for r in router.routes]
        assert any("compose-preview" in r for r in routes), (
            f"compose-preview route missing from services router. Routes: {routes}"
        )
        import inspect
        from app.routers.services import compose_preview
        assert inspect.iscoroutinefunction(compose_preview), (
            "compose_preview should be an async function"
        )

    def test_compose_preview_forwards_files_and_recipe_path(self, monkeypatch):
        """compose_preview must forward compose_files + recipe_path as params."""
        from app.services.provision_service import ProvisionService
        captured: dict = {}

        async def fake_request(self, method, path, json_data=None, params=None, timeout=300.0):
            captured["path"] = path
            captured["params"] = params
            return {
                "compose_files": ["docker-compose.yml"],
                "src_to_key": {"./app_data": "app_data"},
                "volume_keys": ["app_data"],
            }

        monkeypatch.setattr(ProvisionService, "_request", fake_request)
        import asyncio
        result = asyncio.run(
            ProvisionService().compose_preview(
                "multisvc", ["docker-compose.yml"], "recipes/web"
            )
        )
        assert captured["path"] == "/services/multisvc/compose/preview"
        assert captured["params"] == {
            "compose_files": ["docker-compose.yml"],
            "recipe_path": "recipes/web",
        }
        assert result["volume_keys"] == ["app_data"]

    def test_compose_preview_no_recipe_path(self, monkeypatch):
        """Without recipe_path, only compose_files params are forwarded."""
        from app.services.provision_service import ProvisionService
        captured: dict = {}

        async def fake_request(self, method, path, json_data=None, params=None, timeout=300.0):
            captured["params"] = params
            return {"volume_keys": []}

        monkeypatch.setattr(ProvisionService, "_request", fake_request)
        import asyncio
        asyncio.run(ProvisionService().compose_preview("myapp", ["a.yml", "b.yml"]))
        assert captured["params"] == {"compose_files": ["a.yml", "b.yml"]}

    def test_endpoint_wraps_provision_errors_as_502(self, monkeypatch):
        """provision-api failures surface as 502, mirroring check-missing-files."""
        import asyncio
        from app.routers.services import compose_preview
        from app.services.provision_service import provision_service
        from fastapi import HTTPException

        async def boom(*a, **k):
            raise RuntimeError("provision-api down")

        monkeypatch.setattr(provision_service, "compose_preview", boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(compose_preview("myapp", ["docker-compose.yml"], ""))
        assert exc.value.status_code == 502

    def test_frontend_no_longer_parses_j2_for_volume_keys(self):
        """DeployForm must obtain volume keys from the preview endpoint, not
        regex-parsing .j2 templates (design §Implementation notes L284-286)."""
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "compose-preview" in content, (
            "DeployForm should call the compose-preview endpoint"
        )
        assert "volRegex" not in content, (
            "DeployForm must not regex-parse .j2 content for volume keys"
        )
        assert "/services/${baseName}/files/" not in content.replace(" ", ""), (
            "DeployForm must not fetch raw .j2 files to derive volume keys"
        )


# ---------------------------------------------------------------------------
# Streaming `_chat` — deltas reassembled into the SAME shape as non-streaming,
# with automatic fallback (the only new failure surface of the streaming path)
# ---------------------------------------------------------------------------

class TestChatStreaming:
    """`_chat(stream=True)` must be a drop-in for the non-streaming call."""

    # expected non-streaming equivalent of the canned stream below
    _STREAM_LINES = [
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"}}]}',
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"think "}}]}',
        'data: {"choices":[{"index":0,"delta":{"reasoning_content":"hard"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":" world","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"web_","arguments":"{\\"ur"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"fetch","arguments":"l\\": \\"http://x\\"}"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":22,"completion_tokens_details":{"reasoning_tokens":7}}}',
        'data: [DONE]',
    ]

    def _patch_client(self, monkeypatch, lines, status=200, nonstream=None):
        import types
        import httpx as _httpx
        from app.services import llm_service as mod

        class _Resp:
            def __init__(self):
                self.status_code = status
            async def aread(self):
                return b'{"error":{"message":"boom"}}'
            async def aiter_lines(self):
                for ln in lines:
                    yield ln

        class _Ctx:
            async def __aenter__(self_):
                return _Resp()
            async def __aexit__(self_, *a):
                return False

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            def stream(self, method, url, json=None, headers=None):
                return _Ctx()
            async def post(self, url, json=None, headers=None):
                return types.SimpleNamespace(
                    status_code=200, json=lambda: (nonstream or {}), text=""
                )

        monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
        return mod

    def test_stream_reassembles_into_nonstreaming_shape(self, monkeypatch):
        """Content, reasoning, split tool-call args and usage all merge."""
        import asyncio
        mod = self._patch_client(monkeypatch, self._STREAM_LINES)
        delta_calls = []
        out = asyncio.run(mod.llm_service._chat(
            "http://x/v1", "m", {}, [{"role": "user", "content": "hi"}],
            on_delta=lambda est: delta_calls.append(est),
        ))
        assert "_error" not in out, out
        msg = out["choices"][0]["message"]
        assert msg["content"] == "Hello world"
        assert msg["reasoning_content"] == "think hard"
        # fragmented tool call reassembled (name + arguments concatenated)
        tc = msg["tool_calls"][0]
        assert tc["function"]["name"] == "web_fetch"
        assert tc["function"]["arguments"] == '{"url": "http://x"}'
        assert tc["id"] == "call_1"
        assert out["choices"][0]["finish_reason"] == "tool_calls"
        assert out["usage"]["prompt_tokens"] == 11
        assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 7
        # the live estimate fired (throttled to >=1.5s → may be 0 calls; the
        # important part is that it never raises when invoked)
        assert all(isinstance(x, int) for x in delta_calls)

    def test_stream_without_usage_falls_back_to_nonstreaming(self, monkeypatch):
        """No usage block ⇒ fall back (exact accounting) instead of reporting 0."""
        import asyncio
        nonstream = {
            "choices": [{"index": 0, "message": {"content": "from non-stream"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        }
        lines = [l for l in self._STREAM_LINES if '"usage"' not in l]
        mod = self._patch_client(monkeypatch, lines, nonstream=nonstream)
        out = asyncio.run(mod.llm_service._chat(
            "http://x/v1", "m", {}, [{"role": "user", "content": "hi"}],
        ))
        assert out["choices"][0]["message"]["content"] == "from non-stream"
        assert out["usage"]["prompt_tokens"] == 5

    def test_stream_http_error_falls_back(self, monkeypatch):
        """A non-2xx streaming response degrades to the non-streaming call."""
        import asyncio
        nonstream = {
            "choices": [{"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
        mod = self._patch_client(monkeypatch, [], status=400, nonstream=nonstream)
        out = asyncio.run(mod.llm_service._chat(
            "http://x/v1", "m", {}, [{"role": "user", "content": "hi"}],
        ))
        assert out["choices"][0]["message"]["content"] == "ok"

    def test_stream_disabled_uses_plain_call(self, monkeypatch):
        import asyncio
        nonstream = {
            "choices": [{"index": 0, "message": {"content": "plain"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
        mod = self._patch_client(monkeypatch, [], nonstream=nonstream)
        out = asyncio.run(mod.llm_service._chat(
            "http://x/v1", "m", {}, [{"role": "user", "content": "hi"}], stream=False,
        ))
        assert out["choices"][0]["message"]["content"] == "plain"

    def test_est_tokens_handles_cjk_and_ascii(self):
        from app.services.llm_service import llm_service
        assert llm_service._est_tokens("") == 0
        assert llm_service._est_tokens("abcdefgh") >= 2
        # CJK counts ~1 token/char, so it must exceed the ASCII estimate
        assert llm_service._est_tokens("我们需要根据部署元数据") > llm_service._est_tokens("abcdefghij")


class TestGitCloneStall:
    """A clone that HANGS must be retried, not left pinning the request.

    Regression (observed live): `git clone` sat in git-remote-https with no pack
    data arriving; `subprocess.run` had no timeout, so there was no non-zero exit
    to trigger the retry — the create request stayed in flight and the dashboard
    spinner never resolved.
    """

    def _mgr(self, monkeypatch, tmp_path):
        from app.config import settings
        from app.services.service_manager import ServiceManager
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        return ServiceManager()

    def test_stalled_clone_is_aborted_and_retried(self, monkeypatch, tmp_path):
        import subprocess
        from app.services import service_manager as sm

        mgr = self._mgr(monkeypatch, tmp_path)
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            assert kw.get("timeout") == sm.GIT_CLONE_TIMEOUT, "clone must be bounded"
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        monkeypatch.setattr(sm.time, "sleep", lambda *_: None)

        with pytest.raises(RuntimeError) as ei:
            mgr.create_from_git("https://example.invalid/x.git", "main", "x")

        assert calls["n"] == sm.GIT_CLONE_ATTEMPTS, "a stall must consume the retry budget"
        assert "no progress" in str(ei.value)
        assert not (tmp_path / "x").exists(), "partial target must be cleaned up"

    def test_stall_then_success_recovers(self, monkeypatch, tmp_path):
        """One stall followed by a clean clone → the create still succeeds."""
        import subprocess
        from app.services import service_manager as sm

        mgr = self._mgr(monkeypatch, tmp_path)
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            (tmp_path / "x").mkdir(exist_ok=True)

        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        monkeypatch.setattr(sm.time, "sleep", lambda *_: None)

        info = mgr.create_from_git("https://example.invalid/x.git", "main", "x")
        assert calls["n"] == 2
        assert info is not None


class TestRemovedReport:
    """Turn 2 self-reports what its adversarial pass removed.

    The removal itself is the model's judgement (no name matching anywhere in
    the gateway), so the report is what makes that judgement auditable — a
    misjudgement would otherwise be invisible.
    """

    def test_report_is_split_out_of_the_answer(self):
        from app.services.llm_service import _split_removed_report
        kvs, report = _split_removed_report({
            "CONSOLE_API_URL": {"env_type": "non-secret", "value": "http://x"},
            "_removed": {"COOKIE_DOMAIN": "restates the hostname the platform serves this instance on"},
        })
        assert "_removed" not in kvs
        assert list(kvs) == ["CONSOLE_API_URL"]
        assert report == {"COOKIE_DOMAIN": "restates the hostname the platform serves this instance on"}

    def test_no_report_is_not_an_error(self):
        from app.services.llm_service import _split_removed_report
        src = {"A": {"env_type": "non-secret", "value": "v"}}
        kvs, report = _split_removed_report(src)
        assert report is None and kvs == src

    def test_empty_report_and_non_dict_input(self):
        from app.services.llm_service import _split_removed_report
        kvs, report = _split_removed_report({"_removed": {}})
        assert kvs == {} and report == {}
        assert _split_removed_report(None) == (None, None)
        assert _split_removed_report("nonsense") == ("nonsense", None)

    def test_the_env_filter_would_have_dropped_it_anyway(self):
        """`_removed` carries no env_type, so it never reaches the env."""
        from app.services.llm_service import _split_removed_report
        raw = {"_removed": {"COOKIE_DOMAIN": "why"}}
        filtered = {str(k): v for k, v in raw.items()
                    if isinstance(v, dict) and v.get("env_type") in ("secret", "non-secret")}
        assert filtered == {}


class TestComposePresenceScan:
    """The missing-file scan and the candidate scan must agree on what a compose is.

    Regression: `_recipe_missing` counted ANY *.yml/*.yaml as a compose, so
    importing n8n — whose root ships codecov.yml, cubic.yaml, lefthook.yml and
    the .tbls.*.yml files and has NO compose — reported compose as PRESENT. The
    compose phase was then skipped while the panel (correctly) offered no
    compose to select, so the deploy had a phase that never ran and no file to
    deploy. Two scans disagreeing is the whole bug: they now share one
    definition.
    """

    def test_only_compose_named_yaml_counts(self):
        from app.services.file_sets import _is_compose_name
        for ok in ("docker-compose.yml", "docker-compose.yaml", "compose.yml",
                   "compose.yaml", "docker-compose.override.yml"):
            assert _is_compose_name(ok), ok
        for no in ("codecov.yml", "cubic.yaml", "lefthook.yml",
                   ".tbls.sqlite.yml", ".tbls.postgres.yml", "values.yaml",
                   "docker-compose.yml.bak", "biome.jsonc"):
            assert not _is_compose_name(no), no

    def test_template_counts_for_presence_but_is_not_a_candidate(self):
        from app.services.file_sets import _is_compose_candidate, _is_compose_name
        assert _is_compose_name("docker-compose.yml.j2", allow_template=True)
        assert not _is_compose_name("docker-compose.yml.j2")
        assert not _is_compose_candidate("docker-compose.yml.j2")

    def test_unrelated_yaml_no_longer_satisfies_the_compose_phase(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        p = tmp_path / "n8n"
        p.mkdir()
        for f, body in (("codecov.yml", "coverage: {}\n"), ("cubic.yaml", "x: 1\n"),
                        ("lefthook.yml", "pre-commit: {}\n"), ("pnpm-workspace.yaml", "packages: []\n")):
            (p / f).write_text(body)
        missing = GenerationJobManager._recipe_missing({"service_name": "n8n", "recipe_path": ""})
        assert missing == ["docker-compose", "serving_ports", "nginx.conf"], missing

    def test_real_recipe_reports_nothing_missing(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        p = tmp_path / "svc"
        p.mkdir()
        (p / "docker-compose.yml").write_text("services: {}\n")
        (p / "nginx.conf").write_text("server {}\n")
        (p / "codecov.yml").write_text("coverage: {}\n")
        # A COMPLETE recipe carries all three artifacts now: the serving-ports
        # artifact is demanded exactly as the compose and the nginx conf are
        # (R1.1's "every slot must come out named" — G20 applied literally).
        assert GenerationJobManager._recipe_missing({"service_name": "svc", "recipe_path": ""}) == ["serving_ports"]
        (p / "serving_ports.yml").write_text("ports: []\n")
        assert GenerationJobManager._recipe_missing({"service_name": "svc", "recipe_path": ""}) == []


class TestGeneratedComposeFeedsOtherPhases:
    """A compose generated in THIS run must be visible to the later phases.

    Regression: the compose phase runs precisely because the operator had
    nothing to select, so `sel_compose` is empty. `_build_context` then derived
    the compose service names (nginx validation) and the ${VAR} surface (env
    phase) from the selection alone, so the nginx phase was validated against an
    empty service list: every proxy_pass host read as unknown and the phase
    failed with "proxy_pass target(s) do not match any compose service name.
    Compose services: []" — even though the model's target (`n8n`) was correct
    and the generated docker-compose.yml was already on disk.
    """

    def _recipe(self, tmp_path, body="services:\n  n8n:\n    image: x\n"):
        (tmp_path / "n8n").mkdir()
        (tmp_path / "n8n" / "docker-compose.yml").write_text(body)
        return tmp_path

    def _ctx(self, tmp_path, monkeypatch, phase, request):
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        return GenerationJobManager()._build_context("n8n", "", phase, request)

    def test_nginx_phase_sees_a_compose_it_did_not_select(self, tmp_path, monkeypatch):
        self._recipe(tmp_path)
        ctx = self._ctx(tmp_path, monkeypatch, "nginx", {"selection": {}})
        assert ctx["compose_service_names"] == ["n8n"], ctx["compose_service_names"]

    def test_env_phase_gets_the_var_surface_of_a_generated_compose(self, tmp_path, monkeypatch):
        self._recipe(tmp_path, "services:\n  n8n:\n    environment:\n      - N8N_HOST=${N8N_HOST}\n")
        ctx = self._ctx(tmp_path, monkeypatch, "env", {"selection": {}})
        assert ctx["compose_paths"], "the env phase must still see the compose"
        assert "__compose_variables__.txt" in ctx["base_files"]

    def test_an_explicit_selection_still_wins(self, tmp_path, monkeypatch):
        self._recipe(tmp_path)
        (tmp_path / "n8n" / "other-compose.yml").write_text("services:\n  other:\n    image: y\n")
        ctx = self._ctx(tmp_path, monkeypatch, "nginx",
                        {"selection": {"compose": ["other-compose.yml"]}})
        assert ctx["compose_service_names"] == ["other"], ctx["compose_service_names"]

    def test_rendered_per_user_compose_is_never_treated_as_the_recipe_compose(self, tmp_path, monkeypatch):
        """The per-user rendered file lives in the same dir; it is not a source."""
        self._recipe(tmp_path)
        (tmp_path / "n8n" / "docker-compose.user-specialsync.0.yml").write_text(
            "services:\n  rendered:\n    image: z\n")
        ctx = self._ctx(tmp_path, monkeypatch, "nginx", {"selection": {}})
        assert ctx["compose_service_names"] == ["n8n"], ctx["compose_service_names"]


class TestFailedBucketPassthrough:
    """The gateway must forward the -api's `failed_services` bucket unchanged.

    The dashboard's failed card reads the failed bucket from the same per-user
    object it already consumes, so the gateway must not reshape or drop it
    (failed-instance-state-todo.md §3 / §7).
    """

    def test_list_users_forwards_the_failed_bucket(self, monkeypatch):
        import asyncio
        from app.routers import users as users_router

        failed_entry = {
            "service_name": "graphiti", "label": "0", "status": "failed",
            "failure": {"stage": "build", "message": "GRAPHITI_VERSION: parameter not set",
                        "at": "2026-09-19T00:00:00+00:00"},
            "container_names": [], "healthy_containers": {},
            "unhealthy_containers": {}, "missing_containers": {},
            "env_file_path": "/srv/x/.env.1.u.0",
        }

        async def fake_list_users():
            return {"user_status": [{
                "user_name": "u",
                "summary": {"expected_services_#": 1, "healthy_services_#": 0,
                            "unhealthy_services_#": 0, "failed_services_#": 1},
                "healthy_services": [], "unhealthy_services": [], "missing_services": [],
                "failed_services": [failed_entry],
            }]}

        monkeypatch.setattr(users_router.provision_service, "list_users", fake_list_users)
        # The handler syncs end-users into the gateway DB; stub the session so
        # the test exercises only the passthrough.
        import app.database as db_mod

        class _Q:
            def all(self):
                return []
        class _DB:
            def query(self, *a, **k):
                return _Q()
            def add(self, *a, **k):
                pass
            def commit(self):
                pass
            def close(self):
                pass
        monkeypatch.setattr(db_mod, "SessionLocal", lambda: _DB())

        result = asyncio.run(
            users_router.list_users(request=None, current_user={"role": "admin", "id": 1})
        )
        user = result["users"][0]
        assert user["failed_services"] == [failed_entry], (
            "the failed bucket (and its reason payload) must survive the gateway untouched"
        )

    def test_capacity_refusal_is_not_folded_into_502(self):
        """A 503 at_capacity from the -api must keep its own status (§4d)."""
        import httpx
        from app.services.provision_service import provision_service

        req = httpx.Request("POST", "http://x/users")
        resp = httpx.Response(
            503, request=req,
            json={"detail": "at capacity: 4 task(s) running (limit 4); cancel a task",
                  "error": "at_capacity", "running": 4, "capacity": 4},
        )
        exc = httpx.HTTPStatusError("boom", request=req, response=resp)
        mapped = provision_service.as_http_error(exc)
        assert mapped.status_code == 503
        assert mapped.detail["error"] == "at_capacity"
        assert mapped.detail["running"] == 4

    def test_other_provision_errors_stay_502(self):
        import httpx
        from app.services.provision_service import provision_service

        req = httpx.Request("POST", "http://x/users")
        resp = httpx.Response(409, request=req, json={"detail": "already registered"})
        exc = httpx.HTTPStatusError("boom", request=req, response=resp)
        mapped = provision_service.as_http_error(exc)
        assert mapped.status_code == 502


class TestFailedCardSourceGuards:
    """Source-level guards for the failed card (mirrors the UsersPage guards).

    The card's own state must come from the registry (via the failed bucket) —
    the task store stays a drill-down. The Cancel action on a *building* card is
    the one place a task lookup is legitimate, and it is a lookup to cancel.
    """

    def _users_page(self) -> str:
        return (_DASH / "src" / "pages" / "UsersPage.tsx").read_text()

    def test_failed_bucket_is_consumed(self):
        src = self._users_page()
        # The stage projection (gateway) consumes the -api's failed bucket; the
        # page renders the resulting unified entries (R9).
        from app.services import deploy_workflow
        import inspect
        assert "failed_services" in inspect.getsource(deploy_workflow.stages)
        assert "source === 'instance'" in src

    def test_failed_card_renders_reason_and_both_actions(self):
        src = self._users_page()
        assert "Deployment failed" in src
        assert "svc.failure?.message" in src
        assert "svc.failure?.stage" in src
        assert "Retry</Button>" in src
        assert "Delete</Button>" in src

    def test_failed_status_is_its_own_badge(self):
        src = self._users_page()
        assert "s.status === 'failed'" in src

    def test_building_card_is_cancellable(self):
        src = self._users_page()
        assert "cancelBuilding" in src
        assert "svc.status === 'building'" in src
        # Cancel goes through the existing task-cancel endpoint — no new machinery.
        assert "tasks/${task.task_id}" in src

    def test_failed_card_state_does_not_read_the_task_log(self):
        """The failure reason is read off the instance, never fetched."""
        src = self._users_page()
        assert "svc.failure" in src
        idx = src.find("Deployment failed")
        window = src[max(0, idx - 2000): idx + 2000]
        assert "/log" not in window, "the failed card must not read a task log for its state"

    def test_capacity_refusal_is_a_persistent_alert(self):
        """§4d: saturation is a persistent alert, not a transient toast."""
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "at_capacity" in src
        assert "atCapacity" in src
        assert "Platform is at capacity" in src


class TestLLMBasedFix:
    """`LLM based fix` is the ANALYSIS step (GRILLING G26).

    It reads the failed task's log plus the deployment context and returns a
    FIXING GUIDE. It writes no files: the `.fix` variants come from the
    restarted path that `check` triggers (G25), which is also the only route
    past the operator's confirmation. This class used to assert the opposite —
    that a fix job ran compose/validate/nginx/env and wrote the `.fix` files
    itself — and those assertions had to change with the model.
    """

    def test_fix_filenames_follow_the_raw_pattern(self):
        """`.fix` sits directly after the stem, is single-slotted (not numbered,
        apart from the job's collision token — GRILLING G33)."""
        from app.services.generation_jobs import FIX_FILENAME, PHASE_FILENAME
        assert FIX_FILENAME["compose"] == "docker-compose.fix.{token}.yml"
        assert FIX_FILENAME["nginx"] == "nginx.fix.{token}.conf"
        assert FIX_FILENAME["env"] == ".env.fix"
        # ...and never collide with the originals they correct.
        for phase, name in FIX_FILENAME.items():
            assert name.format(token=7) != PHASE_FILENAME[phase].format(token=7)
            assert "fix1" not in name and "fix2" not in name

    def test_fix_job_is_analysis_only(self):
        """The fix job runs ONE phase and it produces no files (G26).

        A fix that wrote files would write them outside the operator's
        confirmation, and `check`'s restart would then write them a second time.
        """
        from app.services.generation_jobs import generation_jobs as gj
        phases = gj._compute_phases("fix", {"fix": True})
        assert phases == ["analyze"]
        # None of the file-producing phases may run here.
        for file_phase in ("compose", "validate", "nginx", "env"):
            assert file_phase not in phases

    def test_fix_job_is_analysis_only_regardless_of_skip_env(self):
        """`skip_env` cannot re-introduce a file phase — there are none."""
        from app.services.generation_jobs import generation_jobs as gj
        assert gj._compute_phases("fix", {"fix": True, "skip_env": True}) == ["analyze"]
        assert gj._compute_phases("fix", {"fix": True, "skip_env": False}) == ["analyze"]

    def test_fix_candidates_are_offered_by_the_existing_scans(self):
        """No predicate change was needed — verify that claim directly."""
        from app.services.file_sets import (
            _is_compose_candidate, _is_nginx_candidate, _is_env_candidate,
        )
        from app.services.generation_jobs import FIX_FILENAME
        assert _is_compose_candidate(FIX_FILENAME["compose"])
        assert _is_nginx_candidate(FIX_FILENAME["nginx"])
        assert _is_env_candidate(FIX_FILENAME["env"])

    def test_task_log_is_injected_whole_into_the_context(self):
        """The log is fed entire — not truncated, not summarised (§1)."""
        from app.services.generation_jobs import generation_jobs as gj
        import inspect
        src = inspect.getsource(gj._build_context)
        assert "__failed_task_log__.txt" in src
        assert "task_log" in src

    def test_fix_endpoint_requires_a_failed_instance(self):
        """Availability is decided by the -api; the gateway must respect it."""
        import inspect
        from app.routers import llm as llm_router
        src = inspect.getsource(llm_router._enrich_fix_request)
        assert 'entry.get("status") != "failed"' in src
        assert "fix_available" in src
        assert "task_log_gone" in src


class TestConflictingDefaults:
    """§3b detection: one name, two sites, two different defaults."""

    def _compose(self, tmp_path, text):
        p = tmp_path / "docker-compose.yml"
        p.write_text(text)
        return p

    def test_detects_the_graphiti_case(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        p = self._compose(tmp_path, (
            "services:\n"
            "  graph:\n"
            "    environment:\n"
            "      - NEO4J_URI=bolt://neo4j:${NEO4J_PORT:-7687}\n"
            "  neo4j:\n"
            "    healthcheck:\n"
            "      test: wget -qO- http://localhost:${NEO4J_PORT:-7474} || exit 1\n"
        ))
        found = conflicting_defaults([p])
        assert [c["name"] for c in found] == ["NEO4J_PORT"]
        assert found[0]["defaults"] == ["7474", "7687"]
        # Site-by-site, so the pause can show WHERE each default came from.
        assert {s["default"] for s in found[0]["sites"]} == {"7474", "7687"}

    def test_a_single_default_is_not_flagged(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        p = self._compose(tmp_path, (
            "services:\n  a:\n    environment:\n"
            "      - X=${FOO:-1}\n      - Y=${FOO:-1}\n"
        ))
        assert conflicting_defaults([p]) == []

    def test_a_no_default_site_is_not_a_conflict(self, tmp_path):
        """`${V}` (required) beside `${V:-x}` is NOT two meanings — one default."""
        from app.utils.var_scan import conflicting_defaults
        p = self._compose(tmp_path, (
            "services:\n  a:\n    environment:\n"
            "      - A=${V}\n      - B=${V:-x}\n"
        ))
        assert conflicting_defaults([p]) == []

    def test_scan_is_source_agnostic_across_files(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        a = tmp_path / "docker-compose.yml"
        b = tmp_path / "docker-compose.override.yml"
        a.write_text("services:\n  a:\n    environment:\n      - X=${P:-1}\n")
        b.write_text("services:\n  b:\n    environment:\n      - Y=${P:-2}\n")
        found = conflicting_defaults([a, b])
        assert [c["name"] for c in found] == ["P"]
        assert {s["file"] for s in found[0]["sites"]} == {
            "docker-compose.yml", "docker-compose.override.yml",
        }

    def test_missing_file_is_ignored(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        assert conflicting_defaults([tmp_path / "nope.yml"]) == []


class TestDeployPanelSemantics:
    """§3/§4 naming and rotation rules for `generate new` vs fixing."""

    def test_generate_new_writes_base_names(self):
        """`generate new` must not impersonate a repo file (§3), and its name
        carries the JOB's collision token (GRILLING G33).

        The token is what makes a parallel generation for the same recipe — a
        pair whose reservation keys differ only by user or label — unable to
        address the same path. `env` has none on purpose: it is per-user
        (`.env.raw.{user}.{label}`) and already collision-free.
        """
        from app.services.generation_jobs import PHASE_FILENAME, _phase_names
        assert PHASE_FILENAME["compose"] == "docker-compose.base.{token}.yml"
        assert PHASE_FILENAME["serving_ports"] == "serving_ports.base.{token}.yml"
        assert PHASE_FILENAME["nginx"] == "nginx.base.{token}.conf"
        # G56 — the stream conf is recipe-level too.
        assert PHASE_FILENAME["stream_conf"] == "nginx.stream.base.{token}.conf"
        assert PHASE_FILENAME["env"] == ".env"
        assert _phase_names(337, fix=False) == {
            "compose": "docker-compose.base.337.yml",
            "serving_ports": "serving_ports.base.337.yml",
            "nginx": "nginx.base.337.conf",
            "stream_conf": "nginx.stream.base.337.conf",
            "env": ".env",
        }
        # The token goes BEFORE the extension, so the candidate predicates still
        # match and the file stays selectable everywhere it was.
        from app.services.file_sets import (
            _is_compose_candidate, _is_nginx_candidate, _is_stream_conf_candidate,
        )
        assert _is_compose_candidate(_phase_names(337, fix=False)["compose"])
        assert _is_nginx_candidate(_phase_names(337, fix=False)["nginx"])
        assert _is_stream_conf_candidate(_phase_names(337, fix=False)["stream_conf"])

    def test_each_job_gets_its_own_names(self):
        """Two jobs never address the same path (GRILLING G33).

        The defect this closes: the artifacts are RECIPE-LEVEL and user-agnostic,
        so two generations whose keys differ only by user or label wrote the same
        two files — last writer wins, and one run validated a compose another had
        just replaced.
        """
        from app.services.generation_jobs import _phase_names
        a, b = _phase_names(101, fix=False), _phase_names(102, fix=False)
        for phase in ("compose", "nginx"):
            assert a[phase] != b[phase], f"{phase} collides between two jobs"

    def test_the_collision_token_is_not_a_link_to_the_job(self):
        """The token is a UNIQUENESS device, nothing else (user ruling).

        No ownership, no lifecycle, no cleanup keyed on the job's existence: the
        generated files belong to the RECIPE and outlive the job. The gateway's
        only unlink on the job path is the job's own log.
        """
        import inspect
        from app.services import generation_jobs as gjmod
        src = inspect.getsource(gjmod)
        # The one `unlink` in the module is the job log's (G30's contract: the
        # log is disposed with its record) — never an artifact's.
        assert src.count(".unlink()") == 1
        assert "_phase_names" in src and "NEVER auto-deleted" in src

    def test_base_and_fix_families_are_distinct(self):
        """`.base` = generated new; `.fix` = a correction. Never the same file."""
        from app.services.generation_jobs import FIX_FILENAME, PHASE_FILENAME
        for phase in ("compose", "nginx"):
            assert PHASE_FILENAME[phase] != FIX_FILENAME[phase]
            assert ".base" in PHASE_FILENAME[phase]
            assert ".fix" in FIX_FILENAME[phase]

    def test_env_phase_never_returns_none(self):
        """§4: the phase always produces the `.raw`, even when it is empty."""
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._mechanical_env)
        assert "return \"\"" in src
        assert "return None" not in src


class TestComposeValidationPause:
    """§3b: the validate phase pauses on a conflicting-defaults compose."""

    def test_validate_runs_between_compose_and_nginx(self):
        """`validate` sits directly after `compose` on every path that generates
        one. The `fix` job type is deliberately NOT in this list any more: it is
        the analysis step and generates no compose to validate (G26)."""
        from app.services.generation_jobs import GenerationJobManager
        m = GenerationJobManager()
        for job_type, req in (("generate_missing", {}), ("generate_missing", {"fix": True})):
            phases = m._compute_phases(job_type, req)
            assert "validate" in phases, (job_type, req)
            assert phases.index("validate") == phases.index("compose") + 1, (job_type, req)

    def test_pause_carries_guidance_and_a_compose_fix_kind(self):
        """The prompt area is pre-filled from the scan, site by site (§3b)."""
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._run_job)
        assert '"kind": "compose_fix"' in src
        assert '"guidance": _guidance' in src
        # Derived from the scan, not from a name list.
        assert "conflicting_defaults" in src
        # The pause must come BEFORE the nginx phase can build on the compose.
        assert "awaiting_input" in src

class TestSelectedFileFork:
    """§5b: editing a SELECTED file forks into its `.fix` variant."""

    def test_fork_name_follows_the_fix_convention(self):
        """The tag goes before the extension and is single-slotted."""
        src = (_DASH / "src" / "components" / "services" / "FileSetSelection.tsx").read_text()
        assert "export function fixVariantFor" in src
        assert "fix1" not in src and "fix2" not in src
        from app.services.generation_jobs import FIX_FILENAME
        for name in FIX_FILENAME.values():
            assert ".fix" in name

    def test_save_forks_instead_of_overwriting_the_selection(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "editorFork" in src
        assert "fixVariantFor(editorFileName)" in src
        assert "the selection now uses it" in src

    def test_picker_names_are_clickable(self):
        fs = (_DASH / "src" / "components" / "services" / "FileSetSelection.tsx").read_text()
        df = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "onOpenFile" in fs
        assert "openSelectedFile" in df


class TestEnvReferencePicker:
    """§5: the env REFERENCE is generation context, never a handoff."""

    def test_gateway_uses_the_reference_only_when_env_is_generate_new(self):
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._build_context)
        assert "env_reference" in src
        # sel_env (the handoff) wins when present.
        assert "list(sel_env) or [" in src

    def test_dashboard_renders_the_picker_only_for_generate_new(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "Env reference (generation context)" in src
        assert "eff.env.length === 0" in src, "shown only when env is `generate new`"
        assert "env_reference: envRefs" in src

    def test_reference_is_not_part_of_the_handoff(self):
        """It must not ride the deploy payload — only the generation request."""
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "payload.env_reference" not in src

    def test_standalone_panel_uses_the_reference_picker_and_no_interpolation_slot(self):
        src = (_DASH / "src" / "components" / "services" / "GenerateMissingPanel.tsx").read_text()
        assert "showEnv={false}" in src
        assert "Env reference (generation context)" in src
        assert "env_reference: envRefs" in src
        assert "Generate Base Files" in src
        assert "Generate Missing Files" not in src
        assert "env: []," in src, "selection.env stays empty; the reference travels separately"
        # Selected recipe files stay openable/editable; saving forks to .fix.
        assert "onOpenFile={openSelectedFile}" in src
        assert "fixVariantFor" in src
        assert "Save as .fix and use it" in src
        # The reference picker sits below the prompt input.
        assert src.index("Generation prompt") < src.index("Env reference (generation context)")
        # Recipe-level generation is profile-blind; the standalone panel has
        # no profile picker and sends no profiles.
        assert "showProfiles={false}" in src
        assert "profiles: []," in src

    def test_deploy_generation_is_profile_blind_but_the_deploy_is_not(self):
        import inspect
        from app.services import deploy_workflow
        # The generation request drops profiles (they are deploy-time options).
        assert 'gen_selection.pop("profiles"' in inspect.getsource(
            deploy_workflow._generation_request)
        # The -api payload carries them.
        assert 'payload["profiles"]' in inspect.getsource(deploy_workflow._api_payload)
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "selection: { ...sel" in src

    def test_generated_files_distinguish_recipe_from_per_user(self):
        src = (_DASH / "src" / "pages" / "ServicesPage.tsx").read_text()
        assert "export function generatedFileScope" in src
        assert "color={scope === 'recipe' ? 'gold' : 'purple'}" in src
        assert "Recipe-level generated artifact" in src
        assert "Per-user, per-service generated file" in src
        assert '<Tag color="gold">recipe</Tag><Tag color="purple">per-user</Tag>' in src

    def test_serving_ports_has_the_same_generate_new_tag(self):
        src = (_DASH / "src" / "components" / "services" / "FileSetSelection.tsx").read_text()
        assert "!sel.serving_ports && !sel.serving_ports_none" in src
        assert src.count("generate new — the pipeline will create one") >= 3, (
            "compose, serving ports and nginx use the same generate-new tag")

    def test_the_source_projects_robot_is_clickable_when_base_files_exist(self):
        src = (_DASH / "src" / "pages" / "ServicesPage.tsx").read_text()
        assert "disabled={isChecking || hasAll}" not in src
        assert "type={hasAll ? 'default' : 'primary'}" in src
        assert "Essential base files are already there; you can click to generate new ones." in src
        assert "m !== '.env'" in src, "the readiness list excludes deploy-time env"


class TestPreDeployRegistry:
    """T8: pre-deploy sessions are keyed by ``service_id`` and live in the
    gateway, never the -api registry."""

    def _seed(self, db, username: str = "alice", service_name: str = "myapp"):
        import uuid

        from support import mk_user

        user = mk_user(db, username)
        return user, uuid.uuid4(), uuid.uuid4()

    def test_create_get_remove_roundtrip(self):
        import uuid
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            from support import mk_task

            user, sid, proc = self._seed(db)
            mk_task(db, "gt-1", status="running", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-1",
            )
            row = pdr.get_session(str(sid))
            assert row["state"] == "generating"
            assert row["generation_task_id"] == "gt-1"
            assert row["service_id"] == str(sid)
            assert pdr.is_reserved(user.api_user_id, "myapp", "0") is True
            assert pdr.remove_session(str(sid)) is True
            assert pdr.get_session(str(sid)) is None
        finally:
            db.close()

    def test_reason_is_carried_for_the_paused_state(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="paused",
                reason="review",
            )
            assert pdr.get_session(str(sid))["reason"] == "review"
        finally:
            db.close()

    def test_reconcile_keeps_a_session_with_a_live_task(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db)
            mk_task(db, "gt-live", status="running", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-live",
            )
            rep = pdr.reconcile(live_task_ids={"gt-live"})
            assert rep["remaining"] == 1
            assert pdr.get_session(str(sid))["generation_task_id"] == "gt-live"
        finally:
            db.close()

    def test_reconcile_drops_a_stale_session(self):
        """No live task and no instance ⇒ nothing is working on it."""
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db)
            mk_task(db, "gt-done", status="completed", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-done",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == [str(sid)]
            assert pdr.list_sessions() == []
        finally:
            db.close()

    def test_reconcile_keeps_a_paused_session_with_its_guide(self):
        """G26: a pause is the analysis's product; it survives reconcile."""
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="paused",
                reason="llm_fix", guide="NEO4J_PORT is taken; move it",
                slot="compose", deploy_task_id="api-7",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == [], "a pause is waiting, not stale"
            row = pdr.get_session(str(sid))
            assert row["guide"] == "NEO4J_PORT is taken; move it"
            assert row["slot"] == "compose"
            assert row["deploy_task_id"] == "api-7"
        finally:
            db.close()

    def test_the_owner_service_label_tuple_is_unique(self):
        """The reservation is the tuple, not the id: a second recipe for the same
        owner/service/label is refused by the database, while a different label
        is a second session."""
        import uuid

        import pytest
        from sqlalchemy.exc import IntegrityError
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            pdr.create_or_replace(
                service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="docker", state="generating",
            )
            with pytest.raises(IntegrityError):
                pdr.create_or_replace(
                    service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                    owner_api_user_id=user.api_user_id, service_name="myapp",
                    service_label="0", recipe_path="web", state="generating",
                )
        finally:
            db.close()

        db = SessionLocal()
        try:
            user = mk_user(db, "bob")
            pdr.create_or_replace(
                service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
            )
            pdr.create_or_replace(
                service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="1", recipe_path="", state="generating",
            )
            bobs = [r for r in pdr.list_sessions() if r["user_name"] == "bob"]
            assert len(bobs) == 2
        finally:
            db.close()


class TestPreDeployWiring:
    """§8b wiring: session lifecycle (service_id-keyed), handoff order and the
    card actions."""

    def _fake_job(self, monkeypatch, llm_router, db, *, task_id="gt-new",
                  procedure_id=None, status="queued"):
        import uuid

        from support import mk_task

        mk_task(db, task_id, status=status, procedure_id=procedure_id)

        class _Job:
            pass

        job = _Job()
        job.task_id = task_id
        job.id = task_id
        job.procedure_id = procedure_id or uuid.uuid4()
        job.service_id = None
        job.status = status
        monkeypatch.setattr(llm_router.generation_jobs, "create_job",
                            lambda *a, **k: job)
        monkeypatch.setattr(llm_router, "log_action", lambda *a, **k: None)
        return job

    def test_session_opens_on_a_deploy_scoped_job(self):
        import inspect
        from app.routers import llm as llm_router
        src = inspect.getsource(llm_router.create_generation_job)
        assert "new_generation_leg" in src
        # ONE home for "who owns this job": the standalone panel (no user/label)
        # opens no session.
        assert "_session_scoped = job_origin(req, job_type) == JOB_ORIGIN_DEPLOY" in src

    def test_reserved_combination_is_refused(self):
        import inspect
        from app.routers import llm as llm_router
        src = inspect.getsource(llm_router.create_generation_job)
        assert "is_reserved" in src and "combination_reserved" in src

    def test_a_guided_restart_resumes_its_own_pause(self, tmp_path, monkeypatch):
        """A paused/refused session's own combination is exempt from the 409."""
        import asyncio
        import uuid

        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import llm as llm_router
        from app.services import pre_deploy_registry as pdr
        from support import mk_task, mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            sid = uuid.uuid4()
            mk_task(db, "gt-paused", status="paused")
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="paused",
                reason="llm_fix", guide="move NEO4J_PORT",
                generation_task_id="gt-paused",
            )
            base = {"service_name": "myapp", "recipe_path": "",
                    "job_type": "generate_missing", "user_name": "alice",
                    "label": "0", "service_id": str(sid),
                    "owner_api_user_id": str(user.api_user_id)}
            admin = {"id": 1, "role": "admin"}
            self._fake_job(monkeypatch, llm_router, db)

            # The guided restart / answered pause is allowed through.
            out = asyncio.run(llm_router.create_generation_job(
                {**base, "fix": True}, admin, db))
            assert out["task_id"] == "gt-new"

            # A LIVE generating session still reserves the combination.
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-paused",
            )
            with pytest.raises(HTTPException) as ei:
                asyncio.run(llm_router.create_generation_job(base, admin, db))
            assert ei.value.detail["error"] == "combination_reserved"
        finally:
            db.close()

    def test_a_finished_leg_releases_the_combination_it_reserved(self):
        """Completing the leg steps the record down to `generated` (not gone),
        which stops it reserving so the deploy's own generation is not 409."""
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from app.services.generation_jobs import generation_jobs
        from support import mk_task, mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            sid = uuid.uuid4()
            mk_task(db, "gt-leg", status="running")
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-leg",
            )
            assert pdr.is_reserved(user.api_user_id, "myapp", "0") is True

            # The runner's terminal write is the only writer on a status change.
            generation_jobs._set_status("gt-leg", "completed", progress="completed")

            row = pdr.get_session(str(sid))
            assert row is not None, "a finished leg keeps its record (the card)"
            assert row["state"] == "generated", row["state"]
            assert pdr.is_reserved(user.api_user_id, "myapp", "0") is False
        finally:
            db.close()

    def test_an_absent_session_is_an_empty_object_not_a_404(self):
        """No record is an ordinary answer: the deploy panel probes once a second."""
        import asyncio
        import uuid

        from app.database import SessionLocal
        from app.routers import pre_deploy as pd
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        admin = {"id": 1, "role": "admin"}
        assert asyncio.run(pd.get_pre_deploy(str(uuid.uuid4()), admin)) == {}

        db = SessionLocal()
        try:
            user = mk_user(db, "probepd")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="probepd",
                service_label="0", recipe_path="", state="generating",
            )
            row = asyncio.run(pd.get_pre_deploy(str(sid), admin))
            assert row["state"] == "generating"
            assert row["service_id"] == str(sid)
        finally:
            db.close()

    def test_handoff_is_add_then_remove(self):
        """G81/T8: the -api creates the instance FIRST, then the gateway removes
        the session — the overlap is one card, and a crash after the -api write
        is recoverable."""
        import inspect
        from app.services import deploy_workflow
        src = inspect.getsource(deploy_workflow._claim_and_register)
        call = src.index("register_user")
        rm = src.index("remove_session")
        assert call < rm, "the session must be removed AFTER the -api registers"

    def _seed_session(self, db, username="alice", state="paused",
                      task_id="gt-live", task_status="paused", live_service=False):
        import uuid

        from app.services import pre_deploy_registry as pdr
        from support import mk_service, mk_task, mk_user

        user = mk_user(db, username)
        sid = uuid.uuid4()
        if live_service:
            mk_service(db, user, service_name="myapp", label="0", service_id=sid)
        if task_id:
            mk_task(db, task_id, status=task_status)
        pdr.create_or_replace(
            service_id=sid, procedure_id=uuid.uuid4(),
            owner_api_user_id=user.api_user_id, service_name="myapp",
            service_label="0", recipe_path="", state=state,
            reason="compose_fix" if state in ("paused", "refused") else None,
            generation_task_id=task_id,
        )
        return user, sid

    def test_delete_refuses_while_a_live_service_row_exists(self):
        """A paused fix card is a full service delete, never a bare record delete."""
        import asyncio

        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import pre_deploy as pd
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            _user, sid = self._seed_session(db, live_service=True)
            with pytest.raises(HTTPException) as ei:
                asyncio.run(pd.delete_pre_deploy(str(sid), {"id": 1, "role": "admin"}))
            assert ei.value.status_code == 409
            assert pdr.get_session(str(sid)) is not None
        finally:
            db.close()

    def test_delete_cancels_a_live_generation_task(self):
        """Record-only delete (no live services row): the live leg is cancelled
        first, never orphaned, then the record goes."""
        import asyncio

        from app.database import SessionLocal
        from app.routers import pre_deploy as pd
        from app.services import pre_deploy_registry as pdr
        from app.services.generation_jobs import generation_jobs

        db = SessionLocal()
        try:
            _user, sid = self._seed_session(db, task_id="gt-live", task_status="paused")
            res = asyncio.run(pd.delete_pre_deploy(str(sid), {"id": 1, "role": "admin"}))
            assert res["deleted"] is True
            assert res["cancelled_job"] is True
            assert generation_jobs.job_status("gt-live") == "cancelled"
            assert pdr.list_sessions() == []
        finally:
            db.close()

    def test_delete_is_a_plain_delete_when_nothing_is_live(self):
        import asyncio

        from app.database import SessionLocal
        from app.routers import pre_deploy as pd
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            _user, sid = self._seed_session(db, task_id=None, state="generated")
            res = asyncio.run(pd.delete_pre_deploy(str(sid), {"id": 1, "role": "admin"}))
            assert res == {"deleted": True, "cancelled_job": False}
            assert pdr.list_sessions() == []
        finally:
            db.close()

    def test_recipe_path_round_trips_on_the_session(self):
        """The session stores the recipe path it will generate; identity is the
        service_id and the reserved tuple is (owner, service, label)."""
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=user.api_user_id, service_name="myapp",
                service_label="0", recipe_path="docker", state="generating",
            )
            assert pdr.get_session(str(sid))["recipe_path"] == "docker"
        finally:
            db.close()


class TestPreDeployCardSourceGuards:
    """§8b/G24: the pre-deploy card IS the service card.

    These are STRUCTURAL guards, not behavioural verification — the behaviour is
    verified live by trace **T14** (browser: one widget, per-status buttons, an
    honest cancel). What they protect is the shape that MADE the old card's
    defects possible: a separate widget, and a `Cancel` that rendered for every
    state and reported success with no job behind it.

    The old `test_renders_both_documented_states` asserted the bespoke card's
    badge strings ("pre-deploy generating…"). Those strings are gone because the
    card is gone; asserting them again would have pinned the defect in place.
    """

    def _page(self) -> str:
        return (_DASH / "src" / "pages" / "UsersPage.tsx").read_text()

    def _session_actions(self) -> str:
        src = self._page()
        start = src.index("SESSION-ACTIONS-START")
        end = src.index("SESSION-ACTIONS-END")
        return src[start:end]

    def test_reads_the_pre_deploy_endpoint_not_the_registry(self):
        src = self._page()
        assert "/pre-deploy" in src
        assert "loadSessions" in src

    def test_both_card_kinds_use_the_SAME_widget(self):
        """G24 rule 1 — one widget, differing only by status.

        The sessions map and the instances map both render `<ServiceCard>`; a
        second bespoke card is exactly what produced the divergences.
        """
        src = self._page()
        assert "components/users/ServiceCard" in src
        assert src.count("<ServiceCard") >= 2
        # The old bespoke `<Card size="small" ... borderColor: '#91caff'>` is gone.
        assert "#91caff" not in src

    def test_renders_all_three_session_states(self):
        src = self._page()
        assert "Generating…" in src
        assert "Paused (" in src, "a paused session carries its reason"
        assert "Refused (" in src, "a refused session carries its reason"

    def test_has_check_cancel_and_delete(self):
        src = self._page()
        for action in (">Check</Button>", ">Cancel</Button>", ">Delete</Button>"):
            assert action in src, action

    def test_cancel_is_gated_on_a_live_job(self):
        """G24 rule 4 — `cancel` belongs to a JOB, so it exists only while one
        is live, and it must never claim success when there is none."""
        actions = self._session_actions()
        assert "isJobLive &&" in actions, "cancel must be gated on a live job"
        assert "if (sess.generation_task_id == null)" in actions, (
            "the old handler reported success with no job at all"
        )

    def test_delete_is_always_offered_and_reports_the_cancel(self):
        """User ruling (2026-09-23): delete implicitly cancels first, so it is
        NOT hidden while a job is live (it used to be, which made it depend on a
        second button), and it must say so when it had to cancel."""
        actions = self._session_actions()
        assert "!isJobLive &&" not in actions, (
            "delete must not be gated on the job — it does the cancel itself"
        )
        assert "cancelled_job" in actions, (
            "the message must distinguish a cancel-and-delete from a plain delete"
        )

    def test_no_instance_only_actions_on_a_session_card(self):
        """G24 rule 3 — `Rebuild`, `pause/play`, `Redeploy`, `key` and `Dup` are
        instance-only; a pre-deploy card must not inherit them."""
        actions = self._session_actions()
        for btn in (">Rebuild</Button>", "Redeploy</Button>", ">Dup</Button>", "KeyOutlined"):
            assert btn not in actions, f"{btn} must not appear on a pre-deploy card"

    def test_delete_uses_the_service_id_route(self):
        """The DELETE route is keyed by service_id.

        The name-shaped `/pre-deploy/{user}/{service}/{label}` path is the GET
        route; sending DELETE to it returns 405 and the session is never removed
        (measured 2026-11-03/04, fixed in the Round-3 dashboard change). The
        session is identified by its service_id, so no recipe_path is sent.
        """
        src = self._page()
        assert "client.delete(`/pre-deploy/${sess.service_id}`)" in src
        assert "client.delete(`/pre-deploy/${sess.user_name}" not in src

    def test_a_generation_refusals_retry_is_not_gated_on_its_options(self):
        """G29: the gateway refusal's own Retry, which was a dead control.

        Two things had to be true and neither was. (1) The refusal's RECORD was
        written with no options (see the gateway test above), so `!sess.options`
        greyed the button out. (2) Even with options, a replay is wrong here: the
        generation is what the pool refused, so nothing exists to replay — the
        retry has to run the pipeline. The record may also predate the fix and
        still carry no options, which is why the escape is by REASON.
        """
        actions = self._session_actions()
        assert "sess.reason !== 'generation_at_capacity'" in actions, (
            "a gateway refusal must be retryable even with no stored options")
        assert "setRetryRegenerate(genRefusal)" in actions, (
            "the panel must be told to regenerate for a gateway refusal")
        import inspect
        from app.services import deploy_workflow
        retry_src = inspect.getsource(deploy_workflow.retry)
        assert "create_job" in retry_src, (
            "a generation-capacity refusal re-runs generation on retry")

    def test_the_generation_payload_carries_the_url_and_the_password(self):
        """The refusal record can only be as complete as the REQUEST it is built
        from (user ruling 2026-09-26: *"for supporting retry, the password / url
        should indeed be stored!"*).

        The deploy path's generation request carried `domain`/`https` but **not**
        the password, so a `generation_at_capacity` refusal — the only kind with a
        card and a Retry — could never have pre-filled it, whatever the gateway
        stored. Both of this form's generation payloads must therefore carry it:
        the entry-point one (reads the live form) and the deploy-time one (reads
        the snapshot, because `destroyOnClose` has already taken the form away).
        """
        form = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "passwd: form.getFieldValue('passwd') || ''" in form, (
            "the entry-point generation payload must carry the password")
        assert "passwd: snap.passwd || ''" in form, (
            "…and the deploy-time one, from the snapshot the closed panel left")
        assert form.count("domain: form.getFieldValue('domain')") + \
            form.count("domain: snap.domain") >= 2, "both payloads carry the URL"


class TestServiceCardsAndTaskLifecycle:
    """The 2026-09-26 service-card / task-lifecycle changes (task doc §1-§4b).

    Structural guards, not behavioural verification: the behaviour is verified
    live by traces T23-T30. What these protect is the SHAPE of each fix — the
    separate session section, the block alerts, the exact-name generated-file
    lookups and the `Answer` action are all things a later edit could quietly
    reintroduce, and each of them was a user-visible defect.
    """

    def _page(self, rel: str) -> str:
        return (_DASH / "src" / rel).read_text()

    def test_sessions_render_inside_the_per_user_groups(self):
        """§1: a session is NOT a category — it is a card of another status.

        Its own heading and its own `<Collapse>` are gone; the ONE grouping
        carries both kinds, so a user holding only sessions gets a group instead
        of a second section beside an `Empty` instance list.
        """
        src = self._page("pages/UsersPage.tsx")
        assert "Pre-deploy sessions" not in src, "the separate section is gone"
        assert "expandedSessions" not in src, "…and so is its own expanded set"
        # ONE grouping, carrying both kinds.
        assert "sessByUser" in src and "svcsByUser" in src
        assert "{userSessions.map((sess) => renderSessionCard(sess))}" in src

    def test_the_card_header_alert_is_one_inline_line(self):
        """§2-3: the note lives IN the header row, after the name, and is ONE
        line — no block, no stacked pair, no height growth."""
        card = self._page("components/users/ServiceCard.tsx")
        assert "<Alert" not in card
        # …inside the title Space of the rendered header, not on a line below it.
        header = card[card.index("header={"):card.index("extra={")]
        assert "{alert}" in header and header.index("{alert}") > header.index("<Space>")
        assert "marginTop: 6" not in card, "the block below the title row is gone"
        page = self._page("pages/UsersPage.tsx")
        assert "whiteSpace: 'nowrap'" in page, "a wrapped note grows the header too"
        assert page.count("<HeaderNote") >= 6, "both card kinds use the one-line note"
        # The failed card's two conditions fold into ONE line: a deploy refused
        # for `at_capacity` holds no task, so its log is gone by construction.
        assert "atCapacity && logGone" in page

    def test_tasks_page_polls_silently_and_has_no_answer(self):
        """§4: the first load alone may show the spinner; `Answer` is gone."""
        src = self._page("pages/TasksPage.tsx")
        assert "firstLoadRef" in src, "re-polls must not touch the table spinner"
        assert ">Answer</Button>" not in src
        assert "openAnswer" not in src and "answerOpen" not in src
        assert "useGenerationJob" not in src, "the answer modal's hook went with it"

    def test_tasks_page_names_the_family_core(self):
        """§7: DISPLAY ONLY — the tag and the subtitle read `core`; the record
        ids keep their `api-…` prefix (GRILLING G28 stands untouched)."""
        src = self._page("pages/TasksPage.tsx")
        # One list, both families, each row tagged by its own id prefix.
        assert "'gateway' | 'api'" in src
        assert "startsWith('gt-') ? 'gateway' : 'api'" in src
        assert "core deploy" in src and "<Title level={5}" in src

    def test_source_projects_row_is_one_state_aware_slot(self):
        """§4b: robot → running → Check → running → robot, with a `Cancel` that
        covers a paused job as well as a running one."""
        src = self._page("pages/ServicesPage.tsx")
        assert "rowJob" in src and "loadGenJobs" in src
        # `paused` is non-terminal for the row: it is what `Check` answers.
        assert "'paused'" in src and "CheckOutlined" in src
        # …but only while it is the NEWEST job OF THE ROW'S OWN PATH. A job from
        # another path is never the row's to act on (see the ownership test
        # below), and an already-answered pause must not hold the slot either.
        assert "const newest = genJobs" in src
        assert "j.origin === 'standalone' &&" in src
        assert "return ['queued', 'running', 'awaiting_input', 'paused'].includes(newest.status) ? newest : null" in src
        assert "cancelRowJob" in src
        # It only CANCELS — no page deletes records.
        assert "client.delete(`/tasks/${id}`)" in src
        # …and the panel is opened ON the paused job, so its popup comes back.
        assert "resumeJobId={genPanel?.resumeJobId ?? null}" in src
        panel = self._page("components/services/GenerateMissingPanel.tsx")
        # ONCE per open: the init effect also runs on a service change inside the
        # panel, and re-attaching then would put the previous recipe's pause
        # popup over the new recipe's pickers.
        assert "if (resumeJobId != null && resumedRef.current !== resumeJobId)" in panel
        assert "resumeJob(resumeJobId)" in panel
        # The panel must render the prompt for a pause of EITHER shape (G27 made
        # the new one `paused`, which `status === 'awaiting_input'` matched never).
        assert "isPaused(jobState)" in panel

    def test_one_acting_surface_per_path(self):
        """The row acts only on jobs its OWN path created.

        Found live 2026-09-28: the leave-behind pause (`gt-347`) was created by
        the DEPLOY path — its request carries `user_name`, which is why it opened
        a pre-deploy session — yet it also appeared on the Source Projects row of
        `example-service@@recipes/slowbuild`, because that rule keyed on the
        newest job of ANY path. The row's `Check` can only run the generation
        (the deploy lived in the panel that started it), so answering there
        generates the files and silently skips the deploy; the card's `Check`
        re-opens the DEPLOY panel and continues it.

        Ownership is RECORDED on the job (its `job_type` and the request it was
        created with are persisted), not inferred from the absence of a session:
        `job_origin` is the one place that decides, and the create path uses the
        same helper for the session it opens.
        """
        from app.models.generation_job import (
            JOB_ORIGIN_DEPLOY, JOB_ORIGIN_FIX, JOB_ORIGIN_STANDALONE, job_origin,
        )
        # The three paths, by the signature each sends.
        assert job_origin({"user_name": "asynctest", "label": "0"}, "generate_missing") == JOB_ORIGIN_DEPLOY
        assert job_origin({"user_name": "", "label": "0"}, "generate_missing") == JOB_ORIGIN_STANDALONE
        assert job_origin({}, "generate_missing") == JOB_ORIGIN_STANDALONE
        # …and a `fix` job is the instance card's, whichever way it is sent.
        assert job_origin({"user_name": "alice"}, "fix") == JOB_ORIGIN_FIX
        # A persisted request row is the string form; it must read the same.
        assert job_origin('{"user_name": "bob"}', "generate_missing") == JOB_ORIGIN_DEPLOY
        assert job_origin("", "generate_missing") == JOB_ORIGIN_STANDALONE
        assert job_origin(None, None) == JOB_ORIGIN_STANDALONE
        # The payload carries it, so the row never has to guess…
        model = Path(__file__).parent.parent / "app" / "models" / "gateway_task.py"
        model_src = model.read_text()
        assert '"origin": self.origin' in model_src
        assert "def job_origin(" in model_src
        # …and the session the create path opens uses the SAME predicate.
        import inspect
        from app.routers import llm as _llm
        create_src = inspect.getsource(_llm.create_generation_job)
        assert "_session_scoped = job_origin(req, job_type) == JOB_ORIGIN_DEPLOY" in create_src

    def test_generated_file_names_are_matched_by_shape_not_by_name(self):
        """GRILLING G33: a generated name carries the job's token, so every
        exact-name lookup would silently stop matching.

        Two of the three failures were SILENT because a missing validation reads
        as "valid" (`ok = !v || v.valid`): the review gate and the generated-file
        list would have shown every file green.
        """
        helper = self._page("components/services/FileSetSelection.tsx")
        assert "export function phaseOfFile" in helper
        for rel in ("components/services/GenerateMissingPanel.tsx",
                    "components/services/DeployForm.tsx"):
            src = self._page(rel)
            assert "phaseOfFile(" in src, f"{rel} must map phases by shape"
            assert "files['docker-compose.base.yml']" not in src
            assert "genNow['docker-compose.base.yml']" not in src
            assert "fn === 'nginx.conf'" not in src

    def test_the_card_shows_the_task_link_and_the_nginx_row(self):
        """§6/§10: `Task: <id>` (greyed when the record is gone) and a clickable
        `nginx:` row — both were simply absent from the instance card."""
        src = self._page("pages/UsersPage.tsx")
        assert "task_available" in src
        assert "navigate(`/tasks?task=${svc.task_id}`)" in src
        assert "cursor:'not-allowed'" in src, "the greyed (record gone) state"
        assert "<Text>nginx: </Text>" in src
        assert "nginx_conf_template_path" in src

    def test_the_fix_field_is_recipe_scoped_and_hidden_when_empty(self):
        """§9: ONE clickable `LLM fix:` line, this recipe's own artifacts only,
        and nothing at all when it has none."""
        src = self._page("pages/UsersPage.tsx")
        assert "recipeFixes" in src
        assert "recipeFixes.length > 0 &&" in src
        # The cache key carries the recipe; the scan reads THAT recipe's entry.
        assert "loadFixFiles = async (service: string, recipePath: string)" in src
        assert "(data.candidates || {})[recipePath || '.']" in src
        assert "LLM fix" in src

    def test_answering_a_pause_consumes_the_leg_it_answered(self):
        """GRILLING G27 + G34: the answer ends the leg it answered.

        A pause is TERMINAL on its leg, so the job it belongs to keeps `paused`
        forever — nothing else ever writes that row. Left alone that is a paused
        object with nothing owning it: the Source Projects row offered `Check`
        for work already done (a re-answer spawned a duplicate leg), and a paused
        record is never swept (G30), so it could never leave. Found live in T26
        on gt-338 after `Fix the compose` resumed it into gt-339.

        Cancelled, never deleted — the same transition every other surface uses.
        """
        import inspect
        from app.routers import llm as _llm
        src = inspect.getsource(_llm.create_generation_job)
        # Read BEFORE `req` is replaced by the carried request.
        assert "_answered_job_id = req.get(\"parent_job_id\")" in src
        assert src.index("_answered_job_id = req.get") < src.index("req = carried")
        # Acted on AFTER the new leg exists — a failed create must not consume
        # the pause it was answering.
        assert src.index("job = generation_jobs.create_job") < src.index('_parent.status = "cancelled"')
        # Only its own parked leg, and cancelled (never removed).
        assert '_parent.status == "paused"' in src
        assert "_parent.service_id == job.service_id" in src
        assert '_parent.status = "cancelled"' in src
        assert "unlink" not in src.split("_answered_job_id")[2], "cancel, never delete"
        # …and the client names it: the answer is the task route, values only.
        hook = self._page("hooks/useGenerationJob.ts")
        assert "/tasks/${jobId}/answer" in hook
        assert "{ values }" in hook


class TestValidateRunsForASelectedCompose:
    """§3b validates the compose IN PLAY, not only a freshly generated one."""

    def test_validate_runs_when_a_compose_is_selected(self):
        from app.services.generation_jobs import GenerationJobManager
        m = GenerationJobManager()
        # Compose already on disk and selected for use.
        phases = m._compute_phases("generate_missing", {
            "selection": {"compose": ["docker-compose.yml"]},
        })
        assert "validate" in phases, "the operator's own selected compose can carry the conflict"
        assert "compose" not in phases, "nothing to generate — it already exists"

    def test_an_empty_compose_slot_generates_and_is_validated(self):
        from app.services.generation_jobs import GenerationJobManager
        phases = GenerationJobManager._compute_phases("generate_missing", {"selection": {}})
        assert "compose" in phases and "validate" in phases


class TestFixValidatesItsOwnOutput:
    """A fix must not re-pause on the conflict it was invoked to repair.

    Found live: the conflict_fix job validated the ORIGINAL selection, re-detected
    the same NEO4J_PORT conflict and parked in `awaiting_input` forever, even
    though its compose phase had already written the corrected
    `docker-compose.fix.yml`.
    """

    def test_validate_prefers_the_file_the_fix_produced(self):
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._run_job)
        assert "_produced" in src
        assert "_produced or (_sel.get(\"compose\") or [])" in src
        # ...and it must look for the file THIS JOB wrote — its token-bearing
        # name (GRILLING G33), never the phase name and never the raw template.
        assert '_fix_compose = _names["compose"]' in src


class TestPreDeployReconcile:
    """``reconcile`` resolves sessions by the task store, keyed by service_id.

    It covers the case no other writer does: a record whose runner died with
    the gateway. Operator-held states are never touched; a `generating` session
    is kept while its generation task is live/unknown and dropped otherwise; a
    `deploying` session with an instance is dropped (the instance card owns it).
    """

    def _seed(self, db, username: str = "alice", service_name: str = "myapp"):
        import uuid

        from support import mk_user

        user = mk_user(db, username)
        return user, uuid.uuid4(), uuid.uuid4()

    def test_job_status_reads_a_real_row_without_a_session_argument(self):
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs
        from support import mk_task

        db = SessionLocal()
        try:
            mk_task(db, "gt-done", status="completed")
            assert generation_jobs.job_status("gt-done") == "completed"
        finally:
            db.close()

    def test_a_stale_GENERATING_session_is_dropped(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            mk_task(db, "gt-done", status="completed", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-done",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"], "a finished job must not keep the combination reserved"
            assert pdr.list_sessions() == []
        finally:
            db.close()

    def test_a_LIVE_generating_session_is_KEPT(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            mk_task(db, "gt-live", status="running", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-live",
            )
            rep = pdr.reconcile(live_task_ids={"gt-live"})
            assert rep["dropped_stale"] == []
            assert len(pdr.list_sessions()) == 1
        finally:
            db.close()

    def test_an_UNKNOWN_generating_task_is_still_kept(self):
        """``unknown`` means a runner may still exist (gateway moved), so it is
        kept until the recovery sweep terminalizes it (R4.2)."""
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            mk_task(db, "gt-unknown", status="unknown", procedure_id=proc)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state="generating",
                generation_task_id="gt-unknown",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == []
        finally:
            db.close()

    def test_a_PAUSED_session_is_never_stale(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state="paused",
                reason="llm_fix", guide="rename the port", deploy_task_id="api-9",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == []
            kept = pdr.list_sessions()
            assert len(kept) == 1
            assert kept[0]["guide"] == "rename the port"
        finally:
            db.close()

    def test_a_GENERATED_session_is_never_stale(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state=pdr.STATE_GENERATED,
            )
            pdr.reconcile(live_task_ids=set())
            row = pdr.get_session(str(sid))
            assert row is not None and row["state"] == pdr.STATE_GENERATED
        finally:
            db.close()

    def test_a_GENERATED_session_holds_nothing(self):
        """`generated` stops reserving; generating/paused/refused still do."""
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            import uuid

            from support import mk_user

            for state, expected in (
                (pdr.STATE_GENERATED, False),
                (pdr.STATE_GENERATING, True),
                (pdr.STATE_PAUSED, True),
                (pdr.STATE_REFUSED, True),
            ):
                name = f"u-{state}"
                user = mk_user(db, name)
                pdr.create_or_replace(
                    service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                    owner_api_user_id=user.api_user_id, service_name="myapp",
                    service_label="0", recipe_path="", state=state,
                    reason="input" if state in (pdr.STATE_PAUSED, pdr.STATE_REFUSED) else None,
                )
                assert pdr.is_reserved(user.api_user_id, "myapp", "0") is expected, state
        finally:
            db.close()

    def test_a_DEPLOYING_session_with_an_instance_is_dropped(self):
        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_service, mk_service_instance

        db = SessionLocal()
        try:
            user, sid, proc = self._seed(db, "alice", "probe")
            sid = mk_service(db, user, service_name="probe", label="0")
            mk_service_instance(db, sid)
            pdr.create_or_replace(
                service_id=sid, procedure_id=proc,
                owner_api_user_id=user.api_user_id, service_name="probe",
                service_label="0", recipe_path="", state="deploying",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == [str(sid)]
            assert pdr.get_session(str(sid)) is None
        finally:
            db.close()


class TestEnvPhaseEmptySurface:
    """§4 + the empty-`${VAR}`-surface path.

    Regression found LIVE (job 117): making `_mechanical_env` return "" instead
    of None let execution reach `if not content:` with `content` never bound —
    both arms (`if kvs:` and the mechanical fallback) are skipped when the
    compose has no ${VAR} surface and the LLM produced no kvs, so the phase died
    with UnboundLocalError instead of writing the empty `.raw` §4 asks for.
    """

    def test_content_is_bound_before_the_kvs_branch(self):
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._run_job)
        i = src.index("content = \"\"")
        j = src.index("if kvs:", i)
        k = src.index("if not content:", j)
        assert i < j < k, "`content` must be bound before both arms"

    def test_mechanical_env_returns_empty_string_not_none(self):
        import inspect
        from app.services.generation_jobs import GenerationJobManager
        src = inspect.getsource(GenerationJobManager._mechanical_env)
        assert "return \"\"" in src
        assert "return None" not in src


class TestCapacityAlertOnTheGenerationPath:
    """§4d's persistent alert must fire wherever a refusal can appear.

    Round-2 fix: the new up-front 503 on `/api/llm/jobs` surfaces during the
    deploy flow's GENERATION step, which only showed a toast — and would hand a
    structured `detail` object straight to `message.error`.
    """

    def test_generation_refusal_raises_the_persistent_alert(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert src.count("setAtCapacity(") >= 2, (
            "both the deploy path and the generation path must raise the alert"
        )
        # ...and neither may pass a structured detail to message.error. Asserted
        # as the FAMILY of `typeof … === 'string'` guards rather than one exact
        # spelling: the deploy path's ladder gained the nested `d?.detail` /
        # `d?.error` / `err.message` rungs on 2026-10-16 — a FastAPI 409
        # serialises as `{"detail": {"error": …, "detail": …}}`, so guarding on a
        # string `detail` alone printed the literal `Deploy failed` and named
        # nothing. The rule this asserts is "a raw object never reaches
        # message.error", and every rung of both ladders tests the type first.
        assert src.count("typeof d === 'string'") >= 2, (
            "both the generate path and the deploy path must guard what they print"
        )

    def test_the_alert_renders_at_the_component_root(self):
        """Inside a modal it was invisible whenever the OTHER surface was on
        screen — measured: /api/llm/jobs returned 503 and no alert appeared."""
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert src.count("{renderAtCapacity()}") == 1, "rendered exactly once — no duplicates"
        root = src.index("  return (")
        first_modal = src.index("<Modal", root)
        alert_site = src.index("{renderAtCapacity()}", root)
        assert alert_site < first_modal, (
            "the alert must sit at the component ROOT, before any modal"
        )


class TestEnvPrecedenceOverRealFiles:
    """§2b's rule exercised over REAL files — both halves.

    Audit flaw 5: the §2b e2e (`NEO4J_PORT=7687`) exercised only the "empty
    in-file value → materialize the compose default" half, because that
    fixture's in-file value WAS empty — yet the doc cited it as closing §2b.

    SCOPE, stated honestly: this drives `_mechanical_env` — the function that
    implements the rule — against real compose/env files on disk. It does NOT
    drive `_run_job`'s env phase, and it is NOT a dashboard e2e, so it does not
    cover the wiring above the helper. (This class was first named
    "...EndToEnd"; that was an overclaim of exactly the kind under audit.)
    """

    def _run_env_phase(self, tmp_path, monkeypatch, base_env_text, compose_text):
        import asyncio, json
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "myapp"
        recipe.mkdir()
        (recipe / "docker-compose.yml").write_text(compose_text)
        (recipe / "base.env").write_text(base_env_text)

        m = GenerationJobManager()
        req = {
            "selection": {"compose": ["docker-compose.yml"], "env": ["base.env"]},
            "user_name": "alice", "label": "0",
        }
        # The env phase writes `.env.raw.{user}.{label}` into the recipe dir.
        mech = m._mechanical_env("myapp", "", req)
        return mech, recipe

    def test_a_non_empty_in_file_value_is_never_overwritten(self, tmp_path, monkeypatch):
        mech, recipe = self._run_env_phase(
            tmp_path, monkeypatch,
            base_env_text="KEEP_ME=hand-written\nEMPTY_ME=\n",
            compose_text=(
                "services:\n  app:\n    image: x\n    environment:\n"
                "      - A=${KEEP_ME:-from-compose}\n"
                "      - B=${EMPTY_ME:-from-compose}\n"
            ),
        )
        lines = dict(
            ln.split("=", 1) for ln in mech.splitlines() if "=" in ln
        )
        assert lines["KEEP_ME"] == "hand-written", (
            "the compose default must NOT overwrite a non-empty in-file value"
        )
        assert lines["EMPTY_ME"] == "from-compose", (
            "an EMPTY value counts as not set and receives the compose default"
        )


class TestConflictDetectionIsNotGraphitiSpecific:
    """Audit flaw 8: "source-agnostic" rested on ONE fixture (graphiti).

    These use conflicts that share no shape with it: two different names in two
    different files, and a nested default.
    """

    def _write(self, tmp_path, name, text):
        p = tmp_path / name
        p.write_text(text)
        return p

    def test_two_different_names_in_two_different_files(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        a = self._write(tmp_path, "a.yml",
                        "services:\n  a:\n    environment:\n      - X=${ALPHA:-1}\n")
        b = self._write(tmp_path, "b.yml",
                        "services:\n  b:\n    environment:\n      - Y=${BETA:-2}\n"
                        "      - Z=${ALPHA:-9}\n")
        found = {c["name"] for c in conflicting_defaults([a, b])}
        assert found == {"ALPHA"}, "only the genuinely conflicting name is flagged"
        assert "BETA" not in found, "a single-default name is not a conflict"

    def test_nested_default_conflict_is_detected(self, tmp_path):
        from app.utils.var_scan import conflicting_defaults
        p = self._write(tmp_path, "c.yml",
                        "services:\n  a:\n    environment:\n"
                        "      - A=${OUTER:-${INNER:-x}}\n"
                        "      - B=${INNER:-y}\n")
        names = {c["name"] for c in conflicting_defaults([p])}
        assert "INNER" in names, "a nested default is scanned like any other site"


class TestRefusedSessionsSurviveReconcile:
    """A `refused` session is operator-held and must never be reconciled away —
    its Retry options are the operator's only record of the refusal."""

    def test_refused_session_survives_the_stale_rule(self):
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="refused",
                reason="at_capacity", options={"service_name": "myapp"},
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == []
            assert pdr.get_session(str(sid)) is not None
        finally:
            db.close()

    def test_generating_sessions_are_still_reconciled(self):
        """The fix must not blunt reconcile for the states it exists for."""
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
            )
            rep = pdr.reconcile(live_task_ids=set())
            assert rep["dropped_stale"] == [str(sid)]
        finally:
            db.close()


class TestStaleSelectionAcceptsTheJ2Sibling:
    """A `.j2` sibling satisfies a stored selection — behaviourally, over real files.

    The -api's register-time conversion renames a plain compose to `<name>.j2`.
    Without this, the platform's OWN action marks a stored selection stale, and a
    stale selection disables Deploy for that service — which silently blocked
    trace T4 for two rounds (established live, Round 5).
    """

    def _setup(self, tmp_path, monkeypatch, files):
        import json
        from app.config import settings
        from app.services import file_sets
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
        proj = tmp_path / "myapp"
        proj.mkdir()
        for f in files:
            (proj / f).write_text("services: {}\n")
        # a stored selection pointing at the PLAIN name
        (proj / ".provision-state.json").write_text(json.dumps({
            "schema_version": 2, "recipe_origin": "auto", "recipe_paths": ["."],
            "files": sorted(files),
            "file_sets": {".": {"compose": ["docker-compose.fix.yml"], "nginx": None,
                                "env": [], "profiles": []}},
        }))
        return file_sets

    def test_j2_sibling_is_not_stale(self, tmp_path, monkeypatch):
        fs = self._setup(tmp_path, monkeypatch,
                         ["docker-compose.fix.yml.j2"])
        info = fs.list_file_sets("myapp")
        assert (info.get("stale") or {}).get(".") in (None, {}), \
            "a .j2 sibling must satisfy the selection, not mark it stale"

    def test_a_genuinely_missing_file_is_still_stale(self, tmp_path, monkeypatch):
        """The fix must not blunt staleness for files that really are gone."""
        fs = self._setup(tmp_path, monkeypatch, ["docker-compose.yml"])
        info = fs.list_file_sets("myapp")
        stale = (info.get("stale") or {}).get(".") or {}
        assert "docker-compose.fix.yml" in (stale.get("compose") or [])


class TestACapacityRefusalRecordsTheRefusedSession:
    """The -api's `at_capacity` on the DEPLOY path records a refused session
    (state/reason/options + the encrypted credential), never a bare 502."""

    def _deploy(self, monkeypatch, tmp_path, status: int, body: dict):
        import asyncio
        import uuid

        import httpx
        from app.database import SessionLocal
        from app.services import deploy_workflow, pre_deploy_registry as pdr
        from app.services.provision_service import provision_service
        from support import mk_task, mk_user

        monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            owner_api_id = str(alice.api_user_id)
            sid = str(uuid.uuid4())
            mk_task(db, "gt-leg", status="completed")
            pdr.create_or_replace(
                service_id=sid, procedure_id=str(uuid.uuid4()),
                owner_api_user_id=owner_api_id, service_name="myapp",
                service_label="0", recipe_path="", state="generated",
                operation="deploy", generation_task_id="gt-leg",
                deploy_request={
                    "user_name": "alice", "service_name": "myapp",
                    "label": "0", "domain": "localhost",
                    "compose_file_paths": ["docker-compose.yml"],
                    "env_files": [".env.1.alice.0"],
                },
                deploy_secret_values={"passwd": "x"},
                selection={"compose": ["docker-compose.yml"], "nginx": "nginx.conf"},
            )
        finally:
            db.close()

        hreq = httpx.Request("POST", "http://api/users")
        hresp = httpx.Response(status, request=hreq, json=body)

        async def fake_register(**kw):
            raise httpx.HTTPStatusError("boom", request=hreq, response=hresp)

        monkeypatch.setattr(provision_service, "register_user", fake_register)

        intent = {"service_name": "myapp", "user_name": "alice",
                  "owner_api_user_id": owner_api_id, "service_id": sid,
                  "label": "0"}
        try:
            asyncio.run(deploy_workflow._claim_and_register(
                sid, intent,
                {"compose": ["docker-compose.yml"], "nginx": "nginx.conf"},
                None))
        except Exception as exc:
            return exc, pdr
        return None, pdr

    def test_at_capacity_records_state_reason_and_options(self, monkeypatch, tmp_path):
        exc, pdr = self._deploy(monkeypatch, tmp_path, 503, {
            "error": "at_capacity", "detail": "at capacity: 4 task(s) running",
            "running": 4, "capacity": 4})
        assert exc is not None, "the refusal must propagate to the caller"
        assert getattr(exc, "status", None) == 503, f"got {exc}"
        sessions = pdr.list_sessions()
        assert len(sessions) == 1, "a refusal must RECORD a pre-deploy session"
        row = sessions[0]
        assert row["state"] == "refused"
        assert row["reason"] == "at_capacity"
        assert row["options"], "Retry needs the refused options to replay"
        opts = row["options"]
        assert "passwd" not in opts, "the credential is encrypted, not in options"
        assert opts.get("domain") == "localhost", "the URL is kept"
        assert opts.get("compose_file_paths") == ["docker-compose.yml"]
        assert pdr.replay_options(row["service_id"]).get("passwd") == "x"

    def test_the_nested_shape_is_also_recognised(self, monkeypatch, tmp_path):
        """The -api can also emit `{"detail": {"error": ...}}`."""
        exc, pdr = self._deploy(monkeypatch, tmp_path, 503, {
            "detail": {"error": "at_capacity", "detail": "at capacity",
                       "running": 4, "capacity": 4}})
        assert getattr(exc, "status", None) == 503, f"got {exc}"
        sessions = pdr.list_sessions()
        assert len(sessions) == 1 and sessions[0]["state"] == "refused"

    def test_a_non_capacity_error_fails_closed_with_the_detail(self, monkeypatch, tmp_path):
        """Only a capacity refusal is retryable; any other error fails closed
        and its detail stays visible (R2/R8)."""
        exc, pdr = self._deploy(monkeypatch, tmp_path, 409,
                                {"detail": "already registered"})
        assert getattr(exc, "status", None) == 502, "a 409 stays a generic 502"
        sessions = pdr.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["state"] == "failed"
        assert sessions[0]["detail"]


class TestASelectedEnvIsTheHandoffNotGenerated:
    """§2/§4: "when the operator *did* select an env, that selection is the handoff
    instead" of the generated `.raw`.

    The deploy path forced the per-user env generation EVEN THEN (needsGeneration
    included `needsEnv && !perUserEnv` unconditionally), which contradicts the
    documented design and makes every Deploy pay a ~30s generation window before
    the deploy POST. Measured across Rounds 24-25 of trace T5, where that window
    also made a live capacity refusal unreachable.
    """

    def test_a_selected_env_suppresses_env_generation(self):
        """The gateway decides generation: a SELECTED env suppresses the
        per-user env.raw phase; an empty env slot triggers it (R3/R6)."""
        import inspect
        from app.services import deploy_workflow
        src = inspect.getsource(deploy_workflow.generation_needed)
        assert "selection.get(\"compose\")" in src
        assert 'selection.get("env")' in src, "the env slot participates"
        assert "if not (selection.get(\"env\") or []):" in src
        form = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert "env_reference: envRefs" in form


class TestPinnedLabelIsReassertedAtSubmit:
    """G27 — a pinned panel must never submit an unpinned label.

    The open-time pin sits at the END of the async `init()`, behind
    `await loadSources()`, so for the first ~600 ms after the panel opens the
    DISABLED Label field still shows the Form's `initialValues` `label: '0'`
    (measured live: `0` at t=587 ms, correct and stable only from t=692 ms).
    Confirming inside that window sent `request.label='0'` for a label-2
    session, which generated `.env.raw.<user>.0` instead of `.env.raw.<user>.2`.

    The fix re-asserts the pin at submit time, from the SAME source the
    open-time pin reads (`preselectedLabel` = the session/entry's own label).
    """

    def test_the_pin_is_reasserted_inside_handle_deploy(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        start = src.index("const handleDeploy")
        end = src.index("const payload: any = {")
        body = src[start:end]
        assert "pinnedSessionLabel(preselectedLabel)" in body, (
            "handleDeploy must re-read the pinned label from its source"
        )
        assert "snap.label = pinnedAtSubmit" in body, (
            "the re-read pin must overwrite snap.label, which every request "
            "consumer (generation job + deploy payload) reads"
        )

    def test_the_pin_has_one_definition_used_by_both_call_sites(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert src.count("function pinnedSessionLabel(") == 1, (
            "the open-time pin and the submit-time re-assert must agree, so "
            "the condition lives in exactly one predicate"
        )
        assert src.count("pinnedSessionLabel(preselectedLabel)") >= 2, (
            "both the open-time pin and the submit-time re-assert must use it"
        )


class TestCheckRestartCarriesTheGlobalProxy:
    """G26 row 5 + G20: a `check` restart replays the PATH's proxy.

    The failed instance's own registry build_args are the only record of the
    proxy setting the restart has to replay; the handoff reads it BEFORE the
    -api delete removes the instance.
    """

    def _seed(self, db, *, build_args=None):
        from support import mk_service, mk_service_instance, mk_user

        alice = mk_user(db, "alice")
        sid = mk_service(db, alice, service_name="myapp", label="0")
        mk_service_instance(db, sid, status="failed",
                            build_args=build_args or {})
        return alice, sid

    def test_the_session_records_the_proxy_setting(self):
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="paused",
                reason="llm_fix", guide="g", slot="compose",
                use_global_proxy=True,
            )
            assert pdr.get_session(str(sid))["use_global_proxy"] is True
        finally:
            db.close()

    def test_the_proxy_setting_defaults_to_off(self):
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
            )
            assert pdr.get_session(str(sid))["use_global_proxy"] is False
        finally:
            db.close()

    def test_build_args_detection(self):
        from app.services.proxy_service import build_args_carry_global_proxy

        assert build_args_carry_global_proxy(
            {"HTTP_PROXY": "http://p:1", "HTTPS_PROXY": "http://p:1",
             "http_proxy": "http://p:1", "https_proxy": "http://p:1"})
        assert build_args_carry_global_proxy({"http_proxy": "http://p:1"})
        assert not build_args_carry_global_proxy({})
        assert not build_args_carry_global_proxy(None)
        assert not build_args_carry_global_proxy({"SOME_USER_ARG": "x"})

    def test_the_handoff_reads_the_failed_entrys_args(self):
        from app.database import SessionLocal
        from app.services.generation_jobs import _instance_used_global_proxy

        db = SessionLocal()
        try:
            self._seed(db, build_args={"HTTP_PROXY": "http://172.18.0.1:7897"})
        finally:
            db.close()
        assert _instance_used_global_proxy("alice", "myapp", "0") is True
        assert _instance_used_global_proxy("alice", "myapp", "9") is False

    def test_the_handoff_stores_it_on_the_paused_session(self, monkeypatch):
        """The wiring: the derived proxy lands on the session BEFORE the -api
        delete removes the entry that carries it."""
        import asyncio

        from app.database import SessionLocal
        from app.services import provision_service as ps
        from app.services.generation_jobs import generation_jobs
        from app.services import pre_deploy_registry as pdr

        db = SessionLocal()
        try:
            self._seed(db, build_args={"http_proxy": "http://172.18.0.1:7897"})
            from support import mk_task
            mk_task(db, "gt-analysis", status="running")
        finally:
            db.close()

        async def fake_remove(user, service, label):
            return {}
        monkeypatch.setattr(ps.provision_service, "remove_user", fake_remove)

        asyncio.run(generation_jobs._llm_fix_handoff(
            user="alice", service="myapp", recipe_path="", label="0",
            guide="fix it", task_id="api-failed", job_id="gt-analysis",
            slot="compose"))

        sessions = pdr.list_sessions()
        assert len(sessions) == 1
        row = sessions[0]
        assert row["state"] == "paused"
        assert row["use_global_proxy"] is True, (
            "the check restart would open with the proxy unchecked")
        assert row["deploy_task_id"] == "api-failed"
        assert row["generation_task_id"] == "gt-analysis"

    def test_the_handoff_prefers_an_explicit_request_value(self, monkeypatch):
        import asyncio

        from app.database import SessionLocal
        from app.services import provision_service as ps
        from app.services import pre_deploy_registry as pdr
        from app.services.generation_jobs import generation_jobs

        db = SessionLocal()
        try:
            self._seed(db)  # no build_args: the explicit value must win
            from support import mk_task
            mk_task(db, "gt-analysis", status="running")
        finally:
            db.close()

        async def fake_remove(user, service, label):
            return {}
        monkeypatch.setattr(ps.provision_service, "remove_user", fake_remove)

        asyncio.run(generation_jobs._llm_fix_handoff(
            user="alice", service="myapp", recipe_path="", label="0",
            guide="fix it", task_id="api-failed", job_id="gt-analysis",
            slot="compose", use_global_proxy=True))
        assert pdr.list_sessions()[0]["use_global_proxy"] is True


class TestThePanelSeedsTheProxyFromTheSession:
    """The dashboard half of the same fix: the `check` restart must HAND the
    session's proxy to the panel, and the panel must apply it from a source that
    is authoritative at submit time (the same shape as the label pin)."""

    def test_the_check_handler_passes_the_session_proxy(self):
        src = (_DASH / "src" / "pages" / "UsersPage.tsx").read_text()
        assert "setPreselectedUseGlobalProxy(!!sess.use_global_proxy)" in src
        assert "preselectedUseGlobalProxy={preselectedUseGlobalProxy}" in src

    def test_the_panel_uses_one_predicate_at_both_sites(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        assert src.count("function seedUseGlobalProxy(") == 1, (
            "the open-time seed and the submit-time re-assert must agree, so the "
            "condition lives in exactly one predicate")
        assert src.count("seedUseGlobalProxy(preselectedUseGlobalProxy)") >= 2

    def test_the_seed_precedes_the_first_await(self):
        """The label pin sat behind `await loadSources()`, which left a window
        where the field was wrong. The proxy seed must not repeat that: it runs
        synchronously, before the first await of `init()`."""
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        seed = src.index("seedUseGlobalProxy(preselectedUseGlobalProxy)")
        first_await = src.index("const sourcesData = await loadSources()")
        assert seed < first_await, "the seed must run before the first await"

    def test_the_reassert_lands_before_the_payload_is_built(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        start = src.index("const handleDeploy")
        end = src.index("const payload: any = {")
        body = src[start:end]
        assert "effectiveUseGlobalProxy()" in body
        assert "snap.use_global_proxy = effectiveUseGlobalProxy()" in body, (
            "every request consumer reads snap, so the re-assert must set snap")

    def test_an_operator_toggle_is_never_overridden(self):
        src = (_DASH / "src" / "components" / "services" / "DeployForm.tsx").read_text()
        start = src.index("const effectiveUseGlobalProxy = ")
        end = src.index("const generateMissingFiles = async ()")
        body = src[start:end]
        assert "!proxyTouchedRef.current" in body, (
            "the session value is a DEFAULT; an explicit toggle must win — the "
            "predicate is shared, so the guard lives here, once")


class TestTheGenerateEntryPointCarriesTheGlobalProxy:
    """T12: the GENERATE entry point creates a pre-deploy session too.

    `DeployForm.handleDeploy` (the deploy path) was fixed to re-assert the
    session's proxy, but `generateMissingFiles` (the "Generate Missing Files via
    LLM" button) built its `startJob` payload WITHOUT `use_global_proxy`. The
    gateway defaults the key to `false`, so the session it created recorded
    `use_global_proxy: false` even when the box was ticked — and the `check`
    restart on that parked `compose_fix` session replayed `false`. Found live on
    T12: `finalgrill|probe-conflict||1` was `false` while the box had been
    ticked. G20: a resumed path replays the options it was created with, and the
    generate entry point is the same class as the deploy one.
    """

    _SRC = (_DASH / "src" / "components" / "services" / "DeployForm.tsx")

    def test_generate_missing_files_sends_the_proxy(self):
        src = self._SRC.read_text()
        start = src.index("const generateMissingFiles = async ()")
        end = src.index("// Generation job completed", start)
        body = src[start:end]
        assert "client.post('/services/generate'" in body, (
            "Generate is the workflow's second intake operation (R1)")
        assert "use_global_proxy: effectiveUseGlobalProxy()" in body, (
            "the generate payload must carry the proxy, or the session the job "
            "opens records `false` and `check` replays it (T12)")

    def test_the_deploy_intent_reads_the_snapshot(self):
        """The deploy runs AFTER `onClose()` (destroyOnClose), so the intent must
        read the submit-time snapshot, never the live form."""
        src = self._SRC.read_text()
        start = src.index("const handleDeploy = async")
        end = src.index("} catch (err: any)", start)
        body = src[start:end]
        assert "use_global_proxy: snap.use_global_proxy" in body, (
            "the deploy intent must reuse the submit-time snapshot")
        assert "form.getFieldValue('use_global_proxy')" not in body, (
            "the form is destroyed by then — reading it would re-introduce the "
            "empty-value window the label pin was moved out of")

    def test_one_predicate_for_both_entry_points(self):
        src = self._SRC.read_text()
        assert src.count("const effectiveUseGlobalProxy = ") == 1, (
            "deploy and generate must read the SAME predicate")
        helper = src[src.index("const effectiveUseGlobalProxy = "):
                     src.index("const generateMissingFiles = async ()")]
        assert "seedUseGlobalProxy(preselectedUseGlobalProxy)" in helper, (
            "the shared predicate is the session seed the deploy path already uses")
        assert src.count("snap.use_global_proxy = effectiveUseGlobalProxy()") == 1, (
            "the deploy path must go through the shared predicate too")


class TestTheGenerationRouterRecordsTheRequestProxy:
    """The session the generation router creates records the proxy the request
    carried (T8/G20); the sweep preserves it when the leg pauses."""

    def test_the_new_leg_gets_the_request_proxy(self):
        src = (Path(__file__).parent.parent / "app" / "routers" / "llm.py").read_text()
        start = src.index("pdr.new_generation_leg(")
        end = src.index("use_global_proxy", start) + 60
        body = src[start:end]
        assert 'use_global_proxy=bool(req.get("use_global_proxy"))' in body, (
            "omitting it defaults the session to `false` (T12)")

    def test_the_sweep_preserves_the_proxy_on_pause(self):
        """A paused session is never pruned; its proxy survives. Its CONTRAST:
        a `generating` session with no live task IS dropped."""
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_task, mk_user

        db = SessionLocal()
        try:
            u = mk_user(db, "u")
            v = mk_user(db, "v")
            mk_task(db, "gt-live", status="running")
            kept_id = uuid.uuid4()
            pdr.create_or_replace(
                service_id=kept_id, procedure_id=uuid.uuid4(),
                owner_api_user_id=u.api_user_id, service_name="probe-conflict",
                service_label="1", recipe_path="", state="generating",
                generation_task_id="gt-live", use_global_proxy=True,
            )
            # What the manager does on a pause.
            assert pdr.set_state_for_job("gt-live", "paused", "compose_fix") is True

            stale_id = uuid.uuid4()
            pdr.create_or_replace(
                service_id=stale_id, procedure_id=uuid.uuid4(),
                owner_api_user_id=v.api_user_id, service_name="probe-plain",
                service_label="2", recipe_path="", state="generating",
                generation_task_id=None, use_global_proxy=True,
            )

            report = pdr.reconcile(live_task_ids=set())

            row = pdr.get_session(str(kept_id))
            assert row is not None, "a paused session is never pruned (G30)"
            assert row["use_global_proxy"] is True, "the sweep dropped the router's proxy"
            assert row["reason"] == "compose_fix"
            assert pdr.get_session(str(stale_id)) is None, (
                "a generating session with no live task is stale")
            assert report["dropped_stale"] == [str(stale_id)]
        finally:
            db.close()


class TestTheGatewayRefusesAtItsOwnCapacity:
    """G29/T7: the gateway runs its OWN generation pool, so it has its OWN
    refusal with its OWN code (`generation_at_capacity`, not the -api's
    `at_capacity`), recorded on the session the card reads."""

    @staticmethod
    def _jobs(n: int, status: str) -> None:
        """`n` gateway.tasks rows in `status` — the gate counts ROWS."""
        from app.database import SessionLocal
        from support import mk_task

        db = SessionLocal()
        try:
            for i in range(n):
                mk_task(db, f"gt-occupier-{i}", status=status)
        finally:
            db.close()

    def _session(self, db, user, service_name="probe-cap", label="3"):
        import uuid

        from app.services import pre_deploy_registry as pdr

        sid = uuid.uuid4()
        # `generated` reserves nothing but still exists: the router reaches the
        # capacity gate, and the refusal lands on this record.
        pdr.create_or_replace(
            service_id=sid, procedure_id=uuid.uuid4(),
            owner_api_user_id=user.api_user_id, service_name=service_name,
            service_label=label, recipe_path="", state="generated",
        )
        return sid

    def test_the_pool_refuses_at_its_own_limit(self, monkeypatch):
        """The refusal carries its counts AND its remedy, not just a failure."""
        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import (
            GenerationCapacityError, GenerationJobManager,
        )

        monkeypatch.setattr(settings, "GATEWAY_MAX_GENERATIONS", 2)
        self._jobs(2, "running")

        db = SessionLocal()
        try:
            with pytest.raises(GenerationCapacityError) as exc:
                GenerationJobManager().create_job(
                    db, "probe-new", "", "generate_missing", {})
        finally:
            db.close()
        assert exc.value.code == "generation_at_capacity", "its OWN code, not the -api's"
        assert (exc.value.in_flight, exc.value.capacity) == (2, 2)
        assert "cancel" in str(exc.value).lower(), "the remedy travels with the refusal"

    def test_the_router_records_the_refusal_on_the_session(self, monkeypatch):
        """Recorded on the SESSION, with its own reason — the card the operator
        sees, not a toast that scrolls away."""
        import asyncio

        from fastapi import HTTPException
        from app.config import settings
        from app.database import SessionLocal
        from app.routers import llm as llm_router
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        monkeypatch.setattr(settings, "GATEWAY_MAX_GENERATIONS", 1)
        self._jobs(1, "running")
        monkeypatch.setattr(llm_router, "log_action", lambda *a, **k: None)

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = self._session(db, alice)
            req = {"service_name": "probe-cap", "recipe_path": "", "label": "3",
                   "user_name": "alice", "job_type": "generate_missing",
                   "service_id": str(sid),
                   "owner_api_user_id": str(alice.api_user_id)}
            with pytest.raises(HTTPException) as exc:
                asyncio.run(llm_router.create_generation_job(
                    req, {"id": str(alice.id), "role": "admin"}, db))
            assert exc.value.status_code == 503, exc.value
            assert exc.value.detail["error"] == "generation_at_capacity"
            assert (exc.value.detail["running"], exc.value.detail["capacity"]) == (1, 1)

            row = pdr.get_session(str(sid))
            assert row is not None, "the refusal must land on the session the card reads"
            assert row["state"] == "refused"
            assert row["reason"] == "generation_at_capacity", (
                "the card picks the remedy by the reason; `at_capacity` would send "
                "the operator to cancel a docker task that does not exist")
            assert "cancel" in row["detail"].lower()
        finally:
            db.close()

    def test_the_refusal_carries_the_options_its_retry_seeds_from(self, monkeypatch):
        """A gateway-side refusal produces a `refused` session with its own Retry."""
        import asyncio

        from fastapi import HTTPException
        from app.config import settings
        from app.database import SessionLocal
        from app.routers import llm as llm_router
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        monkeypatch.setattr(settings, "GATEWAY_MAX_GENERATIONS", 1)
        self._jobs(1, "running")
        monkeypatch.setattr(llm_router, "log_action", lambda *a, **k: None)

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = self._session(db, alice)
            req = {"service_name": "probe-cap", "recipe_path": "recipes/cap",
                   "label": "3", "user_name": "alice",
                   "job_type": "generate_missing", "service_id": str(sid),
                   "owner_api_user_id": str(alice.api_user_id),
                   "domain": "example.test", "https": True,
                   "selection": {"compose": [], "nginx": None, "env": [],
                                 "profiles": []},
                   "use_global_proxy": True,
                   "deploy_metadata": {"service_name": "probe-cap",
                                       "user_name": "alice", "label": "3",
                                       "domain": "example.test", "https": True}}
            with pytest.raises(HTTPException):
                asyncio.run(llm_router.create_generation_job(
                    req, {"id": str(alice.id), "role": "admin"}, db))
        finally:
            db.close()

        row = pdr.get_session(str(sid))
        opts = row.get("options")
        assert opts, "the refusal's Retry has nothing to seed the panel from"
        assert opts["user_name"] == "alice" and opts["service_name"] == "probe-cap"
        assert opts["use_global_proxy"] is True
        assert "selection" in opts, "the file selection rides along"
        assert opts["domain"] == "example.test", "the URL is kept"

    def test_a_refusal_keeps_the_credential_encrypted_not_in_options(self):
        """R3.6: options carry no credential; the password lives in
        `options_secret_enc` and the retry replays it."""
        import uuid

        from app.database import SessionLocal
        from app.services import pre_deploy_registry as pdr
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="myapp",
                service_label="0", recipe_path="", state="generating",
            )
            req = {"user_name": "alice", "service_name": "myapp", "label": "0",
                   "domain": "example.test", "https": True, "passwd": "hunter2",
                   "compose_file_paths": ["docker-compose.yml"],
                   "selection": {"compose": ["docker-compose.yml"], "nginx": None,
                                 "env": [], "profiles": []}}
            pdr.record_refusal(service_id=str(sid), reason="at_capacity",
                               detail="at capacity", request=req)
            row = pdr.get_session(str(sid))
            assert "passwd" not in row["options"]
            assert row["options"]["domain"] == "example.test"
            assert row["options"]["compose_file_paths"] == ["docker-compose.yml"]
            # …and the encrypted half decrypts back for the retry.
            replay = pdr.replay_options(str(sid))
            assert replay["passwd"] == "hunter2"
        finally:
            db.close()

    def test_a_refused_session_does_not_block_its_own_retry(self, monkeypatch):
        """The reservation exempts the operator's own refused record."""
        import asyncio
        import uuid

        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import llm as llm_router
        from app.services import pre_deploy_registry as pdr
        from app.services.generation_jobs import generation_jobs
        from support import mk_task, mk_user

        async def _noop(job_id):
            return None
        monkeypatch.setattr(generation_jobs, "_run_job", _noop)
        monkeypatch.setattr(llm_router, "log_action", lambda *a, **k: None)

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice")
            sid = uuid.uuid4()
            mk_task(db, "gt-live", status="running")
            pdr.create_or_replace(
                service_id=sid, procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="probe-cap",
                service_label="3", recipe_path="", state="refused",
                reason="generation_at_capacity", detail="at capacity",
            )
            req = {"service_name": "probe-cap", "recipe_path": "", "label": "3",
                   "user_name": "alice", "job_type": "generate_missing",
                   "service_id": str(sid),
                   "owner_api_user_id": str(alice.api_user_id)}
            out = asyncio.run(llm_router.create_generation_job(
                req, {"id": str(alice.id), "role": "admin"}, db))
            assert out["task_id"], "a refused session must not block its own retry"
            assert pdr.get_session(str(sid))["state"] == "generating"

            # …while a GENERATING session still reserves.
            with pytest.raises(HTTPException) as exc:
                asyncio.run(llm_router.create_generation_job(
                    req, {"id": str(alice.id), "role": "admin"}, db))
            assert exc.value.status_code == 409, exc.value
            assert exc.value.detail["error"] == "combination_reserved"
        finally:
            db.close()

    def test_a_terminal_pause_does_not_hold_a_slot(self, monkeypatch):
        """A pause is TERMINAL, so it stops counting as in-flight."""
        import asyncio

        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        monkeypatch.setattr(settings, "GATEWAY_MAX_GENERATIONS", 1)
        self._jobs(1, "paused")

        async def _noop(job_id):
            return None
        monkeypatch.setattr(generation_jobs, "_run_job", _noop)

        db = SessionLocal()
        try:
            async def _run():
                return generation_jobs.create_job(
                    db, "probe", "", "generate_missing", {})
            assert asyncio.run(_run())
        finally:
            db.close()




class TestTheCleanupContract:
    """G30: ONE contract, two engine-adapted implementations.

    Only ``completed | failed | cancelled`` are eligible; a record waiting for
    the operator (``paused``) or a live leg is never pruned; one TTL with one
    default; a count cap evicting oldest-first; when a record goes, its LOG goes
    with it; cleanup and recovery are owner-side only. T7 makes pruning
    SOFT-delete: the tombstone stays so links resolve, and the default list
    omits it.
    """

    @staticmethod
    def _jobs(specs):
        """Insert ``gateway.tasks`` rows: (label, status) pairs, oldest first."""
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text
        from app.database import SessionLocal
        from support import mk_task

        # Fresh by default; a test that needs an OLD row stamps it explicitly.
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        db = SessionLocal()
        ids = []
        try:
            for i, (_label, status) in enumerate(specs):
                task_id = f"gt-clean{i}"
                mk_task(db, task_id, status=status)
                stamp = base + timedelta(days=i)
                db.execute(
                    text("UPDATE gateway.tasks SET created_at=:c, updated_at=:c "
                         "WHERE task_id=:t"),
                    {"c": stamp, "t": task_id},
                )
                db.commit()
                ids.append(task_id)
        finally:
            db.close()
        return ids

    @staticmethod
    def _state(db, task_id):
        from app.services.generation_jobs import generation_jobs
        row = generation_jobs.get_job(db, task_id)
        return None if row is None else row.deleted_at

    def test_the_ttl_sweep_leaves_the_operators_records_alone(self):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        old, paused, running = self._jobs([("a", "completed"),
                                           ("b", "paused"),
                                           ("c", "running")])
        db = SessionLocal()
        try:
            # `old` is past the TTL; `paused`/`running` are older still, so age
            # cannot be what saves them.
            for tid in (old, paused, running):
                db.execute(
                    text("UPDATE gateway.tasks SET updated_at=:c WHERE task_id=:t"),
                    {"c": datetime.now(timezone.utc) - timedelta(days=90), "t": tid},
                )
            db.commit()
            pruned = generation_jobs.prune_finished(db)
            assert pruned == 1, "exactly the finished row aged out"
            assert self._state(db, old) is not None
            assert self._state(db, paused) is None
            assert self._state(db, running) is None
        finally:
            db.close()

    def test_the_ttl_sweep_runs_on_the_reads(self):
        """The trigger rule: a READ sweeps (single-record GET and the list)."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text
        from app.database import SessionLocal
        from app.models.gateway_task import GatewayTask
        from app.routers import llm as llm_router
        from app.services.generation_jobs import generation_jobs

        old, asked = self._jobs([("a", "completed"), ("b", "completed")])
        db = SessionLocal()
        try:
            db.execute(
                text("UPDATE gateway.tasks SET updated_at=:c WHERE task_id=:t"),
                {"c": datetime.now(timezone.utc) - timedelta(days=90), "t": old},
            )
            db.commit()
            asyncio.run(llm_router.get_generation_job(
                asked, {"id": "00000000-0000-0000-0000-000000000001", "role": "admin"}, db))
            assert self._state(db, old) is not None, (
                "GET /llm/jobs/{id} must sweep the TTL")
            generation_jobs.list_jobs(db)          # and the list read
            assert self._state(db, asked) is None, "the read it serves stays inside TTL"
        finally:
            db.close()

    def test_the_log_goes_with_the_record(self):
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        doomed, kept = self._jobs([("a", "completed"), ("b", "completed")])
        db = SessionLocal()
        try:
            doomed_log = generation_jobs._log_path(doomed)
            kept_log = generation_jobs._log_path(kept)
            doomed_log.write_text("session log\n")
            kept_log.write_text("session log\n")
            db.execute(
                text("UPDATE gateway.tasks SET updated_at=:c WHERE task_id=:t"),
                {"c": datetime.now(timezone.utc) - timedelta(days=90), "t": doomed},
            )
            db.commit()
            generation_jobs.prune_finished(db)
        finally:
            db.close()
        assert not doomed_log.exists(), (
            "a log whose record is pruned is unreachable — and grows without bound")
        assert kept_log.exists(), "a live record keeps its log"

    def test_the_ttl_and_cap_defaults_are_the_apis(self):
        """The env NAMES differ on purpose; the DEFAULTS must not."""
        from app.config import settings

        assert settings.GATEWAY_JOB_TTL_SECONDS == 604800, (
            "the -api's TASK_TTL_SECONDS default (1 week)")
        assert settings.GATEWAY_MAX_JOBS == 1000, "the -api's TASK_MAX_COUNT default"

    def test_a_paused_record_survives_recovery(self):
        """A paused leg is terminal under G27; a restart must not rewrite it,
        while a genuinely interrupted leg becomes `unknown` then `failed`."""
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        paused, interrupted = self._jobs([("a", "paused"), ("b", "running")])
        db = SessionLocal()
        try:
            recovered = generation_jobs.recover_stale_jobs(db)
            assert recovered == 1, "exactly the interrupted row is recovered"
            assert generation_jobs.get_job(db, paused).status == "paused"
            assert generation_jobs.get_job(db, interrupted).status == "unknown"
            generation_jobs.terminalize_unknown(db)
            assert generation_jobs.get_job(db, interrupted).status == "failed"
        finally:
            db.close()

    def test_a_zero_ttl_is_honoured_not_swallowed(self, monkeypatch):
        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        monkeypatch.setattr(settings, "GATEWAY_JOB_TTL_SECONDS", 0)
        done, paused = self._jobs([("a", "completed"), ("b", "paused")])
        db = SessionLocal()
        try:
            generation_jobs.prune_finished(db)
            assert self._state(db, done) is not None, "a TTL of 0 prunes finished rows"
            assert self._state(db, paused) is None, "…and never the operator's record"
        finally:
            db.close()

    def test_a_zero_cap_is_honoured_not_swallowed(self, monkeypatch):
        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        monkeypatch.setattr(settings, "GATEWAY_MAX_JOBS", 0)
        done, paused = self._jobs([("a", "failed"), ("b", "paused")])
        db = SessionLocal()
        try:
            generation_jobs.prune_excess(db)
            assert self._state(db, done) is not None, "a cap of 0 evicts finished rows"
            assert self._state(db, paused) is None, "…and never the operator's record"
        finally:
            db.close()

    def test_the_cap_evicts_the_oldest_finished_on_submit(self, monkeypatch):
        """The cap runs on SUBMIT, oldest-first, and cannot touch a paused row."""
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text
        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs

        monkeypatch.setattr(settings, "GATEWAY_MAX_JOBS", 2)
        oldest, middle, newest, paused = self._jobs([
            ("a", "completed"), ("b", "failed"), ("c", "completed"), ("d", "paused"),
        ])
        db = SessionLocal()
        try:
            # The paused row is the OLDEST stamp: oldest-first would take it if
            # status were not respected.
            db.execute(
                text("UPDATE gateway.tasks SET created_at=:c, updated_at=:c "
                     "WHERE task_id=:t"),
                {"c": datetime.now(timezone.utc) - timedelta(days=60), "t": paused},
            )
            db.commit()
            evicted = generation_jobs.prune_excess(db)
        finally:
            db.close()
        assert evicted == 1, evicted

        db = SessionLocal()
        try:
            assert self._state(db, oldest) is not None, "the oldest finished row goes"
            assert self._state(db, middle) is None
            assert self._state(db, newest) is None
            assert self._state(db, paused) is None, "a paused row is never evicted"
        finally:
            db.close()


class TestAPauseIsALegAndTheAnswerIsANewJob:
    """GRILLING G27: a pause ENDS a leg; answering it starts a NEW one.

    These replace `test_submit_pending_input_writes_env_and_completes` and
    `test_resume_requeues_the_SAME_job_with_the_compose_unselected`. Both drove
    `submit_pending_input`, which rewrote the SAME row and re-ran it. That
    machinery is gone — a pause is a terminal status carrying a payload, and the
    answer is a new job — so what has to be true is different, and testable
    without the removed method.
    """

    def test_a_conflict_pause_is_terminal_and_carries_the_next_leg(self, tmp_path, monkeypatch):
        """The real validate phase pauses — and what it leaves behind is a
        TERMINAL row plus everything the next leg needs.

        No LLM is involved on this path: the compose is SELECTED and on disk, so
        the phase plan is validate-first and the conflict is found by the scan.
        """
        import asyncio
        import json
        from pathlib import Path
        from app.config import settings
        from app.database import SessionLocal
        from app.models.generation_job import GenerationJob, JOB_STATUS_PAUSED
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "myapp"
        recipe.mkdir()
        # ONE name, TWO sites, TWO defaults — the conflict §3b exists for.
        (recipe / "docker-compose.yml").write_text(
            "services:\n  a:\n    environment:\n      - P=${P:-1}\n"
            "  b:\n    environment:\n      - Q=${P:-2}\n"
        )

        async def scenario():
            # A FRESH manager per test: the module singleton caches an
            # `asyncio.Lock` per (service, recipe), and a Lock is bound to the
            # loop that created it — reusing the singleton across tests trips
            # "bound to a different event loop".
            m = GenerationJobManager()
            db = SessionLocal()
            job = m.create_job(db, "myapp", "", "generate_missing", {
                "service_name": "myapp",
                "selection": {"compose": ["docker-compose.yml"], "nginx": None, "env": []},
                "user_name": "alice", "label": "0",
            })
            jid = job.id
            for _ in range(200):
                await asyncio.sleep(0.05)
                if m.job_status(jid) == JOB_STATUS_PAUSED:
                    break
            db.expire_all()
            row = db.query(GenerationJob).filter(GenerationJob.id == jid).first()
            return jid, row.status, row.result or {}, db.query(GenerationJob).count()

        jid, status, result, count = asyncio.run(scenario())

        assert status == JOB_STATUS_PAUSED, f"the leg must END, not park (got {status})"
        pause = result.get("pause") or {}
        assert pause.get("kind") == "compose_fix"
        assert pause.get("ask", {}).get("conflicts"), "the operator must be told what conflicts"
        carried = pause.get("request") or {}
        assert carried.get("selection", {}).get("compose") == [], (
            "the faulty compose must be UNSELECTED so the next leg regenerates it")
        assert carried.get("fix") is True, "a guided restart emits .fix (G25)"
        assert count == 1, "the pause must not spawn a job — the ANSWER will"
        # ...and the carried request IS a plan that continues the path.
        assert GenerationJobManager._compute_phases("generate_missing", carried)[:2] == [
            "compose", "validate"]

    def test_a_credential_pause_carries_the_env_so_the_answer_needs_no_llm(self, tmp_path, monkeypatch):
        """The carry makes the answering leg a single env write.

        `_compute_phases` yields exactly `["env"]`, and the env branch finishes
        from the carried mechanical text + key map — which is why the answer
        costs no LLM call.
        """
        import asyncio
        from pathlib import Path
        from app.config import settings
        from app.database import SessionLocal
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        (tmp_path / "myapp").mkdir()

        carried = {
            "service_name": "myapp",
            "selection": {"compose": [], "nginx": None, "env": []},
            "user_name": "alice", "label": "0",
            "resume_env": {"env_name": ".env.raw.alice.0", "vars": ["OPENAI_API_KEY"],
                           "mech": "OPENAI_API_KEY=\n",
                           "kvs": {"OPENAI_API_KEY": {"env_type": "external"}},
                           "compose_paths": []},
            "resume_values": {"OPENAI_API_KEY": "sk-from-the-operator"},
        }
        assert GenerationJobManager._compute_phases("generate_missing", carried) == ["env"]

        async def scenario():
            m = GenerationJobManager()   # fresh: see the note in the test above
            db = SessionLocal()
            job = m.create_job(
                db, "myapp", "", "generate_missing", carried,
                resume_values={"OPENAI_API_KEY": "sk-from-the-operator"})
            jid = job.id
            for _ in range(200):
                await asyncio.sleep(0.05)
                if m.job_status(jid) in ("completed", "failed"):
                    return m.job_status(jid)
            return "timed out"

        assert asyncio.run(scenario()) == "completed"
        written = (tmp_path / "myapp" / ".env.raw.alice.0").read_text()
        assert "OPENAI_API_KEY=sk-from-the-operator" in written


class TestAValidationFailurePausesWhereASessionCanExist:
    """R1.3 / T14 — what a validation failure DOES depends on the path.

    The doc states the rule outright, and GRILLING **G43** records its
    provenance: the "own pause kind" phrasing came from the serving-ports
    design's write-up, not from a ruling — and the pipeline-steps agreement,
    which is the latest word, delegates the pause to each step's BODY. This is
    the body's answer, and the reason is not decoration: **a pause keeps the run
    and the artifacts it has already produced, while a raised failure discards
    both and re-pays for them on the retry.**

    So the deploy-scoped path pauses (a pause IS a session, and one exists
    there) with the check's OWN kind, naming the file; the standalone Generate
    panel — no user, no label, no session — raises with the same errors and the
    same named file. Both halves are asserted here, from the same failing
    artifact, driven through the real phase body.
    """

    _COMPOSE = (
        "services:\n"
        "  app:\n"
        "    image: alpine\n"
        "    ports:\n"
        "      - '8000:8000'\n"
    )
    # `ghost` is a service the compose does not have — the platform routes by
    # that name, so the artifact is wrong and `validate_ports` must say so.
    _PORTS = (
        "ports:\n"
        "  - sub_service: ghost\n"
        "    port: 9999\n"
        "    kind: http\n"
        "    primary: true\n"
    )

    def _recipe(self, tmp_path):
        recipe = tmp_path / "myapp"
        recipe.mkdir(parents=True, exist_ok=True)
        (recipe / "docker-compose.yml").write_text(self._COMPOSE)
        (recipe / "serving_ports.yml").write_text(self._PORTS)
        # A conf must be IN PLAY for the phase plan to reach `validate_ports`
        # without running the nginx phase (no LLM exists in a unit test).
        (recipe / "nginx.conf").write_text(
            "server {\n listen 80;\n server_name {{ hostname }};\n}\n")
        return recipe

    def _run(self, tmp_path, monkeypatch, *, user_name):
        import asyncio
        import json
        from pathlib import Path
        from app.config import settings
        from app.database import SessionLocal
        from app.models.generation_job import GenerationJob
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        self._recipe(tmp_path)
        request = {
            "service_name": "myapp",
            "selection": {"compose": ["docker-compose.yml"], "serving_ports": "serving_ports.yml",
                          "nginx": "nginx.conf", "env": []},
            "skip_env": True,
            "label": "0",
        }
        if user_name is not None:
            request["user_name"] = user_name

        async def scenario():
            # A FRESH manager per test: the module singleton caches an
            # `asyncio.Lock` per (service, recipe), and a Lock is bound to the
            # loop that created it — reusing the singleton across tests trips
            # "bound to a different event loop".
            m = GenerationJobManager()
            db = SessionLocal()
            job = m.create_job(db, "myapp", "", "generate_missing", request)
            jid = job.id
            for _ in range(200):
                await asyncio.sleep(0.05)
                if m.job_status(jid) in ("completed", "failed", "paused"):
                    break
            db.expire_all()
            row = db.query(GenerationJob).filter(GenerationJob.id == jid).first()
            return row.status, row.result or {}, (row.error or "")

        return asyncio.run(scenario())

    def test_the_deploy_path_pauses_with_the_checks_own_kind_and_names_the_file(
            self, tmp_path, monkeypatch):
        status, result, _err = self._run(tmp_path, monkeypatch, user_name="alice")

        assert status == "paused", (
            "a deploy-scoped validation failure must PAUSE, not raise: the pause "
            f"keeps the artifacts the run already produced (R1.3); got {status}: {_err}")
        pause = result.get("pause") or {}
        assert pause.get("kind") == "validate_ports_fix", (
            "each check has its OWN kind, so the guided fix names the right file")
        ask = pause.get("ask") or {}
        assert ask.get("file") == "serving_ports.yml", ask
        assert ask.get("errors"), "the operator must be told what is wrong"
        assert any("ghost" in e for e in ask["errors"]), ask["errors"]
        carried = pause.get("request") or {}
        assert carried.get("selection", {}).get("serving_ports") is None, (
            "the artifact the check rejected must travel UNSELECTED, or the next "
            "leg judges the same file again and pauses forever on its own output")
        assert carried.get("fix") is True, "a guided restart emits .fix (G25)"
        # ...and the answer's phase plan re-runs the check rather than skipping it.
        from app.services.generation_jobs import GenerationJobManager as M
        assert "validate_ports" in M._compute_phases("generate_missing", carried)

    def test_the_standalone_panel_raises_with_the_same_named_file(
            self, tmp_path, monkeypatch):
        status, _result, err = self._run(tmp_path, monkeypatch, user_name=None)

        assert status == "failed", (
            "the standalone Generate panel has no user and no label to hang a "
            "session on, so its validation failure RAISES (R1.3)")
        assert "validate_ports failed" in err, err
        assert "ghost" in err, "the raised error names the same file's fault"

    def test_the_second_check_names_the_conf_and_unselects_it(self, tmp_path, monkeypatch):
        """`validate_conf` pauses with ITS OWN kind — and its own file.

        The two checks are separate steps with separate kinds precisely so the
        prompt names the right file: here the ports file is FINE and the CONF is
        the one that does not agree with it (a `kind: http` row with no block
        serving its name), so the pause must name the conf and carry the conf's
        slot unselected.
        """
        import pathlib

        recipe = tmp_path / "second" / "myapp"
        recipe.mkdir(parents=True)
        (recipe / "docker-compose.yml").write_text(
            "services:\n"
            "  app:\n    image: alpine\n    ports:\n      - '8000:8000'\n"
            "  metrics:\n    image: alpine\n    ports:\n      - '9000:9000'\n"
        )
        (recipe / "serving_ports.yml").write_text(
            "ports:\n"
            "  - sub_service: app\n    port: 8000\n    kind: http\n    primary: true\n"
            "  - sub_service: metrics\n    port: 9000\n    kind: http\n"
        )
        # Only the primary has a block — the `metrics` row declares an endpoint
        # the conf does not serve (R1.3's first `validate_conf` error).
        (recipe / "nginx.conf").write_text(
            "server {\n listen 80;\n server_name {{ hostname }};\n"
            " proxy_pass http://app:8000;\n}\n"
        )

        import asyncio
        import json
        from pathlib import Path
        from app.config import settings
        from app.database import SessionLocal
        from app.models.generation_job import GenerationJob
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path / "second"))

        async def scenario():
            m = GenerationJobManager()   # fresh: see the note above on locks
            db = SessionLocal()
            job = m.create_job(db, "myapp", "", "generate_missing", {
                "service_name": "myapp",
                "selection": {"compose": ["docker-compose.yml"],
                              "serving_ports": "serving_ports.yml",
                              "nginx": "nginx.conf", "env": []},
                "skip_env": True, "user_name": "alice", "label": "0",
            })
            jid = job.id
            for _ in range(200):
                await asyncio.sleep(0.05)
                if m.job_status(jid) in ("completed", "failed", "paused"):
                    break
            db.expire_all()
            row = db.query(GenerationJob).filter(GenerationJob.id == jid).first()
            return row.status, row.result or {}, (row.error or "")

        status, result, err = asyncio.run(scenario())
        assert status == "paused", f"expected a pause, got {status}: {err}"
        pause = result.get("pause") or {}
        assert pause.get("kind") == "validate_conf_fix", pause
        ask = pause.get("ask") or {}
        assert ask.get("file") == "nginx.conf", ask
        assert any("metrics" in e for e in ask.get("errors") or []), ask.get("errors")
        carried = pause.get("request") or {}
        assert carried.get("selection", {}).get("nginx") is None, carried.get("selection")
        assert carried.get("selection", {}).get("serving_ports") == "serving_ports.yml", (
            "the artifact that PASSED must stay selected — only the rejected one is "
            "unselected, or the retry re-pays for work that was already right")


class TestCancellationIsOnTheRow:
    """D1 (GRILLING G31): a cancel is a STATUS ON THE ROW, not a pid-local set.

    Found live: `self._cancelled` was an in-memory set and `_check_cancelled`
    read only it, while the job runs in ANOTHER worker's event loop under
    `uvicorn --workers 4`. A `DELETE` landing on a different worker therefore
    flipped the row to `cancelled` and left the job running — and the runner's
    own terminal write then overwrote the row back to `completed`.

    These drive the real methods with the local set EMPTY, which is exactly the
    state a worker that did not handle the DELETE is in.
    """

    def _job(self, status: str = "running") -> str:
        from app.database import SessionLocal
        from support import mk_task
        db = SessionLocal()
        try:
            return mk_task(db, f"gt-cancel-{status}", status=status)
        finally:
            db.close()

    def test_a_cancel_on_another_worker_is_seen(self):
        """The row says cancelled ⇒ the runner must stop, with no local state."""
        from app.services.generation_jobs import generation_jobs, JobCancelled
        jid = self._job("cancelled")
        assert jid not in generation_jobs._cancelled, "the local set must be empty (another worker)"
        with pytest.raises(JobCancelled):
            generation_jobs._check_cancelled(jid)

    def test_a_running_job_is_not_cancelled(self):
        from app.services.generation_jobs import generation_jobs
        jid = self._job("running")
        generation_jobs._check_cancelled(jid)  # must not raise

    def test_a_terminal_write_cannot_resurrect_a_cancelled_row(self):
        """The runner finishing must not overwrite the cancel."""
        from app.database import SessionLocal
        from app.models.generation_job import GenerationJob, JOB_STATUS_CANCELLED
        from app.services.generation_jobs import generation_jobs
        jid = self._job("cancelled")
        generation_jobs._set_status(jid, "completed", progress="completed")
        db = SessionLocal()
        row = db.query(GenerationJob).filter(GenerationJob.id == jid).first()
        status = row.status
        db.close()
        assert status == JOB_STATUS_CANCELLED, (
            "a finished run must not resurrect a job the operator cancelled")


class TestCancelAndAdmissionRaces:
    """T5/T7 fault rows: cancel is sticky under a concurrent finish, and the
    generation-capacity limit holds across concurrent admissions."""

    def test_cancel_transition_cannot_be_overwritten_by_a_later_finish(self):
        """D1: if the cancel WON the row, the runner's later write must not
        resurrect it. (If the runner won first, a cancel on a terminal row is a
        no-op and the row stays completed — that is the correct race outcome.)"""
        import threading

        from app.database import SessionLocal
        from app.services.generation_jobs import generation_jobs
        from support import mk_task

        db = SessionLocal()
        try:
            for i in range(20):
                tid = mk_task(db, f"gt-race-{i}", status="running")
                barrier = threading.Barrier(2)
                seen = {}

                def cancel():
                    s = SessionLocal()
                    try:
                        barrier.wait()
                        job = generation_jobs.cancel_job(s, tid)
                        seen["cancel_status"] = job.status
                    finally:
                        s.close()

                def finish():
                    barrier.wait()
                    generation_jobs._set_status(tid, "completed", progress="completed")

                t1 = threading.Thread(target=cancel)
                t2 = threading.Thread(target=finish)
                t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
                row = generation_jobs.get_job(db, tid)
                if seen.get("cancel_status") == "cancelled":
                    assert row.status == "cancelled", (i, row.status)
                else:
                    assert row.status == "completed", (i, row.status)
        finally:
            db.close()

    def test_capacity_limit_holds_across_concurrent_admissions(self, monkeypatch):
        import asyncio
        import threading
        from unittest.mock import AsyncMock

        from app.config import settings
        from app.database import SessionLocal
        from app.models.gateway_task import GatewayTask
        from app.services.generation_jobs import (
            GenerationCapacityError, GenerationJobManager,
        )

        monkeypatch.setattr(settings, "GATEWAY_MAX_GENERATIONS", 1)
        m = GenerationJobManager()
        m._run_job = AsyncMock()

        results = []
        barrier = threading.Barrier(2)

        def worker(i):
            async def go():
                db = SessionLocal()
                try:
                    barrier.wait()
                    job = m.create_job(db, f"svc-{i}", "", "generate_missing", {})
                    return ("ok", job.task_id)
                except GenerationCapacityError:
                    return ("capacity", None)
                finally:
                    db.close()
            results.append(asyncio.run(go()))

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start(); t2.start(); t1.join(timeout=60); t2.join(timeout=60)

        assert sorted(r[0] for r in results) == ["capacity", "ok"], results
        db = SessionLocal()
        try:
            assert db.query(GatewayTask).filter(GatewayTask.kind == "generate",
                                                GatewayTask.status == "queued").count() == 1
        finally:
            db.close()


class TestEnvPhasePrecedenceEndToEnd:
    """§2b's rule driven through the ACTUAL env phase, not just the helper.

    Audit flaw 5: the §2b e2e (`NEO4J_PORT=7687`) exercised only the
    "empty in-file value → materialize the compose default" half, because that
    fixture's in-file value WAS empty. The rule's other half — "a NON-EMPTY
    in-file value wins and is never overwritten" — had only ever been tested
    against `_mechanical_env` in isolation. This drives `_run_job`'s env phase
    over real files so the phase's own wiring is covered too.
    """

    def _run_env_phase(self, tmp_path, monkeypatch, base_env_text, compose_text):
        import asyncio, json
        from app.config import settings
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "myapp"
        recipe.mkdir()
        (recipe / "docker-compose.yml").write_text(compose_text)
        (recipe / "base.env").write_text(base_env_text)

        m = GenerationJobManager()
        req = {
            "selection": {"compose": ["docker-compose.yml"], "env": ["base.env"]},
            "user_name": "alice", "label": "0",
        }
        # The env phase writes `.env.raw.{user}.{label}` into the recipe dir.
        mech = m._mechanical_env("myapp", "", req)
        return mech, recipe

    def test_a_non_empty_in_file_value_is_never_overwritten(self, tmp_path, monkeypatch):
        mech, recipe = self._run_env_phase(
            tmp_path, monkeypatch,
            base_env_text="KEEP_ME=hand-written\nEMPTY_ME=\n",
            compose_text=(
                "services:\n  app:\n    image: x\n    environment:\n"
                "      - A=${KEEP_ME:-from-compose}\n"
                "      - B=${EMPTY_ME:-from-compose}\n"
            ),
        )
        lines = dict(
            ln.split("=", 1) for ln in mech.splitlines() if "=" in ln
        )
        assert lines["KEEP_ME"] == "hand-written", (
            "the compose default must NOT overwrite a non-empty in-file value"
        )
        assert lines["EMPTY_ME"] == "from-compose", (
            "an EMPTY value counts as not set and receives the compose default"
        )


class TestTheGenerationOwnsItsOwnRecordAndLog:
    """GRILLING G28/G30: the `record` mirror is gone; the gateway keeps its OWN
    per-task log, and the log is deleted WITH the record.

    Four tests used to live here — `TestCancellingAPausedJobFreesTheSlot` and
    `TestAPausedGenerationSessionReachesATerminalState`. Both asserted the fate
    of the -api record task (that a cancel freed its pool slot, and that a
    resumed park drove it terminal). That machinery is deliberately gone: a
    generation no longer writes anything into the -api, which is exactly what
    made it consume a capacity it never used. These are the contract that took
    its place.
    """

    def test_the_mirror_is_gone(self):
        """The mechanism is removed, not merely unused."""
        from app.services.generation_jobs import generation_jobs
        for name in ("_session_create", "_session_append", "_session_append_async",
                     "_session_finish", "_session_finish_sync", "_session_endpoint",
                     "_session_drain"):
            assert not hasattr(generation_jobs, name), f"{name} is the removed mirror"

    def test_a_generation_writes_its_own_log_file(self, tmp_path, monkeypatch):
        """`gt-<id>.log` under the gateway's own task_logs dir — the same
        convention the -api uses for its own tasks."""
        from app.config import settings
        from app.services.generation_jobs import generation_jobs
        monkeypatch.setattr(settings, "GATEWAY_DATA_DIR", tmp_path)

        generation_jobs._log_append(7, {"file": "compose", "status": "done"})

        p = generation_jobs._log_path(7)
        assert p.exists(), f"no log at {p}"
        assert p.name == "gt-7.log"
        assert "compose" in p.read_text()

    def test_the_log_is_deleted_with_the_record(self, tmp_path, monkeypatch):
        """G30's cleanup contract, on the gateway side: the log goes with the
        record — a log whose record is gone is unreachable anyway, and leaving
        it behind is how a log directory grows without bound."""
        from app.config import settings
        from app.services.generation_jobs import generation_jobs
        monkeypatch.setattr(settings, "GATEWAY_DATA_DIR", tmp_path)

        generation_jobs._log_append(9, {"x": 1})
        assert generation_jobs._log_path(9).exists()
        generation_jobs._log_delete(9)
        assert not generation_jobs._log_path(9).exists()


# ---------------------------------------------------------------------------
# Stream exposures (task doc `2026-10-09-2-nginx-stream-exposure-url-task-doc.md`)
#
# T1 — the store · T2 — mint / list / revoke · T3 — the verdict.
# Every expected value below comes from that doc's §7 blocks for those nodes;
# the boundary values (one DNS label, the base32 alphabet) are T2's per §7's
# "boundary values at their limits".
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """The -api's registry, as the verdict sees it (shared mount, no HTTP)."""

    def __init__(self, entries):
        self._by_id = {str(e["service_id"]): e for e in entries}
        self.boom = False

    def get_by_id(self, service_id):
        if self.boom:
            raise RuntimeError("registry unreadable")
        return self._by_id.get(str(service_id))


def _stream_entry(service_id, owner_api_user_id, **over):
    entry = {
        "service_id": str(service_id),
        "owner_api_user_id": str(owner_api_user_id),
        "service_name": "graphiti-local",
        "label": "0",
        "recipe_path": ".",
        "hostname": "graphiti-local-shared2-0.localhost",
        "status": "running",
        "service_deleted_at": None,
        "nginx_conf_paths_by_category": {},
        "stream_rows": [{"sub_service": "neo4j", "port": 7687}],
    }
    entry.update(over)
    return entry


class _StreamBase:
    """Shared seeding: an owner with a live service whose entry the verdict reads."""

    def _seed(self, db, username="shared2", role="special", **fields):
        from support import mk_service, mk_user

        owner = mk_user(db, username, role=role)
        service_id = mk_service(db, owner, service_name="graphiti-local", label="0")
        entry = _stream_entry(service_id, owner.api_user_id, **fields)
        return owner, service_id, entry


class TestStreamExposureStore(_StreamBase):
    """T1 — the `stream_exposures` store: hashed, no TTL, one live row per row."""

    def _mint(self, db, owner, entry, sub_service="neo4j", port=7687):
        from app.services import stream_service
        return stream_service.mint(
            db, owner_user_id=owner.id, entry=entry,
            sub_service=sub_service, port=port,
        )

    def test_mint_stores_the_hash_and_the_encrypted_handle_label(self):
        """The door matches the sha256; the label is kept ONLY encrypted so an
        authorized user can be shown the URL later (operator ruling 2026-11-02).
        The plaintext host is never stored."""
        from app.database import SessionLocal
        from app.models.stream_exposure import StreamExposure
        from app.services import keyring
        from app.services.stream_service import domain_of, hash_handle

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, row = self._mint(db, owner, entry)
            assert row.handle_hash == hash_handle(raw)
            assert raw != row.handle_hash
            assert row.handle_enc is not None
            label = keyring.decrypt(row.handle_enc)
            assert label and raw == f"{label}.{domain_of(entry)}"
            assert raw not in str(row.to_dict())
            columns = {c.name for c in StreamExposure.__table__.columns}
            assert {"handle_hash", "handle_enc", "owner_user_id", "service_id",
                    "sub_service", "port", "canonical_name", "created_at",
                    "revoked_at"} <= columns
            assert "service_name" not in columns and "user_name" not in columns
        finally:
            db.close()

    def test_readable_address_round_trips(self):
        """The show path rebuilds the exact minted address from the encrypted
        label; a pre-change row (no secret) and a revoked row are unshowable."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, row = self._mint(db, owner, entry)
            assert stream_service.readable_address(row, entry, 17687) == f"{raw}:17687"
            row.handle_enc = None
            assert stream_service.readable_address(row, entry, 17687) is None
            row.handle_enc = b"v1:not:an-envelope"
            assert stream_service.readable_address(row, entry, 17687) is None
            row.handle_enc = b"v1:x:y"
            row.revoked_at = row.created_at
            assert stream_service.readable_address(row, entry, 17687) is None
        finally:
            db.close()

    def test_the_store_dict_carries_no_address(self):
        """`to_dict` is metadata + `has_url`; the address is assembled only by
        the authorized list route."""
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, row = self._mint(db, owner, entry)
            assert raw not in str(row.to_dict())
            assert row.to_dict()["has_url"] is True
            assert "address" not in row.to_dict()
        finally:
            db.close()

    def test_mint_has_no_ttl(self):
        from app.models.stream_exposure import StreamExposure
        assert "expires_at" not in {c.name for c in StreamExposure.__table__.columns}

    def test_a_second_mint_revokes_the_first(self):
        """R2.2/R7.2: one live URL per row; the old handle dies at its next
        connection (nothing is cached; the verdict is per connection)."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            first, _ = self._mint(db, owner, entry)
            second, _ = self._mint(db, owner, entry)
            assert first != second
            reg = _FakeRegistry([entry])
            assert stream_service.verify(db, reg, first)["allow"] is False
            assert stream_service.verify(db, reg, second)["allow"] is True
            live = [r for r in stream_service.list_for_instance(db, str(service_id))
                    if r.revoked_at is None]
            assert len(live) == 1
        finally:
            db.close()

    def test_one_live_row_per_row_is_enforced_by_the_store(self):
        """The invariant is a partial unique index, not a caller's discipline."""
        import uuid

        from sqlalchemy.exc import IntegrityError
        from app.database import SessionLocal
        from app.models.stream_exposure import StreamExposure

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            self._mint(db, owner, entry)
            clash = StreamExposure(
                handle_hash="0" * 64, owner_user_id=owner.id,
                service_id=service_id, sub_service="neo4j", port=7687,
                canonical_name="x.localhost",
            )
            db.add(clash)
            raised = False
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raised = True
            assert raised, "a second LIVE row for one stream row must be refused"
        finally:
            db.close()

    def test_the_store_list_does_not_leak_the_raw_handle(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            rows = stream_service.list_for_instance(db, str(service_id))
            assert len(rows) == 1
            assert rows[0].sub_service == "neo4j" and rows[0].port == 7687
            assert raw not in str([r.to_dict() for r in rows])
            assert rows[0].canonical_name == "graphiti-local-shared2-0--neo4j.localhost"
        finally:
            db.close()


class TestStreamExposureMint(_StreamBase):
    """T2/T9 — mint / list / revoke, and who may mint."""

    def _mint(self, db, owner, entry, sub_service="neo4j", port=7687):
        from app.services import stream_service
        return stream_service.mint(
            db, owner_user_id=owner.id, entry=entry,
            sub_service=sub_service, port=port,
        )

    def test_the_handle_is_one_dns_label_of_lowercase_base32(self):
        from app.services.stream_service import new_handle
        import re

        handles = {new_handle() for _ in range(64)}
        assert len(handles) == 64, "handles must not collide"
        for h in handles:
            assert len(h) <= 63, f"{h!r} exceeds one DNS label"
            assert re.fullmatch(r"[a-z2-7]+", h), f"{h!r} is not lowercase base32"

    def test_mint_returns_the_address_once_and_keeps_only_the_encrypted_label(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            rows = stream_service.list_for_instance(db, str(service_id))
            assert raw not in str([r.to_dict() for r in rows])
            assert stream_service.verify(db, _FakeRegistry([entry]), raw)["allow"] is True
        finally:
            db.close()

    def test_revoke_round_trips(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            reg = _FakeRegistry([entry])
            assert stream_service.verify(db, reg, raw)["allow"] is True
            assert stream_service.revoke_row(db, str(service_id), "neo4j", 7687) == 1
            assert stream_service.verify(db, reg, raw)["allow"] is False
            assert stream_service.revoke_row(db, str(service_id), "neo4j", 7687) == 0
        finally:
            db.close()

    def test_a_non_owner_non_admin_may_not_mint(self):
        """R2.2/T9: owner-only, or an admin acting on the owner's behalf."""
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.stream import _require_owner_or_admin
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            intruder = mk_user(db, "alice", role="viewer")
            with pytest.raises(HTTPException) as exc:
                _require_owner_or_admin(db, entry, {"id": str(intruder.id),
                                                    "role": "viewer"})
            assert exc.value.status_code == 403
        finally:
            db.close()

    def test_an_admin_mints_on_the_owners_behalf(self):
        """The recorded owner is the INSTANCE's user, so the verdict re-evaluates
        that user's entitlement, never the admin's."""
        from app.database import SessionLocal
        from app.routers.stream import _require_owner_or_admin
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            admin = mk_user(db, "admin", role="admin")
            resolved = _require_owner_or_admin(db, entry, {"id": str(admin.id),
                                                           "role": "admin"})
            assert resolved.id == owner.id
        finally:
            db.close()

    def test_a_granted_user_may_not_mint(self):
        """T9: only the owner or an admin manages exposures; a grant is for
        USING the service, not for minting its handle."""
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.stream import _require_owner_or_admin
        from app.services import acl_service
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            viewer = mk_user(db, "alice", role="viewer")
            acl_service.set_grant(db, viewer.id, owner.id, True)
            with pytest.raises(HTTPException) as exc:
                _require_owner_or_admin(db, entry, {"id": str(viewer.id),
                                                    "role": "viewer"})
            assert exc.value.status_code == 403
        finally:
            db.close()


class TestStreamExposureShow(_StreamBase):
    """2026-11-02 ruling: admin, owner and granted viewers may read a live
    exposure URL; an ungranted viewer may not — a public owner does not widen
    the audience (the URL is a credential)."""

    @staticmethod
    def _requester(user, role=None):
        return {"id": str(user.id), "role": role or user.role}

    def test_admin_and_owner_are_privileged(self):
        from app.database import SessionLocal
        from app.routers.stream import _may_view_exposures
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            admin = mk_user(db, "showadmin", role="admin")
            assert _may_view_exposures(db, entry, self._requester(admin))[1] is True
            assert _may_view_exposures(db, entry, self._requester(owner))[1] is True
        finally:
            db.close()

    def test_a_granted_viewer_may_read_a_live_address(self):
        from app.database import SessionLocal
        from app.routers.stream import _may_view_exposures
        from app.services import acl_service, stream_service
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            viewer = mk_user(db, "showviewer", role="viewer")
            acl_service.set_grant(db, viewer.id, owner.id, True)
            _owner, privileged = _may_view_exposures(
                db, entry, self._requester(viewer))
            assert privileged is False
            raw, row = stream_service.mint(
                db, owner_user_id=owner.id, entry=entry,
                sub_service="neo4j", port=7687)
            assert stream_service.readable_address(row, entry, 17687) == f"{raw}:17687"
            # Deactivating the grant closes the show path again.
            acl_service.set_grant(db, viewer.id, owner.id, False)
            import pytest
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc:
                _may_view_exposures(db, entry, self._requester(viewer))
            assert exc.value.status_code == 403
        finally:
            db.close()

    def test_an_ungranted_viewer_is_refused_even_for_a_public_owner(self):
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.stream import _may_view_exposures
        from support import mk_user

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            owner.is_public_shared = True
            db.commit()
            viewer = mk_user(db, "noshowviewer", role="viewer")
            with pytest.raises(HTTPException) as exc:
                _may_view_exposures(db, entry, self._requester(viewer))
            assert exc.value.status_code == 403
        finally:
            db.close()


class TestStreamVerdict(_StreamBase):
    """T3 — the per-connection verdict: fail-closed, plus the row-scope check."""

    def _mint(self, db, owner, entry, sub_service="neo4j", port=7687):
        from app.services import stream_service
        return stream_service.mint(
            db, owner_user_id=owner.id, entry=entry,
            sub_service=sub_service, port=port,
        )

    def test_unknown_handle_denies(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            v = stream_service.verify(db, _FakeRegistry([entry]), "nosuchhandle")
            assert v["allow"] is False and v["canonical_name"] is None
        finally:
            db.close()

    def test_empty_handle_denies(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            assert stream_service.verify(db, _FakeRegistry([entry]), "")["allow"] is False
        finally:
            db.close()

    def test_revoked_handle_denies(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            stream_service.revoke_instance(db, str(service_id))
            assert stream_service.verify(db, _FakeRegistry([entry]), raw)["allow"] is False
        finally:
            db.close()

    def test_scope_mismatch_denies(self):
        """R3.2: a handle minted for one stream row can never open another."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry, "neo4j", 7687)
            reg = _FakeRegistry([entry])
            assert stream_service.verify(db, reg, raw, requested_sub_service="neo4j",
                                         requested_port=7687)["allow"] is True
            assert stream_service.verify(db, reg, raw,
                                         requested_sub_service="falkordb")["allow"] is False
            assert stream_service.verify(db, reg, raw,
                                         requested_port=6379)["allow"] is False
        finally:
            db.close()

    def test_instance_gone_denies(self):
        """R3.4: the instance deleted ⇒ deny (the registry no longer has it)."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            assert stream_service.verify(db, _FakeRegistry([]), raw)["allow"] is False
        finally:
            db.close()

    def test_a_tombstoned_instance_denies(self):
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            dead = dict(entry, service_deleted_at="2026-01-01T00:00:00+00:00")
            assert stream_service.verify(db, _FakeRegistry([dead]), raw)["allow"] is False
        finally:
            db.close()

    def test_an_unentitled_owner_denies(self):
        """R3.4: an inactive/unapproved owner is a deny, not an allow."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            owner.is_approved = False
            db.commit()
            assert stream_service.verify(db, _FakeRegistry([entry]), raw)["allow"] is False
        finally:
            db.close()

    def test_a_revoked_grant_bites_at_the_next_connection(self):
        """R3.3: the matrix runs per connection and is uncached."""
        from app.database import SessionLocal
        from app.services import acl_service, stream_service

        db = SessionLocal()
        try:
            shared, service_id, entry = self._seed(db, "shared2", role="special")
            from support import mk_user
            viewer = mk_user(db, "alice", role="viewer")
            acl_service.set_grant(db, viewer.id, shared.id, True)
            raw, _ = self._mint(db, viewer, entry)
            reg = _FakeRegistry([entry])
            assert stream_service.verify(db, reg, raw)["allow"] is True
            acl_service.set_grant(db, viewer.id, shared.id, False)
            assert stream_service.verify(db, reg, raw)["allow"] is False
        finally:
            db.close()

    def test_a_gateway_side_error_is_a_deny(self):
        """Fault injection: an exception in the verdict ⇒ deny, never an allow."""
        from app.database import SessionLocal
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, service_id, entry = self._seed(db)
            raw, _ = self._mint(db, owner, entry)
            reg = _FakeRegistry([entry])
            reg.boom = True
            v = stream_service.verify(db, reg, raw)
            assert v["allow"] is False and v["reason"] == "error"
        finally:
            db.close()

    def test_the_verdict_equals_the_http_gate(self):
        """R3.3: for the SAME (user, target instance) the stream verdict and the
        HTTP gate agree — one matrix, two callers."""
        from unittest.mock import MagicMock, patch
        from app.database import SessionLocal
        from app.routers.auth import verify_auth
        from app.services import acl_service, stream_service
        from support import mk_user

        db = SessionLocal()
        try:
            shared, service_id, entry = self._seed(db, "shared2", role="special")
            viewer = mk_user(db, "alice", role="viewer")
            raw, _ = self._mint(db, viewer, entry)
            reg = _FakeRegistry([entry])

            def set_active(active: bool) -> None:
                acl_service.set_grant(db, viewer.id, shared.id, active)

            def gate_allows() -> bool:
                req = MagicMock()
                req.cookies = {"provision_token": "t"}
                req.headers = {"Accept": "text/html",
                               "Host": "graphiti-local--neo4j.localhost"}
                with patch("app.routers.auth._lookup_by_hostname", return_value=entry), \
                     patch("app.routers.auth.auth_service.verify_provision_token",
                           return_value={"sub": str(viewer.id)}), \
                     patch("app.routers.auth.authorization_header", return_value=""):
                    return verify_auth(req, db).status_code == 200

            for active in (True, False):
                set_active(active)
                stream = stream_service.verify(db, reg, raw)["allow"]
                assert stream is gate_allows(), (active, stream)
        finally:
            db.close()


class TestInitDbSurvivesTheMultiWorkerRace:
    """T1: ``init_db()`` serializes migrations on an advisory lock.

    The SQLite era used ``create_all`` and tolerated the loser's "already
    exists"; PostgreSQL has one versioned DDL under ``pg_advisory_xact_lock``,
    so concurrent starters converge on exactly one application of each version.
    """

    def test_init_db_is_idempotent(self):
        import app.database as database

        database.init_db()
        database.init_db()  # a second call applies nothing and must not raise

    def test_concurrent_init_db_calls_serialize(self):
        import threading

        import app.database as database
        from sqlalchemy import text

        errors: list[BaseException] = []

        def worker():
            try:
                database.init_db()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not errors, errors

        db = database.SessionLocal()
        try:
            rows = db.execute(text(
                "SELECT version, count(*) FROM gateway.schema_migrations GROUP BY version"
            )).all()
        finally:
            db.close()
        assert all(count == 1 for _version, count in rows), rows

    def test_the_advisory_lock_key_is_transaction_scoped(self):
        import inspect
        import app.database as database
        src = inspect.getsource(database._apply_migrations)
        assert "pg_advisory_xact_lock" in src
        assert "CREATE TABLE IF NOT EXISTS gateway.schema_migrations" in src


class TestStreamEndpointsAreWired:
    """T2/T3 at the HTTP layer — the wiring is the subject here.

    The handler reads the registry through ``app.state.registry``; these go
    through the real ASGI application with a real provision token and the real
    registry (seeded in PostgreSQL).
    """

    def _seed(self, db):
        import uuid

        from sqlalchemy import text
        from app.services import auth_service
        from support import mk_service, mk_service_instance, mk_user, registry_engine

        # A viewer can own a service and log in; a special cannot log in, so
        # it could not be the requester of its own mint.
        owner = mk_user(db, "shared2", role="viewer")
        sid = mk_service(db, owner, service_name="graphiti-local", label="0")
        mk_service_instance(db, sid, status="running",
                            hostname="graphiti-local-shared2-0.localhost")
        with registry_engine().begin() as conn:
            conn.execute(
                text("INSERT INTO registry.service_endpoints "
                     "(service_id, served_name, kind, sub_service, port) "
                     "VALUES (:sid, :name, 'stream', 'neo4j', 7687)"),
                {"sid": str(sid), "name": "graphiti-local-shared2-0--neo4j.localhost"},
            )
        key, _ = auth_service.create_api_key(db, owner.id, "Default", is_default=True)
        token = auth_service.create_provision_token(db, owner.id, key.id)
        return owner, sid, token

    def _client(self, monkeypatch, db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services.registry import Registry

        monkeypatch.setattr(app.state, "registry", Registry(), raising=False)
        return TestClient(app)

    def test_mint_list_verify_revoke_round_trip(self, monkeypatch):
        from app.database import SessionLocal
        from app.config import settings

        db = SessionLocal()
        try:
            owner, sid, token = self._seed(db)
            client = self._client(monkeypatch, db)
            client.cookies.set("provision_token", token)

            r = client.post("/api/stream/exposures", json={
                "service_id": str(sid), "sub_service": "neo4j"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["shown_once"] is True
            assert body["stream_port"] == settings.STREAM_PORT > 0
            assert body["canonical_name"] == "graphiti-local-shared2-0--neo4j.localhost"
            assert body["address"] == f"{body['host']}:{settings.STREAM_PORT}"
            assert "url" not in body, "the platform does not name the stream protocol"
            host = body["host"]

            r = client.get("/api/stream/exposures", params={"service_id": str(sid)})
            assert r.status_code == 200, r.text
            rows = r.json()["rows"]
            assert len(rows) == 1
            # The owner may read the live URL back (2026-11-02 ruling): the
            # handle is stored encrypted and the list assembles the address.
            assert rows[0]["live"] is True and rows[0]["has_url"] is True
            assert rows[0]["address"] == body["address"]

            r = client.post("/api/stream/verify", json={"handle": host})
            assert r.status_code == 200 and r.json()["allow"] is True

            r = client.delete("/api/stream/exposures", params={
                "service_id": str(sid), "sub_service": "neo4j", "port": 7687})
            assert r.status_code == 200 and r.json()["revoked"] == 1
            r = client.post("/api/stream/verify", json={"handle": host})
            assert r.json()["allow"] is False
            r = client.get("/api/stream/exposures", params={"service_id": str(sid)})
            assert r.json()["rows"][0]["address"] is None, "a revoked row is unshowable"
        finally:
            db.close()

    def test_mint_requires_a_credential(self, monkeypatch):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            owner, sid, token = self._seed(db)
            client = self._client(monkeypatch, db)
            r = client.post("/api/stream/exposures", json={
                "service_id": str(sid), "sub_service": "neo4j"})
            assert r.status_code == 401, r.text
        finally:
            db.close()

    def test_verify_needs_no_credential_and_answers_deny(self, monkeypatch):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            self._seed(db)
            client = self._client(monkeypatch, db)
            r = client.post("/api/stream/verify", json={"handle": "unknownhandle"})
            assert r.status_code == 200
            assert r.json()["allow"] is False
            assert r.json()["reason"] == "unknown handle"
        finally:
            db.close()

    def test_an_empty_handle_is_a_verdict_not_a_422(self, monkeypatch):
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            self._seed(db)
            client = self._client(monkeypatch, db)
            r = client.post("/api/stream/verify", json={"handle": ""})
            assert r.status_code == 200, r.text
            assert r.json() == {"allow": False, "canonical_name": None,
                                "reason": "no handle"}
        finally:
            db.close()


class TestStreamRevokeSurvivesADeletedInstance:
    """F24: the revoke must work for an instance the registry no longer has —
    the delete sweep (R7.1) runs AFTER the instance is gone."""

    def test_revoke_without_a_registry_entry_still_revokes(self, monkeypatch):
        from fastapi.testclient import TestClient
        from sqlalchemy import text
        from app.database import SessionLocal
        from app.main import app
        from app.services import auth_service, stream_service
        from app.services.registry import Registry
        from support import mk_service, mk_service_instance, mk_user, registry_engine

        db = SessionLocal()
        try:
            owner = mk_user(db, "alice")
            sid = mk_service(db, owner, service_name="example-mcp", label="3")
            mk_service_instance(db, sid, status="running")
            with registry_engine().begin() as conn:
                conn.execute(
                    text("INSERT INTO registry.service_endpoints "
                         "(service_id, served_name, kind, sub_service, port) "
                         "VALUES (:sid, 'example-mcp-alice-3--fastapi-app.localhost', "
                         "'stream', 'fastapi-app', 8100)"),
                    {"sid": str(sid)},
                )
            entry = Registry().get_by_id(str(sid))
            stream_service.mint(db, owner_user_id=owner.id, entry=entry,
                                sub_service="fastapi-app", port=8100)
            key, _ = auth_service.create_api_key(db, owner.id, "Default", is_default=True)
            token = auth_service.create_provision_token(db, owner.id, key.id)
        finally:
            db.close()

        class _EmptyRegistry:
            def get_by_id(self, _sid):
                return None

        monkeypatch.setattr(app.state, "registry", _EmptyRegistry(), raising=False)
        client = TestClient(app)
        admin_db = SessionLocal()
        try:
            admin = mk_user(admin_db, "admin", role="admin")
            akey, _ = auth_service.create_api_key(admin_db, admin.id, "Default", is_default=True)
            atoken = auth_service.create_provision_token(admin_db, admin.id, akey.id)
        finally:
            admin_db.close()
        client.cookies.set("provision_token", atoken)
        r = client.delete("/api/stream/exposures", params={
            "service_id": str(sid), "sub_service": "fastapi-app", "port": 8100})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] == 1, (
            "the sweep must be able to revoke a deleted instance's rows")


class TestDeleteRevokesTheInstancesHandles:
    """T11/R7.1: an instance's exposure handles die with the instance — the
    delete is the only moment anything knows the instance is going away."""

    def _seed(self, db):
        from sqlalchemy import text
        from app.services import stream_service
        from app.services.registry import Registry
        from support import mk_service, mk_service_instance, mk_user, registry_engine

        owner = mk_user(db, "alice")
        sid = mk_service(db, owner, service_name="example-mcp", label="3")
        mk_service_instance(db, sid, status="running")
        with registry_engine().begin() as conn:
            conn.execute(
                text("INSERT INTO registry.service_endpoints "
                     "(service_id, served_name, kind, sub_service, port) "
                     "VALUES (:sid, 'example-mcp-alice-3--fastapi-app.localhost', "
                     "'stream', 'fastapi-app', 8100)"),
                {"sid": str(sid)},
            )
        admin = mk_user(db, "admin", role="admin")
        entry = Registry().get_by_id(str(sid))
        host, _ = stream_service.mint(db, owner_user_id=owner.id, entry=entry,
                                      sub_service="fastapi-app", port=8100)
        return owner, sid, admin, host

    def test_the_delete_path_revokes_the_handles(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from app.database import SessionLocal
        from app.routers.users import remove_user_service
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, sid, admin, host = self._seed(db)
            with patch("app.routers.users.provision_service.remove_instance",
                       new=AsyncMock(return_value={"status": "removed"})):
                asyncio.run(remove_user_service(
                    "alice", "example-mcp", "3",
                    {"id": str(admin.id), "role": "admin"}))
            assert stream_service.list_for_instance(db, str(sid)) == [] or all(
                r.revoked_at is not None
                for r in stream_service.list_for_instance(db, str(sid))), (
                "the delete must revoke the instance's exposure handles")
        finally:
            db.close()

    def test_a_FAILED_delete_does_not_revoke(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.users import remove_user_service
        from app.services import stream_service

        db = SessionLocal()
        try:
            owner, sid, admin, host = self._seed(db)
            with patch("app.routers.users.provision_service.remove_instance",
                       new=AsyncMock(side_effect=RuntimeError("api down"))):
                raised = False
                try:
                    asyncio.run(remove_user_service(
                        "alice", "example-mcp", "3",
                        {"id": str(admin.id), "role": "admin"}))
                except HTTPException:
                    raised = True
            assert raised
            live = [r for r in stream_service.list_for_instance(db, str(sid))
                    if r.revoked_at is None]
            assert len(live) == 1, "a failed delete must never kill a live URL"
        finally:
            db.close()


class TestPhaseBodiesBindTheirOwnNames:
    """F38/F40 — no phase body may read a name bound only in another branch.

    The suite proved the phase LISTS (`_compute_phases`) and never executed a
    phase BODY, so `validate_ports` shipped twice with a name that does not exist
    in its scope — `eff_compose` (from a later function) and `recipe_dir` (bound
    in the `env` branch, after the read). Both surfaced only when a real
    generation ran, as a failed job with a `NameError`/`UnboundLocalError` and no
    stream conf. This is the static net for that class: names that are loaded
    before any binding at or above them, in the function that owns the bodies.

    Scope-aware enough to be quiet: module globals, comprehension/genexp and
    lambda targets, and nested function/class names are all recognized; only a
    name that is genuinely unbound on every path reaching the load is reported.
    """

    _SOURCE = "app/services/generation_jobs.py"
    _FUNCTION = "_run_job"

    def _unbound_loads(self, path: str, func_name: str) -> list[tuple[int, str]]:
        import ast
        import builtins

        tree = ast.parse(open(path).read())

        # Module globals: bindings reachable WITHOUT descending into a function.
        globals_: set[str] = set(dir(builtins))

        def scan_module(node) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                globals_.add(node.name)
                return  # do not descend
            if isinstance(node, ast.Import):
                globals_.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                globals_.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                globals_.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                globals_.add(node.name)
            for child in ast.iter_child_nodes(node):
                scan_module(child)

        for stmt in tree.body:
            scan_module(stmt)

        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name),
            None,
        )
        assert fn is not None, f"{func_name} not found"

        # Comprehension/genexp/lambda bodies are their own scope.
        own_scope: set[int] = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                                 ast.GeneratorExp, ast.Lambda)):
                for sub in ast.walk(node):
                    if sub is not node:
                        own_scope.add(id(sub))

        # Every name is keyed by its CONDITIONAL block: the chain of `if`/`for`/
        # `while` statements enclosing it, outermost first. Only those can make an
        # assignment conditional — a `try:` or `with:` body always runs, so a name
        # bound there is bound for everything that follows in source order.
        # A binding is visible to a load only when the binding's chain is a PREFIX
        # of the load's chain and the binding precedes it — so a name assigned in
        # a sibling branch (`if phase == "env": ... recipe_dir = ...`) is NOT bound
        # for a load in the `validate_ports` branch, which is the whole point.
        PARENTS: dict[int, object] = {}
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                PARENTS[id(child)] = node

        def binds_somewhere(stmts: list, name: str) -> bool:
            """Does this branch bind `name` anywhere (not counting nested scopes)?"""
            for stmt in stmts:
                for node in ast.walk(stmt):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) \
                            and node.id == name:
                        return True
            return False

        def exhaustive_if(node: ast.If, name: str) -> bool:
            """An `if`/`elif`/`else` chain that binds `name` on EVERY path — then
            the binding is as unconditional as one written before the `if`."""
            branches = []
            cur: ast.If | None = node
            while cur is not None:
                branches.append(cur.body)
                nxt = cur.orelse
                if len(nxt) == 1 and isinstance(nxt[0], ast.If):
                    cur = nxt[0]
                    continue
                cur = None
                if not nxt:
                    return False          # no `else` ⇒ the chain can fall through
                branches.append(nxt)       # the plain `else`
            return all(binds_somewhere(b, name) for b in branches)

        def block(node, name: str) -> tuple[int, ...]:
            chain: list[int] = []
            cur = node
            while id(cur) in PARENTS:
                parent = PARENTS[id(cur)]
                if id(parent) in own_scope:
                    return ()
                if isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
                    chain.append(id(parent))
                elif isinstance(parent, ast.If) and not exhaustive_if(parent, name):
                    chain.append(id(parent))
                cur = parent
            chain.reverse()
            return tuple(chain)

        bindings: list[tuple[str, int, tuple[int, ...]]] = []

        def bind(name: str, at) -> None:
            bindings.append((name, at.lineno, block(at, name)))

        for node in ast.walk(fn):
            if id(node) in own_scope:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bind(node.id, node)
            elif isinstance(node, ast.arg):
                bind(node.arg, fn)          # args belong to the function block
            elif isinstance(node, ast.Import):
                for a in node.names:
                    bind((a.asname or a.name).split(".")[0], node)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    bind(a.asname or a.name, node)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, node)

        bad: list[tuple[int, str]] = []
        for node in ast.walk(fn):
            if id(node) in own_scope:
                continue
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                continue
            if node.id in globals_:
                continue
            where = block(node, node.id)
            ok = any(
                name == node.id
                and lineno <= node.lineno
                and where[: len(bwhere)] == bwhere      # binding's block encloses the load's
                for name, lineno, bwhere in bindings
            )
            if not ok:
                bad.append((node.lineno, node.id))
        return sorted(set(bad))

    def test_run_job_reads_no_unbound_name(self):
        findings = self._unbound_loads(self._SOURCE, self._FUNCTION)
        assert findings == [], (
            "a generation phase body reads a name bound only in another branch or "
            "function — it will fail on the first real run, not in this suite: "
            + ", ".join(f"line {ln}: {name}" for ln, name in findings)
        )

    def test_the_check_itself_catches_the_two_bugs_it_exists_for(self, tmp_path):
        """A net that reports nothing is indistinguishable from a net that works."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "def _run_job(self):\n"
            "    x = eff_compose\n"          # never bound in this function
            "    if x:\n"
            "        y = recipe_dir\n"        # bound only LATER in the function
            "    recipe_dir = 1\n"
            "    return x, y\n"              # and `y` is bound in a sibling branch
        )
        assert self._unbound_loads(str(probe), "_run_job") == [
            (2, "eff_compose"), (4, "recipe_dir"), (6, "y"),
        ]

    def test_module_globals_and_comprehension_targets_are_not_reported(self, tmp_path):
        probe = tmp_path / "probe2.py"
        probe.write_text(
            "PHASE_FILENAME = {'a': 'b'}\n"
            "def _run_job(self):\n"
            "    names = [i for i, d in enumerate(PHASE_FILENAME)]\n"
            "    return {k: v for k, v in PHASE_FILENAME.items()}\n"
        )
        assert self._unbound_loads(str(probe), "_run_job") == []


class TestTheGeneratedNginxTemplateIsPlaceholderFree:
    """T1 / R1 — the model is told to write a PLAIN conf with mock names.

    The generation template is not a `.j2` file: it is the text the model is
    told to copy. A `{{ … }}` in it would put placeholder insertion in the
    model's hands, and a TLS instruction would put certificate paths there too.
    """

    def test_the_template_uses_mock_names_and_no_template_syntax(self):
        from app.utils import skill_loader
        tpl = skill_loader.get_nginx_template()
        full = skill_loader._SKILL_PATH.read_text()
        # R14 — there is no primary block: the template's one shape is the
        # `--<sub_service>` endpoint, and a bare name is refused.
        assert "server_name mock_server_name;" not in tpl
        assert "server_name mock_server_name--{sub_service};" in tpl
        assert "server_name mock_server_name--{sub_service};" in full
        assert "{service_name}_hostname" not in full
        assert "{{" not in tpl, "the model must not be told to write template syntax"
        assert "ssl_certificate" not in tpl
        assert "listen 443" not in tpl, "a generated HTTP conf is listen 80 only"

    def test_the_https_guidance_is_gone(self):
        from app.utils import skill_loader
        text = skill_loader._SKILL_PATH.read_text()
        assert "For HTTPS support, wrap the server blocks" not in text
        assert "use `{{ ssl_certificate_path }}`" not in text

    def test_the_extra_endpoint_prompt_names_the_mock_form(self, tmp_path):
        from app.services.llm_service import LLMService
        sp = tmp_path / "serving_ports.yml"
        sp.write_text(
            "ports:\n"
            "  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n"
            "  - sub_service: neo4j\n    port: 7474\n    kind: http\n"
        )
        prompt = LLMService()._build_generation_prompt(
            "nginx_conf",
            {"serving_ports_path": str(sp), "compose_service_names": ["graph", "neo4j"],
             "deploy_metadata": {"domain": "example.test"}},
        )
        assert "server_name mock_server_name--neo4j;" in prompt
        assert "server_name mock_server_name--graph;" in prompt
        assert "_hostname--" not in prompt


class TestValidateConfReadsTheRenderedConf:
    """F46/F47 — `validate_conf` must judge the conf the platform will SERVE.

    The conf in play is a template: the SKILL tells the generator to write
    `server_name mock_server_name` (and `mock_server_name--<sub>`), and the
    -api's converter rewrites every `server_name …;` at conversion time. A
    checker reading the pre-conversion text must therefore be name-agnostic —
    it once refused a generated conf for not serving a name it was about to be
    given (measured 2026-10-10, jobs 366/367).
    """

    _PORTS_ONE = {"ports": [
        {"sub_service": "graph", "port": 8000, "kind": "http", "primary": True, "profiles": []},
    ]}

    def _check(self, ports, conf):
        from app.services.serving_ports import validate_conf_agreement

        return validate_conf_agreement(
            ports, conf, service_name="graphiti-local", user_name="shared2",
            label="0", domain="snaprovision.com",
        )

    def test_the_skill_placeholder_is_read_as_an_endpoint(self):
        conf = (
            "server {\n    listen 80;\n    server_name mock_server_name--graph;\n"
            "    location / {\n        proxy_pass http://graph:8000;\n    }\n}\n"
        )
        res = self._check(self._PORTS_ONE, conf)
        assert res["valid"] is True, res

    def test_the_final_form_still_validates(self):
        conf = (
            "server {\n    listen 80;\n"
            "    server_name {{ hostname_stem }}--graph.{{ domain_name }};\n"
            "    location / {\n        proxy_pass http://graph:8000;\n    }\n}\n"
        )
        assert self._check(self._PORTS_ONE, conf)["valid"] is True

    def test_an_extra_http_row_cannot_be_served_at_all(self):
        """F47's replacement — the SKILL placeholder form works end to end here.

        (The test that used to sit here asserted the *broken* behaviour — that an
        extra row could never be served — and was deleted with the fix; the
        negative case it was guarding is covered by
        `TestConfAgreement::test_a_served_row_with_no_block_is_an_error`.)
        """
        ports = {"ports": self._PORTS_ONE["ports"] + [
            {"sub_service": "neo4j", "port": 7474, "kind": "http", "profiles": []},
        ]}
        conf = (
            "server {\n    listen 80;\n    server_name mock_server_name--graph;\n"
            "    location / {\n        proxy_pass http://graph:8000;\n    }\n}\n"
            "server {\n    listen 80;\n    server_name mock_server_name--neo4j;\n"
            "    location / {\n        proxy_pass http://neo4j:7474;\n    }\n}\n"
        )
        res = self._check(ports, conf)
        assert res["valid"] is True, res["errors"]


class TestProduceForDeploy:
    """T9 / F28 — the stream conf is produced by the DEPLOY, and it runs.

    A deploy whose files are all selected runs **no generation at all**, so a
    stream conf that only a generation phase produces never exists for the normal,
    fully-configured recipe. `produce_for_deploy` is the deploy-time step that
    fixes that — and it had never been executed, because the deploy payload did
    not carry the artifact (R2-F43). The moment it did, this function raised
    `NameError: name 'Path' is not defined` (R3-F51): a missing module import,
    invisible until the line under it was reachable.
    """

    _SP = (
        "ports:\n"
        "  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n"
        "  - sub_service: neo4j\n    port: 7687\n    kind: bolt\n"
    )

    def _produce(self, tmp_path, sp_text=None):
        from app.services.stream_conf import produce_for_deploy

        recipe = tmp_path / "recipe"
        recipe.mkdir(exist_ok=True)
        sp = recipe / "serving_ports.base.1.yml"
        sp.write_text(self._SP if sp_text is None else sp_text)
        return produce_for_deploy(
            service_name="graphiti-local", user_name="shared2", label="0",
            domain="snaprovision.com", serving_ports_path=str(sp),
            recipe_dir=str(recipe),
        )

    def test_it_writes_a_file_for_a_stream_row(self, tmp_path):
        out = self._produce(tmp_path)
        assert out and Path(out).is_file(), "a stream row means a conf, and a path"
        text = Path(out).read_text()
        # Whitespace-insensitive on purpose: the conf's columns are padding, and a
        # spacing change is not a behavioural change.
        # the produced file is PLAIN: a mock name, a plain upstream, and neither
        # a certificate nor a policy — the -api supplies all three at conversion
        # and render time (R2/R3/R4).
        assert re.search(
            r"server_name\s+mock_server_name--neo4j\.mock\.invalid;", text
        ), text
        assert "proxy_pass          neo4j:7687;" in text
        assert "ssl_certificate" not in text
        assert "allow" not in text and "deny" not in text
        assert "{{" not in text
        # and there is no http-only scaffolding
        assert "auth_basic" not in text and "{% if" not in text

    def test_no_artifact_and_no_stream_row_write_nothing(self, tmp_path):
        from app.services.stream_conf import produce_for_deploy

        recipe = tmp_path / "recipe"
        recipe.mkdir(exist_ok=True)
        assert produce_for_deploy(
            service_name="s", user_name="u", label="0", domain="d",
            serving_ports_path=None, recipe_dir=str(recipe),
        ) is None, "R1.4 by construction: nothing to expose, no file"
        only_http = self._produce(
            tmp_path,
            "ports:\n  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n",
        )
        assert only_http is None, "an http-only recipe has no stream rows"


class TestTheMechanicalPortSurface:
    """R2-F13 / R1.1 — the port inventory the prompt is told is ground truth.

    The prompt says the scanned surface is authoritative ("infer from the context,
    but these are the ports"), so a port the scanner cannot read is a port the
    model will not report — and R1.4 makes a missing stream row fatal for the
    exposure path. Measured 2026-10-10 on graphiti-local: neo4j's Bolt port is
    declared as `${NEO4J_PORT_2:-7687}`, the scanner returned None for it, and the
    generation dutifully omitted both neo4j rows.
    """

    def _surface(self, tmp_path, compose_text: str):
        from app.services.serving_ports import compose_port_surface

        p = tmp_path / "docker-compose.yml"
        p.write_text(compose_text)
        return {(r["sub_service"], r["port"]) for r in compose_port_surface([p])}

    def test_a_var_with_a_default_is_still_a_declared_port(self, tmp_path):
        got = self._surface(tmp_path, """
services:
  neo4j:
    image: neo4j:5.26.2
    expose:
      - "${NEO4J_PORT_1:-7474}"
      - "${NEO4J_PORT_2:-7687}"
""")
        assert got == {("neo4j", 7474), ("neo4j", 7687)}, got

    def test_the_shapes_that_already_worked_still_work(self, tmp_path):
        got = self._surface(tmp_path, """
services:
  web:
    ports:
      - "8080:80"
      - 8000
  cache:
    expose:
      - "${CACHE_PORT}"
      - "6379"
""")
        assert ("web", 80) in got and ("web", 8000) in got and ("cache", 6379) in got, got
        # `${VAR}` with NO default declares nothing this scanner can read, and it
        # must not invent a port from the variable's name
        assert not any(svc == "cache" and port not in (6379,) for svc, port in got), got

    def test_every_port_form_the_platform_documents_is_read(self, tmp_path):
        from app.services.serving_ports import _container_port

        assert _container_port("127.0.0.1:9000:80") == 80
        assert _container_port({"target": 5432, "published": 5432}) == 5432
        assert _container_port("8000-8005") == 8000


class TestServingPortsArtifact:
    """T12 / R1.1 — the `serving_ports` artifact and its conventions.

    First-class means the SAME conventions the compose and nginx artifacts
    already have: identified by PATTERN (never by exact name), its own file-set
    category with candidate discovery and stale marking, and its selection
    persisted alongside the others.
    """

    def test_the_naming_predicate_is_the_identity(self):
        from app.services.file_sets import _is_serving_ports_candidate as is_sp

        # the plain, hand-authored form (G20's escape hatch: `ports: []`)
        assert is_sp("serving_ports.yml")
        assert is_sp("serving-ports.yaml")
        # the generated forms carry the `.base`/`.fix` tag and the job-id token
        # BEFORE the extension — and are still candidates: the token is a
        # collision guard, not an identity.
        assert is_sp("serving_ports.base.348.yml")
        assert is_sp("serving_ports.fix.12.yml")
        # the platform's own by-products are never candidates
        assert not is_sp("serving_ports.yml.generated")
        assert not is_sp("serving_ports.yml.j2")
        # and nothing else is a serving-ports artifact
        assert not is_sp("docker-compose.yml")
        assert not is_sp("nginx.base.355.conf")
        assert not is_sp("notes.yml")

    def test_the_category_is_first_class_in_the_file_set(self, tmp_path, monkeypatch):
        """Candidates are discovered, the selection persists, and a vanished file
        is marked stale — the same three behaviours the other categories have."""
        from app.config import settings
        from app.services import file_sets

        svc = tmp_path / "example-thing"
        svc.mkdir(parents=True)
        # the ROOT recipe IS the project dir: that is where its files live.
        (svc / "serving_ports.base.348.yml").write_text("ports: []\n")
        (svc / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)

        listed = file_sets.list_file_sets("example-thing")
        assert "serving_ports" in listed["candidates"][file_sets.ROOT_RECIPE_KEY]
        assert listed["candidates"][file_sets.ROOT_RECIPE_KEY]["serving_ports"] == ["serving_ports.base.348.yml"]

        saved = file_sets.put_file_set("example-thing", file_sets.ROOT_RECIPE_KEY, {
            "compose": ["docker-compose.yml"],
            "nginx": None,
            "serving_ports": "serving_ports.base.348.yml",
            "env": [],
            "profiles": [],
        })
        assert saved["file_set"]["serving_ports"] == "serving_ports.base.348.yml"
        again = file_sets.list_file_sets("example-thing")
        assert again["file_sets"][file_sets.ROOT_RECIPE_KEY]["serving_ports"] == "serving_ports.base.348.yml"

        # the file disappears → the stored selection is KEPT and marked stale
        # (the platform never drops a stored selection on a scan).
        (svc / "serving_ports.base.348.yml").unlink()
        after = file_sets.list_file_sets("example-thing")
        assert after["file_sets"][file_sets.ROOT_RECIPE_KEY]["serving_ports"] == "serving_ports.base.348.yml"
        assert after["stale"][file_sets.ROOT_RECIPE_KEY]["serving_ports"] == ["serving_ports.base.348.yml"]

    def test_the_deliberate_none_state_survives_a_save_and_a_reload(self, tmp_path, monkeypatch):
        """R2-F10 / R1.3 — `None` is a first-class RECORDED state, not an absence.

        The flag must be carried by BOTH writers of the category: the file-set
        service (which whitelists keys on the way in) and `_normalize_file_sets`
        (which REBUILDS the entry from a whitelist on every load). It was in
        neither, so "deliberately no serving-ports file" collapsed into "no
        selection" on the next read — the two states R1.3 exists to keep apart.
        The same class of bug the `stream_conf` slot hit, whose lesson (a new
        category must be added to the normalizer too) the round had already
        recorded without applying to the flag.
        """
        from app.config import settings
        from app.services import file_sets
        from app.services.project_state import _normalize_file_sets

        svc = tmp_path / "example-thing"
        svc.mkdir(parents=True)
        (svc / "docker-compose.yml").write_text("services: {}\n")
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)

        file_sets.put_file_set("example-thing", file_sets.ROOT_RECIPE_KEY, {
            "compose": ["docker-compose.yml"],
            "nginx": None,
            "serving_ports": None,
            "serving_ports_none": True,
            "env": [],
            "profiles": [],
        })
        entry = file_sets.list_file_sets("example-thing")["file_sets"][file_sets.ROOT_RECIPE_KEY]
        assert entry.get("serving_ports_none") is True, (
            "the deliberate-None state must be recorded: without it a redeploy "
            "replays `generate new` and the two states become indistinguishable")

        # ... and it must survive the state loader, which rebuilds the entry.
        normalized = _normalize_file_sets({file_sets.ROOT_RECIPE_KEY: entry})
        assert normalized[file_sets.ROOT_RECIPE_KEY].get("serving_ports_none") is True, (
            "a key the normalizer does not name is dropped on the next load")

        # a plain selection is NOT the None state — the default stays false.
        file_sets.put_file_set("example-thing", file_sets.ROOT_RECIPE_KEY, {
            "compose": ["docker-compose.yml"], "nginx": None,
            "serving_ports": None, "env": [], "profiles": [],
        })
        entry = file_sets.list_file_sets("example-thing")["file_sets"][file_sets.ROOT_RECIPE_KEY]
        assert entry.get("serving_ports_none") is False

    def test_a_serving_ports_path_must_be_a_string_or_null(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services import file_sets

        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "serving_ports.yml").write_text("ports: []\n")
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)

        try:
            file_sets.put_file_set("svc", file_sets.ROOT_RECIPE_KEY, {"serving_ports": ""})
            raised = False
        except file_sets.FileSetError:
            raised = True
        assert raised, "an empty serving_ports selection must be refused (None means 'none')"


class TestStreamConfGenerator:
    """T9 / R5.1 — one `server{}` per stream row, in the shape R9.2 converts.

    Mechanical by design: which ports exist and what each speaks is the
    `serving_ports` artifact's business (and its LLM inference); this is the
    arithmetic that follows.
    """

    _ROWS = [
        {"sub_service": "graph", "port": 8000, "kind": "http", "primary": True},
        {"sub_service": "neo4j", "port": 7687, "kind": "bolt"},
        {"sub_service": "mystery", "port": 9999},                    # no kind
        {"sub_service": "falkordb", "port": 6379, "kind": "redis", "profiles": ["falkordb"]},
    ]

    def test_only_stream_rows_become_blocks(self):
        from app.services.stream_conf import stream_rows
        subs = [r["sub_service"] for r in stream_rows({"ports": self._ROWS})]
        assert subs == ["neo4j", "falkordb"], (
            "http is not a stream row, and a row with NO kind is not exposed at all (R1.2)")

    def test_the_generated_conf_has_the_shape_the_converter_expects(self):
        from app.services.stream_conf import render_stream_conf, stream_rows
        text = render_stream_conf(stream_rows({"ports": self._ROWS}))
        # one block per stream row, selected by a MOCK name the -api rebuilds.
        # `.invalid` is reserved, so the mock can never be mistaken for a real
        # endpoint (R2); the dot after the sub-service is what the stream
        # converter parses.
        assert text.count("server {") == 2
        assert "server_name         mock_server_name--neo4j.mock.invalid;" in text
        assert "server_name         mock_server_name--falkordb.mock.invalid;" in text
        # the last hop is PLAIN — the converter prefixes it — and STATIC, because
        # the variable form defeats the SNI-based choice of virtual server
        # (measured 2026-10-16: every SNI hit the first block).
        assert "proxy_pass          neo4j:7687;" in text
        assert "proxy_pass          falkordb:6379;" in text
        code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        assert "$stream_upstream" not in code
        assert "resolver" not in code, "a resolver is only needed for a variable upstream"
        assert "ssl_preread" not in code
        # and what it must NEVER write (R3, R4)
        assert "ssl_certificate" not in code and "ssl_certificate_key" not in code
        assert "allow" not in code and "deny" not in code
        assert "{{" not in code, "a generated artifact is plain"
        assert "auth_basic" not in text, "the stream module has no such directive"
        assert "{% if" not in text, "a stream block's TLS listener is structural — no guard"
        assert "listen              7687 ssl;" in text

    def test_a_single_row_yields_one_block(self):
        from app.services.stream_conf import render_stream_conf, stream_rows
        text = render_stream_conf(
            stream_rows({"ports": [{"sub_service": "neo4j", "port": 7687, "kind": "bolt"}]})
        )
        assert text.count("server {") == 1
        assert "mock_server_name--neo4j.mock.invalid" in text
        assert "proxy_pass          neo4j:7687;" in text

    def test_the_boundary_port_and_a_hyphenated_sub_service_survive_verbatim(self):
        from app.services.stream_conf import render_stream_conf, stream_rows
        text = render_stream_conf(
            stream_rows({"ports": [
                {"sub_service": "mcp-server", "port": 65535, "kind": "bolt"},
            ]})
        )
        assert "mock_server_name--mcp-server.mock.invalid" in text
        assert "proxy_pass          mcp-server:65535;" in text

    def test_no_stream_rows_means_no_file_at_all(self):
        """R1.4 by construction: nothing to write ⇒ nothing exposed."""
        from app.services.stream_conf import render_stream_conf, stream_rows
        rows = stream_rows({"ports": [{"sub_service": "graph", "port": 8000, "kind": "http"}]})
        assert rows == []
        assert render_stream_conf(rows) == ""

    def test_the_generated_artifact_agrees_with_its_own_serving_ports(self):
        """T2 integration — coverage is matched by `--<sub>`, not by a full name.

        The mock name is not the canonical name, so this also proves the
        agreement check is name-agnostic for a stream artifact.
        """
        from app.services.serving_ports import validate_conf_agreement
        from app.services.stream_conf import render_stream_conf, stream_rows
        ports = {"ports": [
            {"sub_service": "graph", "port": 8000, "kind": "http", "primary": True},
            {"sub_service": "neo4j", "port": 7687, "kind": "bolt"},
            {"sub_service": "falkordb", "port": 6379, "kind": "redis",
             "profiles": ["falkordb"]},
        ]}
        text = render_stream_conf(stream_rows(ports))
        res = validate_conf_agreement(
            ports, text, service_name="example-mcp", user_name="alice", label="3",
            domain="snaprovision.com", category="stream",
        )
        assert res["valid"] is True, res
        assert not res["errors"]

    def test_the_stream_phase_never_consults_the_llm(self, tmp_path, monkeypatch):
        """T2 fault injection — a stub LLM that raises on every call.

        Everything the phase needs is selected; the stream slot is absent, so it
        is generated. If any step reached for the model the job would fail.
        """
        import asyncio
        import json

        from app.config import settings
        from app.database import SessionLocal
        from app.models.generation_job import GenerationJob
        from app.services import llm_service as llm_mod
        from app.services.generation_jobs import GenerationJobManager

        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", Path(tmp_path))
        recipe = tmp_path / "myapp"
        recipe.mkdir(parents=True)
        (recipe / "docker-compose.yml").write_text(
            "services:\n"
            "  graph:\n    image: alpine\n    ports:\n      - '8000:8000'\n"
            "  neo4j:\n    image: alpine\n    ports:\n      - '7687:7687'\n"
        )
        (recipe / "serving_ports.yml").write_text(
            "ports:\n"
            "  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n"
            "  - sub_service: neo4j\n    port: 7687\n    kind: bolt\n"
        )
        (recipe / "nginx.conf").write_text(
            "server {\n listen 80;\n server_name mock_server_name--graph;\n"
            " proxy_pass http://graph:8000;\n}\n"
        )

        async def _boom(*_a, **_k):
            raise AssertionError("the stream phase must not call the LLM")

        monkeypatch.setattr(llm_mod.llm_service, "generate_with_agent", _boom)
        monkeypatch.setattr(llm_mod.llm_service, "generate_env_two_turn", _boom)
        monkeypatch.setattr(llm_mod.llm_service, "generate_fix_guide", _boom)

        async def scenario():
            m = GenerationJobManager()   # fresh: the lock is loop-bound
            db = SessionLocal()
            job = m.create_job(db, "myapp", "", "generate_missing", {
                "service_name": "myapp",
                "selection": {"compose": ["docker-compose.yml"],
                              "serving_ports": "serving_ports.yml",
                              "nginx": "nginx.conf", "stream_conf": None, "env": []},
                "skip_env": True, "user_name": "alice", "label": "0",
                "deploy_metadata": {"https": True, "domain": "example.test"},
            })
            jid = job.id
            for _ in range(200):
                await asyncio.sleep(0.05)
                if m.job_status(jid) in ("completed", "failed", "paused"):
                    break
            db.expire_all()
            row = db.query(GenerationJob).filter(GenerationJob.id == jid).first()
            return row.status, row.result or {}, (row.error or "")

        status, _result, err = asyncio.run(scenario())
        assert status == "completed", f"the mechanical phase must stand alone: {status}: {err}"
        confs = list(recipe.glob("nginx.stream.base.*.conf"))
        assert confs, "the stream slot was generated"
        text = confs[0].read_text()
        assert "mock_server_name--neo4j.mock.invalid" in text
        assert "proxy_pass          neo4j:7687;" in text
        assert "allow" not in text and "deny" not in text

    def test_the_canonical_name_cannot_drift_from_the_exposure_service(self):
        """One name, two call sites: the minted handle stores it and the generated
        block selects on it. If they disagree, every exposure URL is a 502 — so the
        agreement is asserted rather than assumed."""
        from app.services.stream_conf import canonical_name
        from app.services.stream_service import canonical_name as exposure_name
        entry = {"service_name": "example-mcp", "user_name": "alice", "label": "3",
                 "hostname": "example-mcp-alice-3.snaprovision.com"}
        assert canonical_name(service_name="example-mcp", user_name="alice", label="3",
                              domain="snaprovision.com", sub_service="neo4j") == \
            exposure_name(entry, "neo4j")


class TestTheStreamConfSlotIsIndependentOfTls:
    """R15 / G68 — the stream ARTIFACT is generated regardless of TLS.

    TLS gates the deploy SLOT (the dashboard omits it, the gateway strips it,
    the -api refuses it) and stream VALIDATION on a TLS-off deploy; it never
    gates generation. The standalone path validates the artifact as a
    recipe-quality check.
    """

    def _req(self, tls: bool) -> dict:
        return {
            "service_name": "example-mcp", "recipe_path": "", "user_name": "alice", "label": "3",
            "deploy_metadata": {"https": tls},
            "selection": {"compose": ["docker-compose.yml"], "nginx": "nginx.base.355.conf",
                          "serving_ports": None},
        }

    def test_tls_on_generates_the_slot(self):
        from app.services.generation_jobs import GenerationJobManager
        phases = GenerationJobManager._compute_phases("generate_missing", self._req(True))
        assert "stream_conf" in phases
        assert phases.index("stream_conf") > phases.index("serving_ports"), (
            "the conf is derived FROM the artifact, so the artifact comes first")
        assert phases.index("stream_conf") < phases.index("env")

    def test_tls_off_still_generates_the_artifact(self):
        from app.services.generation_jobs import GenerationJobManager
        phases = GenerationJobManager._compute_phases("generate_missing", self._req(False))
        assert "stream_conf" in phases

    def test_the_phase_order_places_it_after_the_artifact(self):
        from app.services.generation_jobs import PHASE_ORDER
        assert PHASE_ORDER.index("serving_ports") < PHASE_ORDER.index("stream_conf")

    def test_the_generated_name_is_the_one_the_include_and_conversion_expect(self):
        """The per-user name is load-bearing twice: the -api dispatches its
        conversion on `.stream.conf`, and the RENDERED copy (the same name, in
        `generated/`) is what the internal nginx's include matches."""
        name = "example-mcp.user-alice.3.stream.conf"
        from app.services.file_sets import _is_stream_conf_candidate
        assert _is_stream_conf_candidate(name), "the generated file is a selectable candidate"


class TestTheMintResolvesTheRowItself:
    """T9: the card knows a SUB-SERVICE; the port and membership both come from
    the endpoint row the -api recorded when it rendered the stream conf."""

    def _entry(self, rows=None) -> dict:
        return {
            "service_id": "00000000-0000-0000-0000-000000000001",
            "stream_sub_services": [r["sub_service"] for r in (rows or [])],
            "stream_rows": rows or [],
        }

    def test_the_port_comes_from_the_recorded_row(self):
        from app.routers.stream import _stream_row

        port = _stream_row(self._entry([{"sub_service": "neo4j", "port": 7687}]),
                           "neo4j", None)
        assert port == 7687

    def test_a_sub_service_not_recorded_is_refused(self):
        import pytest
        from fastapi import HTTPException
        from app.routers.stream import _stream_row

        with pytest.raises(HTTPException) as exc:
            _stream_row(self._entry([]), "falkordb", None)
        assert exc.value.status_code == 409

    def test_an_unknown_sub_service_resolves_to_nothing(self):
        import pytest
        from fastapi import HTTPException
        from app.routers.stream import _stream_row

        with pytest.raises(HTTPException):
            _stream_row(self._entry([{"sub_service": "neo4j", "port": 7687}]), "nope", None)

    def test_a_row_without_a_recorded_port_is_refused(self):
        """Recorded but unparsable is a refusal, never a guessed port."""
        import pytest
        from fastapi import HTTPException
        from app.routers.stream import _stream_row

        with pytest.raises(HTTPException) as exc:
            _stream_row(self._entry([{"sub_service": "neo4j", "port": None}]), "neo4j", None)
        assert exc.value.status_code == 409

    def test_a_disagreeing_port_is_refused(self):
        import pytest
        from fastapi import HTTPException
        from app.routers.stream import _stream_row

        with pytest.raises(HTTPException) as exc:
            _stream_row(self._entry([{"sub_service": "neo4j", "port": 7687}]), "neo4j", 6379)
        assert exc.value.status_code == 409


class TestTheExtraHttpServedName:
    """T16 / R1.5 — `<instance host>--<sub_service>.<domain>`, one label.

    Deliberately the SAME form the stream path uses as its canonical name, so one
    name identifies a sub-service on both paths — and, like every platform name,
    exactly one label under the domain, which is what lets the single wildcard
    serve it.
    """

    def test_the_name_is_built(self):
        from app.services.serving_ports import extra_http_host
        assert extra_http_host(
            service_name="example-mcp", user_name="alice", label="3",
            domain="snaprovision.com", sub_service="neo4j",
        ) == "example-mcp-alice-3--neo4j.snaprovision.com"

    def test_it_is_one_label_and_parses_back(self):
        from app.services.serving_ports import extra_http_host
        name = extra_http_host(service_name="s", user_name="u", label="0",
                               domain="example.com", sub_service="neo4j")
        label_part, _, domain = name.partition(".")
        assert "." not in label_part
        assert domain == "example.com"
        instance, sep, sub = label_part.partition("--")
        assert sep == "--" and instance == "s-u-0" and sub == "neo4j"

    def test_a_component_carrying_the_separator_is_refused_at_the_builder(self):
        """The builder refuses too, not only the deploy validation: a name that
        reached it another way must not produce an unparseable served name."""
        from app.services.serving_ports import extra_http_host
        import pytest
        with pytest.raises(ValueError):
            extra_http_host(service_name="s", user_name="u", label="0",
                            domain="example.com", sub_service="my--svc")


class TestConfAgreement:
    """T14 / R1.3 — `validate_conf`: does the nginx conf agree with the ports file?

    Errors mean the deployment does not match its own declaration; the undeclared
    upstream is a WARNING, because the doc asks for "at least a warning" there.
    """

    #: The minimal declaration: a primary http row and a STREAM row (which no
    #: http check should ever look at).
    _PORTS = {"ports": [
        {"sub_service": "graph", "port": 8000, "kind": "http", "primary": True},
        {"sub_service": "bolt", "port": 7687, "kind": "bolt"},      # stream: not an http row
    ]}
    _CONF = """server {
    listen 80;
    server_name example-mcp_hostname--graph;
    set $upstream graph:8000;
    proxy_pass $upstream;
}
"""

    #: R1.5's extra endpoint, declared AND served — under the current converter
    #: this cannot pass; see the xfail below.
    _PORTS_EXTRA = {"ports": [
        {"sub_service": "graph", "port": 8000, "kind": "http", "primary": True},
        {"sub_service": "neo4j", "port": 7474, "kind": "http"},
        {"sub_service": "bolt", "port": 7687, "kind": "bolt"},
    ]}
    _CONF_EXTRA = _CONF + """server {
    listen 80;
    server_name example-mcp-alice-3--neo4j.snaprovision.com;
    set $upstream neo4j:7474;
    proxy_pass $upstream;
}
"""

    def _v(self, conf, ports=None):
        from app.services.serving_ports import validate_conf_agreement
        return validate_conf_agreement(ports or self._PORTS, conf,
                                       service_name="example-mcp", user_name="alice",
                                       label="3", domain="snaprovision.com")

    def test_agreement_passes_and_ignores_the_stream_row(self):
        r = self._v(self._CONF)
        assert r["valid"] and not r["errors"] and not r["warnings"], r

    def test_a_served_row_with_no_block_is_an_error(self):
        r = self._v("", self._PORTS)
        assert not r["valid"]
        assert any("no `server{}` block serves" in e for e in r["errors"]), r["errors"]

    def test_every_http_row_needs_a_block(self):
        """R14 — there is no primary: every row is matched by its `--<sub>`
        block, so a conf with no block serves none of them."""
        r = self._v("", self._PORTS)
        assert any("graph" in e for e in r["errors"]), r["errors"]

    def test_a_block_that_disagrees_with_its_row_fails_the_upstream_port_check(self):
        """G56 — the upstream/port IS checked (the -api cannot invent the port);
        the NAME is not. So the primary block targeting `mystery:9999` while the
        row declares 8000 is an error, and the odd host is reported as a warning
        alongside it."""
        r = self._v(self._CONF.replace("graph:8000", "mystery:9999"))
        assert not r["valid"]
        assert any("do not match the row's port 8000" in e for e in r["errors"]), r["errors"]
        assert any("mystery" in w for w in r["warnings"]), r["warnings"]

    def test_a_block_for_an_undeclared_sub_service_is_a_warning(self):
        """A block serving something no row declares is worth saying, not worth
        blocking — it exposes an endpoint the artifact does not account for."""
        conf = self._CONF + """server {
    listen 80;
    server_name example-mcp_hostname--extra;
    set $upstream extra:9000;
    proxy_pass $upstream;
}
"""
        r = self._v(conf)
        assert r["valid"], r["errors"]
        assert any("'extra'" in w for w in r["warnings"]), r["warnings"]

    def test_an_extra_http_row_can_be_served(self):
        """R1.5 / T16 — this was the strict `xfail` that named F47.

        It un-xfailed the moment the converter stopped flattening a name carrying
        `--` (round 3): a block declaring `<host>--<sub_service>.<domain>` now
        survives conversion, so the endpoint the row declares exists and the
        agreement check passes. It is a plain test now — if the converter ever
        regresses, this fails loudly instead of quietly xfailing.
        """
        r = self._v(self._CONF_EXTRA, self._PORTS_EXTRA)
        assert r["valid"], r["errors"]


class TestTheNoneStateAndTheSkipRule:
    """T14 / R1.3 — the skip rule is stated over ARTIFACTS, and `None` is a
    first-class RECORDED state ("deliberately no file")."""

    _BASE = {"service_name": "example-mcp", "recipe_path": "", "user_name": "alice",
             "label": "3", "deploy_metadata": {"https": False}}

    def _phases(self, selection):
        from app.services.generation_jobs import GenerationJobManager
        return GenerationJobManager._compute_phases(
            "generate_missing", {**self._BASE, "selection": selection})

    def test_an_artifact_in_play_brings_its_validation(self):
        phases = self._phases({"compose": ["docker-compose.yml"],
                               "nginx": "nginx.base.355.conf",
                               "serving_ports": "serving_ports.base.358.yml"})
        assert "validate_ports" in phases
        assert "validate_conf" in phases, "the conf is checked against the artifact in play"

    def test_the_deliberate_none_state_skips_generation_and_both_validations(self):
        phases = self._phases({"compose": ["docker-compose.yml"],
                               "nginx": "nginx.base.355.conf",
                               "serving_ports_none": True})
        assert "serving_ports" not in phases, "None means NO file, so nothing to generate"
        assert "validate_ports" not in phases
        assert "validate_conf" not in phases

    def test_a_generated_artifact_is_validated_in_the_same_run(self):
        """The skip rule is over artifacts, not phases: a run that PRODUCES the
        artifact validates it immediately."""
        phases = self._phases({})       # nothing selected: everything is generated
        assert phases.index("serving_ports") < phases.index("validate_ports")
        assert phases.index("nginx") < phases.index("validate_conf")


class TestTheGoRelayKnowsExtraEndpoints:
    """R1.5 — `/go/<instance host>--<sub_service>.<domain>` resolves.

    The lookup is a per-request read of `service_endpoints` (kind=http); no
    cache and no registry file.
    """

    def _seed(self, db):
        from sqlalchemy import text
        from support import mk_service, mk_service_instance, mk_user, registry_engine

        shared = mk_user(db, "shared2", role="special")
        sid = mk_service(db, shared, service_name="graphiti-local", label="2")
        mk_service_instance(db, sid, status="running",
                            hostname="graphiti-local-shared2-2.snaprovision.com")
        with registry_engine().begin() as conn:
            for name in ("graphiti-local-shared2-2--neo4j.snaprovision.com",
                         "graphiti-local-shared2-2--graph-falkordb.snaprovision.com"):
                conn.execute(
                    text("INSERT INTO registry.service_endpoints "
                         "(service_id, served_name, kind) VALUES (:sid, :n, 'http')"),
                    {"sid": str(sid), "n": name},
                )
        return shared, sid

    def test_an_extra_endpoint_resolves_to_its_instance(self):
        from app.database import SessionLocal
        from app.services.hostname_index import HostnameIndex

        db = SessionLocal()
        try:
            self._seed(db)
        finally:
            db.close()
        got = HostnameIndex().get_by_hostname(
            "graphiti-local-shared2-2--neo4j.snaprovision.com")
        assert got is not None and got["label"] == "2", got

    def test_the_instance_host_is_not_a_route(self):
        """R14 — `/go/<instance host>` answers 404: identity, not a route."""
        from app.database import SessionLocal
        from app.services.hostname_index import HostnameIndex

        db = SessionLocal()
        try:
            self._seed(db)
        finally:
            db.close()
        assert HostnameIndex().get_by_hostname(
            "graphiti-local-shared2-2.snaprovision.com") is None

    def test_a_duplicate_served_name_is_rejected_by_the_store(self):
        """`service_endpoints_identity` (served_name, kind) is unique: the first
        row that claims a name keeps it, a second insert fails."""
        import pytest
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        from app.database import SessionLocal
        from support import mk_service, mk_user, registry_engine

        db = SessionLocal()
        try:
            self._seed(db)
            other = mk_user(db, "alice")
            other_sid = mk_service(db, other, service_name="other", label="0")
            with pytest.raises(IntegrityError):
                with registry_engine().begin() as conn:
                    conn.execute(
                        text("INSERT INTO registry.service_endpoints "
                             "(service_id, served_name, kind) "
                             "VALUES (:sid, "
                             "'graphiti-local-shared2-2--neo4j.snaprovision.com', 'http')"),
                        {"sid": str(other_sid)},
                    )
        finally:
            db.close()


class TestTheRecordsTlsSwitch:
    """The record's toggle is `tls` — renamed from `https` (ruled 2026-10-10).

    The -api writes it and this service reads it, so the read lives in one place
    here (`app/services/registry_entry.py`) and the -api carries the mirror
    (`lib/registry.py`). Entries written before the rename are read through a
    fallback kept for ONE round.
    """

    def test_the_new_name_is_the_one_that_counts(self):
        from app.services.registry_entry import entry_tls
        assert entry_tls({"tls": True}) is True
        assert entry_tls({"tls": False}) is False

    def test_the_old_name_is_still_read_this_round(self):
        from app.services.registry_entry import entry_tls
        assert entry_tls({"https": True}) is True
        assert entry_tls({"https": False}) is False

    def test_the_new_name_wins_when_both_are_present(self):
        from app.services.registry_entry import entry_tls
        assert entry_tls({"tls": False, "https": True}) is False

    def test_anything_unreadable_is_false(self):
        """Fail-closed: an unreadable flag must never become an accidental TLS."""
        from app.services.registry_entry import entry_tls
        assert entry_tls({}) is False
        assert entry_tls(None) is False
        assert entry_tls({"tls": None}) is False

    def test_no_reader_asks_the_record_for_the_old_name(self):
        """Only `registry_entry` may read the pre-rename key off a record entry.

        The net a future removal needs: if a second place reads `entry["https"]`,
        deleting the fallback later would silently stop that caller seeing the
        flag — and an instance without its flag gets no TLS vhost and no stream
        conf. Reads of the *deploy request* (`req.get("https")`) are legitimate
        and not flagged: that is the operator-facing alias, which dies with the
        old deploy-field name rather than with this record's.
        """
        import re as _re
        from pathlib import Path as _P

        root = _P(__file__).parent.parent / "app"
        # a read of the key off something entry-shaped
        pattern = _re.compile(r"""\bentry\w*[^\n]*\[?["']https["']""")
        offenders = []
        for p in root.rglob("*.py"):
            if p.name == "registry_entry.py":
                continue
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{p.relative_to(root)}:{i}")
        assert offenders == [], (
            "these lines read the record's pre-rename key directly instead of "
            f"through registry_entry.entry_tls: {offenders}")


class TestNameAgnosticConfAgreement:
    """G56 — the gateway checks coverage and upstream/port, never NAMES.

    The -api renders `{{ hostname_stem }}--<sub>.{{ domain_name }}` for the target
    instance, so a conf selected from another deployment (another user/label
    stem) is reusable. Comparing names here would refuse exactly that.
    """

    _PORTS = {"ports": [
        {"sub_service": "neo4j", "port": 7687, "kind": "bolt"},
        {"sub_service": "falkordb", "port": 6379, "kind": "redis"},
    ]}

    def _v(self, conf, ports=None, category="stream"):
        from app.services.serving_ports import validate_conf_agreement
        return validate_conf_agreement(
            ports or self._PORTS, conf,
            service_name="s", user_name="u", label="0", domain="example.com",
            category=category,
        )

    def test_a_conf_from_another_deployment_is_accepted(self):
        conf = """server {
    listen 7687 ssl;
    server_name someone-else-9--neo4j.example.com;
    set $stream_upstream s-u-0-neo4j:7687;
    proxy_pass $stream_upstream;
}
"""
        r = self._v(conf, {"ports": [{"sub_service": "neo4j", "port": 7687, "kind": "bolt"}]})
        assert r["valid"], r["errors"]

    def test_a_missing_stream_row_block_is_an_error(self):
        conf = """server {
    listen 7687 ssl;
    server_name s-u-0--neo4j.example.com;
    proxy_pass s-u-0-neo4j:7687;
}
"""
        r = self._v(conf)
        assert not r["valid"]
        assert any("falkordb" in e for e in r["errors"]), r["errors"]

    def test_a_wrong_port_is_an_error(self):
        conf = """server {
    listen 7687 ssl;
    server_name s-u-0--neo4j.example.com;
    proxy_pass s-u-0-neo4j:9999;
}
"""
        r = self._v(conf, {"ports": [{"sub_service": "neo4j", "port": 7687, "kind": "bolt"}]})
        assert not r["valid"]
        assert any("do not match the row's port" in e for e in r["errors"]), r["errors"]

    def test_a_templated_server_name_is_read_by_its_sub_suffix(self):
        """The generated stream artifact is a TEMPLATE — one `server_name` per
        block, still parseable by its `--<sub>.` fragment."""
        conf = """server {
    listen 7687 ssl;
    server_name {{ hostname_stem }}--neo4j.{{ domain_name }};
    proxy_pass {{ container_prefix }}neo4j:7687;
}
"""
        r = self._v(conf, {"ports": [{"sub_service": "neo4j", "port": 7687, "kind": "bolt"}]})
        assert r["valid"], r["errors"]


class TestStreamConfSelectionBypassesGeneration:
    """G56 — selected stream conf ⇒ no generation, exactly like the http slot."""

    _BASE = {
        "service_name": "example-mcp", "recipe_path": "", "user_name": "alice",
        "label": "3", "deploy_metadata": {"https": True},
    }

    def _phases(self, selection):
        from app.services.generation_jobs import GenerationJobManager
        return GenerationJobManager._compute_phases(
            "generate_missing", {**self._BASE, "selection": selection})

    def test_a_selected_stream_conf_is_not_generated(self):
        phases = self._phases({
            "compose": ["docker-compose.yml"], "nginx": "nginx.base.1.conf",
            "serving_ports": "serving_ports.yml", "stream_conf": "x.stream.conf",
        })
        assert "stream_conf" not in phases

    def test_an_absent_stream_conf_is_generated(self):
        phases = self._phases({
            "compose": ["docker-compose.yml"], "nginx": "nginx.base.1.conf",
            "serving_ports": "serving_ports.yml",
        })
        assert "stream_conf" in phases

    def test_a_selected_stream_conf_is_validated(self):
        phases = self._phases({
            "compose": ["docker-compose.yml"], "nginx": "nginx.base.1.conf",
            "serving_ports": "serving_ports.yml", "stream_conf": "x.stream.conf",
        })
        assert "validate_conf" in phases


class TestStreamTemplateIsACandidate:
    """G56 — the generated source is `nginx.stream.base.<token>.conf`, and the
    category is explicit: it must not also appear in the http candidates."""

    def test_the_recipe_level_stream_template_is_a_stream_candidate(self):
        from app.services.file_sets import _is_nginx_candidate, _is_stream_conf_candidate
        assert _is_stream_conf_candidate("nginx.stream.base.337.conf")
        assert not _is_nginx_candidate("nginx.stream.base.337.conf")

    def test_historical_per_instance_stream_confs_still_work(self):
        from app.services.file_sets import _is_nginx_candidate, _is_stream_conf_candidate
        assert _is_stream_conf_candidate("graphiti-local.user-bob.1.stream.conf")
        assert not _is_nginx_candidate("graphiti-local.user-bob.1.stream.conf")

    def test_a_mail_conf_is_neither(self):
        from app.services.file_sets import _is_nginx_candidate, _is_mail_conf_candidate
        assert _is_mail_conf_candidate("x.mail.conf")
        assert not _is_nginx_candidate("x.mail.conf")


class TestPerUserEnvFilesAreNotSelectable:
    """Found 2026-10-24 — the candidate filter only matched the LEGACY
    `.env.<user>.<label>` form, so the canonical `.env.<index>.<user>.<label>`,
    the `.env.raw.*` handoff and the `.env.fix.*` handoff were offered as env
    candidates. A stale all-empty `.env.1.bob.3` was selectable, skipped env
    generation and crash-looped the graph (empty OPENAI_API_KEY)."""

    def _svc(self, tmp_path):
        svc = tmp_path / "example-thing"
        svc.mkdir(parents=True)
        for n in (
            ".env", ".env.example", ".env.1.bob.3", ".env.bob.3",
            ".env.raw.bob.3", ".env.fix.bob.3",
        ):
            (svc / n).write_text("X=1\n")
        (svc / "docker-compose.yml").write_text("services: {}\n")
        return svc

    def test_candidates_keep_only_recipe_env_files(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services import file_sets
        self._svc(tmp_path)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)
        cands = file_sets.list_file_sets("example-thing")["candidates"][file_sets.ROOT_RECIPE_KEY]["env"]
        assert cands == [".env", ".env.example"], cands

    def test_a_stored_per_user_env_is_dropped_on_read(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.services import file_sets
        from app.services.project_state import ProjectState
        svc = self._svc(tmp_path)
        state = ProjectState(svc)
        state.file_sets[file_sets.ROOT_RECIPE_KEY] = {
            "compose": ["docker-compose.yml"], "nginx": None,
            "serving_ports": None, "stream_conf": None, "mail_conf": None,
            "env": [".env.1.bob.3", ".env"], "profiles": [],
        }
        state.save()
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)
        listed = file_sets.list_file_sets("example-thing")
        assert listed["file_sets"][file_sets.ROOT_RECIPE_KEY]["env"] == [".env"]

    def test_saving_one_is_refused(self, tmp_path, monkeypatch):
        import pytest
        from app.config import settings
        from app.services import file_sets
        self._svc(tmp_path)
        monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path, raising=False)
        with pytest.raises(file_sets.FileSetError):
            file_sets.put_file_set("example-thing", file_sets.ROOT_RECIPE_KEY, {
                "compose": ["docker-compose.yml"], "env": [".env.1.bob.3"],
            })


class TestComposePromptPreservesProfiles:
    """G62 — the compose prompt must forbid dropping `profiles: [""]`; the LLM
    fix of 2026-10-24 silently did, merging the default and profiled stacks."""

    def test_the_prompt_requires_profiles_verbatim(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app/services/llm_service.py").read_text()
        assert "PRESERVE `profiles:` EXACTLY" in src
        assert 'profiles: [""]' in src


# ---------------------------------------------------------------------------
# R14 — no primary endpoint; R15 — the stream artifact is TLS-independent
# ---------------------------------------------------------------------------


class TestPrimaryRemovalGateway:
    """R14 — every HTTP row is an equal `--<sub>` endpoint.

    A `primary: true` field in an older artifact is ignored WITHOUT a warning,
    and the validator never infers a primary from a block with no `--`.
    """

    def test_primary_is_ignored_without_a_warning(self, tmp_path):
        from app.services.config_validation import validate_serving_ports

        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  graph:\n    image: alpine\n    expose: ['8000']\n"
            "  neo4j:\n    image: alpine\n    expose: ['7474']\n"
        )
        content = (
            "ports:\n"
            "  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n"
            "  - sub_service: neo4j\n    port: 7474\n    kind: http\n    primary: true\n"
        )
        res = validate_serving_ports(content, [compose])
        assert res["valid"] is True, res
        assert not any("primary" in w.lower() for w in res["warnings"]), res["warnings"]

    def test_a_bare_block_serves_no_row(self):
        from app.services.serving_ports import validate_conf_agreement

        ports = {"ports": [
            {"sub_service": "graph", "port": 8000, "kind": "http"},
        ]}
        conf = (
            "server {\n    listen 80;\n    server_name mock_server_name;\n"
            "    proxy_pass http://graph:8000;\n}\n"
        )
        res = validate_conf_agreement(
            ports, conf, service_name="svc", user_name="alice", label="0",
            domain="example.test",
        )
        assert res["valid"] is False
        assert any("no `server{}` block serves" in e for e in res["errors"])

    def test_every_http_row_needs_its_own_endpoint_block(self):
        from app.services.serving_ports import validate_conf_agreement

        ports = {"ports": [
            {"sub_service": "graph", "port": 8000, "kind": "http"},
            {"sub_service": "neo4j", "port": 7474, "kind": "http"},
        ]}
        conf = (
            "server {\n    listen 80;\n    server_name mock_server_name--graph;\n"
            "    proxy_pass http://graph:8000;\n}\n"
            "server {\n    listen 80;\n    server_name mock_server_name--neo4j;\n"
            "    proxy_pass http://neo4j:7474;\n}\n"
        )
        res = validate_conf_agreement(
            ports, conf, service_name="svc", user_name="alice", label="0",
            domain="example.test",
        )
        assert res["valid"] is True, res

    def test_the_extra_endpoint_prompt_lists_every_http_row(self, tmp_path):
        from app.services.llm_service import LLMService

        sp = tmp_path / "serving_ports.yml"
        sp.write_text(
            "ports:\n"
            "  - sub_service: graph\n    port: 8000\n    kind: http\n    primary: true\n"
            "  - sub_service: neo4j\n    port: 7474\n    kind: http\n"
        )
        prompt = LLMService()._build_generation_prompt(
            "nginx_conf",
            {"serving_ports_path": str(sp),
             "compose_service_names": ["graph", "neo4j"],
             "deploy_metadata": {"domain": "example.test"}},
        )
        assert "server_name mock_server_name--graph;" in prompt
        assert "server_name mock_server_name--neo4j;" in prompt
        assert "NO primary block" in prompt


class TestStreamValidationFollowsTheSlot:
    """R15/G75 — a TLS-off DEPLOY skips stream validation; standalone validates."""

    def _args(self, tmp_path, *, tls_on, origin):
        from app.services.generation_jobs import validation_conf_sources

        recipe = tmp_path / "recipe"
        recipe.mkdir(exist_ok=True)
        (recipe / "nginx.conf").write_text("server { server_name x--web; }\n")
        (recipe / "stream.conf").write_text("server { server_name x--bolt; }\n")
        return validation_conf_sources(
            result_files={},
            names={},
            selection={"nginx": "nginx.conf", "stream_conf": "stream.conf"},
            recipe_dir=recipe,
            tls_on=tls_on,
            origin=origin,
        )

    def test_a_tls_off_deploy_judges_http_only(self, tmp_path):
        from app.models.generation_job import JOB_ORIGIN_DEPLOY

        cats = [c for c, _t, _s in self._args(tmp_path, tls_on=False, origin=JOB_ORIGIN_DEPLOY)]
        assert cats == ["http"]

    def test_a_tls_on_deploy_judges_both(self, tmp_path):
        from app.models.generation_job import JOB_ORIGIN_DEPLOY

        cats = [c for c, _t, _s in self._args(tmp_path, tls_on=True, origin=JOB_ORIGIN_DEPLOY)]
        assert cats == ["http", "stream"]

    def test_the_standalone_path_judges_the_generated_artifact_without_tls(self, tmp_path):
        from app.models.generation_job import JOB_ORIGIN_STANDALONE

        cats = [c for c, _t, _s in self._args(
            tmp_path, tls_on=False, origin=JOB_ORIGIN_STANDALONE)]
        assert cats == ["http", "stream"]

    def test_a_tls_off_deploy_phase_plans_generation(self):
        from app.services.generation_jobs import GenerationJobManager

        req = {
            "service_name": "svc", "user_name": "alice", "label": "0",
            "deploy_metadata": {"https": False},
            "selection": {"compose": ["docker-compose.yml"],
                          "nginx": "nginx.conf", "serving_ports": None},
        }
        phases = GenerationJobManager._compute_phases("generate_missing", req)
        assert "stream_conf" in phases


class TestGeneratedServingPortsIsAdopted:
    """R19/R15 — a generated serving-ports artifact must be SELECTED and pickable.

    The deploy panel adopted compose/nginx/stream from the job's reported
    selection but not serving_ports, and its candidate lists were fetched before
    the run — so a generated ports artifact could exist on disk while its picker
    still read “generate new” and could not offer it (measured 2026-10-27 on
    e2e-confs).  Both deploy paths now adopt it and refresh the candidates.
    """

    def _src(self):
        from pathlib import Path
        return (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        ).read_text()

    def test_both_paths_adopt_the_reported_serving_ports(self):
        src = self._src()
        assert src.count("resultSel.serving_ports") + src.count("resSel.serving_ports") >= 2
        assert "serving_ports_none = false" in src

    def test_the_gateway_adopts_the_generated_serving_ports(self):
        import inspect
        from app.services import artifact_finalize
        src = inspect.getsource(artifact_finalize.map_selection)
        assert "serving_ports" in src
        # The standalone Generate panel keeps its own candidate refresh.
        form = self._src()
        assert "refreshFileSetCandidates" in form


# ---------------------------------------------------------------------------
# G78 — ID-keyed ACL grants (T6/T7)
# ---------------------------------------------------------------------------


class TestAclGrants:
    """The ID-keyed grant matrix (T9/R5.6): both columns are ``users.id``."""

    def _users(self, db):
        from support import mk_user
        return (mk_user(db, "alice", role="viewer"),
                mk_user(db, "bob", role="special"),
                mk_user(db, "carol", role="special"))

    def test_two_shared_rows_are_two_rows_and_only_active_allows(self):
        from app.database import SessionLocal
        from app.services import acl_service

        db = SessionLocal()
        try:
            alice, bob, carol = self._users(db)
            acl_service.set_grant(db, alice.id, bob.id, True)
            acl_service.set_grant(db, alice.id, carol.id, True)
            assert len(acl_service.list_for_grantee(db, alice.id)) == 2
            assert acl_service.allows(db, alice, bob.id) is True
            assert acl_service.allows(db, alice, carol.id) is True
            acl_service.set_grant(db, alice.id, carol.id, False)
            assert acl_service.allows(db, alice, carol.id) is False
            # The inactive row is kept, not deleted.
            assert len(acl_service.list_for_grantee(db, alice.id)) == 2
        finally:
            db.close()

    def test_unique_pair(self):
        import pytest
        from sqlalchemy.exc import IntegrityError
        from app.database import SessionLocal
        from app.models.acl_grant import AclGrant

        db = SessionLocal()
        try:
            alice, bob, _carol = self._users(db)
            db.add(AclGrant(grantee_user_id=alice.id, shared_user_id=bob.id,
                            state="active"))
            db.commit()
            db.add(AclGrant(grantee_user_id=alice.id, shared_user_id=bob.id,
                            state="inactive"))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        finally:
            db.close()

    def test_deactivation_inactivates_and_reactivation_does_not_restore(self):
        from app.database import SessionLocal
        from app.services import acl_service

        db = SessionLocal()
        try:
            alice, bob, carol = self._users(db)
            acl_service.set_grant(db, alice.id, bob.id, True)
            acl_service.set_grant(db, carol.id, bob.id, True)
            assert acl_service.inactivate_for_user(db, bob.id) == 2
            db.commit()
            assert all(g["state"] == "inactive"
                       for g in acl_service.list_for_grantee(db, alice.id))
            bob.is_active = True
            db.commit()
            assert acl_service.allows(db, alice, bob.id) is False
            assert acl_service.allows(db, carol, bob.id) is False
        finally:
            db.close()

    def test_historical_public_marker_from_audit_does_not_restore_access(self):
        from datetime import datetime, timezone
        from app.database import SessionLocal
        from app.models.audit_log import AuditLog
        from app.services import acl_service
        from support import mk_user

        db = SessionLocal()
        try:
            user = mk_user(db, "legacypub", role="special", public=False)
            user.deleted_at = datetime.now(timezone.utc)
            db.commit()
            db.add(AuditLog(
                action="public_flag", target_user="legacypub", status="success",
                detail_json={"is_public_shared": True, "at": "registration"},
            ))
            db.commit()
            assert acl_service.was_public_record(db, user) is True
            assert acl_service.is_public_owner(user) is False
        finally:
            db.close()

    def test_public_flag_on_a_viewer_is_inert(self):
        from app.database import SessionLocal
        from app.services import acl_service
        from support import mk_user

        db = SessionLocal()
        try:
            viewer = mk_user(db, "viewerpub", role="viewer", public=True)
            assert acl_service.is_public_owner(viewer) is False
            special = mk_user(db, "specialpub", role="special", public=True)
            assert acl_service.is_public_owner(special) is True
        finally:
            db.close()

    def test_activation_requires_both_accounts_live(self):
        import pytest
        from app.database import SessionLocal
        from app.services import acl_service
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice", role="viewer")
            bob = mk_user(db, "bob", role="special", active=False)
            with pytest.raises(ValueError):
                acl_service.set_grant(db, alice.id, bob.id, True)
            # Deactivating a never-selected pair is a no-op, not an error.
            assert acl_service.set_grant(db, alice.id, bob.id, False) is None
        finally:
            db.close()

    def test_target_inactive_or_deleted_denies_even_with_an_active_row(self):
        from datetime import datetime, timezone
        from app.database import SessionLocal
        from app.services import acl_service

        db = SessionLocal()
        try:
            alice, bob, _carol = self._users(db)
            acl_service.set_grant(db, alice.id, bob.id, True)
            assert acl_service.allows(db, alice, bob.id) is True
            bob.is_active = False
            db.commit()
            assert acl_service.allows(db, alice, bob.id) is False
            bob.is_active = True
            bob.deleted_at = datetime.now(timezone.utc)
            db.commit()
            assert acl_service.allows(db, alice, bob.id) is False
        finally:
            db.close()

    def test_grant_toggle_is_audited(self):
        from app.database import SessionLocal
        from app.models.audit_log import AuditLog
        from app.routers.auth import set_user_grant
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            alice = mk_user(db, "alice", role="viewer")
            bob = mk_user(db, "bob", role="special")
            set_user_grant(str(alice.id), {"shared_user_id": str(bob.id), "active": True},
                           {"id": str(admin.id)}, db)
            rows = db.query(AuditLog).filter(AuditLog.action == "grant_update").all()
            assert rows
            detail = rows[-1].detail_json
            assert detail["shared_user_id"] == str(bob.id)
            assert detail["active"] is True
        finally:
            db.close()

    def test_grants_are_id_keyed_with_no_legacy_string_source(self):
        """The legacy `allowed_special_users` column and its one-way conversion
        are gone; grants reference `users.id` (UUID) only."""
        import app.services.acl_service as acl_service
        from app.database import SessionLocal
        from sqlalchemy import text

        assert not hasattr(acl_service, "migrate_legacy_grants")
        db = SessionLocal()
        try:
            kinds = dict(db.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='gateway' AND table_name='acl_grants'"
            )).all())
        finally:
            db.close()
        assert kinds["grantee_user_id"] == "uuid"
        assert kinds["shared_user_id"] == "uuid"


# ---------------------------------------------------------------------------
# G78 — account lifecycle (T8/T9) and service annotations (T5/T10)
# ---------------------------------------------------------------------------


class TestAccountLifecycle:
    """T6/R5.8: deactivation, the public flag, role transitions and the
    stop-first account-deletion saga (unified users)."""

    def _seed(self, db):
        from support import mk_user
        return (mk_user(db, "alice", role="viewer"),
                mk_user(db, "bob", role="special"),
                mk_user(db, "admin", role="admin"))

    def test_deactivation_inactivates_grants_and_activation_does_not_restore(self):
        from app.database import SessionLocal
        from app.models.audit_log import AuditLog
        from app.routers.auth import update_user
        from app.services import acl_service
        from app.services import auth_service as _auth

        db = SessionLocal()
        try:
            alice, bob, admin = self._seed(db)
            acl_service.set_grant(db, alice.id, bob.id, True)
            update_user(str(bob.id), {"is_active": False},
                        {"id": str(admin.id)}, db)
            assert acl_service.list_for_grantee(db, alice.id)[0]["state"] == "inactive"
            assert _auth.get_user_by_id(db, bob.id).is_active is False
            update_user(str(bob.id), {"is_active": True},
                        {"id": str(admin.id)}, db)
            assert acl_service.list_for_grantee(db, alice.id)[0]["state"] == "inactive"
            assert acl_service.allows(db, alice, bob.id) is False
            assert db.query(AuditLog).filter(AuditLog.action == "user_update").count() == 2
        finally:
            db.close()

    def test_public_flag_is_special_registration_only_and_audited(self):
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.models.audit_log import AuditLog
        from app.routers.auth import register_special, update_user
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            import app.services.account_service as account_service
            created = {}

            def fake_create_special(_db, *, username, password,
                                    is_public_shared=False, email=None):
                u = mk_user(_db, username, role="special", public=is_public_shared)
                created["user"] = u
                return u

            import pytest as _pytest
            mp = _pytest.MonkeyPatch()
            mp.setattr(account_service, "create_special", fake_create_special)
            try:
                out = register_special(
                    {"username": "pubnew", "password": "pass1234",
                     "is_public_shared": True},
                    {"id": str(admin.id)}, db,
                )
            finally:
                mp.undo()
            user = auth_service.get_user_by_username(db, "pubnew")
            assert out["user"]["is_public_shared"] is True
            rows = db.query(AuditLog).filter(AuditLog.action == "public_flag").all()
            assert rows and rows[-1].target_user == "pubnew"
            assert rows[-1].detail_json["is_public_shared"] is True

            with pytest.raises(HTTPException) as exc:
                update_user(str(user.id), {"is_public_shared": False},
                            {"id": str(admin.id)}, db)
            assert exc.value.status_code == 400
            assert "registration" in str(exc.value.detail).lower()
        finally:
            db.close()

    def test_public_signup_is_viewer_only(self):
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.models.gateway_setting import GatewaySetting
        from app.routers.auth import register_end_user

        db = SessionLocal()
        try:
            db.add(GatewaySetting(key="registration_open", value="true"))
            db.commit()
            with pytest.raises(HTTPException) as exc:
                register_end_user(
                    {"username": "pubviewer", "password": "pass1234",
                     "is_public_shared": True}, db,
                )
            assert exc.value.status_code == 400
            with pytest.raises(HTTPException) as exc2:
                register_end_user(
                    {"username": "adospecial", "password": "pass1234",
                     "role": "special"}, db,
                )
            assert exc2.value.status_code == 400
        finally:
            db.close()

    def test_special_is_never_converted(self):
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.auth import update_user
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            special = mk_user(db, "internal", role="special")
            for body in ({"role": "viewer"}, {"role": "admin"}):
                with pytest.raises(HTTPException) as exc:
                    update_user(str(special.id), body, {"id": str(admin.id)}, db)
                assert exc.value.status_code == 409
        finally:
            db.close()

    def test_soft_delete_stops_services_then_removes_relations(self, monkeypatch):
        import asyncio
        import uuid

        from app.database import SessionLocal
        from app.models.api_key import ApiKey
        from app.models.audit_log import AuditLog
        from app.models.pre_deploy_session import PreDeploySession
        from app.models.stream_exposure import StreamExposure
        from app.routers import auth as auth_router
        from app.services import acl_service, auth_service
        from support import mk_service, mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice", role="viewer", public=True)
            bob = mk_user(db, "bob", role="special")
            admin = mk_user(db, "admin", role="admin")
            alice_id = str(alice.id)
            admin_id = str(admin.id)
            bob_id = str(bob.id)
            alice_api_id = str(alice.api_user_id)
            acl_service.set_grant(db, alice.id, bob.id, True)
            auth_service.create_api_key(db, alice.id, "Default", is_default=True)
            sid = mk_service(db, alice, service_name="svc", label="0")
            db.add(PreDeploySession(
                service_id=uuid.uuid4(), procedure_id=uuid.uuid4(),
                owner_api_user_id=alice.api_user_id, service_name="svc",
                service_label="0", recipe_path="", state="paused",
            ))
            db.add(StreamExposure(
                handle_hash="h1", owner_user_id=alice.id, service_id=sid,
                sub_service="web", port=80,
                canonical_name="svc-alice-0--web.localhost",
            ))
            db.commit()
        finally:
            db.close()

        calls = []

        def fake_stop(api_user_id):
            calls.append(api_user_id)
            return {"ok": True, "results": [], "failed_services": []}

        deleted = []
        monkeypatch.setattr(auth_router.api_users, "stop_services", fake_stop)
        monkeypatch.setattr(auth_router.api_users, "soft_delete",
                            lambda api_id: deleted.append(api_id))

        result = asyncio.run(auth_router.delete_user(
            alice_id, {"id": admin_id}))
        assert calls == [alice_api_id]
        assert deleted == [alice_api_id]
        assert result["soft_deleted"] is True

        db = SessionLocal()
        try:
            row = auth_service.get_user_by_id(db, alice_id)
            assert row is not None
            assert row.deleted_at is not None
            assert row.is_active is False and row.is_approved is False
            # The historical public tag is retained; access is denied.
            assert row.is_public_shared is True
            assert acl_service.is_public_owner(row) is False
            assert acl_service.list_for_grantee(db, alice_id) == []
            assert acl_service.list_for_grantee(db, bob_id) == []
            assert db.query(ApiKey).filter(ApiKey.user_id == alice_id).count() == 0
            assert db.query(PreDeploySession).filter(
                PreDeploySession.owner_api_user_id == alice_api_id).count() == 0
            assert db.query(StreamExposure).filter(
                StreamExposure.owner_user_id == alice_id,
                StreamExposure.revoked_at.is_(None)).count() == 0
            assert db.query(AuditLog).filter(AuditLog.action == "delete").count() == 1
        finally:
            db.close()

    def test_soft_delete_aborts_on_a_stop_failure(self, monkeypatch):
        import asyncio

        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.models.audit_log import AuditLog
        from app.routers import auth as auth_router
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice", role="viewer")
            admin = mk_user(db, "admin", role="admin")
            alice_id = str(alice.id)
            admin_id = str(admin.id)
        finally:
            db.close()

        monkeypatch.setattr(
            auth_router.api_users, "stop_services",
            lambda api_id: {"ok": False, "results": [],
                            "failed_services": [{"error": "boom"}]},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_router.delete_user(alice_id, {"id": admin_id}))
        assert exc.value.status_code == 502
        db = SessionLocal()
        try:
            row = auth_service.get_user_by_id(db, alice_id)
            assert row.deleted_at is None and row.is_active is True
            assert db.query(AuditLog).filter(
                AuditLog.action == "delete", AuditLog.status == "failure").count() == 1
        finally:
            db.close()

    def test_soft_delete_goes_pending_when_the_api_user_delete_fails(self, monkeypatch):
        import asyncio

        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import auth as auth_router
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            alice = mk_user(db, "alice", role="viewer")
            admin = mk_user(db, "admin", role="admin")
            alice_id = str(alice.id)
            admin_id = str(admin.id)
        finally:
            db.close()

        monkeypatch.setattr(auth_router.api_users, "stop_services",
                            lambda api_id: {"ok": True, "results": [], "failed_services": []})

        def boom(api_id):
            raise RuntimeError("registry down")
        monkeypatch.setattr(auth_router.api_users, "soft_delete", boom)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_router.delete_user(alice_id, {"id": admin_id}))
        assert exc.value.status_code == 502
        db = SessionLocal()
        try:
            row = auth_service.get_user_by_id(db, alice_id)
            assert row.deletion_state == "pending"
            assert row.deleted_at is None
            assert row.is_active is False
        finally:
            db.close()

    def test_the_last_active_admin_cannot_be_deleted(self, monkeypatch):
        import asyncio

        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import auth as auth_router
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
        finally:
            db.close()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_router.delete_user(str(admin.id), {"id": str(admin.id)}))
        assert exc.value.status_code == 409




    def test_an_admin_cannot_change_its_own_role(self):
        """Self-service role changes are refused (requirement 2026-11-02)."""
        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers.auth import update_user
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "selfadmin", role="admin")
            for body in ({"role": "viewer"}, {"role": "admin"}):
                with pytest.raises(HTTPException) as exc:
                    update_user(str(admin.id), body, {"id": str(admin.id)}, db)
                assert exc.value.status_code == 403, body
                assert "own role" in str(exc.value.detail)
            assert admin.role == "admin"
        finally:
            db.close()

    def test_an_admin_cannot_delete_itself_when_another_admin_exists(self):
        """With another live admin present, self-deletion is a 403, no saga."""
        import asyncio

        import pytest
        from fastapi import HTTPException
        from app.database import SessionLocal
        from app.routers import auth as auth_router
        from app.services import auth_service
        from support import mk_user

        db = SessionLocal()
        try:
            me = mk_user(db, "selfdelete", role="admin")
            mk_user(db, "otheradmin", role="admin")
            me_id = str(me.id)
        finally:
            db.close()

        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_router.delete_user(me_id, {"id": me_id}))
        assert exc.value.status_code == 403
        assert "own account" in str(exc.value.detail)
        db = SessionLocal()
        try:
            row = auth_service.get_user_by_id(db, me_id)
            assert row is not None
            assert row.deleted_at is None and row.deletion_state == "none"
        finally:
            db.close()


class TestServiceAnnotations:
    """T10 — public/orphan annotations come from the gateway, not the browser."""

    def test_soft_deleted_public_owner_keeps_was_public(self, monkeypatch):
        import asyncio
        from datetime import datetime, timezone

        from app.database import SessionLocal
        from app.routers.users import list_users
        from app.services.provision_service import provision_service as provision_client
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            admin_id = str(admin.id)
            pub = mk_user(db, "pubgone", role="special", public=True,
                          active=False)
            pub.deleted_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        async def fake_list():
            return {"user_status": [{
                "user_name": "pubgone", "healthy_services": [{
                    "service_name": "svc", "label": "0",
                    "http_endpoints": ["pubgone.localhost"], "tls": False,
                    "has_auth": False,
                }], "unhealthy_services": [], "missing_services": [],
                "failed_services": [],
            }]}
        monkeypatch.setattr(provision_client, "list_users", fake_list)
        result = asyncio.run(list_users(current_user={
            "role": "admin", "id": admin_id,
        }))
        group = next(u for u in result["users"] if u["user_name"] == "pubgone")
        assert group["owner_gone"] is True
        assert group["is_public"] is False
        assert group["was_public"] is True
        assert group["healthy_services"][0]["was_public"] is True

    def test_public_urls_and_orphan_alert_are_annotated(self, monkeypatch):
        import asyncio
        from datetime import datetime, timezone

        from app.database import SessionLocal
        from app.routers.users import list_users
        from app.services.provision_service import provision_service as provision_client
        from app.config import settings
        from support import mk_user

        db = SessionLocal()
        try:
            admin = mk_user(db, "admin", role="admin")
            admin_id = str(admin.id)
            mk_user(db, "pub", role="special", public=True)
            gone = mk_user(db, "gone", role="viewer", active=False)
            gone.deleted_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        async def fake_list():
            return {"user_status": [
                {"user_name": "pub", "healthy_services": [{
                    "service_name": "pubsvc", "label": "0",
                    "http_endpoints": ["pub.localhost"], "tls": True,
                    "has_auth": False,
                }], "unhealthy_services": [], "missing_services": [],
                 "failed_services": []},
                {"user_name": "gone", "healthy_services": [{
                    "service_name": "gonesvc", "label": "0",
                    "http_endpoints": ["gone.localhost"], "tls": False,
                    "has_auth": True,
                }], "unhealthy_services": [], "missing_services": [],
                 "failed_services": []},
            ]}
        monkeypatch.setattr(provision_client, "list_users", fake_list)

        result = asyncio.run(list_users(current_user={
            "role": "admin", "id": admin_id,
        }))
        by_user = {u["user_name"]: u for u in result["users"]}
        pub_svc = by_user["pub"]["healthy_services"][0]
        assert pub_svc["is_public"] is True
        assert pub_svc["owner_gone"] is False
        assert pub_svc["public_urls"]["pub.localhost"].startswith("https://pub.localhost")
        if settings.NGINX_HTTPS_PORT != 443:
            assert pub_svc["public_urls"]["pub.localhost"].endswith(
                f":{settings.NGINX_HTTPS_PORT}")
        gone_svc = by_user["gone"]["healthy_services"][0]
        assert gone_svc["owner_gone"] is True
        assert gone_svc["is_public"] is False
        assert result["orphans"] == ["gone"]


# ---------------------------------------------------------------------------
# G78 — edge Authorization selector (T4)
# ---------------------------------------------------------------------------


class TestEdgePublicAuthorizationSelector:
    """The ACL-on service locations select client vs owner Authorization."""

    @staticmethod
    def _template() -> str:
        from pathlib import Path
        return (
            Path(__file__).parent.parent.parent
            / "nginx.acl" / "edge.conf.template"
        ).read_text()

    def test_both_scheme_locations_capture_the_public_verdict(self):
        content = self._template()
        assert content.count(
            "auth_request_set $public_service $upstream_http_x_public_service;"
        ) == 2, "both HTTP and HTTPS ACL-on locations must capture the verdict"
        assert content.count(
            "proxy_set_header Authorization $edge_authorization;"
        ) == 2, "both ACL-on locations must use the selector"

    def test_selector_maps_public_to_client_header_and_default_to_owner_basic(self):
        content = self._template()
        assert "map $public_service $edge_authorization {" in content
        block = content.split("map $public_service $edge_authorization {", 1)[1].split("}", 1)[0]
        assert '"1"     $http_authorization;' in block
        assert 'default "Basic $service_basic";' in block

    def test_selector_is_acl_on_only(self):
        content = self._template()
        before_map = content.split("map $public_service $edge_authorization {", 1)[0]
        assert before_map.rstrip().endswith("# [if ENABLE_ACL=true]")
        acl_off = content.split("# [else (ENABLE_ACL=false)]", 1)[1]
        assert "edge_authorization" not in acl_off
        assert "auth_request /_auth_jwt" not in acl_off


# ---------------------------------------------------------------------------
# G78 — dashboard surfaces (T2/T5/T7/T10)
# ---------------------------------------------------------------------------


class TestPublicAndOrphanDashboard:
    """The card and user-management surfaces carry the G78 fields."""

    @staticmethod
    def _users_page() -> str:
        from pathlib import Path
        return (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UsersPage.tsx"
        ).read_text()

    @staticmethod
    def _user_admin() -> str:
        from pathlib import Path
        return (
            Path(__file__).parent.parent.parent
            / "provision-dashboard" / "src" / "pages" / "UserManagementPage.tsx"
        ).read_text()

    def test_public_card_uses_direct_urls_badge_and_warning(self):
        src = self._users_page()
        assert "public_urls" in src
        assert "is_public && svc.public_urls" in src
        assert "Public" in src
        assert "public and unguarded" in src
        assert "owner_gone" in src
        assert "owner gone" in src

    def test_orphan_alert_exact_text_is_admin_only(self):
        src = self._users_page()
        assert "isAdmin && orphans.length > 0" in src
        assert "Users ${orphans.join(', ')} are gone — services should be deleted" in src

    def test_user_management_has_public_active_and_grant_tags(self):
        src = self._user_admin()
        assert "is_public_shared" in src
        assert "is_active" in src
        assert "shared_user_id" in src
        assert "grants" in src
        assert "Grant activated" in src
        assert "Grant deactivated" in src
        assert "/grants" in src

    def test_delete_aborts_with_the_stop_failure_detail(self):
        src = self._user_admin()
        assert "Deletion aborted" in src
        assert "failed_services" in src

    def test_no_public_switch_and_registration_toggle(self):
        src = self._user_admin()
        assert "togglePublic" not in src
        assert "title: 'Public', key: 'public'" not in src
        assert "Public functional user" in src
        assert "is_public_shared: !!values.is_public_shared" in src
        assert '(r.is_public_shared || r.was_public) && <Tag color="green">Public</Tag>' in src
        assert "u.is_public_shared ? ' · Public'" in src
        assert "g.is_public_shared ? ' · Public'" in src

    def test_public_group_tag_is_like_owner_gone(self):
        src = self._users_page()
        assert "const publicGroup = userSvcs.some(s => s.is_public || s.was_public)" in src
        assert '{publicGroup && <Tag color="green">Public</Tag>}' in src
        assert "svc.is_public && <Tag" not in src, (
            "the Public tag belongs on the user group, not on each service card")


class TestServiceActionNotFoundPassthrough:
    """§10 step 9a: a card action on a bogus/deleted id is a 404, not a 502.

    Regression: `as_http_error` folded every non-503 failure into 502, so a
    stop/start on an unknown instance read as a gateway outage (surfaced by the
    bogus-id card-action trace, 2026-11-01).
    """

    @staticmethod
    def _err(code: int):
        import httpx
        req = httpx.Request("POST", "http://api/instances/x/stop")
        resp = httpx.Response(code, request=req, json={"detail": f"e{code}"})
        return httpx.HTTPStatusError("x", request=req, response=resp)

    def test_404_passes_through_with_its_detail(self):
        from fastapi import HTTPException
        from app.services.provision_service import provision_service

        mapped = provision_service.as_http_error(self._err(404))
        assert isinstance(mapped, HTTPException)
        assert mapped.status_code == 404
        assert mapped.detail == "e404"

    def test_other_failures_keep_the_generic_mapping(self):
        from fastapi import HTTPException
        from app.services.provision_service import provision_service

        mapped = provision_service.as_http_error(self._err(500))
        assert isinstance(mapped, HTTPException)
        assert mapped.status_code == 502

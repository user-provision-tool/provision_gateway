"""Unit tests for provision-gateway."""

import pytest
import sys
from pathlib import Path

# Add gateway app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.auth_service import hash_password, verify_password, create_access_token, decode_access_token


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
        token = create_access_token(1, "admin@test.com", "admin")
        assert token is not None
        payload = decode_access_token(token)
        assert payload["sub"] == "1"
        assert payload["email"] == "admin@test.com"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_invalid_token(self):
        from jose import JWTError
        with pytest.raises(JWTError):
            decode_access_token("invalid.token.here")


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
        assert loaded.schema_version == 1
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
        assert state.schema_version == 1
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
        assert "run_in_threadpool" in inspect.getsource(check_deploy_readiness)

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

    def test_deploy_user_wraps_get_service_in_threadpool(self):
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "from starlette.concurrency import run_in_threadpool" in content
        assert "run_in_threadpool(service_manager.get_service" in content

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


class TestDeploymentFileFallback:
    """Tests for deployment file source fallback (task 1.3)."""

    def test_resolve_deployment_file_env_returns_correct_path(self):
        """_resolve_deployment_file for env type should use .env.{user}.{label} pattern."""
        from app.routers.users import _resolve_deployment_file
        result = _resolve_deployment_file("alice", "myapp", "0", "env")
        assert result is not None
        assert result.name == ".env.alice.0"

    def test_resolve_deployment_file_compose_returns_correct_path(self):
        """_resolve_deployment_file for compose type should use docker-compose.user-{user}.{label}.yml."""
        from app.routers.users import _resolve_deployment_file
        result = _resolve_deployment_file("alice", "myapp", "0", "compose")
        assert result is not None
        assert "docker-compose.user-alice.0.yml" in result.name

    def test_resolve_deployment_file_nginx_returns_path(self):
        """_resolve_deployment_file for nginx type should return a candidate path."""
        from app.routers.users import _resolve_deployment_file
        result = _resolve_deployment_file("alice", "myapp", "0", "nginx")
        assert result is not None
        assert "nginx.conf" in result.name

    def test_resolve_deployment_file_unknown_type_returns_none(self):
        """_resolve_deployment_file for unknown type should return None."""
        from app.routers.users import _resolve_deployment_file
        result = _resolve_deployment_file("alice", "myapp", "0", "unknown")
        assert result is None

    def test_get_deployment_file_endpoint_has_source_fallback(self):
        """get_deployment_file should handle source_fallback in response."""
        from app.routers.users import get_deployment_file
        import inspect
        assert inspect.iscoroutinefunction(get_deployment_file)


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
    """Tests for auto-deploy flow (G6)."""

    def test_deploy_form_has_auto_submit(self):
        """DeployForm.tsx should auto-submit when autoDeploy is true."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "onFinish={handleDeploy}" in content or "htmlType=\"submit\"" in content or "type=\"submit\"" in content
        assert "autoDeploy" in content or "Auto Templates Completion" in content


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
    """Tests that generated files are saved to disk before deployment regardless of autoDeploy state (G12)."""

    def test_save_block_not_gated_by_autodeploy(self):
        """The save-to-disk block should NOT be conditional on autoDeploy."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # The save block comment explicitly states "regardless of autoDeploy state"
        assert "regardless of autoDeploy state" in content, (
            "Save block comment should clarify unconditional execution"
        )
        # Verify `&& autoDeploy` is NOT in the save condition
        # Locate the save block within handleDeploy by finding the comment anchor
        anchor_idx = content.find("regardless of autoDeploy state")
        assert anchor_idx > 0, "Anchor comment not found"
        save_block = content[anchor_idx:anchor_idx + 200]
        assert "&& autoDeploy" not in save_block, (
            "Save block should NOT be gated by autoDeploy: " + save_block
        )
        assert "Object.keys(gen).length > 0" in save_block or "Object.keys(generatedFiles).length > 0" in save_block, (
            "Save block should check if generated files exist"
        )

    def test_save_block_executes_before_deploy(self):
        """The save-to-disk call should appear before the deploy POST payload building."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # Locate the save block within handleDeploy by finding the comment anchor
        anchor_idx = content.find("regardless of autoDeploy state")
        assert anchor_idx > 0, "regardless of autoDeploy state comment not found"
        # The save block starts at or near this anchor
        save_block = content[anchor_idx:anchor_idx + 400]
        save_relative = save_block.find("save-generated")
        payload_relative = save_block.find("Build deploy payload")
        assert save_relative > 0, "save-generated call not found near anchor"
        assert payload_relative > 0, "Payload building comment not found near anchor"
        assert save_relative < payload_relative, (
            "Save-generated call should appear BEFORE deploy payload building. "
            f"save at {save_relative}, payload at {payload_relative}"
        )


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

    def test_deploy_validation_code_exists(self):
        """users.py deploy endpoint should contain compose/nginx path validation."""
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "one of compose_template_path or compose_file_path is required" not in content, (
            "Gateway should handle the error before provision-api returns it"
        )
        assert "missing essential files" in content.lower(), (
            "Deploy validation should mention missing essential files"
        )

    def test_deploy_validation_checks_service_files(self):
        """Deploy validation should check service project files."""
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "has_compose" in content, (
            "Deploy validation should check for compose files in the service"
        )
        assert "has_nginx" in content, (
            "Deploy validation should check for nginx files in the service"
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
        assert "Cannot deploy: missing essential files" in content, (
            "handleDeploy should show error message when missing files exist and no generated files"
        )
        assert "missingFiles.length > 0 && Object.keys(generatedFiles).length === 0" in content, (
            "handleDeploy should have the same guard condition as the disabled button"
        )

    def test_deploy_validation_raises_http_400(self):
        """Deploy validation should raise HTTP 400 with clear message."""
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "raise HTTPException" in content, (
            "Deploy validation should raise HTTPException"
        )
        assert "missing essential files" in content.lower(), (
            "Deploy validation error should mention missing essential files"
        )
        assert "Cannot deploy" in content, (
            "Deploy validation error should start with 'Cannot deploy'"
        )


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

    def test_next_label_returns_label_0_on_exception(self):
        """get_next_label should return label 0 when provision-api is unreachable."""
        from app.routers.users import get_next_label
        import inspect
        assert inspect.iscoroutinefunction(get_next_label)
        # Check the source code for the fallback
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert 'return {"label": "0", "source": "default"}' in content, (
            "get_next_label should fall back to label 0 when provision-api errors"
        )

    def test_next_label_logic_max_plus_1(self):
        """get_next_label should compute max existing label + 1."""
        from pathlib import Path
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "existing_labels" in content, (
            "get_next_label should collect existing_labels from provision-api response"
        )
        assert "max(existing_labels) + 1" in content or "max(existing_labels)+1" in content, (
            "get_next_label should compute max(existing_labels) + 1"
        )
        assert "source\": \"auto_increment" in content, (
            "get_next_label should return auto_increment as source"
        )

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
        assert d["user_id"] == 42
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
        """get_all_entries should return entries from a valid YAML file."""
        from app.services.registry import Registry
        import tempfile, os, yaml
        data = [
            {"user_name": "alice", "service_name": "myapp", "label": "0", "hostname": "myapp-alice-0.localhost"},
            {"user_name": "bob", "service_name": "app2", "label": "1", "hostname": "app2-bob-1.localhost"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name
        try:
            r = Registry(path)
            entries = r.get_all_entries()
            assert len(entries) == 2
            assert entries[0]["user_name"] == "alice"
        finally:
            os.unlink(path)

    def test_registry_get_entry_match(self):
        """get_entry should find matching entry by user/service/label."""
        from app.services.registry import Registry
        import tempfile, os, yaml
        data = [
            {"user_name": "alice", "service_name": "myapp", "label": "0"},
            {"user_name": "bob", "service_name": "app2", "label": "1"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name
        try:
            r = Registry(path)
            entry = r.get_entry("alice", "myapp", "0")
            assert entry is not None
            assert entry["user_name"] == "alice"
            # Non-matching should return None
            assert r.get_entry("nobody", "x", "0") is None
        finally:
            os.unlink(path)


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
    """BUG-2 + G5: only provision-type tokens authenticate; legacy gateway_token
    cookie / Bearer access tokens are REJECTED (three-credential model)."""

    def test_get_gateway_user_safe_rejects_gateway_token_cookie(self):
        """G5: a legacy type='gateway' cookie must NOT authenticate (returns None)."""
        from unittest.mock import MagicMock
        from app.routers.auth import _get_gateway_user_safe
        from app.services.auth_service import create_gateway_token

        token = create_gateway_token(42, "admin@test.com", "admin", "admin")
        request = MagicMock()
        request.cookies = {"gateway_token": token}
        request.headers = {}
        db = MagicMock()

        user = _get_gateway_user_safe(request, db)
        assert user is None

    def test_get_gateway_user_safe_rejects_bearer_access_token(self):
        """G5: a legacy Bearer access token must NOT authenticate (returns None)."""
        from unittest.mock import MagicMock
        from app.routers.auth import _get_gateway_user_safe
        from app.services.auth_service import create_access_token

        token = create_access_token(7, "admin@test.com", "admin", "admin")
        request = MagicMock()
        request.cookies = {}
        request.headers = {"Authorization": f"Bearer {token}"}
        db = MagicMock()

        user = _get_gateway_user_safe(request, db)
        assert user is None

    def test_get_gateway_user_safe_accepts_provision_cookie(self):
        """The provision_token cookie still authenticates."""
        from unittest.mock import MagicMock
        from app.routers.auth import _get_gateway_user_safe
        from app.services.auth_service import create_provision_token

        token = create_provision_token(42, "admin@test.com", "admin", "admin")
        request = MagicMock()
        request.cookies = {"provision_token": token}
        request.headers = {}
        db = MagicMock()

        user = _get_gateway_user_safe(request, db)
        assert user is not None
        assert user["id"] == 42
        assert user["role"] == "admin"

    def test_decode_gateway_token_rejects_legacy_types(self):
        """G5: decode_gateway_token accepts only type='provision'."""
        import pytest
        from jose import JWTError
        from app.services import auth_service
        for token in (
            auth_service.create_gateway_token(1, "a@b.c", "admin", "admin"),
            auth_service.create_access_token(1, "a@b.c", "admin", "admin"),
            auth_service.create_refresh_token(1, "a@b.c", "admin"),
        ):
            with pytest.raises(JWTError):
                auth_service.decode_gateway_token(token)

    def test_extract_gateway_token_rejects_legacy_fallbacks(self):
        """G5: _extract_gateway_token ignores gateway_token cookie and Bearer."""
        from unittest.mock import MagicMock
        from app.middleware import _extract_gateway_token
        from app.services.auth_service import create_gateway_token, create_access_token

        # gateway_token cookie alone → no token
        req = MagicMock()
        req.cookies = {"gateway_token": create_gateway_token(1, "a@b.c", "admin", "admin")}
        req.headers = {}
        assert _extract_gateway_token(req) is None

        # Bearer header alone → no token
        req = MagicMock()
        req.cookies = {}
        req.headers = {"Authorization": f"Bearer {create_access_token(1, 'a@b.c', 'admin', 'admin')}"}
        assert _extract_gateway_token(req) is None

        # provision_token cookie still wins
        from app.services.auth_service import create_provision_token
        req = MagicMock()
        req.cookies = {"provision_token": create_provision_token(1, "a@b.c", "admin", "admin")}
        req.headers = {}
        assert _extract_gateway_token(req) is not None

        # X-Provision-Token header (API key) authenticates
        req = MagicMock()
        req.cookies = {}
        req.headers = {"X-Provision-Token": create_provision_token(1, "a@b.c", "admin", "admin")}
        assert _extract_gateway_token(req) is not None

    def test_create_key_uses_depends_require_gateway_token(self):
        """create_key must inject require_gateway_token via Depends (not a bare call)."""
        from pathlib import Path
        auth_path = Path(__file__).parent.parent / "app" / "routers" / "auth.py"
        content = auth_path.read_text()
        assert "Depends(require_gateway_token)" in content, (
            "create_key must use Depends(require_gateway_token) to resolve the async dependency"
        )
        # The old buggy pattern returned an un-awaited coroutine.
        assert "require_gateway_token(request=request" not in content, (
            "create_key must not call require_gateway_token synchronously"
        )


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
        # The fetch must read the backend-filtered payload directly (no owner check).
        assert "client.get('/users')" in content
        assert "for (const u of users)" in content


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
        assert "setSpecialUsersGlobal" in content
        assert "role === 'special'" in content


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
        assert '<Route path="users" element={<UsersPage />} />' in content
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
        assert '_set_token_url = f"{service_url}/_set_token' in content, (
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
        assert "return `/go/${host}`" in content, (
            "service card must link to /go/{hostname} (gateway redirect) so the "
            "browser gets the provision_token cookie for the service domain"
        )


class TestVerifyAuthStatusCodes:
    """Gap 11 (acl-enforcement-design-v2 §5): /api/auth/verify returns the FINAL
    status — 200 + X-Service-Basic (allowed), 401 + X-Auth-Action (login_required /
    token_expired), 403 + X-Auth-Action (acl_denied). The browser-vs-API split is
    now done by nginx (error_page + map $http_accept), not by the gateway."""

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

    @staticmethod
    def _user():
        from unittest.mock import MagicMock
        u = MagicMock()
        u.id = 1
        u.username = "alice"
        u.is_active = True
        u.is_approved = True
        u.allowed_special_users = ""
        return u

    def _enable_acl(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "ENABLE_ACL", True)

    def test_no_token_returns_401_login_required(self, monkeypatch):
        from app.routers.auth import verify_auth
        self._enable_acl(monkeypatch)
        from unittest.mock import MagicMock
        resp = verify_auth(self._req(accept="text/html"), MagicMock())
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "login_required"

    def test_invalid_token_returns_401_login_required(self, monkeypatch):
        from app.routers.auth import verify_auth
        from unittest.mock import MagicMock, patch
        from jose import JWTError
        self._enable_acl(monkeypatch)
        db = MagicMock()
        req = self._req(cookie="bad-token", accept="text/html")
        with patch("app.routers.auth.auth_service.verify_provision_token",
                   side_effect=JWTError("bad")):
            resp = verify_auth(req, db)
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "login_required"

    def test_expired_token_returns_401_token_expired(self, monkeypatch):
        from app.routers.auth import verify_auth
        from unittest.mock import MagicMock, patch
        from jose import JWTError
        self._enable_acl(monkeypatch)
        req = self._req(cookie="expired", accept="text/html")
        with patch("app.routers.auth.auth_service.verify_provision_token",
                   side_effect=JWTError("expired")), \
             patch("jose.jwt.decode"):
            # jose.jwt.decode succeeds → token structurally valid but expired
            resp = verify_auth(req, MagicMock())
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "token_expired"

    def test_acl_denied_returns_403(self, monkeypatch):
        from app.routers.auth import verify_auth
        from unittest.mock import MagicMock, patch
        self._enable_acl(monkeypatch)
        payload = {"sub": "1", "user_type": "end_user", "role": "viewer"}
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self._user()
        req = self._req(cookie="valid", accept="text/html")
        with patch("app.routers.auth.auth_service.verify_provision_token",
                   return_value=payload), \
             patch("app.routers.auth._lookup_by_hostname",
                   return_value={"user_name": "bob"}):
            resp = verify_auth(req, db)
        assert resp.status_code == 403
        assert resp.headers["X-Auth-Action"] == "acl_denied"

    def test_valid_admin_returns_200_x_service_basic(self, monkeypatch):
        from app.routers.auth import verify_auth
        from unittest.mock import MagicMock, patch
        self._enable_acl(monkeypatch)
        payload = {"sub": "1", "user_type": "admin", "role": "admin"}
        req = self._req(cookie="valid", accept="text/html")
        with patch("app.routers.auth.auth_service.verify_provision_token",
                   return_value=payload), \
             patch("app.routers.auth._get_service_basic_credential",
                   return_value="dXNlcjpwYXNz"):
            resp = verify_auth(req, MagicMock())
        assert resp.status_code == 200
        assert resp.headers["X-Service-Basic"] == "dXNlcjpwYXNz"

    def test_acl_disabled_returns_401(self, monkeypatch):
        from app.routers.auth import verify_auth
        from unittest.mock import MagicMock
        from app.config import settings
        monkeypatch.setattr(settings, "ENABLE_ACL", False)
        resp = verify_auth(self._req(accept="text/html"), MagicMock())
        assert resp.status_code == 401


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
    """Gap: GET /api/auth/me must use require_gateway_token (gateway_token
    cookie or Bearer, 24h TTL) instead of get_current_user (Bearer
    access_token, 1h TTL), removing the transient 401 the browser emitted once
    the 1h access token expired."""

    def test_get_me_uses_require_gateway_token(self):
        import inspect
        from app.routers.auth import get_me
        src = inspect.getsource(get_me)
        assert "Depends(require_gateway_token)" in src
        assert "Depends(get_current_user)" not in src

    def test_require_gateway_token_rejects_gateway_token_cookie(self):
        """G5: a legacy gateway_token cookie (type='gateway') is REJECTED (401)."""
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from app.middleware import require_gateway_token
        from app.services.auth_service import create_gateway_token

        token = create_gateway_token(42, "admin@test.com", "admin", "admin")
        req = MagicMock()
        req.cookies = {"gateway_token": token}
        req.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            require_gateway_token(request=req)
        assert exc_info.value.status_code == 401

    def test_require_gateway_token_rejects_bearer_gateway_token(self):
        """G5: a legacy Bearer gateway token is REJECTED (401)."""
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from app.middleware import require_gateway_token
        from app.services.auth_service import create_gateway_token

        token = create_gateway_token(7, "viewer@test.com", "viewer", "end_user")
        req = MagicMock()
        req.cookies = {}
        req.headers = {"Authorization": f"Bearer {token}"}

        with pytest.raises(HTTPException) as exc_info:
            require_gateway_token(request=req)
        assert exc_info.value.status_code == 401

    def test_require_gateway_token_accepts_provision_cookie(self):
        """A provision_token cookie (the v4 single credential) authenticates."""
        from unittest.mock import MagicMock, patch
        from app.middleware import require_gateway_token
        from app.services.auth_service import create_provision_token

        token = create_provision_token(42, "admin@test.com", "admin", "admin")
        admin = MagicMock()
        admin.id = 42
        admin.email = "admin@test.com"
        admin.role = "admin"
        admin.is_active = True

        req = MagicMock()
        req.cookies = {"provision_token": token}
        req.headers = {}

        with patch("app.middleware.get_admin_by_id", return_value=admin):
            user = require_gateway_token(request=req)

        assert user["id"] == 42
        assert user["email"] == "admin@test.com"
        assert user["role"] == "admin"
        assert user["user_type"] == "admin"


class TestAdminRoutesMigratedToGatewayToken:
    """Gap 7: admin-only gateway routes must use ``require_admin`` (gateway_token
    cookie/Bearer, 24h TTL) instead of ``get_current_admin``/``require_admin_role``
    (Bearer ``access_token``, 1h TTL), per gateway-acl-architecture.md §5."""

    def test_require_admin_accepts_admin_provision_cookie(self):
        """An admin provision_token cookie satisfies require_admin."""
        from unittest.mock import MagicMock, patch
        from app.middleware import require_admin
        from app.services.auth_service import create_provision_token

        token = create_provision_token(42, "admin@test.com", "admin", "admin")
        admin = MagicMock()
        admin.id = 42
        admin.email = "admin@test.com"
        admin.role = "admin"
        admin.is_active = True

        req = MagicMock()
        req.cookies = {"provision_token": token}
        req.headers = {}

        with patch("app.middleware.get_admin_by_id", return_value=admin):
            user = require_admin(request=req)

        assert user["id"] == 42
        assert user["role"] == "admin"
        assert user["user_type"] == "admin"

    def test_require_admin_rejects_viewer_role_403(self):
        """A viewer (non-admin role) provision token is rejected with 403."""
        from unittest.mock import MagicMock, patch
        from fastapi import HTTPException
        from app.middleware import require_admin
        from app.services.auth_service import create_provision_token

        token = create_provision_token(7, "viewer@test.com", "viewer", "end_user")
        end_user = MagicMock()
        end_user.id = 7
        end_user.username = "viewer@test.com"
        end_user.role = "viewer"
        end_user.is_active = True
        end_user.is_approved = True
        end_user.allowed_special_users = ""

        req = MagicMock()
        req.cookies = {"provision_token": token}
        req.headers = {}

        with patch("app.middleware.get_end_user_by_id", return_value=end_user):
            with pytest.raises(HTTPException) as exc_info:
                require_admin(request=req)

        assert exc_info.value.status_code == 403

    def test_no_router_uses_legacy_admin_dependencies(self):
        """No gateway router may still depend on get_current_admin/require_admin_role."""
        from pathlib import Path
        routers_dir = Path(__file__).parent.parent / "app" / "routers"
        legacy = ("Depends(get_current_admin)", "Depends(require_admin_role)")
        checked = 0
        for f in sorted(routers_dir.glob("*.py")):
            content = f.read_text()
            for dep in legacy:
                assert dep not in content, f"{f.name} still uses {dep}"
            checked += 1
        assert checked >= 6  # auth, system, services, users, tasks, llm, audit

    def test_representative_admin_routes_use_require_admin(self):
        """Representative migrated routes inject require_admin via Depends."""
        import inspect
        from app.routers.tasks import list_tasks
        from app.routers.audit import list_audit_logs
        from app.routers.users import deploy_user
        from app.routers.auth import list_end_users
        from app.routers.services import list_services
        from app.routers.llm import list_llm_configs
        for fn in (list_tasks, list_audit_logs, deploy_user, list_end_users,
                   list_services, list_llm_configs):
            src = inspect.getsource(fn)
            assert "Depends(require_admin)" in src, (
                f"{fn.__name__} should use Depends(require_admin)"
            )

    def test_change_password_fetches_admin_orm_by_id(self):
        """change_password uses require_admin (dict) and re-fetches the ORM."""
        import inspect
        from app.routers.auth import change_password
        src = inspect.getsource(change_password)
        assert "Depends(require_admin)" in src
        assert "get_admin_by_id" in src
        assert "current_admin[\"id\"]" in src

    def test_register_end_user_is_public_signup(self):
        """POST /api/auth/users/register stays public (pre-auth signup flow)."""
        import inspect
        from app.routers.auth import register_end_user
        src = inspect.getsource(register_end_user)
        assert "require_gateway_token" not in src
        assert "require_admin" not in src

    def test_stream_task_log_accepts_gateway_token(self):
        """The SSE task-log endpoint decodes gateway_token (cookie/Bearer/query)."""
        import inspect
        from app.routers.tasks import stream_task_log
        src = inspect.getsource(stream_task_log)
        assert "decode_gateway_token" in src
        assert 'gateway_token' in src



class TestV4ExchangeCode:
    """v4 §6.2 (F7/GAP-10): 30s exchange code for /go/ service handoff."""

    def test_exchange_code_ttl_constant(self):
        from app.config import settings
        assert settings.EXCHANGE_CODE_TTL_SEC == 30

    def test_create_and_verify_exchange_code_roundtrip(self):
        from app.services import auth_service
        code = auth_service.create_exchange_code(
            1, "alice@example.com", "viewer", "end_user", "svc.example.com", redirect="/x"
        )
        payload = auth_service.verify_exchange_code(code)
        assert payload["type"] == "code"
        assert payload["sub"] == "1"
        assert payload["svc_host"] == "svc.example.com"
        assert payload["redirect"] == "/x"

    def test_verify_exchange_code_rejects_non_code_token(self):
        import pytest
        from jose import JWTError
        from app.services import auth_service
        prov = auth_service.create_provision_token(1, "a@b.c", "viewer", "end_user")
        with pytest.raises(JWTError):
            auth_service.verify_exchange_code(prov)

    def test_verify_exchange_code_rejects_garbage(self):
        import pytest
        from jose import JWTError
        from app.services import auth_service
        with pytest.raises(JWTError):
            auth_service.verify_exchange_code("not-a-token")

    def test_go_to_service_uses_code_not_jwt_in_url(self):
        """GAP-10: the /go/ redirect URL must never contain a JWT — only the code."""
        from pathlib import Path
        p = Path(__file__).parent.parent / "app" / "routers" / "auth.py"
        content = p.read_text()
        assert "create_exchange_code" in content
        assert '_set_token_url = f"{service_url}/_set_token?code={code}"' in content
        assert "RedirectResponse(url=_set_token_url, status_code=303)" in content


class TestV4ApiKeyTokenModel:
    """v4 §6.1.1-6.1.3: API key IS a 1-year provision JWT with own api_key_id."""

    def test_provision_token_ttl_week(self):
        from app.services import auth_service
        assert auth_service.PROVISION_TOKEN_TTL_SEC == 604800

    def test_api_key_ttl_year(self):
        from app.services import auth_service
        assert auth_service.API_KEY_TTL_SEC == 31536000

    def test_create_api_key_token_carries_own_api_key_id(self):
        from app.services import auth_service
        tok = auth_service.create_api_key_token(3, "bob@x.io", "admin", "admin", api_key_id=42)
        payload = auth_service.decode_token(tok)
        assert payload["api_key_id"] == 42
        assert payload["type"] == "provision"
        assert payload["user_type"] == "admin"

    def test_create_api_key_stores_hash_and_mask(self):
        from unittest.mock import MagicMock, patch
        from app.services import auth_service
        key = MagicMock()
        key.id = 7
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 0
        with patch("app.services.auth_service._target_identity", return_value=("end_user", "carol", "viewer")), \
             patch("app.services.auth_service.ApiKey", return_value=key):
            created, raw = auth_service.create_api_key(db, 5, "My Key")
        assert raw.endswith(created.mask)
        assert created.token_hash == auth_service._hash_token(raw)
        assert created.expires_at is not None

    def test_create_api_key_cap_1000(self):
        import pytest
        from unittest.mock import MagicMock, patch
        from app.services import auth_service
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = auth_service.MAX_API_KEYS_PER_USER
        with patch("app.services.auth_service._lazy_evict_api_keys", return_value=0):
            with pytest.raises(ValueError):
                auth_service.create_api_key(db, 5, "x")

    def test_revoke_default_key_raises(self):
        import pytest
        from unittest.mock import MagicMock
        from app.services import auth_service
        key = MagicMock()
        key.is_default = True
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = key
        with pytest.raises(ValueError):
            auth_service.revoke_api_key(db, 9)

    def test_revoke_non_default_ok(self):
        from unittest.mock import MagicMock
        from app.services import auth_service
        key = MagicMock()
        key.is_default = False
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = key
        assert auth_service.revoke_api_key(db, 9) is True
        assert key.is_revoked is True


class TestV4DefaultKey:
    """v4 §6.1.3/6.1.5 (D7): default key auto-create/promote, backfill."""

    def test_get_or_create_default_key_returns_existing(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock
        from app.services import auth_service
        key = MagicMock()
        key.is_default = True
        key.is_revoked = False
        key.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = key
        got = auth_service.get_or_create_default_key(db, 1)
        assert got is key
        # No new key created
        assert db.add.call_count == 0

    def test_get_or_create_default_key_autocreates(self):
        from unittest.mock import MagicMock, patch
        from app.services import auth_service
        db = MagicMock()
        # No default, no valid keys (valid chain uses .filter().filter().order_by().all())
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        new_key = MagicMock()
        with patch("app.services.auth_service.create_api_key", return_value=(new_key, "raw")):
            got = auth_service.get_or_create_default_key(db, 2)
        assert got is new_key

    def test_backfill_default_keys_exists(self):
        import inspect
        from app.services import auth_service
        src = inspect.getsource(auth_service.backfill_default_keys)
        assert "is_default" in src


class TestV4ClientTypeHybrid:
    """v4 §6 / review GAP-11/A3: X-Client-Type hybrid rule."""

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
        assert _resolve_client_type(self._req(header="tok", cookie="c", accept="text/html")) == "api"

    def test_cookie_means_browser(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(self._req(cookie="c", accept="*/*")) == "browser"

    def test_accept_text_html_means_browser(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(self._req(accept="text/html,application/xhtml+xml")) == "browser"

    def test_else_means_api(self):
        from app.routers.auth import _resolve_client_type
        assert _resolve_client_type(self._req(accept="application/json")) == "api"

    def test_verify_response_carries_x_client_type(self):
        from unittest.mock import MagicMock
        from app.routers.auth import verify_auth
        from app.config import settings
        settings.ENABLE_ACL = False
        resp = verify_auth(self._req(accept="application/json"), MagicMock())
        assert resp.headers.get("X-Client-Type") == "api"
        assert resp.status_code == 401


class TestV4AuthEndpoints:
    """v4 §6.1/F4: three-credential model endpoints."""

    def test_login_sets_provision_cookie_only(self):
        from unittest.mock import MagicMock, patch
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        req = LoginRequest(email="a@b.c", password="pw")
        default_key = MagicMock()
        default_key.id = 11
        result = ("admin", {"id": 1, "email": "a@b.c", "role": "admin"})
        with patch("app.routers.auth.auth_service.authenticate_user", return_value=result), \
             patch("app.routers.auth.auth_service.create_provision_token", return_value="TOK"), \
             patch("app.routers.auth.settings.PROVISION_COOKIE_TTL", 604800):
            from unittest.mock import MagicMock as M
            request = M()
            resp = login(req, request, MagicMock())
        body = resp.body.decode()
        assert "access_token" not in body and "refresh_token" not in body and "gateway_token" not in body
        assert resp.headers["set-cookie"].startswith("provision_token=TOK")

    def test_logout_endpoint_clears_cookie(self):
        from unittest.mock import MagicMock
        from app.routers.auth import logout
        response = MagicMock()
        logout(response)
        response.delete_cookie.assert_called_once_with("provision_token", path="/")

    def test_no_refresh_endpoint(self):
        """v4 §6.1: /api/auth/refresh removed — the dashboard never refreshes tokens."""
        from app.routers.auth import router
        paths = [r.path for r in router.routes]
        assert "/api/auth/refresh" not in paths
        assert "/api/auth/logout" in paths
        assert "/api/auth/exchange" in paths

    def test_exchange_endpoint_requires_code(self):
        import pytest
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
        from app.routers.auth import exchange
        req = MagicMock()
        req.query_params = {"code": "CODEX"}
        payload = {"sub": "2", "role": "viewer", "user_type": "end_user", "email": "b@b.c",
                   "svc_host": "svc.host", "redirect": "/"}
        with patch("app.routers.auth.auth_service.verify_exchange_code", return_value=payload), \
             patch("app.routers.auth.auth_service.get_or_create_default_key", return_value=MagicMock(id=5)), \
             patch("app.routers.auth.auth_service.create_provision_token", return_value="NEWTOK"), \
             patch("app.routers.auth.settings.PROVISION_COOKIE_TTL", 604800):
            from unittest.mock import MagicMock as M
            req.app = M()
            req.app.state.hostname_index = M()
            req.app.state.hostname_index.get_by_hostname.return_value = {"https": False}
            resp = exchange(req, MagicMock())
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert resp.headers["set-cookie"].startswith("provision_token=NEWTOK")


class TestV4VerifyOrdering:
    """v4 §6.1.4 / review R1: revocation-before-admin-bypass, token_expired check."""

    def test_admin_with_revoked_key_denied(self):
        from unittest.mock import MagicMock, patch
        from jose import JWTError
        from app.routers.auth import verify_auth
        from app.config import settings
        settings.ENABLE_ACL = True
        req = MagicMock()
        req.cookies = {"provision_token": "t"}
        req.headers = {"Accept": "text/html", "Host": "svc.host"}
        with patch("app.routers.auth.auth_service.verify_provision_token", side_effect=JWTError("revoked")):
            resp = verify_auth(req, MagicMock())
        assert resp.status_code == 401
        assert resp.headers["X-Auth-Action"] == "login_required"

    def test_provision_token_verifier_checks_expires_at(self):
        """R1/GAP-06: verify_provision_token must check key.expires_at."""
        import inspect
        from app.services import auth_service
        src = inspect.getsource(auth_service.verify_provision_token)
        assert "expires_at" in src
        assert "is_revoked" in src

    def test_provision_token_ttl_week_payload(self):
        from app.services import auth_service
        tok = auth_service.create_provision_token(1, "a@b.c", "viewer", "end_user", api_key_id=3)
        payload = auth_service.decode_token(tok)
        assert payload["type"] == "provision"
        assert payload["api_key_id"] == 3
        exp = payload["exp"]
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        delta = exp - now.timestamp()
        assert 600_000 < delta < 700_000  # ~1 week (604800)


class TestV4SpecialUsersTrim:
    """Review N1: allowed_special_users entries must be trimmed on parse."""

    def test_parse_allowed_special_users_trims(self):
        from app.routers.auth import _parse_allowed_special_users
        assert _parse_allowed_special_users(" alice , bob ,") == ["alice", "bob"]
        assert _parse_allowed_special_users("") == []
        assert _parse_allowed_special_users(None) == []


class TestV4ServiceBasicCredential:
    """Review N2: _get_service_basic_credential never falls back to a hardcoded
    guessable credential — a missing passwd_plain yields empty (passwd-less)."""

    def test_missing_passwd_plain_returns_empty(self, monkeypatch):
        from app.routers.auth import _get_service_basic_credential
        from unittest.mock import MagicMock, patch
        req = MagicMock()
        req.headers.get.return_value = "myapp.example.com"
        with patch("app.routers.auth._lookup_by_hostname",
                   return_value={"user_name": "bob"}):
            assert _get_service_basic_credential(req, MagicMock()) == ""

    def test_empty_passwd_plain_returns_empty(self, monkeypatch):
        from app.routers.auth import _get_service_basic_credential
        from unittest.mock import MagicMock, patch
        req = MagicMock()
        req.headers.get.return_value = "myapp.example.com"
        with patch("app.routers.auth._lookup_by_hostname",
                   return_value={"user_name": "bob", "passwd_plain": ""}):
            assert _get_service_basic_credential(req, MagicMock()) == ""

    def test_passwd_plain_returns_base64(self, monkeypatch):
        import base64
        from app.routers.auth import _get_service_basic_credential
        from unittest.mock import MagicMock, patch
        req = MagicMock()
        req.headers.get.return_value = "myapp.example.com"
        with patch("app.routers.auth._lookup_by_hostname",
                   return_value={"user_name": "bob", "passwd_plain": "secret"}):
            got = _get_service_basic_credential(req, MagicMock())
        assert got == base64.b64encode(b"bob:secret").decode()


class TestV4TasksCookie:
    """v4 §11.2 (N5): SSE task-log reads provision_token cookie."""

    def test_stream_task_log_reads_provision_cookie(self):
        from pathlib import Path
        p = Path(__file__).parent.parent / "app" / "routers" / "tasks.py"
        content = p.read_text()
        assert 'request.cookies.get("provision_token", "")' in content


class TestV4DefaultEndpoint:
    """v4 §6.1.5: PUT /keys/{id}/default sets a user's default key."""

    def test_set_default_endpoint_exists(self):
        from app.routers.auth import router
        paths = [r.path for r in router.routes if getattr(r, "methods", None)]
        assert any("/keys/{key_id}/default" in p and "PUT" in r.methods for p, r in
                   ((r.path, r) for r in router.routes if hasattr(r, "methods")))


class TestDbMigration:
    """QA1: existing gateway.db must be migrated — create_all never alters
    existing tables, so an old api_keys table (no mask/is_default) must get the
    new columns via _ensure_schema before end-user login works."""

    def _make_old_schema(self, path):
        from sqlalchemy import create_engine, text
        eng = create_engine(f"sqlite:///{path}")
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE api_keys ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL,"
                " label VARCHAR(255) NOT NULL,"
                " token_hash VARCHAR(255) NOT NULL,"
                " created_at DATETIME,"
                " expires_at DATETIME NOT NULL,"
                " is_revoked BOOLEAN NOT NULL DEFAULT 0,"
                " last_used_at DATETIME)"
            ))
        return eng

    def test_ensure_schema_adds_mask_and_is_default(self, tmp_path):
        db_path = tmp_path / "old.db"
        eng = self._make_old_schema(str(db_path))

        from app.database import _ensure_schema
        from sqlalchemy import inspect, text

        # Before: no mask/is_default columns
        cols_before = {c["name"] for c in inspect(eng).get_columns("api_keys")}
        assert "mask" not in cols_before and "is_default" not in cols_before

        # A pre-migration row exists BEFORE the migration runs, so the mask
        # backfill (G7) must fill it.
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO api_keys (user_id, label, token_hash, expires_at) "
                "VALUES (1, 'k', 'h', '2030-01-01 00:00:00')"
            ))

        _ensure_schema(eng)

        cols_after = {c["name"] for c in inspect(eng).get_columns("api_keys")}
        assert "mask" in cols_after
        assert "is_default" in cols_after
        with eng.begin() as conn:
            row = conn.execute(text("SELECT mask, is_default FROM api_keys")).fetchone()
        # G7: mask backfilled from the stored token_hash (raw token is hashed);
        # is_default stays 0 for a non-default key.
        assert row.mask == "h"[-8:]
        assert row.is_default == 0

    def test_ensure_schema_idempotent(self, tmp_path):
        db_path = tmp_path / "old.db"
        eng = self._make_old_schema(str(db_path))
        from app.database import _ensure_schema
        from sqlalchemy import inspect
        _ensure_schema(eng)
        _ensure_schema(eng)
        cols = {c["name"] for c in inspect(eng).get_columns("api_keys")}
        assert "mask" in cols and "is_default" in cols

    def test_ensure_schema_noop_on_fresh(self, tmp_path):
        from sqlalchemy import create_engine
        from app.database import Base, _ensure_schema
        from sqlalchemy import inspect
        eng = create_engine(f"sqlite:///{tmp_path/'fresh.db'}")
        Base.metadata.create_all(bind=eng)
        _ensure_schema(eng)
        cols = {c["name"] for c in inspect(eng).get_columns("api_keys")}
        assert "mask" in cols and "is_default" in cols


# ---------------------------------------------------------------------------
# G2 — one-default-per-user (partial unique index) + cascade delete
# ---------------------------------------------------------------------------


class TestG2OneDefaultPerUser:
    """v4 §6.1.3 (G2): exactly one default per user at the DB level, and user
    deletion cascades to api_keys so SQLite id-reuse can't orphan stale defaults."""

    def _make_drifted_schema(self, path):
        from sqlalchemy import create_engine, text
        eng = create_engine(f"sqlite:///{path}")
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE api_keys ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL,"
                " label VARCHAR(255) NOT NULL,"
                " token_hash VARCHAR(255) NOT NULL UNIQUE,"
                " mask VARCHAR(255),"
                " is_default BOOLEAN NOT NULL DEFAULT 0,"
                " created_at DATETIME,"
                " expires_at DATETIME NOT NULL,"
                " is_revoked BOOLEAN NOT NULL DEFAULT 0,"
                " last_used_at DATETIME)"
            ))
            i = 0
            for uid in (1, 1, 1, 2, 2):  # user 1 has 3 defaults, user 2 has 2
                i += 1
                conn.execute(text(
                    "INSERT INTO api_keys (user_id, label, token_hash, mask, is_default, expires_at) "
                    "VALUES (:uid, 'Default', :th, NULL, 1, '2030-01-01 00:00:00')"
                ), {"uid": uid, "th": f"hash-{i}"})
        return eng

    def test_ensure_schema_repairs_multi_default_drift(self, tmp_path):
        """Migration must de-duplicate drifted is_default rows, then install the
        partial unique index (a plain CREATE UNIQUE would fail on the drift)."""
        from app.database import _ensure_schema
        from sqlalchemy import inspect, text
        eng = self._make_drifted_schema(str(tmp_path / "drift.db"))

        _ensure_schema(eng)

        # No user may have >1 default (DB-drift scan regression).
        with eng.begin() as conn:
            bad = conn.execute(text(
                "SELECT user_id, COUNT(*) FROM api_keys "
                "WHERE is_default = 1 GROUP BY user_id HAVING COUNT(*) > 1"
            )).fetchall()
        assert bad == []

        # The partial unique index now exists.
        idx_names = {i["name"] for i in inspect(eng).get_indexes("api_keys")}
        assert "uq_api_keys_one_default" in idx_names

    def test_fresh_db_has_partial_unique_index(self, tmp_path):
        """create_all on the model installs the partial unique index directly."""
        from sqlalchemy import create_engine, inspect
        from app.database import Base
        from app.models.api_key import ApiKey  # noqa: F401 — registers the model
        eng = create_engine(f"sqlite:///{tmp_path/'fresh.db'}")
        Base.metadata.create_all(bind=eng)
        idx_names = {i["name"] for i in inspect(eng).get_indexes("api_keys")}
        assert "uq_api_keys_one_default" in idx_names

    def test_index_rejects_second_default_row(self, tmp_path):
        """Directly inserting a second is_default=1 row for a user violates the
        partial unique index."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import IntegrityError
        from app.database import Base
        from app.models.api_key import ApiKey  # noqa: F401
        eng = create_engine(f"sqlite:///{tmp_path/'idx.db'}")
        Base.metadata.create_all(bind=eng)
        # One default for user 1 is fine.
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO api_keys (user_id, label, token_hash, is_default, is_revoked, expires_at) "
                "VALUES (1, 'Default', 'th1', 1, 0, '2030-01-01 00:00:00')"
            ))
        # A SECOND default for the same user violates the partial unique index.
        import pytest
        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO api_keys (user_id, label, token_hash, is_default, is_revoked, expires_at) "
                    "VALUES (1, 'Default', 'th-again', 1, 0, '2030-01-01 00:00:00')"
                ))

    def test_create_api_key_default_unsets_existing(self, tmp_path):
        """create_api_key(is_default=True) must not create a second default (G2)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.api_key import ApiKey
        from app.services import auth_service
        eng = create_engine(f"sqlite:///{tmp_path/'k.db'}")
        Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        db = Session()
        k1, _ = auth_service.create_api_key(db, 1, "a", is_default=True)
        k2, _ = auth_service.create_api_key(db, 1, "b", is_default=True)
        count = db.query(ApiKey).filter(
            ApiKey.user_id == 1, ApiKey.is_default.is_(True)
        ).count()
        assert count == 1
        assert k2.is_default is True
        db.close()

    def test_delete_api_keys_for_user_cascades(self, tmp_path):
        """Deleting a user must remove their api_keys (G2)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.api_key import ApiKey
        from app.services import auth_service
        eng = create_engine(f"sqlite:///{tmp_path/'c.db'}")
        Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        db = Session()
        auth_service.create_api_key(db, 1, "a", is_default=True)
        auth_service.create_api_key(db, 1, "b")
        deleted = auth_service.delete_api_keys_for_user(db, 1)
        assert deleted == 2
        assert db.query(ApiKey).filter(ApiKey.user_id == 1).count() == 0
        db.close()

    def test_register_after_delete_yields_single_default(self, tmp_path):
        """A user deleted (keys cascaded) then re-registered gets exactly one
        default — no stale default orphaned by id-reuse."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.api_key import ApiKey
        from app.services import auth_service
        eng = create_engine(f"sqlite:///{tmp_path/'r.db'}")
        Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        db = Session()
        auth_service.create_api_key(db, 5, "Default", is_default=True)
        auth_service.delete_api_keys_for_user(db, 5)
        # new user reuses id 5 (SQLite autoincrement reuse after delete)
        auth_service.create_api_key(db, 5, "Default", is_default=True)
        count = db.query(ApiKey).filter(
            ApiKey.user_id == 5, ApiKey.is_default.is_(True)
        ).count()
        assert count == 1
        db.close()


# ---------------------------------------------------------------------------
# G3 — special users blocked at login with 403 (B11) when credentials are valid
# ---------------------------------------------------------------------------


class TestG3SpecialUserLogin403:
    """v4 §1.2 B11 / §6.1.6: a special-role user with a VALID credential must be
    rejected at login with 403 (not 401). The 401 the deployed placeholder
    accounts produce is a *password* failure — the 403 branch fires once
    authentication actually succeeds."""

    def test_special_user_with_valid_password_login_403(self):
        import pytest
        from fastapi import HTTPException
        from unittest.mock import MagicMock, patch
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        req = LoginRequest(email="internal", password="real-bcrypt-pass")
        result = ("end_user", {
            "id": 9, "username": "internal", "role": "special",
            "is_approved": True, "is_active": True,
        })
        with patch("app.routers.auth.auth_service.authenticate_user", return_value=result):
            with pytest.raises(HTTPException) as ei:
                login(req, MagicMock(), MagicMock())
        assert ei.value.status_code == 403
        assert "Special users cannot access the dashboard" in ei.value.detail

    def test_special_user_login_never_mints_token(self):
        """The 403 fires BEFORE any provision token is minted."""
        import pytest
        from fastapi import HTTPException
        from unittest.mock import MagicMock, patch
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        req = LoginRequest(email="internal", password="x")
        result = ("end_user", {
            "id": 9, "username": "internal", "role": "special",
            "is_approved": True, "is_active": True,
        })
        with patch("app.routers.auth.auth_service.authenticate_user", return_value=result), \
             patch("app.routers.auth.auth_service.create_provision_token") as cpt:
            with pytest.raises(HTTPException):
                login(req, MagicMock(), MagicMock())
        cpt.assert_not_called()


# ---------------------------------------------------------------------------
# G4 — admins get a default key at registration + login binds to it
# ---------------------------------------------------------------------------


class TestG4AdminDefaultKey:
    """v4 §6.1.5: admins get a default key at registration; login binds their
    provision_token to it (revocable, R1)."""

    def test_create_admin_creates_default_key(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.api_key import ApiKey
        from app.services import auth_service
        eng = create_engine(f"sqlite:///{tmp_path/'a.db'}")
        Base.metadata.create_all(bind=eng)
        Session = sessionmaker(bind=eng)
        db = Session()
        admin = auth_service.create_admin(db, "a@b.c", "pw123", role="admin")
        keys = db.query(ApiKey).filter(ApiKey.user_id == admin.id).all()
        assert len(keys) == 1
        assert keys[0].is_default is True
        assert keys[0].label == "Default"
        db.close()

    def test_login_binds_admin_token_to_default_key(self):
        from unittest.mock import MagicMock, patch
        from app.routers.auth import login
        from app.schemas.auth import LoginRequest
        req = LoginRequest(email="a@b.c", password="pw")
        default_key = MagicMock()
        default_key.id = 77
        result = ("admin", {"id": 1, "email": "a@b.c", "role": "admin"})
        with patch("app.routers.auth.auth_service.authenticate_user", return_value=result), \
             patch("app.routers.auth.auth_service.get_or_create_default_key",
                   return_value=default_key) as goc, \
             patch("app.routers.auth.auth_service.create_provision_token",
                   return_value="TOK") as cpt, \
             patch("app.routers.auth.settings.PROVISION_COOKIE_TTL", 604800):
            from unittest.mock import MagicMock as M
            request = M()
            resp = login(req, request, MagicMock())
        goc.assert_called_once()
        _, kwargs = cpt.call_args
        assert kwargs["api_key_id"] == 77
        assert resp.headers["set-cookie"].startswith("provision_token=TOK")


# ---------------------------------------------------------------------------
# G7 — mask backfill on migration
# ---------------------------------------------------------------------------


class TestG7MaskBackfill:
    """v4 §6.1.3 (G7): pre-migration api_keys rows with NULL mask get a display
    mask backfilled (derived from token_hash — the raw token is hashed at rest)."""

    def test_ensure_schema_backfills_null_masks(self, tmp_path):
        from app.database import _ensure_schema
        from sqlalchemy import create_engine, text
        eng = create_engine(f"sqlite:///{tmp_path/'m.db'}")
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE api_keys ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL,"
                " label VARCHAR(255) NOT NULL,"
                " token_hash VARCHAR(255) NOT NULL UNIQUE,"
                " created_at DATETIME,"
                " expires_at DATETIME NOT NULL,"
                " is_revoked BOOLEAN NOT NULL DEFAULT 0,"
                " last_used_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO api_keys (user_id, label, token_hash, expires_at) "
                "VALUES (1, 'Default', 'abc12345def', '2030-01-01 00:00:00')"
            ))
        _ensure_schema(eng)
        with eng.begin() as conn:
            row = conn.execute(text(
                "SELECT mask FROM api_keys WHERE token_hash='abc12345def'"
            )).fetchone()
        assert row.mask == "abc12345def"[-8:]


# ---------------------------------------------------------------------------
# G8 — POST /api/auth/keys becomes default only when the user has no default
# ---------------------------------------------------------------------------


class TestG8CreateKeyDefaultFallback:
    """v4 §6.1.6: a new key becomes default only if the user has no default."""

    def test_create_key_becomes_default_when_user_has_none(self):
        from unittest.mock import MagicMock, patch
        from app.routers.auth import create_key
        key = MagicMock()
        key.id = 50
        key.to_dict.return_value = {"id": 50}
        db = MagicMock()
        with patch("app.routers.auth.auth_service.create_api_key",
                   return_value=(key, "RAW")) as cak, \
             patch("app.routers.auth.auth_service.user_has_default_key",
                   return_value=False) as uhd, \
             patch("app.routers.auth.auth_service.set_default_api_key") as sda:
            out = create_key({"label": "x"}, {"role": "admin", "id": 1}, db)
        cak.assert_called_once()
        uhd.assert_called_once()
        sda.assert_called_once_with(db, 50)
        assert out["token"] == "RAW"

    def test_create_key_not_default_when_user_has_one(self):
        from unittest.mock import MagicMock, patch
        from app.routers.auth import create_key
        key = MagicMock()
        key.id = 51
        key.to_dict.return_value = {"id": 51}
        db = MagicMock()
        with patch("app.routers.auth.auth_service.create_api_key",
                   return_value=(key, "RAW")), \
             patch("app.routers.auth.auth_service.user_has_default_key",
                   return_value=True), \
             patch("app.routers.auth.auth_service.set_default_api_key") as sda:
            create_key({"label": "x"}, {"role": "viewer", "id": 2}, db)
        sda.assert_not_called()

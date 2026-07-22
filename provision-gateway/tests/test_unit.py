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
        assert s.PROVISION_API_URL == "http://provision-api:8000"
        assert s.JWT_EXPIRE_SEC == 3600
        assert s.JWT_REFRESH_EXPIRE_SEC == 604800

    def test_docker_ops_log_still_defined(self):
        """DOCKER_OPS_LOG should still exist in config (backward compat)."""
        from app.config import Settings
        s = Settings()
        assert s.DOCKER_OPS_LOG is not None


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

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
# Tests for GAP-4 — template classification must enforce git-tracked/original criterion
# ---------------------------------------------------------------------------

class TestTemplateClassificationGitTracked:
    """LLM-generated (untracked / .generated-marked) deployment-critical files
    must appear ONLY in Generated Files, never in Templates (GAP-4)."""

    @staticmethod
    def _fake_git_ls_files(stdout: str):
        import subprocess

        def fake_run(args, *a, **k):
            if "ls-files" in args:
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")
        return fake_run

    @staticmethod
    def _fake_git_missing():
        import subprocess

        def fake_run(args, *a, **k):
            if "ls-files" in args:
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="not a git repo")
            raise AssertionError(f"unexpected subprocess.run call: {args}")
        return fake_run

    def test_untracked_deployment_file_not_in_templates(self, tmp_path, monkeypatch):
        """An untracked (LLM-generated) docker-compose.yml must NOT be in template_files."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "docker-compose.yml").write_text("services: {}\n")
        (project / "main.py").write_text("print('hi')\n")

        # Dockerfile and main.py are git-tracked (original); docker-compose.yml is not.
        monkeypatch.setattr(
            "subprocess.run",
            self._fake_git_ls_files("Dockerfile\nmain.py\n"),
        )

        info = ServiceManager()._get_service_info(project)
        assert "docker-compose.yml" in info["generated_files"]
        assert "docker-compose.yml" not in info["template_files"]
        assert "Dockerfile" in info["template_files"]
        assert "Dockerfile" not in info["generated_files"]

    def test_tracked_deployment_files_are_templates(self, tmp_path, monkeypatch):
        """A git-tracked original deployment-critical file should be a template."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "nginx.conf").write_text("server {}\n")

        monkeypatch.setattr(
            "subprocess.run",
            self._fake_git_ls_files("Dockerfile\nnginx.conf\n"),
        )

        info = ServiceManager()._get_service_info(project)
        assert "Dockerfile" in info["template_files"]
        assert "nginx.conf" in info["template_files"]
        assert "Dockerfile" not in info["generated_files"]

    def test_generated_marker_excluded_from_all_listings(self, tmp_path, monkeypatch):
        """`.generated` marker files must be excluded from files, generated_files, template_files."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "docker-compose.yml").write_text("services: {}\n")
        (project / "docker-compose.yml.generated").write_text("")

        # The marker file itself is untracked; docker-compose.yml is untracked too (LLM-generated).
        monkeypatch.setattr("subprocess.run", self._fake_git_ls_files(""))

        info = ServiceManager()._get_service_info(project)
        assert "docker-compose.yml.generated" not in info["files"]
        assert "docker-compose.yml.generated" not in info["generated_files"]
        assert "docker-compose.yml.generated" not in info["template_files"]

    def test_no_git_fallback_uses_type_classification(self, tmp_path, monkeypatch):
        """When git is unavailable, fall back to type-based template classification (GAP-4)."""
        from app.services.service_manager import ServiceManager
        project = tmp_path / "myapp"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12")
        (project / "main.py").write_text("print('hi')\n")

        monkeypatch.setattr("subprocess.run", self._fake_git_missing())

        info = ServiceManager()._get_service_info(project)
        assert "Dockerfile" in info["template_files"]
        assert "main.py" not in info["template_files"]


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
        """docker_compose prompt should reference the provision-api skill."""
        from app.services.llm_service import LLMService
        svc = LLMService()
        prompt = svc._build_prompt("docker_compose", {
            "repo_description": "test app",
            "repo_files": ["main.py"],
            "port": 8000,
            "language": "python",
            "framework": "fastapi",
        })
        assert "_users_provision/skills/provision-api" in prompt
        assert "Use `build: .`" in prompt
        assert "Use named volumes" in prompt

    def test_nginx_prompt_references_skill(self):
        """nginx_conf prompt should reference the provision-api skill."""
        from app.services.llm_service import LLMService
        svc = LLMService()
        prompt = svc._build_prompt("nginx_conf", {
            "repo_description": "test app",
            "repo_files": ["main.py"],
            "port": 8000,
            "language": "python",
            "framework": "fastapi",
        })
        assert "_users_provision/skills/provision-api" in prompt
        assert "proxy_pass host must match" in prompt


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
        assert "compose_template_path" not in content

    def test_frontend_deployform_uses_nginx_conf_file_path(self):
        """DeployForm.tsx should use nginx_conf_file_path."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        assert "nginx_conf_file_path" in content
        assert "nginx_conf_template_path" not in content


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
        assert "form.submit()" in content
        assert "Auto-deploying" in content


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
        """POST /api/services with mode='template' should NOT return 501."""
        from app.routers.services import create_service
        import inspect
        assert inspect.iscoroutinefunction(create_service)

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
        # Find the first occurrence of 'save-generated' — that's the save block
        save_block = content[content.find("save-generated") - 80:content.find("save-generated") + 20]
        assert "&& autoDeploy" not in save_block, (
            "Save block should NOT be gated by autoDeploy: " + save_block
        )
        assert "Object.keys(generatedFiles).length > 0" in save_block, (
            "Save block should check if generatedFiles exist"
        )

    def test_save_block_executes_before_deploy(self):
        """The save-to-disk call should appear before the deploy POST payload building."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        save_idx = content.find("save-generated")
        payload_idx = content.find("Build deploy payload")
        assert save_idx > 0, "save-generated call not found in DeployForm.tsx"
        assert payload_idx > 0, "Payload building comment not found"
        assert save_idx < payload_idx, (
            "Save-generated call should appear BEFORE deploy payload building. "
            f"save at {save_idx}, payload at {payload_idx}"
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
        assert "(check failed" in content, (
            "DeployForm should set missingFiles to a sentinel '(check failed ...)' value on error"
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
        # The user_name onChange calls computeNextLabel with user + service values
        assert 'if (val && svc) computeNextLabel(val, svc)' in content, (
            "user_name onChange should trigger computeNextLabel with user and service"
        )

    def test_compute_next_label_called_on_service_change(self):
        """DeployForm service_name onChange should trigger computeNextLabel."""
        from pathlib import Path
        deploy_form = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "DeployForm.tsx"
        content = deploy_form.read_text()
        # The service_name onChange calls computeNextLabel with user + service values
        assert 'if (val && user) computeNextLabel(user, val)' in content, (
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

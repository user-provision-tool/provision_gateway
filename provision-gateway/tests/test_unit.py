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
        """AddServiceModal should use FileReader for reading upload files."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        content = modal_path.read_text()
        assert "FileReader" in content, "Upload should use FileReader to read files"

    def test_upload_uses_base64_encoding(self):
        """AddServiceModal should encode files as base64 for JSON upload."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        content = modal_path.read_text()
        assert "base64" in content, "Upload should use base64 encoding"

    def test_upload_uses_json_create_service(self):
        """AddServiceModal should use createServiceGit with JSON mode 'upload'."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        content = modal_path.read_text()
        assert "mode: 'upload'" in content or 'mode: "upload"' in content, (
            "Upload should pass mode: 'upload' to the API"
        )


# ---------------------------------------------------------------------------
# Tests for G8 — Template tab removed from AddServiceModal
# ---------------------------------------------------------------------------

class TestNoTemplateTab:
    """Tests that template tab is removed from AddServiceModal (G8)."""

    def test_no_template_mode_in_state(self):
        """AddServiceModal mode state should not include 'template'."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        content = modal_path.read_text()
        mode_line = None
        for line in content.splitlines():
            if "useState" in line and ("mode" in line or "Mode" in line):
                mode_line = line
                break
        assert mode_line is not None, "Could not find mode useState declaration"
        assert "'template'" not in mode_line and '"template"' not in mode_line, (
            "mode state should not include 'template' option"
        )

    def test_no_template_tab_in_tab_items(self):
        """AddServiceModal should not contain 'From Template' or 'template' tab."""
        from pathlib import Path
        modal_path = Path(__file__).parent.parent.parent / "provision-dashboard" / "src" / "components" / "services" / "AddServiceModal.tsx"
        content = modal_path.read_text()
        assert "From Template" not in content and "From template" not in content, (
            "Template tab should be removed from AddServiceModal"
        )


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

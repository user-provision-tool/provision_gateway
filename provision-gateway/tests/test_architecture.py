"""Architecture validation tests — verify gateway delegates to provision-api.

These tests validate that the gateway does NOT duplicate provision-api
functionality and properly delegates ALL Docker/filesystem operations.
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestNoDuplicates:
    """Verify duplicate modules have been removed."""

    def test_docker_service_removed(self):
        """docker_service.py must not exist — Docker ops delegated to provision-api."""
        path = Path(__file__).parent.parent / "app" / "services" / "docker_service.py"
        assert not path.exists(), (
            "docker_service.py still exists! All Docker ops must go through "
            "provision_service.py → provision-api. Remove this file."
        )

    def test_nginx_parser_removed(self):
        """nginx_parser.py must not exist — nginx parsing delegated to provision-api."""
        path = Path(__file__).parent.parent / "app" / "utils" / "nginx_parser.py"
        assert not path.exists(), (
            "nginx_parser.py still exists! Nginx conf parsing is provision-api's job. "
            "Remove this file."
        )

    def test_compose_converter_removed(self):
        """Gateway must not have its own compose converter."""
        path = Path(__file__).parent.parent / "app" / "lib" / "compose_converter.py"
        assert not path.exists(), (
            "compose_converter.py still exists! Template conversion is provision-api's job. "
            "Remove this file."
        )

    def test_nginx_converter_removed(self):
        """Gateway must not have its own nginx converter."""
        path = Path(__file__).parent.parent / "app" / "lib" / "nginx_converter.py"
        assert not path.exists(), (
            "nginx_converter.py still exists! Template conversion is provision-api's job. "
            "Remove this file."
        )


class TestProvisionServiceHasAllProxies:
    """Verify ProvisionService exposes proxy methods for all provision-api endpoints."""

    def test_provision_service_has_deploy_methods(self):
        """Core CRUD: register, remove, rebuild, start, stop, password."""
        from app.services.provision_service import ProvisionService
        ps = ProvisionService()
        methods = [m for m in dir(ps) if not m.startswith('_')]
        required = [
            'register_user', 'remove_user', 'rebuild_user',
            'start_user', 'stop_user', 'change_user_password',
            'list_users', 'get_user',
        ]
        for method in required:
            assert method in methods, f"ProvisionService missing method: {method}"

    def test_provision_service_has_task_methods(self):
        """Task management."""
        from app.services.provision_service import ProvisionService
        ps = ProvisionService()
        methods = [m for m in dir(ps) if not m.startswith('_')]
        for method in ['list_tasks', 'get_task', 'cancel_task']:
            assert method in methods, f"ProvisionService missing method: {method}"

    def test_provision_service_has_docker_methods(self):
        """Docker stats and operations delegated to provision-api."""
        from app.services.provision_service import ProvisionService
        ps = ProvisionService()
        methods = [m for m in dir(ps) if not m.startswith('_')]
        required = [
            'docker_ps', 'docker_stats', 'docker_info', 'host_stats',
            'container_exists', 'container_running',
            'network_connect', 'nginx_reload',
        ]
        for method in required:
            assert method in methods, f"ProvisionService missing method: {method}"

    def test_provision_service_has_nginx_methods(self):
        """Nginx management delegated to provision-api."""
        from app.services.provision_service import ProvisionService
        ps = ProvisionService()
        methods = [m for m in dir(ps) if not m.startswith('_')]
        for method in ['nginx_reconnect_all', 'nginx_connections']:
            assert method in methods, f"ProvisionService missing method: {method}"


class TestAuthServiceEndUserSupport:
    """Verify auth_service supports both admin and end-user authentication."""

    def test_authenticate_user_function_exists(self):
        """authenticate_user must exist and support both admin and end_user types."""
        from app.services.auth_service import authenticate_user
        assert callable(authenticate_user), "authenticate_user must be callable"

    def test_get_end_user_functions_exist(self):
        """End-user lookup functions must exist."""
        from app.services.auth_service import (
            get_end_user_by_username,
            get_end_user_by_id,
            authenticate_end_user,
        )
        assert callable(get_end_user_by_username)
        assert callable(get_end_user_by_id)
        assert callable(authenticate_end_user)

    def test_jwt_supports_user_type(self):
        """JWT tokens must include user_type field."""
        from app.services.auth_service import create_access_token
        # Create token with user_type
        token = create_access_token(1, "test@test.com", "admin", user_type="admin")
        from app.services.auth_service import decode_access_token
        payload = decode_access_token(token)
        assert payload.get("user_type") == "admin", "JWT must include user_type field"

        # End-user token
        token2 = create_access_token(2, "bob", "viewer", user_type="end_user")
        payload2 = decode_access_token(token2)
        assert payload2.get("user_type") == "end_user"
        assert payload2.get("role") == "viewer"


class TestMiddlewareSupportsEndUsers:
    """Verify auth middleware supports end-user tokens."""

    def test_get_current_user_function_exists(self):
        """get_current_user dependency must exist."""
        from app.middleware import get_current_user
        assert callable(get_current_user), "get_current_user must be callable"


class TestRouterImports:
    """Verify routers import from provision_service, not docker_service."""

    def test_system_router_no_docker_service_import(self):
        """system.py must NOT import docker_service."""
        system_path = Path(__file__).parent.parent / "app" / "routers" / "system.py"
        content = system_path.read_text()
        assert "from ..services import docker_service" not in content, (
            "system.py imports docker_service! "
            "Use provision_service for all Docker operations."
        )
        assert "docker_service." not in content, (
            "system.py references docker_service! "
            "Use provision_service for all Docker operations."
        )

    def test_system_router_uses_provision_service(self):
        """system.py must use provision_service for stats."""
        system_path = Path(__file__).parent.parent / "app" / "routers" / "system.py"
        content = system_path.read_text()
        assert "provision_service." in content, (
            "system.py should use provision_service for Docker/host operations"
        )

    def test_users_router_no_direct_docker_ops(self):
        """users.py must NOT use subprocess for Docker operations."""
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert 'subprocess.run(["docker"' not in content, (
            "users.py runs docker commands directly! "
            "Use provision_service for all provision operations."
        )
        assert 'docker exec' not in content, (
            "users.py has 'docker exec'! "
            "Use provision_service for all provision operations."
        )
        assert '_bcrypt.hashpw' not in content, (
            "users.py has bcrypt hashing! "
            "Password change must delegate to provision-api."
        )

    def test_users_router_uses_provision_service_for_up_down(self):
        """users.py up/down endpoints must use provision_service."""
        users_path = Path(__file__).parent.parent / "app" / "routers" / "users.py"
        content = users_path.read_text()
        assert "provision_service.start_user" in content, (
            "users.py /up endpoint must use provision_service.start_user()"
        )
        assert "provision_service.stop_user" in content, (
            "users.py /down endpoint must use provision_service.stop_user()"
        )
        assert "provision_service.change_user_password" in content, (
            "users.py /password endpoint must use provision_service.change_user_password()"
        )

    def test_reconciliation_no_docker_service_import(self):
        """reconciliation.py must NOT import from docker_service."""
        recon_path = Path(__file__).parent.parent / "app" / "services" / "reconciliation.py"
        content = recon_path.read_text()
        assert "from .docker_service import" not in content, (
            "reconciliation.py imports from docker_service! "
            "Use provision_service for all Docker operations."
        )


class TestLibIsClean:
    """Verify app/lib/ contains no duplicates of provision-api converters."""

    def test_lib_directory_clean(self):
        """lib/ should only have __init__.py, no converters."""
        lib_dir = Path(__file__).parent.parent / "app" / "lib"
        files = list(lib_dir.glob("*.py"))
        converter_files = [f.name for f in files if f.name not in ('__init__.py',)]
        assert len(converter_files) == 0, (
            f"app/lib/ contains converter files: {converter_files}. "
            "Template conversion is provision-api's job."
        )

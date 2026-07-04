"""Service manager — file operations, git clone, template conversion for service projects."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import settings


class ServiceManager:
    """Manages service project files in SOURCE_PROJECTS_DIR."""

    def __init__(self) -> None:
        self._source_dir = settings.SOURCE_PROJECTS_DIR
        self._source_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_services(self) -> list[dict[str, Any]]:
        """List all service projects in source_projects."""
        services = []
        if not self._source_dir.exists():
            return services

        for project_dir in sorted(self._source_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            services.append(self._get_service_info(project_dir))
        return services

    def get_service(self, name: str) -> dict[str, Any] | None:
        """Get info for a single service project."""
        project_dir = self._source_dir / name
        if not project_dir.is_dir():
            return None
        return self._get_service_info(project_dir)

    def _get_service_info(self, project_dir: Path) -> dict[str, Any]:
        """Build the service info dict for a project directory."""
        name = project_dir.name
        files = []
        for f in sorted(project_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                rel = str(f.relative_to(project_dir))
                files.append(rel)

        has_compose_template = any(f.endswith(".yml.j2") for f in files)
        has_nginx_template = any(f.endswith(".nginx.conf.j2") for f in files)
        has_dockerfile = any("Dockerfile" in f for f in files)

        return {
            "name": name,
            "path": str(project_dir),
            "files": files,
            "has_compose_template": has_compose_template,
            "has_nginx_template": has_nginx_template,
            "has_dockerfile": has_dockerfile,
            "active_users": 0,  # Will be enriched later
            "active_instances": [],
            "created_at": "",
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_from_git(
        self, repo_url: str, branch: str = "main", name: str | None = None
    ) -> dict[str, Any]:
        """Clone a git repository into source_projects."""
        if name is None:
            name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target = self._source_dir / name
        if target.exists():
            raise FileExistsError(f"Service '{name}' already exists at {target}")

        subprocess.run(
            ["git", "clone", "--branch", branch, "--single-branch", repo_url, str(target)],
            check=True, capture_output=True, text=True,
        )
        return self._get_service_info(target)

    def create_from_upload(
        self, name: str, files: dict[str, str]
    ) -> dict[str, Any]:
        """Create a service project from uploaded file contents."""
        target = self._source_dir / name
        if target.exists():
            raise FileExistsError(f"Service '{name}' already exists at {target}")
        target.mkdir(parents=True)

        for filename, content in files.items():
            filepath = target / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)

        return self._get_service_info(target)

    def delete_service(self, name: str) -> bool:
        """Delete a service project directory."""
        target = self._source_dir / name
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def get_file(self, service_name: str, filename: str) -> str | None:
        """Read a file from a service project."""
        filepath = self._source_dir / service_name / filename
        if not filepath.is_file():
            return None
        return filepath.read_text()

    def write_file(self, service_name: str, filename: str, content: str) -> bool:
        """Write or update a file in a service project."""
        filepath = self._source_dir / service_name / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
        return True

    def list_files(self, service_name: str) -> list[str]:
        """List all files in a service project."""
        target = self._source_dir / service_name
        if not target.is_dir():
            return []
        files = []
        for f in sorted(target.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                files.append(str(f.relative_to(target)))
        return files

    # ------------------------------------------------------------------
    # Template conversion (delegates to provision-api or local logic)
    # ------------------------------------------------------------------

    def convert_compose(
        self, service_name: str, compose_file: str
    ) -> dict[str, str]:
        """Convert a plain docker-compose file to a Jinja2 template.
        
        This can either call provision-api's converter or do it locally.
        For now, we copy the provision-api converter logic.
        """
        from .provision_service import provision_service
        from ..lib.compose_converter import compose_file_to_template

        src = self._source_dir / service_name / compose_file
        if not src.exists():
            raise FileNotFoundError(f"Compose file not found: {src}")

        template_out = src.parent / f"{src.stem}.yml.j2"
        compose_file_to_template(str(src), str(template_out), service_name_hint=service_name)
        return {
            "compose_template": str(template_out.name),
            "compose_file": compose_file,
        }

    def convert_nginx(
        self, service_name: str, nginx_file: str, compose_service_names: list[str] | None = None,
    ) -> dict[str, str]:
        """Convert a plain nginx conf to a Jinja2 template."""
        from ..lib.nginx_converter import nginx_file_to_template

        src = self._source_dir / service_name / nginx_file
        if not src.exists():
            raise FileNotFoundError(f"Nginx file not found: {src}")

        template_out = src.parent / f"{src.name}.j2"
        nginx_file_to_template(str(src), str(template_out), service_name, compose_service_names)
        return {
            "nginx_template": str(template_out.name),
            "nginx_file": nginx_file,
        }


# Singleton
service_manager = ServiceManager()

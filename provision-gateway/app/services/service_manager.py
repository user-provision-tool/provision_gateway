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

    # Patterns to exclude from file listings (build artifacts, VCS, etc.)
    _EXCLUDE_PATTERNS = [
        ".git", ".gitignore", ".gitattributes", ".gitmodules",
        "node_modules", ".npmignore", "package-lock.json",
        "dist", ".vite", ".tsbuildinfo",
        ".github", ".DS_Store",
    ]

    def _is_excluded(self, rel_path: str) -> bool:
        """Check if a relative path should be excluded from file listings."""
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            if part in self._EXCLUDE_PATTERNS:
                return True
        # Exclude compiled JS/map/ts files inside dist/ directories
        if "/dist/" in rel_path or rel_path.startswith("dist/"):
            return True
        return False

    def _get_service_info(self, project_dir: Path) -> dict[str, Any]:
        """Build the service info dict for a project directory. Excludes build artifacts and VCS."""
        name = project_dir.name
        files = []
        generated_files = []
        for f in sorted(project_dir.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(project_dir))
                if self._is_excluded(rel):
                    continue
                files.append(rel)
                if rel.endswith(".generated") or "generated_" in rel:
                    generated_files.append(rel)

        has_compose_template = any(f.endswith(".yml.j2") for f in files)
        has_nginx_template = any(f.endswith(".nginx.conf.j2") or f.endswith(".conf.j2") for f in files)
        has_dockerfile = any("Dockerfile" in f for f in files)

        # Detect active users from registry
        active_users = 0
        active_instances = []
        try:
            from ..config import settings
            registry_file = settings.GENERATED_DIR / "user_registry.yml"
            if registry_file.exists():
                import yaml
                with open(registry_file) as rf:
                    registry = yaml.safe_load(rf) or []
                for entry in registry:
                    if entry.get("service_name") == name:
                        active_users += 1
                        active_instances.append(f"{entry.get('user_name')}/{entry.get('label', '0')}")
        except Exception:
            pass

        return {
            "name": name,
            "path": str(project_dir),
            "files": files,
            "generated_files": generated_files,
            "has_compose_template": has_compose_template,
            "has_nginx_template": has_nginx_template,
            "has_dockerfile": has_dockerfile,
            "active_users": active_users,
            "active_instances": active_instances,
            "created_at": "",
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_from_git(
        self, repo_url: str, branch: str = "main", name: str | None = None,
        use_proxy: bool = False, db_session=None,
    ) -> dict[str, Any]:
        """Clone a git repository into source_projects.
        
        If use_proxy is True, configures git to use the global proxy
        before cloning, and cleans up after.
        """
        if name is None:
            name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target = self._source_dir / name
        if target.exists():
            raise FileExistsError(f"Service '{name}' already exists at {target}")

        # Configure git proxy if requested
        if use_proxy and db_session:
            from .proxy_service import configure_git_proxy, has_active_proxy
            if not has_active_proxy(db_session):
                raise ValueError("No active proxy configured. Activate a proxy in Settings first.")
            configure_git_proxy(db_session)

        try:
            subprocess.run(
                ["git", "clone", "--branch", branch, "--single-branch", repo_url, str(target)],
                check=True, capture_output=True, text=True,
            )
        finally:
            # Clean up git proxy config
            if use_proxy:
                subprocess.run(
                    ["git", "config", "--global", "--unset", "http.proxy"],
                    check=False, capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "--global", "--unset", "https.proxy"],
                    check=False, capture_output=True,
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
        """List all files in a service project, excluding build artifacts and VCS."""
        target = self._source_dir / service_name
        if not target.is_dir():
            return []
        files = []
        for f in sorted(target.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(target))
                if not self._is_excluded(rel):
                    files.append(rel)
        return files

    # ------------------------------------------------------------------
    # Template conversion (delegates to provision-api or local logic)
    # ------------------------------------------------------------------

    def convert_compose(
        self, service_name: str, compose_file: str
    ) -> dict[str, str]:
        """Mark a plain docker-compose file for template conversion.
        
        Conversion is handled by provision-api at deploy time.
        The gateway just copies the file with a .j2 extension as a marker.
        """
        src = self._source_dir / service_name / compose_file
        if not src.exists():
            raise FileNotFoundError(f"Compose file not found: {src}")

        template_out = src.parent / f"{src.stem}.yml.j2"
        # Copy raw content — provision-api's converter handles real transformation at deploy time
        content = src.read_text()
        header = f"# Jinja2 compose template — conversion handled by provision-api at deploy time\n# Service: {service_name}\n\n"
        template_out.write_text(header + content)
        return {
            "compose_template": str(template_out.name),
            "compose_file": compose_file,
        }

    def convert_nginx(
        self, service_name: str, nginx_file: str, compose_service_names: list[str] | None = None,
    ) -> dict[str, str]:
        """Mark a plain nginx conf for template conversion.
        
        Conversion is handled by provision-api at deploy time.
        The gateway just copies the file with a .j2 extension as a marker.
        """
        src = self._source_dir / service_name / nginx_file
        if not src.exists():
            raise FileNotFoundError(f"Nginx file not found: {src}")

        template_out = src.parent / f"{src.name}.j2"
        content = src.read_text()
        header = f"# Jinja2 nginx template — conversion handled by provision-api at deploy time\n# Service: {service_name}\n\n"
        template_out.write_text(header + content)
        return {
            "nginx_template": str(template_out.name),
            "nginx_file": nginx_file,
        }


    def create_from_zip(self, name: str, zip_content: bytes) -> dict[str, Any]:
        """Extract a zip file into source_projects."""
        import io, zipfile, os as _os
        target = self._source_dir / name
        if target.exists():
            raise FileExistsError(f"Service '{name}' already exists at {target}")
        target.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
            members = zf.namelist()
            prefix = _os.path.commonpath(members) if members else ""
            if prefix and prefix != "/" and all(m.startswith(prefix + "/") or m == prefix for m in members):
                for m in members:
                    if m == prefix or m == prefix + "/": continue
                    rel = m[len(prefix)+1:]
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True) if not m.endswith("/") else dest.mkdir(parents=True, exist_ok=True)
                    if not m.endswith("/"): dest.write_bytes(zf.read(m))
            else:
                zf.extractall(target)
        return self._get_service_info(target)


# Singleton
service_manager = ServiceManager()

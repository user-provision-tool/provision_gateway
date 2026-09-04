"""Service manager — file operations, git clone, template conversion for service projects."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from .project_state import (
    PROJECT_STATE_FILENAME,
    compute_dir_fingerprint,
    fingerprints_equal,
    now_iso,
    ProjectState,
)


class ServiceNotFoundError(Exception):
    """Raised when a service project directory does not exist.

    Distinct from ``ValueError`` so routers can map "unknown service" to
    404 deterministically instead of matching on message text (a
    non-directory recipe path also reads as "not found").
    """


class ServiceManager:
    """Manages service project files in SOURCE_PROJECTS_DIR."""

    def __init__(self) -> None:
        self._source_dir = settings.SOURCE_PROJECTS_DIR
        self._source_dir.mkdir(parents=True, exist_ok=True)
        # Project change tracking for active monitoring (GAP-004)
        self._known_projects: set[str] = set()
        self._new_project_events: list[dict[str, Any]] = []
        self._last_scan_time: float = 0.0
        # Per-project scan-state locks (threadpool handlers can rescan concurrently)
        self._project_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Registry YAML TTL cache: (loaded_at_epoch, {service_name: [entries]})
        self._registry_cache: tuple[float, dict[str, list[dict[str, Any]]]] | None = None
        self._registry_guard = threading.Lock()
        # Initially populate known projects
        self._refresh_known_projects()

    def _load_registry(self) -> dict[str, list[dict[str, Any]]]:
        """Load ``user_registry.yml`` → ``{service_name: [entry, ...]}``.

        The file can be tens of KB (hundreds of users); parsing it once per
        project per request made a warm /api/services take ~200ms. The result
        is cached for 1 second so listing stays well under 100ms (DB1).
        """
        now = time.monotonic()
        with self._registry_guard:
            if self._registry_cache and now - self._registry_cache[0] < 1.0:
                return self._registry_cache[1]
            by_service: dict[str, list[dict[str, Any]]] = {}
            try:
                registry_file = settings.GENERATED_DIR / "user_registry.yml"
                if registry_file.exists():
                    import yaml
                    # Prefer the C loader (libyaml) when available — pure-python
                    # SafeLoader parses a 25KB registry in ~40ms, which alone
                    # would bust the DB1 < 100ms warm-scan budget.
                    loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
                    with open(registry_file) as rf:
                        registry = yaml.load(rf, Loader=loader) or []
                    for entry in registry:
                        sn = entry.get("service_name") if isinstance(entry, dict) else None
                        if sn:
                            by_service.setdefault(sn, []).append(entry)
            except Exception:
                pass
            self._registry_cache = (now, by_service)
            return by_service

    def _refresh_known_projects(self) -> None:
        """Refresh the set of known project names."""
        current = set()
        if self._source_dir.exists():
            for d in self._source_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    current.add(d.name)
        self._known_projects = current

    def scan_for_new_projects(self) -> list[dict[str, Any]]:
        """Scan source_projects directory and detect newly added projects.

        Returns a list of new project info dicts.
        Detects projects added since the last scan by comparing against
        the known set.
        """
        now = time.time()
        new_projects: list[dict[str, Any]] = []
        current = set()

        if self._source_dir.exists():
            for d in self._source_dir.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    current.add(d.name)
                    if d.name not in self._known_projects:
                        new_projects.append({
                            "name": d.name,
                            "path": str(d),
                            "detected_at": now,
                        })

        # Update known set
        self._known_projects = current
        self._last_scan_time = now

        # Store events for retrieval via notifications endpoint
        for np in new_projects:
            self._new_project_events.append(np)

        return new_projects

    def get_new_project_events(self, clear: bool = True) -> list[dict[str, Any]]:
        """Return accumulated new project detection events.

        If clear=True, clears the event buffer after reading.
        """
        events = list(self._new_project_events)
        if clear:
            self._new_project_events.clear()
        return events

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
        PROJECT_STATE_FILENAME,
    ]

    # Template file patterns — only these file types are shown in the
    # "Templates" column. Everything else is classified as "generated"
    # for the deployment-focused summary view.
    _TEMPLATE_PATTERNS = (
        "Dockerfile",
        "docker-compose",
        ".nginx.conf",
        ".conf",
        ".env",
    )

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

    @staticmethod
    def _is_template_file(rel_path: str) -> bool:
        """Check if a file matches the template file patterns.

        Only these file types are considered "templates" in the deployment
        summary view: Dockerfile, docker-compose*, *.nginx.conf, *.conf,
        .env, .env.example.
        """
        basename = rel_path.split("/")[-1]
        if basename == "Dockerfile":
            return True
        if basename in (".env", ".env.example"):
            return True
        if basename.startswith("docker-compose"):
            return True
        if basename.endswith(".nginx.conf"):
            return True
        if basename.endswith(".conf"):
            return True
        return False

    def _project_lock(self, name: str) -> threading.Lock:
        """Per-project lock, created on demand.

        Threadpool handlers mean concurrent rescans of the same project are
        possible; the lock serializes them.
        """
        with self._locks_guard:
            lock = self._project_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[name] = lock
            return lock

    def _scan_recipe_dir(self, project_dir: Path, recipe_path: str) -> dict | None:
        """Shallow TOP-LEVEL scan of one recipe dir.

        ``recipe_path`` is project-root-relative (``"."`` = project root).
        Returns ``{"fingerprint", "files", "generated", "templates"}`` with
        project-root-relative paths, or ``None`` if the dir is missing.

        Classification is marker-only: a file is "generated" iff a sibling
        ``{file}.generated`` marker exists. No git anywhere.
        """
        if recipe_path in ("", "."):
            dir_path = project_dir
            prefix = ""
        else:
            # Traversal-safe join (GAP-3) — callers pre-normalize via
            # _normalize_recipe_paths, this guards stale stored state too.
            try:
                from ..services.file_sets import _recipe_dir
                dir_path = _recipe_dir(project_dir, recipe_path)
            except Exception:
                return None  # traversal-invalid recipe path → skip, no crash
            prefix = recipe_path.rstrip("/") + "/"
        if not dir_path.is_dir():
            return None
        fingerprint = compute_dir_fingerprint(dir_path)
        files: list[str] = []
        generated: list[str] = []
        templates: list[str] = []
        try:
            with os.scandir(str(dir_path)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return None
        for entry in entries:
            name = entry.name
            rel = prefix + name
            # ``.generated`` markers are tracking metadata; the state file
            # (and its atomic-write .tmp sibling) is never a project file.
            if rel.endswith(".generated") or rel.startswith(".provision-state"):
                continue
            if self._is_excluded(rel):
                continue
            try:
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not is_file:
                continue  # dirs/symlinks are not part of the files union
            files.append(rel)
            has_marker = (dir_path / f"{name}.generated").is_file()
            if has_marker:
                generated.append(rel)
            elif self._is_template_file(rel):
                templates.append(rel)
        return {
            "fingerprint": fingerprint,
            "files": files,
            "generated": generated,
            "templates": templates,
        }

    @staticmethod
    def _resolve_recipe_paths(state: ProjectState) -> list[str]:
        """User-configured recipe dirs, or root-only when not configured."""
        if state.recipe_origin == "user":
            return list(state.recipe_paths) or ["."]
        return ["."]

    @staticmethod
    def _normalize_recipe_paths(project_dir: Path, recipe_paths: list[str]) -> list[str]:
        """Normalize + validate recipe dirs; raises ValueError on any bad path."""
        normalized: list[str] = []
        for raw in recipe_paths:
            p = (raw or "").strip()
            if p in ("", "."):
                normalized.append(".")
                continue
            if p.startswith("/") or "\\" in p or ":" in p:
                raise ValueError(f"Recipe path must be project-root-relative, got: {raw!r}")
            parts = [part for part in p.split("/") if part not in ("", ".")]
            if not parts:
                normalized.append(".")
                continue
            if any(part == ".." for part in parts):
                raise ValueError(f"Recipe path must not contain '..': {raw!r}")
            norm = "/".join(parts)
            if not (project_dir / norm).is_dir():
                raise ValueError(f"Recipe directory not found: {raw!r}")
            normalized.append(norm)
        return normalized or ["."]

    def _get_service_info(self, project_dir: Path) -> dict[str, Any]:
        """Build the service info dict for a project directory (cache-warm).

        Loads the per-project state under the project lock, rescans a recipe
        dir only when it is missing from the cache or its top-level fingerprint
        changed, rebuilds the sorted unions, and saves the state when anything
        changed. Never walks the full tree and never invokes git for
        classification.
        """
        name = project_dir.name
        lock = self._project_lock(name)
        with lock:
            state = ProjectState.load(project_dir) or ProjectState(project_dir)
            recipe_paths = self._resolve_recipe_paths(state)
            changed = False

            # Recipe set changed (e.g. user reconfigured) → drop stale scans.
            if list(state.recipe_paths) != recipe_paths:
                state.recipe_paths = recipe_paths
                state.dir_scans = {}
                changed = True

            scans: dict[str, dict] = {}
            for rp in recipe_paths:
                scan = self._scan_recipe_dir(project_dir, rp)
                if scan is None:
                    continue  # recipe dir deleted → skip, no crash
                cached = state.dir_scans.get(rp)
                cached_ok = (
                    isinstance(cached, dict)
                    and all(k in cached for k in ("fingerprint", "files", "generated", "templates"))
                    and fingerprints_equal(cached.get("fingerprint"), scan["fingerprint"])
                )
                if cached_ok:
                    scans[rp] = cached
                else:
                    scans[rp] = scan
                    changed = True
            state.dir_scans = scans

            files = sorted({f for s in scans.values() for f in s["files"]})
            generated_files = sorted({f for s in scans.values() for f in s["generated"]})
            template_files = sorted({f for s in scans.values() for f in s["templates"]})

            # recipes list in the preserved frontend shape:
            # [{path, label, is_root, template_files}, ...], root first.
            recipes: list[dict[str, Any]] = []
            for rp in recipe_paths:
                scan = scans.get(rp)
                if scan is None:
                    continue
                is_root = rp in ("", ".")
                recipes.append({
                    "path": "" if is_root else rp,
                    "label": "(root)" if is_root else rp,
                    "is_root": is_root,
                    "template_files": sorted(scan["templates"]),
                })
            recipes.sort(key=lambda r: (not r["is_root"], r["path"]))

            has_compose_template = any(f.endswith(".yml.j2") for f in files)
            has_nginx_template = any(f.endswith(".nginx.conf.j2") or f.endswith(".conf.j2") for f in files)
            has_dockerfile = any("Dockerfile" in f for f in files)

            if changed:
                state.files = files
                state.generated_files = generated_files
                state.template_files = template_files
                state.recipes = recipes
                state.updated_at = now_iso()
                try:
                    state.save()
                except OSError:
                    pass  # the state is a cache — a failed write must not break listing

        # Detect active users from registry (TTL-cached — the yaml can be
        # tens of KB and parsing it once per project per request dominated
        # /api/services latency; see DB1 warm-scan < 100ms success criteria)
        active_users = 0
        active_instances = []
        for entry in self._load_registry().get(name, []):
            active_users += 1
            active_instances.append(f"{entry.get('user_name')}/{entry.get('label', '0')}")

        return {
            "name": name,
            "path": str(project_dir),
            "files": files,
            "generated_files": generated_files,
            "template_files": template_files,
            "recipes": recipes,
            "has_compose_template": has_compose_template,
            "has_nginx_template": has_nginx_template,
            "has_dockerfile": has_dockerfile,
            "active_users": active_users,
            "active_instances": active_instances,
            "created_at": "",
        }

    def set_recipes(self, name: str, recipe_paths: list[str] | None) -> dict[str, Any]:
        """Set the recipe dirs scanned for a service project (user-configured).

        Normalizes (``.``/``''`` → ``.``), rejects ``..``/absolute/non-dir
        paths with ``ValueError`` (router → 400), sets ``recipe_origin="user"``,
        clears the scan cache, rescans, and returns the updated service info.
        """
        project_dir = self._source_dir / name
        if not project_dir.is_dir():
            raise ServiceNotFoundError(f"Service '{name}' not found")
        normalized = self._normalize_recipe_paths(project_dir, recipe_paths or ["."])
        lock = self._project_lock(name)
        with lock:
            state = ProjectState.load(project_dir) or ProjectState(project_dir)
            state.recipe_origin = "user"
            state.recipe_paths = normalized
            state.dir_scans = {}
            state.updated_at = now_iso()
            try:
                state.save()
            except OSError:
                pass
        return self._get_service_info(project_dir)

    def list_tree_children(self, name: str, dir_rel: str = "") -> list[dict[str, Any]]:
        """Live listing of a directory's IMMEDIATE children for the lazy tree.

        Returns ``[{name, path, type, is_generated, is_template}, ...]`` with
        project-root-relative paths. Raises ``ValueError`` on path traversal
        or a missing dir (router → 400).
        """
        project_dir = self._source_dir / name
        if not project_dir.is_dir():
            raise ServiceNotFoundError(f"Service '{name}' not found")
        project_resolved = project_dir.resolve()
        target = (project_dir / (dir_rel or "")).resolve()
        if target != project_resolved and not str(target).startswith(str(project_resolved) + os.sep):
            raise ValueError(f"Path traversal is not allowed: {dir_rel!r}")
        if not target.is_dir():
            raise ValueError(f"Directory not found: {dir_rel or '.'}")
        children: list[dict[str, Any]] = []
        try:
            with os.scandir(str(target)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            raise ValueError(f"Cannot read directory: {dir_rel or '.'}")
        for entry in entries:
            name_e = entry.name
            rel = f"{dir_rel}/{name_e}" if dir_rel else name_e
            if rel.endswith(".generated") or rel.startswith(".provision-state"):
                continue
            if self._is_excluded(rel):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir and not entry.is_file(follow_symlinks=False):
                continue  # sockets/fifos etc.
            children.append({
                "name": name_e,
                "path": rel,
                "type": "dir" if is_dir else "file",
                "is_generated": (not is_dir) and (target / f"{name_e}.generated").is_file(),
                "is_template": (not is_dir) and self._is_template_file(rel),
            })
        return children

    def invalidate_state(self, name: str) -> None:
        """Drop the cached scan state so the next listing rescans fresh.

        Called after direct file writes (LLM save-generated, editor saves)
        that may touch files below the scanned top level.
        """
        try:
            (self._source_dir / name / PROJECT_STATE_FILENAME).unlink()
        except OSError:
            pass

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
        self.invalidate_state(service_name)
        return True

    def create_file(self, service_name: str, filename: str, content: str) -> bool:
        """Create a new file in a service project with .generated marker."""
        filepath = self._source_dir / service_name / filename
        if filepath.exists():
            return False  # already exists
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content)
        # Track as generated
        (self._source_dir / service_name / f"{filename}.generated").write_text("")
        self.invalidate_state(service_name)
        return True

    def delete_file(self, service_name: str, filename: str) -> bool:
        """Delete a file from a service project and its .generated marker."""
        filepath = self._source_dir / service_name / filename
        if not filepath.is_file():
            return False
        filepath.unlink()
        # Also remove .generated marker if present
        marker = self._source_dir / service_name / f"{filename}.generated"
        if marker.is_file():
            marker.unlink()
        self.invalidate_state(service_name)
        return True

    def list_files(self, service_name: str) -> list[str]:
        """List the cached shallow-scan file union for a service project."""
        project_dir = self._source_dir / service_name
        if not project_dir.is_dir():
            return []
        return self._get_service_info(project_dir).get("files", [])

    # ------------------------------------------------------------------
    # Template conversion (delegates to provision-api or local logic)
    # ------------------------------------------------------------------

    def convert_compose(
        self, service_name: str, compose_file: str, recipe_path: str = "",
    ) -> dict[str, str]:
        """Mark a plain docker-compose file for template conversion.
        
        Conversion is handled by provision-api at deploy time.
        The gateway just copies the file with a .j2 extension as a marker.

        Args:
            recipe_path: Subdirectory of the recipe (empty = root recipe).
        """
        src = self._source_dir / service_name / compose_file
        if not src.exists():
            raise FileNotFoundError(f"Compose file not found: {src}")

        template_out = src.parent / f"{src.stem}.yml.j2"
        # Copy raw content — provision-api's converter handles real transformation at deploy time
        content = src.read_text()
        header = f"# Jinja2 compose template — conversion handled by provision-api at deploy time\n# Service: {service_name}\n\n"
        template_out.write_text(header + content)
        # Mark as generated
        (template_out.parent / f"{template_out.name}.generated").write_text("")
        self.invalidate_state(service_name)
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
        # Mark as generated
        (template_out.parent / f"{template_out.name}.generated").write_text("")
        self.invalidate_state(service_name)
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

    def create_from_template(
        self, name: str, template_id: int, db_session,
    ) -> dict[str, Any]:
        """Create a service project from a stored template.

        Reads the ServiceTemplate from the database and writes its
        content files (compose_j2, nginx_j2, env_template, dockerfile)
        into the project directory.
        """
        from ..models.service_template import ServiceTemplate

        target = self._source_dir / name
        if target.exists():
            raise FileExistsError(f"Service '{name}' already exists at {target}")

        template = db_session.query(ServiceTemplate).filter(
            ServiceTemplate.id == template_id
        ).first()
        if not template:
            raise FileNotFoundError(f"Template id={template_id} not found")

        target.mkdir(parents=True)

        # Write compose template
        compose_filename = f"{name}.yml.j2"
        (target / compose_filename).write_text(template.compose_j2)

        # Write nginx template if present
        if template.nginx_j2:
            nginx_filename = f"{name}.nginx.conf.j2"
            (target / nginx_filename).write_text(template.nginx_j2)

        # Write env template if present
        if template.env_template:
            (target / ".env").write_text(template.env_template)

        # Write Dockerfile if present
        if template.dockerfile:
            (target / "Dockerfile").write_text(template.dockerfile)

        return self._get_service_info(target)


# Singleton
service_manager = ServiceManager()

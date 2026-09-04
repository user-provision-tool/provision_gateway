"""Services router — /api/services/* endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..database import get_db
from ..middleware import require_admin
from ..models.service_template import ServiceTemplate
from ..services.audit_service import log_action
from ..services.service_manager import service_manager, ServiceNotFoundError
from ..services.llm_service import llm_service
from ..config import settings
from ..utils.file_scanner import scan_directory
from ..services.provision_service import provision_service
from ..schemas.services import ServiceRecipesRequest, ServiceTreeResponse

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
def list_services(
    current_admin: dict = Depends(require_admin),
):
    """List all service projects in source_projects.

    Synchronous handler: list_services rescans the (potentially huge)
    source_projects dir, so it must run in a worker thread and never block
    the event loop (see DB1/GAP-14). FastAPI runs sync ``def`` handlers in
    the threadpool.
    """
    services = service_manager.list_services()
    return {"services": services}


# ------------------------------------------------------------------
# Service Templates (GAP-001)
# ------------------------------------------------------------------


@router.get("/templates")
def list_templates(
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all available service templates.

    Synchronous endpoint: runs in a worker thread so the DB query cannot block
    the event loop (same rationale as the auth middleware deps).
    """
    templates = db.query(ServiceTemplate).order_by(ServiceTemplate.name).all()
    return {"templates": [t.to_dict() for t in templates]}


# ------------------------------------------------------------------
# Project change notifications (GAP-004)
# ------------------------------------------------------------------


@router.get("/notifications")
def get_project_notifications(
    current_admin: dict = Depends(require_admin),
):
    """Return newly detected project events from background monitoring.

    These are projects that appeared in source_projects since the
    last check. The frontend can poll this endpoint to show a
    notification banner.
    """
    events = service_manager.get_new_project_events(clear=True)
    return {"notifications": events, "count": len(events)}


@router.post("/{name}/recipes")
def set_service_recipes(
    name: str,
    req: ServiceRecipesRequest,
    current_admin: dict = Depends(require_admin),
):
    """Set the recipe (template) subdirectories for a service project.

    Body: either ``{"recipe_paths": ["docker"]}`` (explicit; empty list resets
    to root-only scanning) or ``{"auto": true}`` (re-enable auto-detect of the
    root directory only). Invalid paths (traversal, absolute, non-directory)
    are rejected with 400; unknown services with 404.
    """
    try:
        if req.auto:
            info = service_manager.set_recipes(name, [])
        else:
            info = service_manager.set_recipes(name, req.recipe_paths or [])
    except ServiceNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return info


@router.get("/{name}/tree")
def get_service_tree(
    name: str,
    dir: str = Query("", description="Directory relative to project root ('' = root)"),
    current_admin: dict = Depends(require_admin),
):
    """Return the immediate children of a directory in a service project.

    Lazy tree endpoint: one request per expanded directory, never a full
    recursive walk (see DB1/GAP-14). Traversal attempts yield 400.
    """
    try:
        children = service_manager.list_tree_children(name, dir)
    except ServiceNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ServiceTreeResponse(name=name, dir=dir, children=children)


@router.get("/{name}")
def get_service(
    name: str,
    current_admin: dict = Depends(require_admin),
):
    """Get details for a single service project."""
    svc = service_manager.get_service(name)
    if not svc:
        raise HTTPException(404, f"Service '{name}' not found")
    return svc


@router.post("", status_code=201)
def create_service(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new service project.

    Modes:
    - git: clone from repo_url
    - upload: from file contents
    - template: from template_id (future)
    """
    mode = req.get("mode", "git")
    name = req.get("name")
    if not name:
        raise HTTPException(400, "'name' is required")

    try:
        if mode == "git":
            repo_url = req.get("repo_url")
            if not repo_url:
                raise HTTPException(400, "'repo_url' is required for git mode")
            branch = req.get("branch", "main")
            use_proxy = req.get("use_proxy", False)
            svc = service_manager.create_from_git(
                repo_url, branch, name, use_proxy=use_proxy, db_session=db,
            )
        elif mode == "upload":
            zip_b64 = req.get("zip_content", "")
            if zip_b64:
                import base64
                svc = service_manager.create_from_zip(name, base64.b64decode(zip_b64))
            else:
                files = req.get("files", {})
                svc = service_manager.create_from_upload(name, files)
        elif mode == "template":
            template_id = req.get("template_id")
            if not template_id:
                raise HTTPException(400, "'template_id' is required for template mode")
            svc = service_manager.create_from_template(name, template_id, db_session=db)
        else:
            raise HTTPException(400, f"Unknown mode: {mode}")
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

    log_action(db, action="service_create", admin_id=current_admin["id"],
               target_service=name, status="success")
    return svc


def _compute_needs_env(name: str, recipe_path: str, api_result: dict) -> bool:
    """Selection-aware needs_env: scan the SELECTED compose files.

    Falls back to a union scan over all compose candidates in the recipe dir
    (over-approximation is safe for requiredness — design §Implementation
    notes L256-258); the provision-api's own scan is the final fallback.
    """
    from pathlib import Path
    from ..services import file_sets as file_sets_svc
    from ..services.file_sets import FileSetError as _FileSetError
    from ..services.file_sets import _recipe_dir as _fs_recipe_dir
    from ..utils.var_scan import needs_env as _scan_needs_env

    project_dir = settings.SOURCE_PROJECTS_DIR / name
    try:
        info = file_sets_svc.list_file_sets(name)
        sel = info["file_sets"].get(recipe_path or ".", {})
        compose_sel = [p for p in sel.get("compose", []) if isinstance(p, str)]
    except Exception:
        compose_sel = []
    try:
        recipe_dir = _fs_recipe_dir(project_dir, recipe_path)
    except _FileSetError:
        # Traversal-invalid recipe_path → no local scan; the api result stands.
        return bool(api_result.get("needs_env"))
    if compose_sel:
        paths = [recipe_dir / p for p in compose_sel]
    else:
        paths = [
            recipe_dir / f
            for f in (recipe_dir.iterdir() if recipe_dir.is_dir() else [])
            if f.is_file() and (f.name.endswith(".yml") or f.name.endswith(".yaml"))
            and not f.name.endswith(".j2") and not f.name.endswith(".generated")
        ]
    try:
        if paths and _scan_needs_env(paths):
            return True
    except OSError:
        pass
    return bool(api_result.get("needs_env"))


@router.get("/{name}/check-missing-files")
async def check_missing_files(
    name: str,
    recipe_path: str = Query("", description="Recipe subdirectory path"),
    current_admin: dict = Depends(require_admin),
):
    """Check which essential deployment files are missing for a service.

    Proxied to provision-api, then enriches with repo scan context
    for LLM-based file generation.

    Args:
        recipe_path: Optional subdirectory for multi-recipe projects.
    """
    try:
        result = await provision_service.check_missing_files(name, recipe_path)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")

    # needs_env-aware missing list computed GATEWAY-SIDE (design §Env story
    # L192-194): the gateway holds the selection + merged config, so the
    # provision-api result is post-processed here rather than given the
    # selection. When needs_env=false the env section is disabled and .env is
    # removed from the missing list (fixes the unconditional flag).
    result["needs_env"] = _compute_needs_env(name, recipe_path, result)
    if not result.get("needs_env") and ".env" in result.get("missing", []):
        result["missing"] = [m for m in result["missing"] if m != ".env"]

    # Enrich with repo scan context for LLM generation
    from pathlib import Path
    from ..services.file_sets import FileSetError as _FileSetError
    from ..services.file_sets import _recipe_dir as _fs_recipe_dir
    from ..utils.file_scanner import scan_directory
    project_dir = settings.SOURCE_PROJECTS_DIR / name
    try:
        scan_dir = _fs_recipe_dir(project_dir, recipe_path)
    except _FileSetError:
        scan_dir = project_dir  # traversal-invalid → root fallback, no crash
    if recipe_path and not scan_dir.is_dir():
        scan_dir = project_dir
    if scan_dir.is_dir():
        try:
            ctx = await run_in_threadpool(scan_directory, scan_dir)
            result["scan_context"] = {
                "repo_description": ctx.repo_description,
                "repo_files": ctx.repo_files,
                "port": ctx.port,
                "needs_db": ctx.needs_db,
                "needs_cache": ctx.needs_cache,
                "needs_volume": ctx.needs_volume,
                "language": ctx.language,
                "framework": ctx.framework,
                "has_dockerfile": ctx.has_dockerfile,
                "has_compose": ctx.has_compose,
                "has_nginx_conf": ctx.has_nginx_conf,
                "compose_services": ctx.compose_services,
            }
        except Exception:
            pass

    return result


@router.get("/{name}/compose-preview")
async def compose_preview(
    name: str,
    compose_files: list[str] = Query(default=[], description="Recipe-relative compose file paths (ordered)"),
    recipe_path: str = Query("", description="Recipe subdirectory path"),
    current_admin: dict = Depends(require_admin),
):
    """Lightweight convert/preview — converter in-call src→key mapping.

    Proxied to provision-api: the converter runs IN-CALL on the given compose
    file(s) and returns the bind-mount source→volume-key mapping (``src_to_key``
    + ordered ``volume_keys``).  The deploy panel's advanced volume-override
    rows consume ``volume_keys`` from this response instead of parsing ``.j2``
    files in the frontend (design §Implementation notes L284-286).  Pure
    preview — no templates or markers are written.
    """
    try:
        return await provision_service.compose_preview(name, compose_files, recipe_path)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")


@router.post("/check-deploy")
async def check_deploy_readiness(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Check if a service project has all files needed for deployment.

    Readiness REPORT ONLY — the implicit LLM auto-generation inside
    check-deploy is RETIRED (design §Implementation notes L252-253); explicit
    panel-driven generation with selection, prompt, and review gate replaces
    it (async generation jobs + save-generated with the overwrite matrix).

    Dependency graph (design §Dependency graph L211-219): compose is the
    root. Nginx readiness is reported only once compose resolves — generation
    of nginx is blocked until compose exists.
    """
    service_name = req.get("service_name")
    if not service_name:
        raise HTTPException(400, "'service_name' is required")
    recipe_path = req.get("recipe_path") or ""

    try:
        result = await provision_service.check_missing_files(service_name, recipe_path)
    except Exception as e:
        raise HTTPException(502, f"provision-api error: {e}")

    missing = list(result.get("missing") or [])
    needs_env = _compute_needs_env(service_name, recipe_path, result)
    if not needs_env and ".env" in missing:
        missing.remove(".env")

    has_compose = "docker-compose" not in missing
    has_nginx = "nginx.conf" not in missing
    # Dependency gate: nginx is not reported missing while compose is missing
    # (it cannot be validated or generated against an unresolved service set).
    gated_missing = [m for m in missing if m != "nginx.conf"] if not has_compose else missing

    return {
        "service": service_name,
        "ready": len(gated_missing) == 0,
        "has_compose": has_compose,
        "has_nginx": has_nginx,
        "missing": gated_missing,
        "needs_env": needs_env,
        "generated": {},
        "warnings": [],
        "needs_confirmation": False,
    }


def _fresh_generated_name(filepath: Path) -> str:
    """Distinct marked name for an original (unmarked) file (overwrite matrix).

    ``docker-compose.yml`` → ``docker-compose.generated-<epoch>.yml``.
    """
    import time as _time
    stem = filepath.stem
    suffix = filepath.suffix
    return f"{stem}.generated-{int(_time.time())}{suffix}"


@router.post("/save-generated")
def save_generated_files(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save LLM-generated files to the service project (overwrite matrix).

    Body::

        { "service_name": "myapp", "recipe_path": "sub/dir",
          "files": { "nginx.conf": "...", "docker-compose.yml": "..." },
          "selection": { "compose": [...], "nginx": ..., "env": [...], "profiles": [...] } }

    Overwrite matrix (design §Generation rules L124-129):
      - file MISSING → write as a new file + ``.generated`` marker;
      - file exists with a ``.generated`` marker → re-generate IN PLACE;
      - file exists as an ORIGINAL (unmarked) → originals are NEVER touched —
        the output is written to a fresh, marked new file (distinct name).
    A successful generate-save persists the (renamed) selection as the new
    stored default (design §Selection & UI L43-46).

    RACE WINDOW (documented, not mitigated — design §Implementation notes
    L270-272): generation-save runs in this gateway process while deploy runs
    in the provision-api process (register reads at render time).  There is no
    shared lock between the two.  Saves are ATOMIC (write content, then write
    the ``.generated`` marker) and deploys read at render time, leaving a
    narrow acceptable race window — a deploy racing an in-progress save simply
    sees the pre-write snapshot.  This is intentional: the window is
    DOCUMENTED, NOT mitigated (no mutex/fencing across processes).
    """
    import time as _time
    service_name = req.get("service_name")
    recipe_path = req.get("recipe_path", "")
    files = req.get("files", {})
    if not service_name or not files:
        raise HTTPException(400, "service_name and files required")

    # Traversal-safe target (GAP-3): recipe_path and every filename must stay
    # inside the recipe dir (design §Per-recipe scoping L221-238).
    from ..services.file_sets import FileSetError, _recipe_dir, _validate_paths
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    try:
        target = _recipe_dir(project_dir, recipe_path)
    except FileSetError as e:
        raise HTTPException(400, str(e))
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)

    rename_map: dict[str, str] = {}
    saved = []
    for filename, content in files.items():
        if not isinstance(filename, str) or not isinstance(content, str):
            raise HTTPException(400, "files must map filename -> string content")
        try:
            filename = _validate_paths(project_dir, recipe_path, [filename])[0]
        except FileSetError as e:
            raise HTTPException(400, str(e))
        filepath = target / filename
        marker = target / f"{filename}.generated"
        if filepath.exists() and not marker.is_file():
            # ORIGINAL (unmarked) — never clobber; write a fresh marked file.
            new_name = _fresh_generated_name(filepath)
            new_path = target / new_name
            new_path.write_text(content)
            (target / f"{new_name}.generated").write_text("")
            saved.append(new_name)
            rename_map[filename] = new_name
        else:
            filepath.write_text(content)
            marker.write_text("")
            saved.append(filename)
            rename_map[filename] = filename

    # Persist the selection as the new default (renamed to the saved names).
    selection = req.get("selection")
    if isinstance(selection, dict):
        from ..services.file_sets import FileSetError, put_file_set
        mapped: dict[str, Any] = {"profiles": selection.get("profiles") or []}
        mapped["compose"] = [rename_map.get(p, p) for p in (selection.get("compose") or [])]
        nginx = selection.get("nginx")
        mapped["nginx"] = rename_map.get(nginx, nginx) if nginx else None
        mapped["env"] = [rename_map.get(p, p) for p in (selection.get("env") or [])]
        try:
            put_file_set(service_name, recipe_path, mapped)
        except FileSetError:
            pass  # selection persistence is best-effort — save still succeeds

    log_action(db, action="llm_generated_files", admin_id=current_admin["id"],
               target_service=service_name, status="success",
               detail={"files": saved})

    return {"saved": saved, "service": service_name}


@router.delete("/{name}")
def delete_service(
    name: str,
    force: bool = Query(False),
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a service project."""
    deleted = service_manager.delete_service(name)
    if not deleted:
        raise HTTPException(404, f"Service '{name}' not found")

    log_action(db, action="service_delete", admin_id=current_admin["id"],
               target_service=name, status="success")
    return {"deleted": True}


@router.get("/{name}/files/{filename:path}")
def get_service_file(
    name: str,
    filename: str,
    current_admin: dict = Depends(require_admin),
):
    """Read a file from a service project."""
    content = service_manager.get_file(name, filename)
    if content is None:
        raise HTTPException(404, f"File '{filename}' not found in service '{name}'")
    return {"filename": filename, "content": content}


@router.put("/{name}/files/{filename:path}")
def write_service_file(
    name: str,
    filename: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Write or update a file in a service project."""
    content = req.get("content", "")
    ok = service_manager.write_file(name, filename, content)

    log_action(db, action="config_edit", admin_id=current_admin["id"],
               target_service=name, status="success",
               detail={"filename": filename})
    return {"filename": filename, "written": ok}


@router.post("/{name}/files/{filename:path}", status_code=201)
def create_service_file(
    name: str,
    filename: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new file in a service project with .generated marker."""
    content = req.get("content", "")
    created = service_manager.create_file(name, filename, content)

    log_action(db, action="file_create", admin_id=current_admin["id"],
               target_service=name, status="success",
               detail={"filename": filename})
    return {"filename": filename, "created": created}


@router.delete("/{name}/files/{filename:path}")
def delete_service_file(
    name: str,
    filename: str,
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a file from a service project."""
    deleted = service_manager.delete_file(name, filename)
    if not deleted:
        raise HTTPException(404, f"File '{filename}' not found in service '{name}'")

    log_action(db, action="file_delete", admin_id=current_admin["id"],
               target_service=name, status="success",
               detail={"filename": filename})
    return {"filename": filename, "deleted": True}


@router.post("/{name}/convert")
def convert_service_files(
    name: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Convert plain compose/nginx files to Jinja2 templates.

    Accepts optional ``recipe_path`` for multi-recipe projects.
    """
    result = {}
    recipe_path = req.get("recipe_path", "")

    compose_file = req.get("compose_file")
    if compose_file:
        try:
            converted = service_manager.convert_compose(name, compose_file, recipe_path)
            result.update(converted)
        except Exception as e:
            raise HTTPException(422, f"Compose conversion failed: {e}")

    nginx_file = req.get("nginx_file")
    if nginx_file:
        try:
            converted = service_manager.convert_nginx(name, nginx_file)
            result.update(converted)
        except Exception as e:
            raise HTTPException(422, f"Nginx conversion failed: {e}")

    log_action(db, action="config_edit", admin_id=current_admin["id"],
               target_service=name, status="success",
               detail={"converted": list(result.keys())})
    return result


# ------------------------------------------------------------------
# Per-recipe file sets (file-selection-and-generation design §Model)
# ------------------------------------------------------------------


@router.get("/{name}/file-sets")
def get_file_sets(
    name: str,
    current_admin: dict = Depends(require_admin),
):
    """Get stored file sets + live candidates + stale markers per recipe.

    Stored set = dashboard pre-selection ONLY (no selection stored → all
    options unselected). ``candidates`` lists what the panels may select;
    ``stale`` flags stored entries that no longer exist at scan time
    (keep + mark stale — never dropped here).
    """
    from ..services.file_sets import FileSetError, list_file_sets
    try:
        return list_file_sets(name)
    except FileSetError as e:
        raise HTTPException(400, str(e))


@router.put("/{name}/file-sets")
def put_file_sets(
    name: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Persist one recipe's file set (explicit choices only).

    Body: ``{ "recipe_path": "docker", "file_set": {"compose": [...], "nginx": ..., "env": [...], "profiles": [...]} }``
    Empty lists record an explicit empty selection. Paths may reference
    just-generated files that do not exist yet.
    """
    from ..services.file_sets import FileSetError, put_file_set
    try:
        result = put_file_set(name, req.get("recipe_path") or "", req.get("file_set") or {})
    except FileSetError as e:
        raise HTTPException(400, str(e))
    log_action(db, action="file_set_update", admin_id=current_admin["id"],
               target_service=name, status="success",
               detail={"recipe_path": result["recipe_path"]})
    return result


@router.post("/{name}/file-sets/derive")
def derive_file_sets_profiles(
    name: str,
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
):
    """Derive profile candidates from an IN-PANEL compose selection (GAP-2).

    Body: ``{ "recipe_path": "docker", "compose": ["a.yml", "b.yml"] }``.
    Pure derivation — nothing is persisted. The panels call this when the
    compose selection changes so the profiles section follows the compose
    selection dependency (design §Selection & UI L59-62) instead of staying
    stale from the stored file set.
    """
    from ..services.file_sets import FileSetError, derive_profiles
    compose = [p for p in (req.get("compose") or []) if isinstance(p, str)]
    try:
        result = derive_profiles(name, req.get("recipe_path") or "", compose)
    except FileSetError as e:
        raise HTTPException(400, str(e))
    return {
        "service_name": name,
        "recipe_path": req.get("recipe_path") or "",
        "candidates": result,
    }


@router.post("/scan")
def scan_repo(
    req: dict[str, Any],
    current_admin: dict = Depends(require_admin),
):
    """Scan a directory and return RepoContext for LLM generation."""
    directory = req.get("directory", "")
    if not directory:
        raise HTTPException(400, "'directory' is required")

    from pathlib import Path
    ctx = scan_directory(Path(directory))
    return {
        "repo_description": ctx.repo_description,
        "repo_files": ctx.repo_files,
        "port": ctx.port,
        "needs_db": ctx.needs_db,
        "needs_cache": ctx.needs_cache,
        "needs_volume": ctx.needs_volume,
        "language": ctx.language,
        "framework": ctx.framework,
        "has_dockerfile": ctx.has_dockerfile,
        "has_compose": ctx.has_compose,
        "has_nginx_conf": ctx.has_nginx_conf,
    }


# ------------------------------------------------------------------
# Git integration — real git status / git diff for change tracking
# ------------------------------------------------------------------

import subprocess
from pathlib import Path


def _git_command(service_name: str, *args: str) -> str:
    """Run a git command in the service project directory and return stdout."""
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        raise HTTPException(404, f"Service '{service_name}' not found")
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Git command timed out")
    except FileNotFoundError:
        raise HTTPException(500, "git CLI not available")


@router.get("/{name}/git/status")
def git_status(
    name: str,
    current_admin: dict = Depends(require_admin),
):
    """Get git status for a service project (git status --porcelain).

    State files and generated markers are infrastructure bookkeeping, not
    user edits — ``.provision-state*`` and ``*.generated`` entries are
    filtered out of modified/untracked (F9).
    """
    try:
        output = _git_command(name, "status", "--porcelain")
    except HTTPException:
        raise
    lines = [l for l in output.split("\n") if l.strip()]
    # Parse: " M file" or "?? file" format
    modified = []
    untracked = []
    for line in lines:
        if len(line) < 3:
            continue
        status = line[:2].strip()
        filename = line[3:].strip()
        base = filename.rsplit("/", 1)[-1]
        if base.startswith(".provision-state") or filename.endswith(".generated"):
            continue
        if status in ("M", "A", "D", "R"):
            modified.append({"status": status, "file": filename})
        elif status == "??":
            untracked.append({"status": "?", "file": filename})
    return {"modified": modified, "untracked": untracked, "raw": lines}


@router.get("/{name}/git/diff")
def git_diff(
    name: str,
    file: str = Query(None, description="Specific file to diff (relative path)"),
    current_admin: dict = Depends(require_admin),
):
    """Get git diff for a service project (working tree vs HEAD)."""
    try:
        args = ["diff"]
        if file:
            args.extend(["--", file])
        output = _git_command(name, *args)
    except HTTPException:
        raise
    return {"diff": output}


@router.get("/{name}/git/head-file")
def git_head_file(
    name: str,
    file: str = Query(..., description="File path relative to project root"),
    current_admin: dict = Depends(require_admin),
):
    """Get file content from HEAD revision (git show HEAD:file)."""
    try:
        content = _git_command(name, "show", f"HEAD:{file}")
    except HTTPException:
        raise
    return {"content": content, "file": file}

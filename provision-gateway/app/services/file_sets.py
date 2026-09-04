"""Per-recipe file set service — selection persistence + candidate discovery.

The file set (file-selection-and-generation design §Model) records the
dashboard's explicit base-file selection per recipe::

    file_sets: { "<recipe_path>": {
        compose:  [ordered paths],   # multi, ordered
        nginx:    path | null,       # single
        env:      [ordered paths],   # INTERPOLATION env only (.env-class)
        profiles: [str]              # selectable OPTION category
    }}

Stored in ``.provision-state.json`` (schema v2). No selection stored → all
options unselected (no convention prefill). Stale stored entries (file
disappeared / profile no longer present in the selected compose) are flagged
at read time but NOT removed — the stored set is untouched until the operator
re-persists (§Selection & UI, stale-selection keep + mark stale).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import settings
from .project_state import (
    ROOT_RECIPE_KEY,
    STATE_SCHEMA_VERSION,
    now_iso,
    ProjectState,
)

# Per-user deployment files (.env.{user}.{label}) are NOT candidates.
_PER_USER_ENV_RE = re.compile(r"^\.env\.[^./]+\.[0-9]+$")

_FILE_SET_CATEGORIES = ("compose", "nginx", "env", "profiles")


class FileSetError(ValueError):
    """Raised for invalid file-set payloads (router → 400)."""


def _recipe_dir(project_dir: Path, recipe_path: str) -> Path:
    """Resolve a recipe path to a directory (traversal-safe)."""
    if recipe_path in ("", ".", ROOT_RECIPE_KEY):
        return project_dir
    rp = recipe_path.strip().strip("/")
    parts = [p for p in rp.split("/") if p not in ("", ".")]
    if not parts:
        return project_dir
    if any(p == ".." for p in parts):
        raise FileSetError(f"Recipe path must not contain '..': {recipe_path!r}")
    if rp.startswith("/") or "\\" in rp or ":" in rp:
        raise FileSetError(f"Recipe path must be project-root-relative: {recipe_path!r}")
    return project_dir.joinpath(*parts)


# ---------------------------------------------------------------------------
# Candidate discovery (from the recipe dir's shallow scan)
# ---------------------------------------------------------------------------

def _is_compose_candidate(name: str) -> bool:
    if name.endswith(".generated") or name.endswith(".j2"):
        return False
    return (name.startswith("docker-compose") or name.startswith("compose")) and (
        name.endswith(".yml") or name.endswith(".yaml")
    )


def _is_nginx_candidate(name: str) -> bool:
    if name.endswith(".generated") or name.endswith(".j2"):
        return False
    return name.endswith(".conf") and not name.endswith(".j2")


def _is_env_candidate(name: str) -> bool:
    if name.startswith(".provision-state") or name.endswith(".generated"):
        return False
    if _PER_USER_ENV_RE.match(name):
        return False
    return name.startswith(".env")


def _scan_recipe_files(project_dir: Path, recipe_path: str) -> list[str]:
    """Top-level file names of a recipe dir (relative to the recipe dir)."""
    rdir = _recipe_dir(project_dir, recipe_path)
    if not rdir.is_dir():
        return []
    try:
        return [e.name for e in rdir.iterdir() if e.is_file()]
    except OSError:
        return []


def _compose_service_profiles(paths: list[Path]) -> list[str]:
    """Union of non-empty profile names across the selected compose files.

    Parses the raw files with the merged service-name set; activation
    awareness is the LLM's job via its context, not the validator's.
    """
    import yaml

    names: list[str] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            continue
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            for prof in svc.get("profiles") or []:
                if isinstance(prof, str) and prof and prof not in names:
                    names.append(prof)
    return names


def compose_service_names(paths: list[Path]) -> list[str]:
    """Union of service keys across the selected compose files.

    The merged service-name set — which includes profile-gated services —
    is what nginx target validation runs against (design §Implementation
    notes L259-261).
    """
    import yaml

    names: list[str] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        services = data.get("services") if isinstance(data, dict) else None
        if isinstance(services, dict):
            for key in services.keys():
                if isinstance(key, str) and key not in names:
                    names.append(key)
    return names


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _load_state(service_name: str) -> ProjectState | None:
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        return None
    return ProjectState.load(project_dir) or ProjectState(project_dir)


def _recipe_paths(state: ProjectState) -> list[str]:
    """State's recipe dirs, root-only when not configured."""
    if state.recipe_origin == "user":
        return list(state.recipe_paths) or [ROOT_RECIPE_KEY]
    return [ROOT_RECIPE_KEY]


def list_file_sets(service_name: str) -> dict[str, Any]:
    """Return stored file sets + live candidates + stale markers per recipe.

    ``candidates`` holds what the panels may select (existing files / current
    profile names); ``stale`` flags stored entries that no longer exist at
    scan time (keep + mark stale — never dropped here).
    """
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        raise FileSetError(f"Service '{service_name}' not found")

    state = _load_state(service_name)
    if state is None:
        raise FileSetError(f"Service '{service_name}' not found")
    state.schema_version = STATE_SCHEMA_VERSION  # in-memory upgrade of v1 files

    recipe_paths = _recipe_paths(state)
    file_sets = dict(state.file_sets)
    candidates: dict[str, dict[str, Any]] = {}
    stale: dict[str, dict[str, list[str]]] = {}

    for rp in recipe_paths:
        names = _scan_recipe_files(project_dir, rp)
        compose_cands = sorted(n for n in names if _is_compose_candidate(n))
        nginx_cands = sorted(n for n in names if _is_nginx_candidate(n))
        env_cands = sorted(n for n in names if _is_env_candidate(n))

        # Profile candidates derive from the SELECTED compose (the merged set);
        # when nothing is selected the section stays disabled (dependency on
        # the compose selection resolving).
        selected = file_sets.get(rp, {})
        sel_compose = [p for p in selected.get("compose", []) if isinstance(p, str)]
        sel_paths = [_recipe_dir(project_dir, rp) / p for p in sel_compose]
        prof_cands = _compose_service_profiles(sel_paths)

        candidates[rp] = {
            "compose": compose_cands,
            "nginx": nginx_cands,
            "env": env_cands,
            "profiles": prof_cands,
        }

        # Stale detection: stored entry whose file vanished / profile gone.
        entry_stale: dict[str, list[str]] = {}
        if selected:
            gone = [
                p for p in selected.get("compose", [])
                if not (_recipe_dir(project_dir, rp) / p).is_file()
            ]
            if gone:
                entry_stale["compose"] = gone
            sel_nginx = selected.get("nginx")
            if sel_nginx and not (_recipe_dir(project_dir, rp) / sel_nginx).is_file():
                entry_stale["nginx"] = [sel_nginx]
            gone_env = [
                p for p in selected.get("env", [])
                if not (_recipe_dir(project_dir, rp) / p).is_file()
            ]
            if gone_env:
                entry_stale["env"] = gone_env
            if sel_compose and prof_cands is not None:
                stored_profiles = selected.get("profiles", [])
                gone_profs = [p for p in stored_profiles if p not in prof_cands]
                if gone_profs:
                    entry_stale["profiles"] = gone_profs
        if entry_stale:
            stale[rp] = entry_stale

    return {
        "service_name": service_name,
        "file_sets": file_sets,
        "candidates": candidates,
        "stale": stale,
    }


def _validate_paths(project_dir: Path, recipe_path: str, paths: list[str]) -> list[str]:
    """Validate selection paths live inside the recipe dir (traversal-safe)."""
    rdir = _recipe_dir(project_dir, recipe_path)
    rdir_resolved = rdir.resolve()
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise FileSetError("File set paths must be non-empty strings")
        if p.startswith("/") or "\\" in p or ":" in p:
            raise FileSetError(f"Path must be recipe-relative, got: {p!r}")
        parts = [part for part in p.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise FileSetError(f"Path must not contain '..': {p!r}")
        if not parts:
            raise FileSetError(f"Invalid path: {p!r}")
        resolved = rdir.joinpath(*parts).resolve()
        if not str(resolved).startswith(str(rdir_resolved) + "/"):
            raise FileSetError(f"Path escapes the recipe dir: {p!r}")
        out.append("/".join(parts))
    return out


def put_file_set(service_name: str, recipe_path: str, file_set: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist one recipe's file set.

    Paths may reference just-generated files that do not exist on disk yet
    (the save flow persists the selection before/with the write). Empty lists
    record an explicit empty selection (which for env still gets the
    always-pass per-user empty file at deploy time).
    """
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        raise FileSetError(f"Service '{service_name}' not found")
    if not isinstance(file_set, dict):
        raise FileSetError("file_set must be an object")

    recipe_key = recipe_path if recipe_path not in ("", ROOT_RECIPE_KEY) else ROOT_RECIPE_KEY

    # Recipe must be a known recipe dir.
    state = _load_state(service_name)
    if state is None:
        raise FileSetError(f"Service '{service_name}' not found")
    known = set(_recipe_paths(state))
    if recipe_key not in known:
        raise FileSetError(f"Unknown recipe: {recipe_path or '.'}")

    compose = _validate_paths(project_dir, recipe_key, [p for p in (file_set.get("compose") or []) if isinstance(p, str)])
    env = _validate_paths(project_dir, recipe_key, [p for p in (file_set.get("env") or []) if isinstance(p, str)])
    nginx = file_set.get("nginx")
    if nginx is not None:
        if not isinstance(nginx, str) or not nginx.strip():
            raise FileSetError("nginx must be a path string or null")
        nginx = _validate_paths(project_dir, recipe_key, [nginx])[0]
    profiles = [p for p in (file_set.get("profiles") or []) if isinstance(p, str)]

    entry = {
        "compose": compose,
        "nginx": nginx,
        "env": env,
        "profiles": profiles,
    }
    for key in _FILE_SET_CATEGORIES:
        if key not in entry:
            entry[key] = [] if key != "nginx" else None

    from ..services.service_manager import service_manager

    lock = service_manager._project_lock(service_name)
    with lock:
        state = _load_state(service_name) or ProjectState(project_dir)
        state.schema_version = STATE_SCHEMA_VERSION
        state.file_sets[recipe_key] = entry
        state.updated_at = now_iso()
        try:
            state.save()
        except OSError:
            pass

    return {
        "service_name": service_name,
        "recipe_path": recipe_key,
        "file_set": entry,
    }


def derive_profiles(
    service_name: str, recipe_path: str, compose_paths: list[str]
) -> dict[str, list[str]]:
    """Profile candidates derived from an IN-PANEL compose selection (GAP-2).

    Pure derivation — nothing is persisted. ``recipe_path`` and each compose
    path are validated traversal-safe (``_recipe_dir`` + ``_validate_paths``);
    compose files may reference just-generated files that do not exist yet
    (``_compose_service_profiles`` skips missing files). The panels call this
    when the compose selection changes so the profiles section reflects the
    merged compose (design §Selection & UI L59-62) instead of only the stored
    file set.
    """
    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    if not project_dir.is_dir():
        raise FileSetError(f"Service '{service_name}' not found")
    validated = _validate_paths(project_dir, recipe_path, compose_paths)
    paths = [_recipe_dir(project_dir, recipe_path) / p for p in validated]
    return {"profiles": _compose_service_profiles(paths)}

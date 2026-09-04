"""Per-project scan state persistence + directory fingerprinting.

Stores the recipe dirs and cached shallow scans for a service project in a
hidden ``.provision-state.json`` file (like ``.generated`` markers). A recipe
dir is re-scanned only when its top-level fingerprint changed, so listing a
large repo stays fast after the first scan.

Schema v2 adds the per-recipe **file sets** (file-selection-and-generation
design)::

    file_sets: { "<recipe_path>": {
        compose:  [ordered paths],   # multi, ordered
        nginx:    path | null,       # single
        env:      [ordered paths],   # INTERPOLATION env only (.env-class; order matters)
        profiles: [str]              # selectable option category
    }}

v1 files load with empty ``file_sets`` (backward compatible — the bump only
adds the field). Recipe keys use ``"."`` for the project root.

No scan logic here — this module only fingerprints and persists; scanning
lives in ``service_manager`` (avoids circular imports).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_STATE_FILENAME = ".provision-state.json"
STATE_SCHEMA_VERSION = 2
# v1 files are accepted and silently upgraded in-memory (file_sets added).
_ACCEPTED_SCHEMA_VERSIONS = (1, 2)

ROOT_RECIPE_KEY = "."


def compute_dir_fingerprint(dir_path: str | Path) -> dict[str, dict]:
    """Fingerprint the TOP-LEVEL entries of a directory.

    Returns ``{entry_name: {"mtime_ns": int, "size": int, "is_dir": bool}}``
    built with ``os.scandir`` — never the directory's own mtime (a dir mtime
    does not change when a contained file is edited). ``mtime_ns`` captures
    sub-second edits. Missing/unreadable dirs yield ``{}``.
    """
    fingerprint: dict[str, dict] = {}
    try:
        with os.scandir(str(dir_path)) as it:
            for entry in it:
                # The state file itself (and its atomic-write .tmp sibling)
                # must never invalidate the cache — its mtime changes on every
                # save, which would otherwise trigger a rescan each listing.
                if entry.name.startswith(".provision-state"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                fingerprint[entry.name] = {
                    "mtime_ns": st.st_mtime_ns,
                    "size": st.st_size,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                }
    except OSError:
        return {}
    return fingerprint


def fingerprints_equal(a: Any, b: Any) -> bool:
    """True when both values are dicts describing identical directory entries."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return a == b


def now_iso() -> str:
    """UTC ISO-8601 timestamp used for the state's ``updated_at``."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_file_sets(raw: Any) -> dict[str, dict]:
    """Validate + normalize a loaded ``file_sets`` payload.

    Accepts v2 payloads only; anything malformed is dropped per-key so a
    corrupt entry can never crash listing. Each recipe entry keeps the four
    known categories and discards unknown keys.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict] = {}
    for recipe_key, entry in raw.items():
        if not isinstance(recipe_key, str) or not isinstance(entry, dict):
            continue
        compose = [p for p in (entry.get("compose") or []) if isinstance(p, str)]
        nginx = entry.get("nginx")
        if nginx is not None and not isinstance(nginx, str):
            nginx = None
        env = [p for p in (entry.get("env") or []) if isinstance(p, str)]
        profiles = [p for p in (entry.get("profiles") or []) if isinstance(p, str)]
        result[recipe_key] = {
            "compose": compose,
            "nginx": nginx,
            "env": env,
            "profiles": profiles,
        }
    return result


class ProjectState:
    """Serialized per-project scan state.

    Fields:
        schema_version, recipe_origin ("user"|"auto"), recipe_paths: list[str],
        recipes: list[dict], files, generated_files, template_files: list[str],
        dir_scans: dict[str, dict], file_sets: dict[str, dict], updated_at: str
    """

    def __init__(
        self,
        project_dir: str | Path,
        schema_version: int = STATE_SCHEMA_VERSION,
        recipe_origin: str = "auto",
        recipe_paths: list[str] | None = None,
        recipes: list[dict] | None = None,
        files: list[str] | None = None,
        generated_files: list[str] | None = None,
        template_files: list[str] | None = None,
        dir_scans: dict[str, dict] | None = None,
        file_sets: dict[str, dict] | None = None,
        updated_at: str = "",
    ) -> None:
        self.project_dir = Path(project_dir)
        self.schema_version = schema_version
        self.recipe_origin = recipe_origin
        self.recipe_paths = list(recipe_paths or [])
        self.recipes = list(recipes or [])
        self.files = list(files or [])
        self.generated_files = list(generated_files or [])
        self.template_files = list(template_files or [])
        self.dir_scans = dict(dir_scans or {})
        self.file_sets = _normalize_file_sets(file_sets or {})
        self.updated_at = updated_at

    @classmethod
    def load(cls, project_dir: str | Path) -> ProjectState | None:
        """Load the state file for a project; None on missing or corrupt.

        A corrupt file (bad JSON, wrong schema version, non-dict payload) is
        treated as absent so the caller starts fresh. v1 payloads load with
        empty ``file_sets`` (the schema bump only adds the field).
        """
        path = Path(project_dir) / PROJECT_STATE_FILENAME
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema_version") not in _ACCEPTED_SCHEMA_VERSIONS:
            return None
        try:
            state = cls(project_dir)
            state.schema_version = int(data.get("schema_version", STATE_SCHEMA_VERSION))
            state.recipe_origin = str(data.get("recipe_origin", "auto"))
            state.recipe_paths = [str(p) for p in (data.get("recipe_paths") or [])]
            state.recipes = list(data.get("recipes") or [])
            state.files = [str(f) for f in (data.get("files") or [])]
            state.generated_files = [str(f) for f in (data.get("generated_files") or [])]
            state.template_files = [str(f) for f in (data.get("template_files") or [])]
            state.dir_scans = dict(data.get("dir_scans") or {})
            state.file_sets = _normalize_file_sets(data.get("file_sets") or {})
            state.updated_at = str(data.get("updated_at") or "")
        except (TypeError, ValueError):
            return None
        return state

    def save(self) -> None:
        """Atomically persist the state: write a tmp sibling, then os.replace.

        The tmp file (``.provision-state.json.tmp``) starts with
        ``.provision-state`` so the scan/tree exclusion guard covers it too.
        """
        path = self.project_dir / PROJECT_STATE_FILENAME
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "schema_version": self.schema_version,
            "recipe_origin": self.recipe_origin,
            "recipe_paths": self.recipe_paths,
            "recipes": self.recipes,
            "files": self.files,
            "generated_files": self.generated_files,
            "template_files": self.template_files,
            "dir_scans": self.dir_scans,
            "file_sets": self.file_sets,
            "updated_at": self.updated_at,
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)

"""Artifact finalization — the overwrite matrix, server-side (R3, T4).

The generation pipeline produces file *metadata* in the task result and leaves
the content on disk. Finalization applies the platform's overwrite matrix:

* file MISSING -> write as a new file + ``.generated`` marker;
* file exists with a ``.generated`` marker -> re-generate IN PLACE;
* file exists as an ORIGINAL (unmarked) -> originals are NEVER touched; the
  output is written to a fresh marked name and the selection is renamed.

The result also carries the selection mapped through the renames, so the deploy
payload names the files that actually exist. The standalone Generate panel's
explicit Save and the workflow's automatic finalization share this function.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..config import settings
from .file_sets import FileSetError, _recipe_dir, _validate_paths, put_file_set


def _fresh_generated_name(filepath: Path) -> str:
    """Distinct marked name for an original (unmarked) file."""
    return f"{filepath.stem}.generated-{int(time.time())}{filepath.suffix}"


def map_selection(selection: dict | None, rename_map: dict[str, str]) -> dict[str, Any]:
    """Rename every selected path through the finalization map (R3/G21)."""
    if not isinstance(selection, dict):
        return {}
    mapped: dict[str, Any] = {"profiles": selection.get("profiles") or []}
    mapped["compose"] = [rename_map.get(p, p) for p in (selection.get("compose") or [])]
    nginx = selection.get("nginx")
    mapped["nginx"] = rename_map.get(nginx, nginx) if nginx else None
    mapped["env"] = [rename_map.get(p, p) for p in (selection.get("env") or [])]
    sp = selection.get("serving_ports")
    mapped["serving_ports"] = rename_map.get(sp, sp) if sp else None
    # Categories the function does not know about must survive (R2-F9): the
    # stream/mail slots and the deliberate Empty-serving-ports state.
    for key in ("stream_conf", "mail_conf"):
        if selection.get(key):
            mapped[key] = selection[key]
    if selection.get("serving_ports_none"):
        mapped["serving_ports_none"] = True
    return mapped


def finalize_files(service_name: str, recipe_path: str,
                   files: dict[str, str | dict],
                   selection: dict | None = None,
                   *, persist_selection: bool = True
                   ) -> dict[str, Any]:
    """Apply the overwrite matrix and return the saved names + mapped selection."""
    if not service_name or not files:
        raise ValueError("service_name and files required")

    project_dir = settings.SOURCE_PROJECTS_DIR / service_name
    try:
        target = _recipe_dir(project_dir, recipe_path)
    except FileSetError as exc:
        raise ValueError(str(exc)) from exc
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)

    rename_map: dict[str, str] = {}
    saved: list[str] = []
    for filename, content in files.items():
        if not isinstance(filename, str):
            raise ValueError("files must map filename -> content")
        try:
            filename = _validate_paths(project_dir, recipe_path, [filename])[0]
        except FileSetError as exc:
            raise ValueError(str(exc)) from exc
        if isinstance(content, dict):
            # A task result carries {name, sha256, size} metadata only; the
            # content is read from the on-disk generated file it names.
            try:
                content = (target / filename).read_text(encoding="utf-8")
            except OSError as exc:
                raise FileNotFoundError(
                    f"generated file is not on disk: {filename}: {exc}") from exc
        if not isinstance(content, str):
            raise ValueError("files must map filename -> string content")
        filepath = target / filename
        marker = target / f"{filename}.generated"
        if filepath.exists() and not marker.is_file():
            new_name = _fresh_generated_name(filepath)
            (target / new_name).write_text(content)
            (target / f"{new_name}.generated").write_text("")
            saved.append(new_name)
            rename_map[filename] = new_name
        else:
            filepath.write_text(content)
            marker.write_text("")
            saved.append(filename)
            rename_map[filename] = filename

    mapped = map_selection(selection, rename_map) if isinstance(selection, dict) else {}
    if persist_selection and isinstance(selection, dict):
        try:
            put_file_set(service_name, recipe_path, mapped)
        except FileSetError:
            pass  # selection persistence is best-effort — the save still succeeds
    return {"saved": saved, "rename_map": rename_map, "selection": mapped}

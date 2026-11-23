"""The dependency-aware env model (GRILLING G103; 2026-11-19 rev 3).

One ``dependency.<source-service>.<target-service>.json`` per service→service
RELATION, user- and label-agnostic, in the source recipe dir, gateway-written.
The file holds the relation's env map (source variable → target service); it is
reused across labels and owners, and the selected target's label only enters at
deploy, when its serving URL is substituted.

The deploy's per-instance snapshots are separate, per-(user, service, label)
COPIES — ``serving_ports.user-<u>.<l>.yml`` and
``dependency.<source>.<target>.user-<u>.<l>.json`` — so a later edit of the
shared artifact cannot change what the instance records.

This module is the single definition of the names, the relation JSON, and the
serving-URL derivation; the deploy workflow, the generation phase and the fix
analysis all read it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RELATION_RE = re.compile(
    r"^dependency\.(?P<source>[^.]+)\.(?P<target>[^.]+)\.json$")
# R28/R29 — `dependency.<source>.<target>.<fingerprint-id>.json`.
RELATION_VARIANT_RE = re.compile(
    r"^dependency\.(?P<source>[^.]+)\.(?P<target>[^.]+)\.(?P<fpid>[0-9a-f]{16})\.json$")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def relation_filename(source_service: str, target_service: str) -> str:
    """The legacy (rev 1) relation name; snapshots keep this shape."""
    return f"dependency.{source_service}.{target_service}.json"


def relation_variant_filename(source_service: str, target_service: str,
                              fingerprint: dict | None) -> str:
    from .env_classification import fingerprint_id
    return (f"dependency.{source_service}.{target_service}."
            f"{fingerprint_id(fingerprint)}.json")


def relation_snapshot_filename(source_service: str, target_service: str,
                               user: str, label: str) -> str:
    return (f"dependency.{source_service}.{target_service}"
            f".user-{user}.{label}.json")


def serving_ports_snapshot_name(user: str, label: str) -> str:
    return f"serving_ports.user-{user}.{label}.yml"


def serving_url(entry: dict) -> str:
    """The target's serving URL, exactly as the env fill uses it.

    The served name (the ``--<sub>`` endpoint) is preferred over the instance
    hostname; the scheme and the external port follow the target's TLS flag.
    """
    hosts = entry.get("http_endpoints") or []
    host = (hosts[0] if hosts else entry.get("hostname")) or ""
    scheme = "https" if entry.get("tls") else "http"
    # The gateway's settings are the same ports the edge serves on.
    from ..config import settings
    port = settings.NGINX_HTTPS_PORT if entry.get("tls") else settings.NGINX_HTTP_PORT
    std = (scheme == "http" and int(port) == 80) or (
        scheme == "https" and int(port) == 443)
    return f"{scheme}://{host}" if std else f"{scheme}://{host}:{port}"


def target_identity(entry: dict) -> dict:
    return {
        "key": f"{entry.get('user_name')}/{entry.get('service_name')}/"
               f"{entry.get('label')}",
        "user": entry.get("user_name"),
        "service": entry.get("service_name"),
        "label": str(entry.get("label")),
        "hostname": (entry.get("http_endpoints") or [entry.get("hostname") or ""])[0],
        "serving_url": serving_url(entry),
    }


def entries_for_ids(ids: list[str]) -> list[dict]:
    """Resolve SELECTED dependency ids to registry entries.

    The deploy intent's ids are the targets themselves — NOT a source whose
    grants are read. Used at intake / generation / snapshot time, before the
    grant rows exist.
    """
    from .registry import Registry
    reg = Registry()
    out: list[dict] = []
    for sid in ids:
        try:
            entry = reg.get_by_id(str(sid))
        except Exception:  # noqa: BLE001
            entry = None
        if entry and service_grants_live(entry):
            out.append(entry)
    return out


def service_grants_live(entry: dict) -> bool:
    """A tombstoned entry is not a deployable dependency."""
    return not (entry.get("service_deleted_at") or entry.get("deleted_at"))


def dependency_entries(service_id: str) -> list[dict]:
    """The deployed relations of *service_id*: its explicit grant targets.

    Read from the gateway's own grant rows + the read-only registry view; no
    live-state query, no -api call. A missing/stale target is skipped.
    """
    from ..database import SessionLocal
    from . import service_grants
    from .registry import Registry

    db = SessionLocal()
    try:
        rows = service_grants.explicit_rows(db, service_id)
        ids = [str(r.target_service_id) for r in rows]
    except Exception:  # noqa: BLE001 — context is best-effort
        ids = []
    finally:
        db.close()
    reg = Registry()
    out: list[dict] = []
    for sid in ids:
        try:
            entry = reg.get_by_id(sid)
        except Exception:  # noqa: BLE001
            entry = None
        if entry and service_grants.live(entry):
            out.append(entry)
    return out


def _read_relation_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_relation(recipe_dir: str | Path, source_service: str,
                  target_service: str) -> dict | None:
    """The legacy relation file's parsed content, or None.

    Kept for rev-1 readers (snapshots, old sessions). A rev 7 caller reads a
    named fingerprint variant with `read_relation_variant`.
    """
    return _read_relation_file(
        Path(recipe_dir) / relation_filename(source_service, target_service))


def read_relation_variant(recipe_dir: str | Path, source_service: str,
                          target_service: str, name: str) -> dict | None:
    """One named relation variant (a bare file name), parsed or None."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    return _read_relation_file(Path(recipe_dir) / name)


def list_relation_variants(recipe_dir: str | Path, source_service: str,
                           target_service: str) -> list[dict]:
    """Every relation file for this source→target pair, legacy included."""
    rdir = Path(recipe_dir)
    try:
        names = sorted(e.name for e in rdir.iterdir() if e.is_file())
    except OSError:
        return []
    out: list[dict] = []
    legacy = relation_filename(source_service, target_service)
    for name in names:
        if name == legacy:
            out.append({"name": name, "fingerprint_id": "", "legacy": True})
            continue
        match = RELATION_VARIANT_RE.match(name)
        if match and match.group("source") == source_service \
                and match.group("target") == target_service:
            out.append({"name": name, "fingerprint_id": match.group("fpid"),
                        "legacy": False})
    return out


def matched_relation_variant(recipe_dir: str | Path, source_service: str,
                             target_service: str,
                             fingerprint: dict | None) -> tuple[str, dict] | None:
    """The relation variant whose stored fingerprint matches, or None."""
    if not isinstance(fingerprint, dict):
        return None
    expected = relation_variant_filename(source_service, target_service, fingerprint)
    for item in list_relation_variants(recipe_dir, source_service, target_service):
        data = read_relation_variant(recipe_dir, source_service, target_service,
                                     item["name"])
        if data is None or not relation_is_current(data, fingerprint):
            continue
        if item["legacy"] or item["name"] == expected:
            return item["name"], data
    return None


def relation_projection_errors(data: dict | None,
                               expected: list[str]) -> tuple[list[str], list[str]]:
    """The 1:1 check (R28): ``(missing, extra)`` for one target.

    ``expected`` is the classification's projection for the target. A relation
    variable outside the project, or a projected variable the relation omits,
    is red.
    """
    present = set(relation_env(data))
    wanted = set(expected or [])
    return sorted(wanted - present), sorted(present - wanted)


def relation_env(data: dict | None) -> dict[str, str]:
    """ ``{variable: target-service}`` from a relation's ``env`` map.

    Both the decision-4 object form (``{"depends_on": target}``) and a plain
    string target are accepted; anything else is ignored.
    """
    out: dict[str, str] = {}
    env = (data or {}).get("env")
    if not isinstance(env, dict):
        return out
    for var, value in env.items():
        if not isinstance(var, str) or not var:
            continue
        if isinstance(value, dict):
            target = value.get("depends_on")
        else:
            target = value
        if isinstance(target, str) and target:
            out[var] = target
    return out


def write_relation(recipe_dir: str | Path, source_service: str,
                   target_service: str, env_map: dict[str, str],
                   fingerprint: dict | None = None) -> Path:
    """Write the current fingerprint's relation variant (+ marker) mechanically.

    Only the matched variant is overwritten — never another fingerprint's file
    (R29). The caller owns the selection decision: `generate new`, a red
    variant's regeneration, or a missing 1:1 relation all land here.
    """
    path = Path(recipe_dir) / relation_variant_filename(
        source_service, target_service, fingerprint)
    payload = {
        "version": 1,
        "source_service": source_service,
        "target_service": target_service,
        "fingerprint": fingerprint or {},
        "env": {var: {"depends_on": target_service} for var in sorted(env_map)},
        "generated_at": _stamp(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    Path(str(path) + ".generated").write_text("")
    return path


def relation_is_current(data: dict | None, fingerprint: dict | None) -> bool:
    """True when the relation's recorded fingerprint matches the current one."""
    if not isinstance(data, dict) or not isinstance(fingerprint, dict):
        return False
    recorded = data.get("fingerprint")
    if not isinstance(recorded, dict):
        return False
    for key in ("compose", "profiles", "env_bases"):
        if list(recorded.get(key) or []) != list(fingerprint.get(key) or []):
            return False
    return True


def duplicate_target_services(entries: list[dict]) -> bool:
    """True when two selected dependencies are the SAME service name.

    The duplicate fallback removes the dependencies from the env context; the
    env pickers/ref behave normally and the dependencies are grants only.
    """
    names = [str(e.get("service_name") or "") for e in entries]
    return len(names) != len(set(names))


def copy_snapshot(src: Path, dest: Path) -> Path:
    """Copy a used artifact to its per-instance snapshot name (+ marker)."""
    dest.write_text(src.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8")
    Path(str(dest) + ".generated").write_text("")
    return dest


def snapshot_used_artifacts(recipe_dir: str | Path, *, source_service: str,
                            user: str, label: str,
                            serving_ports: str | None,
                            target_services: list[str],
                            include_classification: bool = False,
                            classification_used: bool = False,
                            relation_variants: dict[str, str] | None = None,
                            classification_name: str | None = None) -> dict:
    """Snapshot what the env build actually used or wrote.

    Serving ports keep their independent rule (copied whenever the selected or
    generated artifact is sent). A dependency relation is copied only when the
    env build read or wrote it — the used set, never "every selected target"
    (C12). ``relation_variants`` names the exact selected/written variant per
    target; without it the legacy file name is tried (rev-1 sessions). The
    classification is copied iff the artefact was read or written, from the
    named variant it used.
    """
    rdir = Path(recipe_dir)
    out: dict[str, Any] = {"serving_ports": None, "dependencies": [],
                           "env_classification": None}
    if serving_ports:
        src = rdir / serving_ports
        if src.is_file():
            name = serving_ports_snapshot_name(user, label)
            copy_snapshot(src, rdir / name)
            out["serving_ports"] = name
    variants = relation_variants or {}
    for target in target_services:
        src_name = variants.get(target) or relation_filename(source_service, target)
        src = rdir / str(src_name)
        if not src.is_file():
            continue
        name = relation_snapshot_filename(source_service, target, user, label)
        copy_snapshot(src, rdir / name)
        out["dependencies"].append({"target_service": target, "path": name})
    if include_classification or classification_used:
        from .env_classification import snapshot_classification
        out["env_classification"] = snapshot_classification(
            rdir, user=user, label=label, source_name=classification_name)
    return out

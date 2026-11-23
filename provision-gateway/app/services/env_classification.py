"""The env classification artefact: schema, fingerprint, injector and check.

This module is the single definition of the classification contract (the six
categories and the source kinds), the structural fingerprint, the artefact
``env.classification.json`` and its per-instance snapshot, the template renderer
and the mechanical post-assembly category check.

Nothing here calls an LLM and nothing here writes a base file: the injector is
pure data-in/data-out, and the only files it writes are the artefact, its
marker and the per-instance snapshot copy.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets as _secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Frozen contract (T0)
# ---------------------------------------------------------------------------

ARTIFACT_NAME = "env.classification.json"
MARKER_SUFFIX = ".generated"

CATEGORIES = (
    "required", "external", "secret", "deploy_value", "dependency", "defaulted",
)
SOURCE_KINDS = (
    "file", "compose_default", "metadata_key", "dependency_target",
    "operator", "minted",
)
TOOL_ALLOWLIST = ("web_fetch", "read_file", "list_files")
TOOL_ROUNDS_MAX = 6
KNOWN_METADATA_KEYS = (
    "service_name", "user_name", "label", "domain", "hostname", "scheme",
    "http_port", "https_port", "serving_url",
)


class ClassificationError(ValueError):
    """The model's classification does not satisfy the frozen contract."""


class MissingMetadataKey(ValueError):
    """A ``deploy_value`` template names a metadata key that does not exist."""

    def __init__(self, key: str):
        super().__init__(f"unknown metadata key {key!r}")
        self.key = key


class MechanicalCheckError(ValueError):
    """One category is unsatisfied after assembly.

    ``missing`` are values that must fail the deploy; ``externals`` are values
    the operator must supply through the pause.
    """

    def __init__(self, missing: list[str], externals: list[str] | None = None):
        self.missing = sorted(missing)
        self.externals = sorted(externals or [])
        detail = []
        if self.missing:
            detail.append("missing value(s): " + ", ".join(self.missing))
        if self.externals:
            detail.append("operator-supplied credential(s): " + ", ".join(self.externals))
        super().__init__("; ".join(detail) or "classification check failed")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fingerprint (R17)
# ---------------------------------------------------------------------------

def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "missing:" + path.name


def structural_fingerprint(
    recipe_dir: str | Path,
    *,
    compose_names: list[str],
    profiles: list[str],
    env_base_names: list[str],
) -> dict[str, Any]:
    """The classification's structural inputs.

    Hashes only — the file order within a list is irrelevant, so the same
    inputs in a different selection order give the same fingerprint. Profiles
    are sorted and de-duplicated; an empty list fingerprints as ``[]``.
    """
    rdir = Path(recipe_dir)
    return {
        "compose": sorted({_sha256_file(rdir / n) for n in compose_names if n}),
        "profiles": sorted({str(p) for p in profiles if p}),
        "env_bases": sorted({_sha256_file(rdir / n) for n in env_base_names if n}),
    }


def fingerprint_matches(left: dict | None, right: dict | None) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("compose", "profiles", "env_bases"):
        if list(left.get(key) or []) != list(right.get(key) or []):
            return False
    return True


# ---------------------------------------------------------------------------
# Env template / reference files (R7, R12, R15)
# ---------------------------------------------------------------------------

def env_file_order(names: list[str]) -> list[str]:
    """Deterministic template merge order: base files first, ``.fix`` last."""
    return sorted(
        (n for n in names if isinstance(n, str) and n),
        key=lambda n: (n.endswith(".fix"), n),
    )


def implicit_env_names(recipe_dir: str | Path) -> list[str]:
    """The recipe's own env candidates, i.e. the implicit template/references.

    Per-user and generated files are never template material; the candidate
    predicate (``file_sets._is_env_candidate``) is the one definition of what
    an env file is.
    """
    from .file_sets import _is_env_candidate

    rdir = Path(recipe_dir)
    if not rdir.is_dir():
        return []
    try:
        names = [e.name for e in rdir.iterdir() if e.is_file()]
    except OSError:
        return []
    return env_file_order([n for n in names if _is_env_candidate(n)])


def read_env_template(recipe_dir: str | Path, names: list[str]) -> str:
    """The merged template text, comments and order preserved.

    Files are concatenated in the given order; a later file wins at parse time,
    exactly as the platform's ordered env lists already behave. Missing files
    are skipped here — the intake refuses an explicit base ref by name before a
    job starts.
    """
    rdir = Path(recipe_dir)
    parts: list[str] = []
    seen: set[str] = set()
    for name in names:
        path = rdir / name
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Two byte-identical template files (`.env` and `.env.example` are
        # often the same file, measured on graphiti-mcp) contribute nothing
        # new; concatenating them duplicated every variable and left the
        # earlier duplicate EMPTY after an external was filled (later-wins
        # edits the last occurrence). One copy per distinct content.
        digest = _sha256_text(raw)
        if digest in seen:
            continue
        seen.add(digest)
        if parts and not parts[-1].endswith("\n"):
            parts[-1] += "\n"
        parts.append(raw if raw.endswith("\n") else raw + "\n")
    return "".join(parts)


def is_empty_value(value: Any) -> bool:
    """An absent value, an empty string, or its quoted spelling is unset."""
    return str(value if value is not None else "").strip() in ("", '""', "''")


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE pairs, later wins (comments/blank lines ignored)."""
    vals: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        vals[key.strip()] = value
    return vals


def apply_env_values(text: str, values: dict[str, str]) -> str:
    """Change only the named keys, keeping every other byte and the order.

    The LAST occurrence of a key is the effective one (later wins); a key the
    template lacks is appended at the end. Values are single-line by contract.
    """
    lines = (text or "").splitlines()
    for key, raw_value in (values or {}).items():
        key = str(key).strip()
        if not key:
            continue
        value = str(raw_value if raw_value is not None else "").replace("\r", "").replace("\n", "")
        target = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.partition("=")[0].strip() == key:
                target = idx
        if target is None:
            lines.append(f"{key}={value}")
        else:
            indent = lines[target][: len(lines[target]) - len(lines[target].lstrip())]
            lines[target] = f"{indent}{key}={value}"
    return "\n".join(lines) + ("\n" if lines else "")


def apply_compose_defaults(
    text: str, defaults: dict[str, str], required: list[str] | None = None
) -> str:
    """Fill an empty compose-referenced variable with its compose default.

    An empty value counts as unset (``:-`` semantics); a non-empty value is
    never overwritten. Required variables with no default are appended empty
    only by the caller's check — this helper writes defaults alone.
    """
    present = parse_env_text(text)
    fill: dict[str, str] = {}
    for name, default in (defaults or {}).items():
        if is_empty_value(present.get(name)):
            fill[name] = default
    return apply_env_values(text, fill)


# ---------------------------------------------------------------------------
# Classification JSON (R11, R13)
# ---------------------------------------------------------------------------

def normalize_classification(raw: Any) -> dict[str, Any]:
    """Validate and normalize turn 1's answer.

    Accepted shape::

        {"variables": {"VAR": {"category": ..., "source": {"kind": ...},
                               "template": ..., "reason": ...}},
         "dependencies": {"target-service": ["VAR", ...]}}

    A flat ``{VAR: {category: ...}}`` map is also accepted. An unknown category
    or source kind is a hard error: a partial artefact must never be written.
    """
    if not isinstance(raw, dict):
        raise ClassificationError("the classification is not a JSON object")
    variables_raw = raw.get("variables")
    if not isinstance(variables_raw, dict):
        variables_raw = {
            str(k): v for k, v in raw.items()
            if isinstance(v, dict) and "category" in v
        }
    if not variables_raw:
        raise ClassificationError("the classification carries no variables")
    variables: dict[str, Any] = {}
    for name, spec in variables_raw.items():
        name = str(name).strip()
        if not name:
            continue
        if not isinstance(spec, dict):
            raise ClassificationError(f"{name}: the entry is not an object")
        category = str(spec.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            raise ClassificationError(
                f"{name}: unknown category {category!r} (expected one of "
                + ", ".join(CATEGORIES) + ")")
        source = spec.get("source")
        if isinstance(source, str):
            source = {"kind": source}
        if not isinstance(source, dict):
            source = {}
        kind = str(source.get("kind") or "").strip().lower()
        if not kind:
            # Tolerant default for a model that answered the category but not
            # the source: the category determines the only kind that can be
            # authoritative. A PRESENT unknown kind is still a hard error.
            kind = {
                "required": "file",
                "external": "operator",
                "secret": "minted",
                "deploy_value": "metadata_key",
                "dependency": "dependency_target",
                "defaulted": "compose_default",
            }[category]
            source["kind"] = kind
        if kind not in SOURCE_KINDS:
            raise ClassificationError(
                f"{name}: unknown source kind {kind!r} (expected one of "
                + ", ".join(SOURCE_KINDS) + ")")
        entry: dict[str, Any] = {
            "category": category,
            "source": dict(source),
            "reason": str(spec.get("reason") or "").strip(),
        }
        template = spec.get("template")
        if template is not None:
            entry["template"] = str(template)
        value = spec.get("value")
        if value is not None:
            entry["value"] = str(value)
        variables[name] = entry
    deps = raw.get("dependencies")
    dependencies = deps if isinstance(deps, dict) else {}
    # The dependency map is a RECORD for filling relation files; derive any
    # missing entry from the variable's own source.
    for name, entry in variables.items():
        source = entry.get("source") or {}
        if entry["category"] != "dependency":
            continue
        target = source.get("target") or source.get("service") or source.get("key")
        if not isinstance(target, str) or not target:
            continue
        service = target.split("/")[1] if target.count("/") == 2 else target
        dependencies.setdefault(service, [])
        if isinstance(dependencies[service], list) and name not in dependencies[service]:
            dependencies[service].append(name)
    return {"variables": variables, "dependencies": {
        str(k): sorted({str(v) for v in (vals or [])})
        for k, vals in dependencies.items() if isinstance(vals, list)
    }}


def validate_values(raw: Any, variables: dict[str, Any]) -> dict[str, Any]:
    """Normalize turn 2's answer: ``{VAR: {value, source}}`` (null allowed).

    Every classified variable must survive the normalization — a ``defaulted``
    variable is never dropped — and a value may be null only for ``external``,
    ``secret`` and ``dependency`` (the gateway fills those).
    """
    if not isinstance(raw, dict):
        raise ClassificationError("the value map is not a JSON object")
    out: dict[str, Any] = {}
    for name in variables:
        spec = raw.get(name)
        if isinstance(spec, dict):
            value = spec.get("value")
            source = spec.get("source")
            if not isinstance(source, (dict, str)):
                source = None
            out[name] = {"value": value, "source": source}
        elif spec is None:
            out[name] = {"value": None, "source": None}
        else:
            out[name] = {"value": spec, "source": None}
    return out


def dependencies_map(variables: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, entry in variables.items():
        if entry.get("category") != "dependency":
            continue
        source = entry.get("source") or {}
        target = source.get("target") or source.get("service") or source.get("key")
        if not isinstance(target, str) or not target:
            continue
        service = target.split("/")[1] if target.count("/") == 2 else target
        out.setdefault(service, []).append(name)
    return {k: sorted(set(v)) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Artefact (R16)
# ---------------------------------------------------------------------------

def artifact_path(recipe_dir: str | Path) -> Path:
    return Path(recipe_dir) / ARTIFACT_NAME


def read_artifact(recipe_dir: str | Path) -> dict | None:
    """The artefact, or None when absent/unreadable/incomplete."""
    path = artifact_path(recipe_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if not isinstance(data.get("variables"), dict):
        return None
    if not isinstance(data.get("fingerprint"), dict):
        return None
    return data


def write_artifact(
    recipe_dir: str | Path,
    *,
    source_service: str,
    recipe_path: str,
    fingerprint: dict[str, Any],
    variables: dict[str, Any],
    dependencies: dict[str, list[str]] | None = None,
) -> Path:
    path = artifact_path(recipe_dir)
    payload = {
        "version": 1,
        "source_service": source_service,
        "recipe_path": recipe_path or "",
        "fingerprint": fingerprint,
        "variables": variables,
        "dependencies": dependencies if dependencies is not None
        else dependencies_map(variables),
        "generated_at": _stamp(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    Path(str(path) + MARKER_SUFFIX).write_text("", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Renderer, injector and the mechanical check (R14, R18, R21)
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def render_template(template: str, metadata: dict[str, Any]) -> str:
    """Render ``{{ key }}`` from the deployment metadata.

    A template that names a key the metadata does not carry is a hard error:
    the deploy blocks and names the variable (the caller adds the variable).
    """
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in metadata or metadata.get(key) is None:
            raise MissingMetadataKey(key)
        return str(metadata.get(key))

    return _TEMPLATE_RE.sub(_sub, str(template or ""))


def inject_values(
    artifact: dict[str, Any],
    *,
    base_values: dict[str, str],
    metadata: dict[str, Any],
    relation_values: dict[str, str],
    external_values: dict[str, str] | None = None,
    mint: bool = True,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Compute the values the assembler applies, category by category.

    Returns ``(values_to_apply, missing, externals)``. ``missing`` fails the
    deploy; ``externals`` are routed to the operator pause. No file is written
    and no LLM is called.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    externals: list[str] = []
    supplied = external_values or {}
    for name, entry in (artifact.get("variables") or {}).items():
        category = entry.get("category")
        current = str(base_values.get(name, "") or "")
        if category == "deploy_value":
            template = entry.get("template")
            if not template:
                missing.append(name)
                continue
            try:
                rendered = render_template(template, metadata)
            except MissingMetadataKey:
                missing.append(name)
                continue
            values[name] = rendered
        elif category == "dependency":
            target_url = relation_values.get(name)
            if not str(target_url or "").strip():
                missing.append(name)
            else:
                values[name] = str(target_url)
        elif category == "external":
            val = str(supplied.get(name, "") or "")
            if not val and is_empty_value(current):
                externals.append(name)
            elif val:
                values[name] = val
        elif category == "secret":
            if not is_empty_value(current):
                values[name] = current
            elif mint:
                values[name] = _secrets.token_urlsafe(18)
            else:
                missing.append(name)
        else:  # required / defaulted
            val = ("" if is_empty_value(current) else current.strip()) or str(
                entry.get("value") or "").strip()
            if not val:
                missing.append(name)
            elif val != current:
                values[name] = val
    return values, sorted(missing), sorted(externals)


def check_assembled(
    artifact: dict[str, Any],
    assembled: dict[str, str],
    *,
    relation_values: dict[str, str],
    external_values: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """The one post-assembly category check (C9).

    Every category is enforced mechanically over the classification, the
    assembled env and the current relation files. ``dependency`` is satisfied
    only by a mapping in a current relation file — never by a stale base value.
    An unsupplied ``external`` routes to the pause; everything else is missing.
    """
    missing: list[str] = []
    externals: list[str] = []
    supplied = external_values or {}
    for name, entry in (artifact.get("variables") or {}).items():
        category = entry.get("category")
        value = str(assembled.get(name, "") or "").strip()
        if category == "external":
            if is_empty_value(supplied.get(name)) and is_empty_value(value):
                externals.append(name)
            continue
        if category == "dependency":
            if is_empty_value(relation_values.get(name)):
                missing.append(name)
            elif is_empty_value(value):
                missing.append(name)
            continue
        if is_empty_value(value):
            missing.append(name)
    return sorted(missing), sorted(externals)


# ---------------------------------------------------------------------------
# Per-instance snapshot (R21, R22)
# ---------------------------------------------------------------------------

def classification_snapshot_name(user: str, label: str) -> str:
    return f"env.classification.user-{user}.{label}.json"


def snapshot_classification(
    recipe_dir: str | Path, *, user: str, label: str
) -> str | None:
    """Copy the artefact to its per-instance snapshot (+ marker).

    Returns the snapshot name, or None when there is no artefact to copy. A
    write failure propagates to the caller, which logs it and never blocks the
    deploy (the existing snapshot contract).
    """
    src = artifact_path(recipe_dir)
    if not src.is_file():
        return None
    name = classification_snapshot_name(user, str(label))
    dest = Path(recipe_dir) / name
    dest.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    Path(str(dest) + MARKER_SUFFIX).write_text("", encoding="utf-8")
    return name


def snapshot_exists(recipe_dir: str | Path, *, user: str, label: str) -> bool:
    return (Path(recipe_dir) / classification_snapshot_name(user, str(label))).is_file()

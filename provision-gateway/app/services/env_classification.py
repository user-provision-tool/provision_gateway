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

ARTIFACT_NAME = "env.classification.json"  # legacy, read but never written
ARTIFACT_SUFFIX = ".yaml"
MARKER_SUFFIX = ".generated"
ARTIFACT_PREFIX = "env.classification."
_VARIANT_RE = re.compile(r"^env\.classification\.([0-9a-f]{16})\.yaml$")

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


def _canonical_fingerprint(fingerprint: dict | None) -> dict[str, list[str]]:
    return {
        key: sorted({str(x) for x in ((fingerprint or {}).get(key) or [])})
        for key in ("compose", "profiles", "env_bases")
    }


def fingerprint_id(fingerprint: dict | None) -> str:
    """The short, stable variant id (R29).

    Only the structural fingerprint enters it — no classifier, prompt or model
    version — so reverting the structural inputs re-exposes the same variant.
    """
    canonical = json.dumps(_canonical_fingerprint(fingerprint), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def variant_filename(fingerprint: dict | None) -> str:
    return f"{ARTIFACT_PREFIX}{fingerprint_id(fingerprint)}{ARTIFACT_SUFFIX}"


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
    """The legacy JSON artefact's path (read for back-compat, never written)."""
    return Path(recipe_dir) / ARTIFACT_NAME


def variant_path(recipe_dir: str | Path, fingerprint: dict | None) -> Path:
    return Path(recipe_dir) / variant_filename(fingerprint)


def _load_yaml(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _basic_artifact(data: Any) -> dict | None:
    """A parseable artefact with the required top-level shape, or None."""
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if not isinstance(data.get("variables"), dict):
        return None
    if not isinstance(data.get("fingerprint"), dict):
        return None
    return data


def read_artifact_file(path: str | Path) -> dict | None:
    """One YAML variant (or the legacy JSON artefact), parsed and shape-checked."""
    p = Path(path)
    if not p.is_file():
        return None
    if p.suffix == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
    else:
        data = _load_yaml(p)
    return _basic_artifact(data)


def read_artifact(recipe_dir: str | Path) -> dict | None:
    """The legacy JSON artefact, or None — kept for back-compat readers."""
    return read_artifact_file(artifact_path(recipe_dir))


def read_variant(recipe_dir: str | Path, name: str) -> dict | None:
    """One named variant from the recipe dir (a bare file name)."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    return read_artifact_file(Path(recipe_dir) / name)


def read_variant_any(recipe_dir: str | Path, name: str) -> dict | None:
    """One variant as raw data, WITHOUT the version/shape gate.

    The panel lists a schema-invalid variant as RED (selectable for inspection
    and editing) rather than hiding it; the engine still uses the strict
    reader, so a red variant is never injected (R26/R29).
    """
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    path = Path(recipe_dir) / name
    if not path.is_file():
        return None
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
    else:
        data = _load_yaml(path)
    return data if isinstance(data, dict) else None


def list_variants(recipe_dir: str | Path) -> list[dict[str, Any]]:
    """Every classification artefact in the recipe, legacy included.

    Returns ``[{name, fingerprint_id, legacy}]`` sorted by name. A custom
    fingerprint id that does not match the file's own fingerprint is still
    listed; the caller compares the stored fingerprint before offering it.
    """
    rdir = Path(recipe_dir)
    out: list[dict[str, Any]] = []
    try:
        names = sorted(e.name for e in rdir.iterdir() if e.is_file())
    except OSError:
        return out
    for name in names:
        if name == ARTIFACT_NAME:
            out.append({"name": name, "fingerprint_id": "", "legacy": True})
            continue
        match = _VARIANT_RE.match(name)
        if match:
            out.append({"name": name, "fingerprint_id": match.group(1),
                        "legacy": False})
    return out


def matched_variant(recipe_dir: str | Path,
                    fingerprint: dict | None) -> tuple[str, dict] | None:
    """The variant whose stored fingerprint matches, or None.

    A variant is a candidate only when BOTH its file name's id and its stored
    fingerprint match the current structural inputs. The legacy JSON artefact
    is read when its fingerprint matches (it is never overwritten).
    """
    if not isinstance(fingerprint, dict):
        return None
    expected = variant_filename(fingerprint)
    matches: list[tuple[dict, dict]] = []
    for item in list_variants(recipe_dir):
        data = read_variant(recipe_dir, item["name"])
        if data is None:
            continue
        if not fingerprint_matches(data.get("fingerprint"), fingerprint):
            continue
        if item["legacy"] or item["name"] == expected:
            matches.append((item, data))
    # M-A22 — a legacy JSON and the canonical YAML can both match; the YAML is
    # the canonical variant and the duplicate is ignored.
    for item, data in matches:
        if not item["legacy"]:
            return item["name"], data
    if matches:
        return matches[0][0]["name"], matches[0][1]
    return None


def artifact_errors(data: dict | None) -> list[str]:
    """The schema check that makes a variant RED (R26/R29).

    A red variant is still selectable for inspection and editing; it is never
    injected. Returning the errors (rather than None) is what lets the panel
    name what is wrong.
    """
    if not isinstance(data, dict):
        return ["the artefact is unreadable or not an object"]
    errors: list[str] = []
    if data.get("version") != 1:
        errors.append("version must be 1")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, dict):
        errors.append("fingerprint is missing")
    else:
        for key in ("compose", "profiles", "env_bases"):
            if not isinstance(fingerprint.get(key), list):
                errors.append(f"fingerprint.{key} must be a list")
    variables = data.get("variables")
    if not isinstance(variables, dict):
        return errors + ["variables must be a map keyed by env var"]
    if not variables:
        errors.append("variables is empty")
    for name, entry in variables.items():
        if not isinstance(name, str) or not name.strip():
            errors.append("a variable name is empty")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{name}: the entry is not an object")
            continue
        category = str(entry.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            errors.append(f"{name}: unknown category {category!r}")
        source = entry.get("source")
        if isinstance(source, str):
            source = {"kind": source}
        if not isinstance(source, dict):
            source = {}
        kind = str(source.get("kind") or "").strip().lower()
        if kind and kind not in SOURCE_KINDS:
            errors.append(f"{name}: unknown source kind {kind!r}")
        if category == "dependency" and not dependency_target(entry):
            errors.append(f"{name}: a dependency variable has no target")
    return errors


def coverage_missing(data: dict | None, env_keys: list[str] | None) -> list[str]:
    """Env keys the classification does not name (R26).

    Extra classification keys are allowed; a missing env key is red.
    """
    variables = (data or {}).get("variables")
    if not isinstance(variables, dict):
        return sorted({str(k) for k in (env_keys or []) if k})
    return sorted({str(k) for k in (env_keys or []) if k and str(k) not in variables})


def target_allowlist_violations(data: dict | None,
                                targets: list[str] | None) -> list[dict[str, str]]:
    """Dependency variables pointing at a target that is not selected (R26)."""
    allowed = {str(t) for t in (targets or []) if t}
    out: list[dict[str, str]] = []
    for name, entry in ((data or {}).get("variables") or {}).items():
        if not isinstance(entry, dict) or entry.get("category") != "dependency":
            continue
        target = dependency_target(entry)
        if target and target not in allowed:
            out.append({"variable": str(name), "target": target})
    return sorted(out, key=lambda v: v["variable"])


def dependency_target(entry: dict | None) -> str:
    """The target service named by a dependency variable's source."""
    source = (entry or {}).get("source")
    if isinstance(source, str):
        source = {"kind": source}
    if not isinstance(source, dict):
        return ""
    target = source.get("target") or source.get("service") or source.get("key")
    if not isinstance(target, str) or not target:
        return ""
    return target.split("/")[1] if target.count("/") == 2 else target


def projection(data: dict | None, target: str) -> list[str]:
    """``{var | cls.variables[var].category == dependency and target == T}``."""
    return sorted(
        str(name) for name, entry in ((data or {}).get("variables") or {}).items()
        if isinstance(entry, dict) and entry.get("category") == "dependency"
        and dependency_target(entry) == target
    )


def internal_target_violations(
    data: dict | None, internal_services: list[str] | set[str] | None,
) -> list[dict[str, str]]:
    """Dependency entries whose target is a service defined in the SAME compose.

    C22 — a container inside the service is an internal reference, never a
    dependency, even when a deployed dependency shares its name.
    """
    internal = {str(s) for s in (internal_services or []) if s}
    out: list[dict[str, str]] = []
    for name, entry in ((data or {}).get("variables") or {}).items():
        if not isinstance(entry, dict) or entry.get("category") != "dependency":
            continue
        target = dependency_target(entry)
        if target and target in internal:
            out.append({"variable": str(name), "target": target})
    return sorted(out, key=lambda v: v["variable"])


def enforce_internal_targets(
    data: dict | None,
    internal_services: list[str] | set[str] | None,
    base_values: dict[str, str] | None = None,
) -> tuple[dict, list[dict[str, str]]]:
    """Mechanically demote same-compose dependency targets (C22).

    The internal match wins over any selected dependency of the same name. A
    non-empty template value becomes `defaulted`; an empty one becomes
    `required` (the category check then names it instead of inventing a URL).
    The target is dropped from `dependencies`.
    """
    import copy

    violations = internal_target_violations(data, internal_services)
    if not violations or not isinstance(data, dict):
        return data or {}, []
    out = copy.deepcopy(data)
    variables = out.setdefault("variables", {})
    values = base_values or {}
    for item in violations:
        name, target = item["variable"], item["target"]
        entry = variables.get(name)
        if not isinstance(entry, dict):
            continue
        value = str(values.get(name) or entry.get("value") or "")
        empty = is_empty_value(value)
        entry["category"] = "required" if empty else "defaulted"
        entry["source"] = {"kind": "file"}
        if empty:
            entry.pop("value", None)
        else:
            entry["value"] = value
        reason = str(entry.get("reason") or "").strip()
        note = (f"internal service '{target}' (C22): "
                + ("kept the template value" if not empty
                   else "the template has no value"))
        entry["reason"] = (reason + "; " + note) if reason else note
    out["dependencies"] = dependencies_map(variables)
    return out, violations


def write_artifact(
    recipe_dir: str | Path,
    *,
    source_service: str,
    recipe_path: str,
    fingerprint: dict[str, Any],
    variables: dict[str, Any],
    dependencies: dict[str, list[str]] | None = None,
) -> Path:
    """Write the current fingerprint's YAML variant (+ marker).

    Only the matched variant is overwritten — never another fingerprint's file
    (R29). ``generate new`` calls this for the current structural inputs.
    """
    import yaml

    path = variant_path(recipe_dir, fingerprint)
    payload = {
        "version": 1,
        "source_service": source_service,
        "recipe_path": recipe_path or "",
        "fingerprint": _canonical_fingerprint(fingerprint),
        "variables": variables,
        "dependencies": dependencies if dependencies is not None
        else dependencies_map(variables),
        "generated_at": _stamp(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
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
    active_vars: set[str] | None = None,
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
                # F7 — a variable that belongs only to an inactive-profile
                # service is not required; an empty value is harmless.
                if active_vars is not None and name not in active_vars:
                    continue
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
    active_vars: set[str] | None = None,
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
            # F7 — only an active compose service makes a required/defaulted
            # variable necessary; an inactive-profile variable may stay empty.
            if (active_vars is not None and category in ("required", "defaulted")
                    and name not in active_vars):
                continue
            missing.append(name)
    return sorted(missing), sorted(externals)


# ---------------------------------------------------------------------------
# Per-instance snapshot (R21, R22)
# ---------------------------------------------------------------------------

def classification_snapshot_name(user: str, label: str) -> str:
    return f"env.classification.user-{user}.{label}.yaml"


def snapshot_classification(
    recipe_dir: str | Path, *, user: str, label: str,
    source_name: str | None = None,
) -> str | None:
    """Copy the USED variant to its per-instance snapshot (+ marker).

    ``source_name`` is the selected or written variant's file name; without one
    there is nothing to copy (the legacy JSON artefact is accepted as a source
    for a rev-1 session). Returns the snapshot name, or None. A write failure
    propagates to the caller, which logs it and never blocks the deploy.
    """
    name = classification_snapshot_name(user, str(label))
    candidates: list[Path] = []
    if source_name:
        candidates.append(Path(recipe_dir) / str(source_name))
    if not candidates:
        return None
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        return None
    dest = Path(recipe_dir) / name
    dest.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    Path(str(dest) + MARKER_SUFFIX).write_text("", encoding="utf-8")
    return name


def snapshot_exists(recipe_dir: str | Path, *, user: str, label: str) -> bool:
    return (Path(recipe_dir) / classification_snapshot_name(user, str(label))).is_file()

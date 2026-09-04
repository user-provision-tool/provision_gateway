"""Gateway-side compose ${VAR} scanner.

Determines ``needs_env`` and env completeness from selected compose files
without invoking docker (the design's union-scan fallback: over-approximation
is safe for requiredness). Handles the robustness cases the design mandates:

- nested defaults ``${A:-${B:-x}}``
- required syntax ``${A:?err}``
- ``$`` escapes (``${LITERAL}`` is NOT a variable; ``$${VAR}`` parses as the
  literal text ``${VAR}`` — an even number of leading ``$`` escapes a ref, an
  odd number yields one literal ``$`` plus a live ref)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class VarRef:
    """One ``${...}`` reference found in compose text."""

    name: str
    has_default: bool
    required: bool  # ${NAME:?err} form
    default_raw: str = ""


@dataclass
class ScanResult:
    """Result of scanning a compose text for variable references."""

    refs: list[VarRef] = field(default_factory=list)

    def names(self) -> list[str]:
        """Variable names in first-appearance order (deduplicated)."""
        seen: list[str] = []
        for ref in self.refs:
            if ref.name not in seen:
                seen.append(ref.name)
        return seen

    def missing(self, provided: set[str]) -> list[str]:
        """Names without a default that are absent from *provided* (sorted)."""
        need = {r.name for r in self.refs if not r.has_default} - set(provided)
        return sorted(need)


def _iter_var_contents(text: str):
    """Yield the raw inner contents of every non-escaped ``${...}`` occurrence.

    ``$`` escaping: ``$${X}`` is a literal ``${X}`` and is skipped; ``$$${X}``
    yields a live ref (only the odd ``$``-prefixed one counts — a ref whose
    ``$``-run before the brace is odd is escaped). Nested braces
    (``${A:-${B:-x}}``) are brace-matched. Mirrors the api-side scanner.
    """
    i = 0
    n = len(text)
    while i < n:
        idx = text.find("${", i)
        if idx == -1:
            return
        # Count consecutive '$' before the '{' — odd count = escaped literal.
        dollars = 0
        j = idx - 1
        while j >= 0 and text[j] == "$":
            dollars += 1
            j -= 1
        if dollars % 2 == 1:
            i = idx + 2
            continue
        # Brace-match (nested ${...} inside defaults).
        depth = 1
        k = idx + 2
        while k < n and depth > 0:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            k += 1
        if depth != 0:
            return  # unclosed — stop scanning
        yield text[idx + 2: k - 1]
        i = k


def _split_modifier(content: str) -> tuple[str, str, str]:
    """Split ``NAME`` / ``NAME:-def`` / ``NAME-`` / ``NAME:?err``.

    Returns (name, modifier_kind, rest) where modifier_kind is one of
    ``":"`` (default), ``":"?"`` (required) or ``"-"`` (empty-default).
    Mirrors the api-side scanner (modifiers only at depth 0, nested ``${}``
    tracked with startswith("${")).
    """
    content = content.strip()
    if not content:
        return "", "", ""
    depth = 0
    i = 0
    n = len(content)
    while i < n:
        if content.startswith("${", i):
            depth += 1
            i += 2
            continue
        if content[i] == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and content[i] == ":" and i + 1 < n and content[i + 1] in "-?":
            kind = "required" if content[i + 1] == "?" else "default"
            return content[:i], kind, content[i + 2:].strip()
        if depth == 0 and content[i] == "-":
            return content[:i], "empty", content[i + 1:].strip()
        i += 1
    return content, "", ""


def _parse_ref(content: str) -> VarRef | None:
    """Parse one ``${...}`` inner content into a VarRef (None if not a var)."""
    name, kind, rest = _split_modifier(content)
    if not name:
        return None
    # Docker compose variable names: [a-zA-Z_][a-zA-Z0-9_]*
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        return None
    has_default = kind in ("default", "empty")
    return VarRef(name=name, has_default=has_default, required=kind == "required", default_raw=rest)


def scan_text(text: str) -> ScanResult:
    """Scan raw compose text for ${VAR} references.

    Nested defaults (``${A:-${B:-x}}``) are recorded as separate refs — the
    default rest is re-scanned recursively (parity with the api-side scanner).
    """
    result = ScanResult()
    for content in _iter_var_contents(text):
        ref = _parse_ref(content)
        if ref:
            result.refs.append(ref)
            if "${" in ref.default_raw:
                result.refs.extend(scan_text(ref.default_raw).refs)
    return result


def scan_compose_file(path: str | Path) -> ScanResult:
    """Scan one compose file on disk (missing/empty file → empty result)."""
    p = Path(path)
    if not p.is_file():
        return ScanResult()
    try:
        return scan_text(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ScanResult()


def scan_compose_files(paths: Iterable[str | Path]) -> ScanResult:
    """Union scan across multiple compose files (order-preserving, deduped)."""
    result = ScanResult()
    seen: set[str] = set()
    for p in paths:
        for ref in scan_compose_file(p).refs:
            if ref.name not in seen:
                seen.add(ref.name)
                result.refs.append(ref)
    return result


def needs_env(paths: Iterable[str | Path]) -> bool:
    """True when any selected compose file contains a ${VAR} interpolation."""
    return len(scan_compose_files(paths).refs) > 0


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a .env-class file into ``{KEY: value}`` (skip comments/blank)."""
    p = Path(path)
    result: dict[str, str] = {}
    if not p.is_file():
        return result
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip().strip('"').strip("'")
    return result


def env_completeness(
    compose_paths: Iterable[str | Path], env_paths: Iterable[str | Path]
) -> list[str]:
    """Vars required by compose but absent from the merged env files (sorted).

    'Required' = no ``:-``/``-`` default. Later env files win, so the union of
    all provided keys is checked.
    """
    scan = scan_compose_files(compose_paths)
    if not scan.refs:
        return []
    provided: set[str] = set()
    for ep in env_paths:
        provided.update(parse_env_file(ep).keys())
    return scan.missing(provided)


def skeleton_env(compose_paths: Iterable[str | Path]) -> str:
    """Deterministic ``KEY=`` lines for every no-default var (sorted)."""
    scan = scan_compose_files(compose_paths)
    names = sorted({r.name for r in scan.refs if not r.has_default})
    return "\n".join(f"{n}=" for n in names) + ("\n" if names else "")

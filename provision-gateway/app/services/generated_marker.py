"""The generated-artifact marker contract (GRILLING G109–G111, task-doc R1).

A platform-written artifact is ``<artifact>`` and its marker sidecar is
``<artifact>.<class>.generated``. The marker holds a small JSON provenance
record; the artifact and its validated marker are one **write pair** — success
is reported only when both exist.

This module is the single definition of the contract for the gateway. The
provision-api carries a byte-identical copy (``lib/generated_marker.py``)
because the two engines ship in separate containers; the contract test in both
repositories asserts the two copies agree.

The class set is closed: ``base``, ``fix``, ``user``, ``ins``, ``cls``, ``rel``.
``legacy`` is a READ-TIME state of a bare empty marker (or an unreadable
record), never a filename class.

An unmarked file is **repo-shipped**, whatever its name: it is never marked,
overwritten or renamed. A write that targets one is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MARKER_SUFFIX = ".generated"
CLASSES = ("base", "fix", "user", "ins", "cls", "rel")
GENERATORS = ("llm", "mechanical", "operator")
DESCRIPTION_MAX = 200
GUIDE_MAX = 1000
LEGACY = "legacy"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_KEY_RE = re.compile(
    r"(password|passwd|secret|credential|api[_-]?key|access[_-]?token)", re.I)


class MarkerError(ValueError):
    """The marker record does not satisfy the frozen contract."""


class UnmarkedTargetError(MarkerError):
    """A write targeted an existing file with no marker (repo-shipped)."""


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def marker_name(artifact_name: str, cls: str) -> str:
    if cls not in CLASSES:
        raise MarkerError(f"unknown marker class {cls!r}")
    return f"{artifact_name}.{cls}{MARKER_SUFFIX}"


def parse_marker_name(filename: str) -> tuple[str, str | None] | None:
    """``(artifact_name, class)`` for a marker filename, or None.

    A bare ``<artifact>.generated`` returns class ``None`` (read as legacy).
    """
    if not filename.endswith(MARKER_SUFFIX):
        return None
    stem = filename[: -len(MARKER_SUFFIX)]
    artifact, dot, cls = stem.rpartition(".")
    if dot and cls in CLASSES:
        return artifact, cls
    return stem, None


def marker_for(path: str | Path, cls: str) -> Path:
    p = Path(path)
    return p.with_name(marker_name(p.name, cls))


def find_marker(path: str | Path) -> Path | None:
    """The marker beside ``path``: a subclass marker first, then a bare one.

    When two subclass markers exist (a legacy stray base marker beside the
    real class), the non-``base`` one wins: a platform artifact is written
    with its own class, and ``base`` is the fallback a finalizer may once have
    added. One artifact carries one class; the writer removes the stray.
    """
    p = Path(path)
    for cls in ("fix", "user", "ins", "cls", "rel", "base"):
        candidate = p.with_name(marker_name(p.name, cls))
        if candidate.is_file():
            return candidate
    bare = p.with_name(p.name + MARKER_SUFFIX)
    return bare if bare.is_file() else None


def is_marked(path: str | Path) -> bool:
    return find_marker(path) is not None


def _check_text(record: dict, key: str, limit: int) -> None:
    value = record.get(key)
    if value is None:
        return
    if not isinstance(value, str):
        raise MarkerError(f"{key!r} must be a string")
    if len(value) > limit:
        raise MarkerError(f"{key!r} exceeds {limit} characters")


def _check_credentials(value: Any, path: str = "record") -> None:
    """Refuse credential-bearing keys; the raw generation context never lands."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _CREDENTIAL_KEY_RE.search(str(key)):
                raise MarkerError(
                    f"credential-bearing field {path}.{key} is not allowed")
            _check_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _check_credentials(item, f"{path}[{idx}]")


def validate_record(record: Any) -> dict:
    """Validate a record against the frozen contract; return it unchanged."""
    if not isinstance(record, dict):
        raise MarkerError("marker record must be a JSON object")
    if record.get("schema") != SCHEMA_VERSION:
        raise MarkerError(
            f"schema must be {SCHEMA_VERSION}, got {record.get('schema')!r}")
    cls = record.get("class")
    if cls not in CLASSES:
        raise MarkerError(f"class must be one of {CLASSES}, got {cls!r}")
    _check_text(record, "description", DESCRIPTION_MAX)
    _check_text(record, "guide", GUIDE_MAX)
    _check_text(record, "validation", GUIDE_MAX)
    _check_text(record, "source", 500)
    digest = record.get("sha256")
    if digest is not None and (not isinstance(digest, str)
                               or not _SHA256_RE.match(digest)):
        raise MarkerError("sha256 must be 64 lowercase hex characters")
    created = record.get("created_at")
    if created is not None and not isinstance(created, str):
        raise MarkerError("created_at must be an ISO-8601 string")
    job = record.get("job")
    if job is not None and not isinstance(job, (str, int)):
        raise MarkerError("job must be a string or an integer")
    generator = record.get("generator")
    if generator is not None and generator not in GENERATORS:
        raise MarkerError(f"generator must be one of {GENERATORS}")
    fingerprint = record.get("fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, dict):
        raise MarkerError("fingerprint must be an object")
    _check_credentials(record)
    return record


def build_record(*, cls: str, description: str = "", content: str | bytes | None = None,
                 job: str | int | None = None, generator: str = "mechanical",
                 fingerprint: dict | None = None, validation: str | None = None,
                 guide: str | None = None, source: str | None = None,
                 created_at: str | None = None, sha256: str | None = None) -> dict:
    """Build a record; the content hash is computed when content is given."""
    if content is not None:
        digest = (sha256_text(content) if isinstance(content, str)
                  else sha256_bytes(content))
    else:
        digest = sha256
    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "class": cls,
        "description": (description or "").strip(),
        "sha256": digest,
        "created_at": created_at or _stamp(),
        "job": job,
        "generator": generator,
        "fingerprint": fingerprint or {},
        "validation": validation,
        "guide": guide,
        "source": source,
    }
    return validate_record(record)


@dataclass(frozen=True)
class MarkerInfo:
    """What one artifact's marker says (read-time, never raises)."""

    path: Path
    cls: str | None          # a class, "legacy", or None when unmarked
    record: dict | None
    marker: Path | None
    legacy: bool = False

    @property
    def present(self) -> bool:
        return self.cls is not None

    @property
    def description(self) -> str:
        return str((self.record or {}).get("description") or "")

    @property
    def created_at(self) -> str:
        return str((self.record or {}).get("created_at") or "")

    @property
    def job(self) -> str | int | None:
        return (self.record or {}).get("job")


def read_marker(path: str | Path) -> MarkerInfo:
    """Read one artifact's marker state. Corruption degrades to ``legacy``."""
    p = Path(path)
    found = find_marker(p)
    if found is None:
        return MarkerInfo(path=p, cls=None, record=None, marker=None)
    parsed = parse_marker_name(found.name)
    name_cls = parsed[1] if parsed else None
    try:
        raw = found.read_text(encoding="utf-8", errors="replace")
        record = json.loads(raw) if raw.strip() else None
        record = validate_record(record) if record is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        return MarkerInfo(path=p, cls=LEGACY, record=None, marker=found, legacy=True)
    if record is None:
        return MarkerInfo(path=p, cls=LEGACY, record=None, marker=found, legacy=True)
    if name_cls is None:
        # A bare marker with a JSON record: the record's class wins but the
        # name shape is legacy.
        return MarkerInfo(path=p, cls=str(record.get("class")), record=record,
                          marker=found, legacy=True)
    return MarkerInfo(path=p, cls=name_cls, record=record, marker=found)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_pair(path: str | Path, content: str, record: dict, *,
               created: bool = False) -> Path:
    """Write artifact + validated marker as one pair; return the marker path.

    * missing name -> insert the pair;
    * already-marked name -> regenerate in place (both files);
    * unmarked existing name -> refused (repo-shipped): never overwritten.

    ``created=True`` is for a caller that has JUST written the file through an
    external routine (a copy, a hashing helper) and is only recording it; the
    refusal still applies to every name the caller did not create.
    """
    p = Path(path)
    validate_record(record)
    if not created and p.exists() and not is_marked(p):
        raise UnmarkedTargetError(
            f"{p.name} exists without a marker — a repo-shipped file is never "
            "overwritten, marked or renamed")
    target_marker = marker_for(p, str(record["class"]))
    bare = p.with_name(p.name + MARKER_SUFFIX)
    old_content = None
    if p.exists():
        try:
            old_content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            old_content = None
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    _atomic_write(p, content)
    try:
        _atomic_write(target_marker, payload)
    except BaseException:
        # No partial pair: restore the prior bytes when the marker cannot land.
        if old_content is not None:
            _atomic_write(p, old_content)
        else:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    if bare.is_file() and bare != target_marker:
        try:
            bare.unlink()
        except OSError:
            pass
    return target_marker


def write_marker_pair(path: str | Path, content: str, *, cls: str,
                      description: str = "", job: str | int | None = None,
                      generator: str = "mechanical",
                      fingerprint: dict | None = None,
                      validation: str | None = None,
                      guide: str | None = None,
                      source: str | None = None) -> Path:
    """Convenience: build the record from the content, then write the pair."""
    record = build_record(cls=cls, description=description, content=content,
                          job=job, generator=generator,
                          fingerprint=fingerprint, validation=validation,
                          guide=guide, source=source)
    return write_pair(path, content, record)


def mark_file(path: str | Path, *, cls: str, description: str = "",
              job: str | int | None = None, generator: str = "mechanical",
              fingerprint: dict | None = None, validation: str | None = None,
              guide: str | None = None, source: str | None = None) -> Path:
    """Mark a file the caller has just written (re-reads its bytes)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    record = build_record(cls=cls, description=description, content=text,
                          job=job, generator=generator,
                          fingerprint=fingerprint, validation=validation,
                          guide=guide, source=source)
    return write_pair(p, text, record, created=True)


def remove_marker(path: str | Path) -> None:
    """Remove an artifact's marker, whatever its class (file left in place)."""
    found = find_marker(path)
    if found is not None:
        try:
            found.unlink(missing_ok=True)
        except OSError:
            pass


def remove_pair(path: str | Path) -> None:
    """Remove an artifact and its marker (used for a deployment's copies only)."""
    p = Path(path)
    found = find_marker(p)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    if found is not None:
        try:
            found.unlink(missing_ok=True)
        except OSError:
            pass

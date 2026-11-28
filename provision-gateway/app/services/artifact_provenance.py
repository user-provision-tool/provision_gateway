"""Derived evidence for the generated-artifact pickers (task-doc R4, T6).

The markers carry creation facts; this service derives the three READ-TIME
facts the dashboard shows beside a candidate:

* **exact match** — the marker's stored structural fingerprint equals the
  current inputs (a hint, never a filter);
* **deploy history** — a read-only join over the provision-api registry's
  recorded selections; a missing/unreachable registry omits the fact;
* **duplicate group** — how many artifacts in the recipe share the content
  hash;
* **staleness** — the file's current bytes no longer match the marker's hash,
  so its history is unknown.

Nothing here is written and nothing here gates a deploy (R10.1). A corrupt or
missing marker degrades the affected fact; it is never an error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from .env_classification import fingerprint_matches
from .generated_marker import read_marker, sha256_file

_log = logging.getLogger("gateway.provenance")

_SELECTION_KEYS = (
    "selected_compose_paths", "selected_nginx_paths", "selected_env_paths",
    "selected_serving_ports", "selected_stream_conf", "selected_mail_conf",
)


def _names_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value.rsplit("/", 1)[-1]
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item.rsplit("/", 1)[-1]
    elif isinstance(value, dict):
        for item in value.values():
            if isinstance(item, str):
                yield item.rsplit("/", 1)[-1]


def deploy_counts(service_name: str) -> dict[str, int]:
    """``{artifact name: number of live instances whose selection names it}``.

    A registry outage returns ``{}`` — the deploy row is omitted, never an
    error (R4.2/R10.2).
    """
    counts: dict[str, int] = {}
    try:
        from .registry import Registry
        entries = Registry().get_all_entries()
    except Exception:  # noqa: BLE001 — one optional source
        _log.warning("deploy history unavailable for %s", service_name, exc_info=True)
        return counts
    for entry in entries:
        if str(entry.get("service_name") or "") != service_name:
            continue
        if entry.get("service_deleted_at"):
            continue
        seen: set[str] = set()
        for key in _SELECTION_KEYS:
            for name in _names_in(entry.get(key)):
                if name and name not in seen:
                    seen.add(name)
                    counts[name] = counts.get(name, 0) + 1
    return counts


def duplicate_counts(recipe_dir: str | Path, names: Iterable[str]) -> dict[str, int]:
    """``{name: number of OTHER artifacts in the recipe with the same sha256}``."""
    rdir = Path(recipe_dir)
    by_hash: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for name in names:
        path = rdir / name
        if not path.is_file():
            continue
        digest = sha256_file(path)
        if not digest:
            continue
        hashes[name] = digest
        by_hash[digest] = by_hash.get(digest, 0) + 1
    return {name: by_hash[digest] - 1 for name, digest in hashes.items()}


def summarize(path: str | Path, *, fingerprint: dict | None = None,
              deploys: int | None = None, duplicates: int = 0) -> dict[str, Any]:
    """One candidate's provenance, degraded field by degraded field."""
    p = Path(path)
    info = read_marker(p)
    record = info.record or {}
    digest_now = sha256_file(p)
    stored = record.get("sha256")
    history_unknown = bool(stored and digest_now and stored != digest_now)
    exact: bool | None = None
    fp = record.get("fingerprint")
    if isinstance(fingerprint, dict) and isinstance(fp, dict) and fp:
        exact = fingerprint_matches(fp, fingerprint)
    return {
        "class": info.cls,
        "legacy": bool(info.legacy),
        "description": info.description,
        "created_at": info.created_at,
        "sha256": stored,
        "exact": exact,
        "deploys": deploys,
        "duplicates": duplicates,
        "history_unknown": history_unknown,
    }


def build_provenance(recipe_dir: str | Path, names: Iterable[str], *,
                     fingerprint: dict | None,
                     deploys: dict[str, int] | None = None) -> dict[str, dict]:
    """The provenance map for one recipe's candidate names."""
    rdir = Path(recipe_dir)
    names = list(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    dups = duplicate_counts(rdir, names)
    counts = deploys or {}
    out: dict[str, dict] = {}
    for name in names:
        path = rdir / name
        if not path.is_file():
            continue
        out[name] = summarize(
            path, fingerprint=fingerprint,
            deploys=counts.get(name),
            duplicates=dups.get(name, 0))
    return out

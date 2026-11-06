"""R7 — the boundary guard.

Fails the build when the supported clients grow an -api address, when a gateway
migration writes the registry schema, or when gateway code issues a registry
write. The `_users_provision` diff check is `scripts/boundary_guard.sh` (it
needs the sibling worktree and git).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]           # provision-gateway/
PROJECT = REPO.parent                                # _provision_gateway/
DASHBOARD_SRC = PROJECT / "provision-dashboard" / "src"
CLI = REPO / "scripts" / "thin_cli_check.sh"
MIGRATIONS = REPO / "app" / "db" / "migrations"

_API_ADDRESSES = re.compile(r"(?::8875\b|subnet-acl-provision-api\b|provision-api:8875)")
_REGISTRY_WRITE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE|"
    r"DROP\s+TABLE|TRUNCATE)\s+registry\.",
    re.IGNORECASE,
)


def _iter_files(root: Path, suffixes: tuple[str, ...]):
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in suffixes:
            if "node_modules" in path.parts:
                continue
            yield path


def test_clients_carry_no_api_address():
    offenders = []
    for path in _iter_files(DASHBOARD_SRC, (".ts", ".tsx", ".js", ".jsx", ".json", ".html")):
        if _API_ADDRESSES.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(path.relative_to(PROJECT)))
    if CLI.is_file() and _API_ADDRESSES.search(CLI.read_text(encoding="utf-8")):
        offenders.append(str(CLI.relative_to(PROJECT)))
    assert offenders == [], f"-api address in a supported client: {offenders}"


def test_gateway_migrations_never_write_the_registry_schema():
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if _REGISTRY_WRITE.search(text):
            offenders.append(path.name)
    assert offenders == [], f"registry write in a gateway migration: {offenders}"


def test_gateway_code_never_writes_registry_tables():
    offenders = []
    for path in _iter_files(REPO / "app", (".py",)):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _REGISTRY_WRITE.search(text):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], f"registry write in gateway code: {offenders}"

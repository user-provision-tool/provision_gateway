"""Backfill the existing same-owner relationships as ordinary grant rows (G102 R3.5).

Run this operator one-off **before** the explicit-same-owner enforcement is
live.  It lists every ordered same-owner pair whose two services exist and are
not tombstoned/deleted (runtime state is irrelevant: a stopped pair is
included) and writes an ordinary row through the add-only path.  No marker, no
schema change; an existing row is untouched; a second run changes nothing.

Usage::

    python -m app.scripts.backfill_same_owner_grants [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from app.database import SessionLocal
from app.services import service_grants


def backfill(*, dry_run: bool = False) -> dict:
    db = SessionLocal()
    try:
        entries = service_grants.live_entries()
        by_owner: dict[str, list[dict]] = {}
        for entry in entries:
            owner = str(entry.get("owner_api_user_id"))
            by_owner.setdefault(owner, []).append(entry)
        created = 0
        existing = 0
        refused: list[dict] = []
        for owner, group in by_owner.items():
            for source in group:
                for target in group:
                    if str(source["service_id"]) == str(target["service_id"]):
                        continue
                    if service_grants.explicit_row(
                            db, str(source["service_id"]), str(target["service_id"])):
                        existing += 1
                        continue
                    if dry_run:
                        created += 1
                        continue
                    try:
                        result = service_grants.ensure_grant(
                            db, str(source["service_id"]), str(target["service_id"]))
                        if result.get("created"):
                            created += 1
                        else:
                            existing += 1
                    except ValueError as exc:
                        refused.append({
                            "source": str(source["service_id"]),
                            "target": str(target["service_id"]),
                            "reason": str(exc),
                        })
        return {
            "services": len(entries),
            "owners": len(by_owner),
            "created": created,
            "existing": existing,
            "refused": refused,
            "dry_run": dry_run,
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="count the rows that would be written; write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)
    report = backfill(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"same-owner backfill: {report['services']} service(s), "
            f"{report['owners']} owner(s); created={report['created']} "
            f"existing={report['existing']} refused={len(report['refused'])}"
            + (" (dry run)" if report["dry_run"] else "")
        )
        for item in report["refused"]:
            print(f"  refused {item['source']} -> {item['target']}: {item['reason']}")
    return 1 if report["refused"] else 0


if __name__ == "__main__":  # pragma: no cover - operator entry
    sys.exit(main())

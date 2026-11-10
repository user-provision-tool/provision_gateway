"""Claim a periodic loop for this worker (GRILLING G31).

`acquire_lease` is one atomic statement: an INSERT that becomes an UPDATE only
while the existing lease has expired. PostgreSQL evaluates the `WHERE` on the
`DO UPDATE`, so a live lease makes the statement touch no row — `rowcount` is 0
and the caller skips its turn. No read-then-write race, and no lock of our own.

The table is **schema-qualified** (`gateway.loop_leases`): the gateway role's
`search_path` is `"$user", public`, so an unqualified name raised "relation does
not exist", the catch-all fallback returned True on every call, and every loop
ran in all four workers (measured 2026-11-09: zero rows in the table). The ORM
model already declares the schema; only this raw SQL was missing it.
"""

from __future__ import annotations

import time


def acquire_lease(name: str, seconds: float) -> bool:
    """True when THIS worker now holds `name` for `seconds`.

    Call it at the top of each iteration. A window longer than the loop's own
    interval means the same worker keeps winning (the loops are cheap and
    idempotent), while a dead holder is replaced once its window expires.
    """
    from sqlalchemy import text

    from ..database import SessionLocal

    now = time.time()
    db = SessionLocal()
    try:
        res = db.execute(
            text(
                "INSERT INTO gateway.loop_leases AS ll (name, locked_until) "
                "VALUES (:n, :u) "
                "ON CONFLICT(name) DO UPDATE SET locked_until = :u "
                "WHERE ll.locked_until < :now"
            ),
            {"n": name, "u": now + float(seconds), "now": now},
        )
        db.commit()
        return bool(res.rowcount)
    except Exception:
        # A missing table (an older DB mid-migration) must not take the loop
        # down: fall back to running it, which is the pre-lease behaviour.
        return True
    finally:
        db.close()

"""Locked pre-deploy identity reservations (R5, T1).

The gateway owns the pre-deploy identity. A reservation is one row in
``gateway.label_reservations`` for ``(owner_api_user_id, service_name, label)``
and exists only while the tuple is reserved: handoff and session delete both
release it, so a released label is reusable. The next free label is computed
from the registry's services (including tombstones — a label is never reused
across history), the pre-deploy sessions and the reservations, all inside one
transaction-scoped advisory lock so two concurrent intents cannot pick the same
number.

The -api's registry uniqueness stays the final backstop: an external
registration that wins the race after the reservation makes the register call
fail; the workflow then reserves the next label and rebinds, or returns a
structured retryable refusal — never a wrong identity.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

_LOCK_NAMESPACE = "label-reservation:"


class LabelConflict(ValueError):
    """The pinned label is already in use for this tuple."""

    def __init__(self, label: str):
        super().__init__(f"label {label!r} is already in use for this service")
        self.label = label


def _now():
    return datetime.now(timezone.utc)


def _sessions():
    from ..database import SessionLocal
    return SessionLocal()


def _tuple_labels(db, owner_api_user_id, service_name: str,
                  exclude_service_id: str | None = None,
                  *, persistent_only: bool) -> set[str]:
    """Persistent labels (registry history + sessions) or active reservations."""
    sql = (
        "SELECT s.label::text FROM registry.services s "
        " WHERE s.owner_api_user_id = :o AND s.service_name = :n "
        "   AND s.id <> :sid "
        "UNION "
        "SELECT p.service_label FROM gateway.pre_deploy_sessions p "
        " WHERE p.owner_api_user_id = :o AND p.service_name = :n "
        "   AND p.service_id <> :sid"
    )
    if not persistent_only:
        sql = (
            "SELECT r.label FROM gateway.label_reservations r "
            " WHERE r.owner_api_user_id = :o AND r.service_name = :n "
            "   AND r.service_id <> :sid"
        )
    rows = db.execute(
        text(sql),
        {"o": str(owner_api_user_id), "n": service_name,
         "sid": str(exclude_service_id or "00000000-0000-0000-0000-000000000000")},
    ).all()
    return {str(r[0]) for r in rows if r[0] is not None}


def _next_number(persistent: set[str], reserved: set[str]) -> str:
    """The next free label.

    A label that was REGISTERED (or reached the registry as a tombstone) is
    never reused, so numbers below the registered maximum are passed. A
    RELEASED reservation that never registered IS reusable: when only
    reservations are holding numbers, take the smallest free one.
    """
    def numbers(values):
        out = set()
        for value in values:
            try:
                out.add(int(str(value)))
            except (TypeError, ValueError):
                continue
        return out

    persistent_numbers = numbers(persistent)
    if persistent_numbers:
        return str(max(persistent_numbers | numbers(reserved)) + 1)
    taken = numbers(reserved)
    candidate = 0
    while candidate in taken:
        candidate += 1
    return str(candidate)


def reserve(*, owner_api_user_id, service_name: str, service_id: str,
            requested_label: str | None = None) -> str:
    """Reserve the pinned label, or the next free one, under the tuple lock.

    Raises ``LabelConflict`` for a pinned label that is already used. An auto
    label always succeeds (the next number is free by construction).
    """
    db = _sessions()
    try:
        # One critical section per (owner, service): the count and the insert
        # cannot interleave with another intake (the same primitive the
        # generation capacity gate uses).
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"{_LOCK_NAMESPACE}{owner_api_user_id}:{service_name}"},
        )
        persistent = _tuple_labels(db, owner_api_user_id, service_name,
                                   service_id, persistent_only=True)
        reserved = _tuple_labels(db, owner_api_user_id, service_name,
                                 service_id, persistent_only=False)
        pinned = str(requested_label) if requested_label not in (None, "", "auto") else None
        if pinned is not None and pinned in (persistent | reserved):
            raise LabelConflict(pinned)
        label = pinned if pinned is not None else _next_number(persistent, reserved)

        # A service id owns at most one reservation; rebinding replaces it.
        db.execute(
            text("DELETE FROM gateway.label_reservations WHERE service_id = :sid"),
            {"sid": str(service_id)},
        )
        db.execute(
            text(
                "INSERT INTO gateway.label_reservations "
                "(owner_api_user_id, service_name, label, service_id, created_at) "
                "VALUES (:o, :n, :l, :sid, :t)"
            ),
            {"o": str(owner_api_user_id), "n": service_name, "l": label,
             "sid": str(service_id), "t": _now()},
        )
        db.commit()
        return label
    finally:
        db.close()


def rebind(*, service_id: str, label: str) -> None:
    """Point this service's reservation at a new label (external-race repair)."""
    db = _sessions()
    try:
        db.execute(
            text("UPDATE gateway.label_reservations SET label = :l WHERE service_id = :sid"),
            {"l": str(label), "sid": str(service_id)},
        )
        db.commit()
    finally:
        db.close()


def release(service_id: str) -> bool:
    """Release the tuple's reservation (handoff or session delete)."""
    db = _sessions()
    try:
        n = db.execute(
            text("DELETE FROM gateway.label_reservations WHERE service_id = :sid"),
            {"sid": str(service_id)},
        ).rowcount
        db.commit()
        return bool(n)
    finally:
        db.close()


def reserved_label(service_id: str) -> str | None:
    db = _sessions()
    try:
        row = db.execute(
            text("SELECT label FROM gateway.label_reservations WHERE service_id = :sid"),
            {"sid": str(service_id)},
        ).first()
        return str(row[0]) if row else None
    finally:
        db.close()

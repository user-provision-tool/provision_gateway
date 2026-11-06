"""Pre-deploy sessions, keyed by service_id (T8).

The session is the pre-deploy card's source: display fields, state, options and
task links live here, never on the generation task. Handoff is add-then-remove:
the -api creates the instance first, then the gateway removes this row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from ..models.pre_deploy_session import (
    OPERATION_DEPLOY, OPERATION_GENERATE, PreDeploySession, STATE_DEPLOYING,
    STATE_FAILED, STATE_GENERATED, STATE_GENERATING, STATE_PAUSED, STATE_REFUSED,
)

OPERATOR_HELD_STATES = (STATE_PAUSED, STATE_REFUSED, STATE_GENERATED)

# R3.6: the retry replay may carry the deploy credentials. They are stored in
# `options_secret_enc` (gateway keyring, AES-GCM) and never in `options`, a log
# or the refusal detail.
SECRET_OPTION_KEYS = ("passwd", "passwd_plain", "password", "privkey")


def secret_option_values(request: dict | None) -> dict:
    req = request or {}
    return {k: req[k] for k in SECRET_OPTION_KEYS if req.get(k) not in (None, "")}


def _encrypt_secret(values: dict) -> tuple[bytes | None, int | None]:
    if not values:
        return None, None
    from . import keyring

    envelope = keyring.encrypt(json.dumps(values, sort_keys=True))
    marker = envelope.decode("utf-8").split(":", 1)[0]   # "v<ring key>"
    ring_key = marker[1:]
    digits = ring_key[1:] if ring_key.startswith("v") else ring_key
    version = int(digits) if digits.isdigit() else None
    return envelope, version


def _decrypt_secret(envelope: bytes | None) -> dict:
    if not envelope:
        return {}
    from . import keyring

    raw = keyring.decrypt(envelope)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return {k: v for k, v in parsed.items() if k in SECRET_OPTION_KEYS}


def _now():
    return datetime.now(timezone.utc)


def _sessions():
    from ..database import SessionLocal
    return SessionLocal()


def _username(db, api_user_id) -> str | None:
    row = db.execute(
        text("SELECT username FROM registry.api_users WHERE id=:id"),
        {"id": str(api_user_id)},
    ).first()
    return row[0] if row else None


def _to_dict(db, row: PreDeploySession) -> dict[str, Any]:
    return row.to_dict(username=_username(db, row.owner_api_user_id))


def list_sessions() -> list[dict[str, Any]]:
    db = _sessions()
    try:
        rows = db.query(PreDeploySession).order_by(PreDeploySession.updated_at).all()
        return [_to_dict(db, r) for r in rows]
    finally:
        db.close()


def get_session(service_id: str) -> dict[str, Any] | None:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        return _to_dict(db, row) if row else None
    finally:
        db.close()


def find_by_tuple(owner_api_user_id, service_name: str, label: str) -> dict[str, Any] | None:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(
            owner_api_user_id=owner_api_user_id,
            service_name=service_name,
            service_label=str(label),
        ).first()
        return _to_dict(db, row) if row else None
    finally:
        db.close()


def is_reserved(owner_api_user_id, service_name: str, label: str) -> bool:
    db = _sessions()
    try:
        return db.query(PreDeploySession).filter(
            PreDeploySession.owner_api_user_id == owner_api_user_id,
            PreDeploySession.service_name == service_name,
            PreDeploySession.service_label == str(label),
            PreDeploySession.state != STATE_GENERATED,
        ).count() > 0
    finally:
        db.close()


def create_or_replace(
    *, service_id, procedure_id, owner_api_user_id, service_name: str,
    service_label: str, recipe_path: str, state: str = STATE_GENERATING,
    generation_task_id: str | None = None, deploy_task_id: str | None = None,
    reason: str | None = None, detail: str | None = None,
    guide: str | None = None, slot: str | None = None,
    use_global_proxy: bool | None = None, options: dict | None = None,
    secret_values: dict | None = None,
    operation: str | None = None,
    deploy_request: dict | None = None,
    deploy_secret_values: dict | None = None,
    selection: dict | None = None,
    env_name: str | None = None,
) -> dict[str, Any]:
    """Create or update a session.

    The workflow fields (`operation`, `deploy_request`, `selection`, `env_name`)
    are only written when supplied: a new generation leg replaces the task link
    but must NOT reset the operation or the replayable deploy intent.
    """
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            row = PreDeploySession(
                service_id=service_id, procedure_id=procedure_id,
                owner_api_user_id=owner_api_user_id, service_name=service_name,
                service_label=str(service_label), recipe_path=recipe_path or "",
                created_at=_now(),
            )
            db.add(row)
        row.procedure_id = procedure_id
        row.owner_api_user_id = owner_api_user_id
        row.service_name = service_name
        row.service_label = str(service_label)
        row.recipe_path = recipe_path or ""
        row.state = state
        row.generation_task_id = generation_task_id
        row.deploy_task_id = deploy_task_id
        row.reason = reason
        row.detail = detail
        row.guide = guide
        row.slot = slot
        row.use_global_proxy = use_global_proxy
        row.options = options
        # A new leg replaces the replay payload; the encrypted half is written
        # with it (or cleared when the leg carries no credential). The deploy
        # intent's own credential half is separate and only touched when the
        # caller supplies it.
        envelope, version = _encrypt_secret(secret_values or {})
        row.options_secret_enc = envelope
        row.options_key_version = version
        if operation is not None:
            row.operation = operation
        if deploy_request is not None:
            row.deploy_request = deploy_request
        if deploy_secret_values is not None:
            denv, dver = _encrypt_secret(deploy_secret_values or {})
            row.deploy_secret_enc = denv
            row.deploy_key_version = dver
        if selection is not None:
            row.selection = selection
        if env_name is not None:
            row.env_name = env_name
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return _to_dict(db, row)
    finally:
        db.close()


def new_generation_leg(
    *, service_id, procedure_id, owner_api_user_id, service_name: str,
    service_label: str, recipe_path: str, generation_task_id: str,
    guide: str | None = None, slot: str | None = None,
    use_global_proxy: bool | None = None,
) -> dict[str, Any]:
    """A new generation leg replaces the procedure/task links and stale data.

    A leg started outside the intake endpoints (the standalone/Check path)
    defaults to `operation=generate`, so it stops at the review gate. An
    existing workflow record keeps its operation — answering its pause must not
    turn a deploy into a review stop or vice versa.
    """
    existing = get_session(service_id)
    operation = (existing or {}).get("operation") or OPERATION_GENERATE
    return create_or_replace(
        service_id=service_id, procedure_id=procedure_id,
        owner_api_user_id=owner_api_user_id, service_name=service_name,
        service_label=service_label, recipe_path=recipe_path,
        state=STATE_GENERATING, generation_task_id=generation_task_id,
        deploy_task_id=None, reason=None, detail=None, guide=guide, slot=slot,
        use_global_proxy=use_global_proxy, options=None, operation=operation,
    )


def remove_session(service_id: str) -> bool:
    """Delete the session and release its label reservation (R5).

    Every path that removes a record — handoff, an explicit delete, a failed
    leg — must release the reservation, or the identity leaks.
    """
    db = _sessions()
    try:
        n = db.query(PreDeploySession).filter_by(service_id=service_id).delete(
            synchronize_session=False)
        db.commit()
        try:
            from . import label_reservations
            label_reservations.release(service_id)
        except Exception:  # noqa: BLE001 — the delete succeeded; release is best-effort
            pass
        return bool(n)
    finally:
        db.close()


def set_state_for_job(task_id: str, state: str, reason: str | None = None) -> bool:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(generation_task_id=task_id).first()
        if row is None:
            return False
        row.state = state
        if reason is not None:
            row.reason = reason
        row.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def finish_leg(task_id: str, state: str | None) -> bool:
    """Advance or drop a session when its generation leg ends."""
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(generation_task_id=task_id).first()
        if row is None or row.state in (STATE_PAUSED, STATE_REFUSED):
            return False
        service_id = str(row.service_id)
        deleted = state is None
        if deleted:
            db.delete(row)
        else:
            row.state = state
        row.updated_at = _now()
        db.commit()
        if deleted:
            try:
                from . import label_reservations
                label_reservations.release(service_id)
            except Exception:  # noqa: BLE001
                pass
        return True
    finally:
        db.close()


def mark_refused(*, service_id: str, reason: str, detail: str = "",
                 options: dict[str, Any] | None = None,
                 secret_values: dict | None = None) -> dict[str, Any] | None:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return None
        row.state = STATE_REFUSED
        row.reason = reason
        row.detail = detail or ""
        row.options = options
        envelope, version = _encrypt_secret(secret_values or {})
        row.options_secret_enc = envelope
        row.options_key_version = version
        row.updated_at = _now()
        db.commit()
        db.refresh(row)
        return _to_dict(db, row)
    finally:
        db.close()


def set_deploying(service_id: str, procedure_id=None, deploy_task_id: str | None = None) -> bool:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return False
        row.state = STATE_DEPLOYING
        if procedure_id is not None:
            row.procedure_id = procedure_id
        if deploy_task_id is not None:
            row.deploy_task_id = deploy_task_id
        row.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def claim_for_deploy(service_id: str) -> bool:
    """Atomically claim `generated -> deploying`; exactly one completer wins.

    The claim is the deploy-once gate (R2): two completion observers, a retry
    and a reviewed-session Deploy can race, and only the rowcount-1 caller may
    call the -api register.
    """
    db = _sessions()
    try:
        n = (
            db.query(PreDeploySession)
            .filter_by(service_id=service_id, state=STATE_GENERATED)
            .update({PreDeploySession.state: STATE_DEPLOYING,
                     PreDeploySession.updated_at: _now()},
                    synchronize_session=False)
        )
        db.commit()
        return n == 1
    finally:
        db.close()


def set_selection(service_id: str, selection: dict | None,
                  env_name: str | None = None) -> bool:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return False
        if selection is not None:
            row.selection = selection
        if env_name is not None:
            row.env_name = env_name
        row.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def set_label(service_id: str, label: str) -> bool:
    """Rebind the session to a newly reserved label (external-race repair)."""
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return False
        row.service_label = str(label)
        row.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def mark_deploy_failed(service_id: str, detail: str,
                       reason: str = "deploy_failed") -> bool:
    """Fail closed with the detail visible in the status (R2/R8).

    The row stays as the failure record; the next deploy replaces it and an
    explicit session delete removes it. No instance was created, so it is not a
    live card.
    """
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return False
        row.state = STATE_FAILED
        row.reason = reason
        row.detail = detail or ""
        row.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def session_record(service_id: str) -> dict[str, Any] | None:
    """All session fields, including the replayable intent and its credentials.

    Internal-use only: the public `get_session` deliberately omits both.
    """
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return None
        data = _to_dict(db, row)
        data["deploy_request"] = dict(row.deploy_request or {})
        try:
            data["deploy_secrets"] = _decrypt_secret(row.deploy_secret_enc)
        except Exception:  # noqa: BLE001 — a missing/wrong key fails closed
            data["deploy_secrets"] = {}
        return data
    finally:
        db.close()


def _task_row(task_id: str | None):
    if not task_id:
        return None
    from sqlalchemy import text
    db = _sessions()
    try:
        return db.execute(
            text("SELECT status, request, target_snapshot FROM gateway.task_view WHERE task_id=:id"),
            {"id": task_id},
        ).mappings().first()
    finally:
        db.close()


def _instance_status(service_id) -> str | None:
    """A live instance for this service id, if one exists (tombstones excluded)."""
    from sqlalchemy import text
    db = _sessions()
    try:
        row = db.execute(
            text(
                "SELECT i.status FROM registry.service_instances i "
                "JOIN registry.services s ON s.id = i.service_id "
                "WHERE i.service_id = :id AND s.deleted_at IS NULL"
            ),
            {"id": str(service_id)},
        ).first()
        return row[0] if row else None
    finally:
        db.close()


def reconcile(live_task_ids: set[str]) -> dict[str, Any]:
    """Resolve stale pre-deploy sessions by the deploy/generation task (R3.5).

    * ANY session whose service id now has a live instance is dropped: the
      instance is the truth, even for an operator-held (`generated`) row (R13).
      That is what repairs the accept->delete crash window;
    * operator-held (`paused`/`refused`/`generated`/`failed`) are otherwise kept;
    * `deploying` + instance exists -> the instance card takes over, drop it;
    * `deploying` + no instance + live/unknown deploy task -> keep;
    * `deploying` + no instance + terminal/no task -> back to `generated`;
    * `generating` + live/unknown generation task -> keep;
    * a lost answer handoff (generation task failed/cancelled and its request
      carries the answer provenance) -> restore `paused`;
    * otherwise drop the session (its leg produced nothing and nothing lives).
    """
    dropped: list[str] = []
    restored: list[str] = []
    kept: list[str] = []
    db = _sessions()
    try:
        rows = db.query(PreDeploySession).all()
        for row in rows:
            # Instance wins — even over an operator-held `generated` row (R13).
            if _instance_status(row.service_id) is not None:
                dropped.append(str(row.service_id))
                db.delete(row)
                continue
            if row.state in OPERATOR_HELD_STATES or row.state == STATE_FAILED:
                kept.append(str(row.service_id))
                continue
            if row.state == STATE_DEPLOYING:
                if _instance_status(row.service_id) is not None:
                    dropped.append(str(row.service_id))
                    db.delete(row)
                    continue
                task = _task_row(row.deploy_task_id)
                if task is not None and task["status"] in ("queued", "running", "unknown"):
                    kept.append(str(row.service_id))
                    continue
                row.state = STATE_GENERATED
                row.detail = "reconciled: no instance and no live deploy task"
                row.updated_at = _now()
                restored.append(str(row.service_id))
                continue
            task = _task_row(row.generation_task_id)
            if task is not None and task["status"] in ("queued", "running", "unknown"):
                kept.append(str(row.service_id))
                continue
            request = task["request"] if task is not None else None
            if isinstance(request, dict) and request.get("_answer_parent") \
                    and task and task["status"] in ("failed", "cancelled"):
                row.state = STATE_PAUSED
                row.updated_at = _now()
                restored.append(str(row.service_id))
                continue
            dropped.append(str(row.service_id))
            db.delete(row)
        if dropped or restored:
            db.commit()
        remaining = db.query(PreDeploySession).count()
    finally:
        db.close()
    # A dropped record releases its label reservation (R5); the reconcile is
    # another session-removal path, so it owes the same cleanup.
    for service_id in dropped:
        try:
            from . import label_reservations
            label_reservations.release(service_id)
        except Exception:  # noqa: BLE001
            pass
    return {"dropped_stale": dropped, "restored": restored,
            "kept": kept, "remaining": remaining}


# ---------------------------------------------------------------------------
# Refusal options
# ---------------------------------------------------------------------------

def record_refusal(*, service_id: str, reason: str, detail: str = "",
                   request: dict[str, Any] | None = None,
                   use_global_proxy: bool = False) -> dict[str, Any] | None:
    """Record a refusal (non-secret options + encrypted credentials, R3.6)."""
    req = request or {}
    return mark_refused(
        service_id=service_id, reason=reason, detail=detail,
        options=refusal_options(req, use_global_proxy=use_global_proxy),
        secret_values=secret_option_values(req),
    )


def replay_options(service_id: str) -> dict[str, Any] | None:
    """The retry payload: stored non-secret options + decrypted credentials."""
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter_by(service_id=service_id).first()
        if row is None:
            return None
        options = dict(row.options or {})
        try:
            options.update(_decrypt_secret(row.options_secret_enc))
        except Exception:  # noqa: BLE001 — a missing/wrong key fails closed
            pass
        return options
    finally:
        db.close()


def refusal_options(request: dict[str, Any], *, use_global_proxy: bool) -> dict[str, Any]:
    """The exact deploy payload Retry replays, minus credential values."""
    cleaned = {k: v for k, v in request.items()
               if k not in ("passwd", "passwd_plain", "password", "privkey")}
    return {**cleaned, "use_global_proxy": bool(use_global_proxy)}


def migrate_legacy_json() -> int:
    """Retired: the one-way import (T8) owns legacy JSON migration."""
    return 0

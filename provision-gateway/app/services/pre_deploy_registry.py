"""Pre-deploy sessions — the gateway's own registry (doc §8b).

G10 restated, not bent: **registered instances live in the -api's registry;
pre-deploy sessions live here.** A session is a generation/deploy that has not
reached the -api yet, so writing it into the -api's registry would make it look
registered — reconciliation and the status scans would pick it up as an instance
that does not exist.

Storage is the gateway's SQLite table `pre_deploy_sessions` (GRILLING G31). It
used to be a JSON file with an in-process lock, which four uvicorn workers
cannot share — see the model's docstring for the lost update that caused.

Handoff order is **remove then add** (doc §8b): when the deploy is passed to the
-api the session is removed FIRST and the -api then adds its entry. The other
order leaves the same service with two cards.

`reconcile` closes the two-write gap:
- a session whose key already exists in the -api registry ⇒ the registration
  succeeded ⇒ drop the session;
- a session with no live job and no registry entry ⇒ stale ⇒ drop —
  **except** a session that is waiting for the operator (`refused` or `paused`),
  which is dropped only by explicit operator action.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..models.pre_deploy_session import PreDeploySession


def _sessions():
    """A short-lived session, resolved AT CALL TIME.

    `from ..database import SessionLocal` at module level binds the object at
    import, so a caller that swaps `database.SessionLocal` (the test suite, and
    any future multi-DB setup) keeps talking to the original engine. Every other
    service in this codebase imports it inside the function for exactly this
    reason — this module matches them.
    """
    from ..database import SessionLocal
    return SessionLocal()


def _legacy_store_path() -> Path:
    base = os.environ.get("GATEWAY_DATA_DIR") or "/srv/provision_subnet_acl/gateway_data"
    return Path(base) / "pre-deploy-registry.json"


def _key(user: str, service: str, recipe_path: str, label: str) -> str:
    return f"{user}|{service}|{recipe_path or ''}|{str(label)}"


def _as_text(value: Any) -> str:
    """The `detail` column is Text; callers may hand it a dict.

    The old JSON store accepted any object, and the refusal path does exactly
    that when the -api's 503 arrives in its NESTED shape: `as_http_error` puts
    the inner mapping under `detail`, so `detail.get("detail")` is a dict. A
    typed column cannot take it — and because the router wraps the record in
    `except Exception: pass`, the failure was silent and the refusal vanished.
    Coerce here so the store is robust to every caller shape, the way the file
    was.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("detail") or value.get("message")
        if isinstance(inner, str):
            return inner
    return json.dumps(value, ensure_ascii=False)


def _fields(*, user: str, service: str, recipe_path: str, label: str) -> dict[str, Any]:
    return {
        "key": _key(user, service, recipe_path, label),
        "user_name": user,
        "service_name": service,
        "recipe_path": recipe_path or "",
        "label": str(label),
    }


# ---------------------------------------------------------------------------
# The one-time import of the legacy JSON store
# ---------------------------------------------------------------------------

_IMPORTED = False


def migrate_legacy_json() -> int:
    """Import `pre-deploy-registry.json` into the table, once, then retire it.

    Called after ``init_db``. Idempotent and safe to call repeatedly: the flag
    short-circuits, and the file is renamed once imported so a second call (or a
    second worker) has nothing to read. Importing — rather than starting empty —
    is what keeps a live session (a refusal the operator has not acted on yet)
    from vanishing when the store moves.
    """
    global _IMPORTED
    if _IMPORTED:
        return 0
    _IMPORTED = True

    path = _legacy_store_path()
    if not path.is_file():
        return 0
    try:
        rows = json.loads(path.read_text() or "[]")
    except (OSError, ValueError):
        rows = []
    if not isinstance(rows, list):
        rows = []

    imported = 0
    db = _sessions()
    try:
        for row in rows:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            if db.query(PreDeploySession).filter(PreDeploySession.key == row["key"]).first():
                continue
            db.add(PreDeploySession(
                key=row["key"],
                user_name=row.get("user_name") or "",
                service_name=row.get("service_name") or "",
                recipe_path=row.get("recipe_path") or "",
                label=str(row.get("label", "0")),
                state=row.get("state") or "generating",
                reason=row.get("reason") or "",
                detail=row.get("detail") or "",
                job_id=row.get("job_id"),
                guide=row.get("guide") or "",
                task_id=row.get("task_id") or "",
                slot=row.get("slot") or "",
                use_global_proxy=bool(row.get("use_global_proxy")),
                options=json.dumps(row["options"], ensure_ascii=False) if row.get("options") else None,
            ))
            imported += 1
        db.commit()
    finally:
        db.close()

    # Retire the file so nothing reads it again (and so a rollback is possible
    # by hand — the data is still there, just renamed).
    try:
        path.rename(path.with_name(path.name + ".migrated"))
    except OSError:
        pass
    return imported


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_sessions() -> list[dict[str, Any]]:
    db = _sessions()
    try:
        rows = db.query(PreDeploySession).order_by(PreDeploySession.updated_at).all()
        return [r.to_dict() for r in rows]
    finally:
        db.close()


def get_session(user: str, service: str, recipe_path: str, label: str) -> dict[str, Any] | None:
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter(
            PreDeploySession.key == _key(user, service, recipe_path, label)
        ).first()
        return row.to_dict() if row else None
    finally:
        db.close()


def is_reserved(user: str, service: str, recipe_path: str, label: str) -> bool:
    """The combination is reserved while a session exists (doc §8b).

    A new generation/deploy for the same (user, service, label) is refused until
    the operator checks, cancels or deletes the existing session.
    """
    db = _sessions()
    try:
        return db.query(PreDeploySession).filter(
            PreDeploySession.key == _key(user, service, recipe_path, label)
        ).count() > 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_session(
    *, user: str, service: str, recipe_path: str, label: str,
    state: str, job_id: int | None = None, reason: str = "",
    guide: str = "", task_id: str = "", slot: str = "",
    use_global_proxy: bool = False,
) -> dict[str, Any]:
    """Record a pre-deploy session (state: `generating` | `paused`), replacing
    any row already held for the same combination.

    ``guide`` and ``task_id`` belong to the LLM-fix pause (GRILLING G26); they
    are cleared on every write that does not pass them, so a session can never
    carry a stale guide from a different pause kind.
    """
    fields = _fields(user=user, service=service, recipe_path=recipe_path, label=label)
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter(PreDeploySession.key == fields["key"]).first()
        if row is None:
            row = PreDeploySession(**fields)
            db.add(row)
        row.state = state
        row.reason = reason
        row.job_id = job_id
        row.guide = guide or ""
        row.task_id = task_id or ""
        row.slot = slot or ""
        row.use_global_proxy = bool(use_global_proxy)
        # A fresh session is not a refusal: `detail`/`options` belong to the
        # state it is replacing, and carrying them over would leave a live
        # session advertising a stale refusal payload to Retry.
        row.detail = ""
        row.options = None
        db.commit()
        db.refresh(row)
        return row.to_dict()
    finally:
        db.close()


def remove_session(user: str, service: str, recipe_path: str, label: str) -> bool:
    db = _sessions()
    try:
        n = db.query(PreDeploySession).filter(
            PreDeploySession.key == _key(user, service, recipe_path, label)
        ).delete(synchronize_session=False)
        db.commit()
        return bool(n)
    finally:
        db.close()


def set_state_for_job(job_id: int, state: str, reason: str | None = None) -> bool:
    """Mirror a job's state onto the session that owns it.

    Replaces the private reach-through that used to live in
    ``generation_jobs._set_status`` (``_pdr.list_sessions()`` → mutate a dict →
    ``_pdr._save(rows)``): that pattern rewrote the whole file from four
    processes, which is the lost update G31 removes.
    """
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter(PreDeploySession.job_id == job_id).first()
        if row is None:
            return False
        row.state = state
        if reason is not None:
            row.reason = reason
        db.commit()
        return True
    finally:
        db.close()


def _with_all_three_paths(options: dict[str, Any]) -> dict[str, Any]:
    """G20: a refused deployment **always** carries all three paths.

    The reason is the RETRY panel, not tidiness. Retry opens the deploy panel
    pre-filled with these options, and the pickers have **no `None` option** — a
    slot is either an existing file or `generate new`. So a record missing the
    nginx path seeds that picker at `generate new`, and the retry re-runs the
    nginx phase instead of reusing the file the refused attempt already
    generated and sent (G20's clarification, 2026-09-20).

    Enforced HERE rather than at the caller so the invariant holds for every
    refusal path: `options` used to be a verbatim copy of the request, and only
    the request's shape kept nginx in it. The nginx path is derived the way the
    deploy path already derives compose (`users.py`): the explicit key first,
    else the selection the deploy carried.
    """
    if options.get("nginx_conf_file_path"):
        return options
    sel = options.get("selection")
    nginx = sel.get("nginx") if isinstance(sel, dict) else None
    return {**options, "nginx_conf_file_path": nginx} if nginx else options


def refusal_options(request: dict[str, Any], *, use_global_proxy: bool) -> dict[str, Any]:
    """Everything a `refused` record KEEPS from the request that was refused.

    User ruling, 2026-09-26 (a correction to the first reading of the same
    conversation, which had blanked the URL and the password): *"for the generate
    path which is part of a deploy path, it indeed sees the password, and for
    supporting retry, the password / url should indeed be stored!"*

    That is the point of the record: its Retry must replay the deployment the
    operator confirmed — the **file selection**, the **URL** (`domain`), the
    **HTTPS** flag, the **password** and the **proxy** toggle, all of them. A
    blanked field is not a safer field, it is a broken retry: the operator would
    silently re-deploy with a different domain, or a password nothing set.

    The one case with nothing to store is the **standalone Source Projects
    Generate** panel: it sends no `user_name`, so no session is recorded for it at
    all (G15) — it has no card and no Retry to feed.

    Both refusal entry points go through here so the record cannot mean two
    different things.
    """
    return {**dict(request), "use_global_proxy": bool(use_global_proxy)}


def mark_refused(*, user: str, service: str, recipe_path: str, label: str,
                 reason: str, detail: str = "",
                 options: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Record that this combination was REFUSED, on the gateway's own record.

    A capacity refusal happens in the -api's `submit()`, before any task runs
    and before any registry entry exists — so there is no instance to carry the
    reason. But a pre-deploy record already exists for the combination (the
    deploy path created one), and THAT is the card the operator sees. Recording
    it here is what makes the refusal visible with its remedy, rather than a
    transient toast the operator may miss.
    """
    fields = _fields(user=user, service=service, recipe_path=recipe_path, label=label)
    db = _sessions()
    try:
        row = db.query(PreDeploySession).filter(PreDeploySession.key == fields["key"]).first()
        if row is None:
            row = PreDeploySession(**fields)
            db.add(row)
        row.state = "refused"
        row.reason = reason
        row.detail = _as_text(detail)
        # The EXACT deploy options, so Retry can replay the deployment verbatim:
        # no re-selection, no generation pipeline. Any files the rejected
        # attempt generated are reused as-is, and the URL and the credential ride
        # along — a retry that has to guess either is a different deployment
        # (`refusal_options` is the one place that builds this; both callers use it).
        if options is not None:
            row.options = json.dumps(_with_all_three_paths(options), ensure_ascii=False)
        db.commit()
        db.refresh(row)
        return row.to_dict()
    finally:
        db.close()


def reconcile(registered_keys: set[str], live_job_ids: set[int]) -> dict[str, Any]:
    """Drop the sessions nothing is working on any more.

    ``registered_keys`` are the (user, service, recipe_path, label) keys present
    in the -api registry; ``live_job_ids`` are jobs the gateway still considers
    live. Returns a report so a caller can log what it removed and why.
    """
    dropped_registered: list[str] = []
    dropped_stale: list[str] = []
    db = _sessions()
    try:
        rows = db.query(PreDeploySession).all()
        for row in rows:
            # A session WAITING FOR THE OPERATOR is never stale, whatever the job
            # and registry say — `refused` (the deploy was refused before any
            # task ran, so it has no job by construction and may share a key
            # with an older registration) and `paused` (the G26 handoff writes
            # the session and then deletes the -api entry, so it has no registry
            # entry by construction and its job has already completed).
            #
            # Dropping either destroys the operator's only record of it — the
            # refusal's Retry options, or the fixing guide and the task id that
            # make `check` reachable. Found live in Round 1 for `refused`; the
            # same rule was then needed for `paused`.
            if row.state in ("refused", "paused"):
                continue
            if row.key in registered_keys:
                # The registration succeeded → the session is done.
                dropped_registered.append(row.key)
                db.delete(row)
                continue
            if row.job_id is not None and int(row.job_id) in live_job_ids:
                continue
            # No live job and no registry entry ⇒ nothing is working on it.
            dropped_stale.append(row.key)
            db.delete(row)
        if dropped_registered or dropped_stale:
            db.commit()
        remaining = db.query(PreDeploySession).count()
    finally:
        db.close()
    return {
        "dropped_registered": dropped_registered,
        "dropped_stale": dropped_stale,
        "remaining": remaining,
    }

"""Gateway-owned deploy/generate workflow (R1–R6, R13, T2–T5).

The browser and the thin CLI only collect an intent and poll a read model. This
module owns the whole asynchronous path: validation -> label reservation ->
session (operation, deploy request, selection) -> generation -> pause ->
finalization -> claim -> register -> follow-ups. The -api is called only through
its existing register/task endpoints; no registry write ever happens here.

Two intake operations share ONE pre-deploy record:

* ``deploy`` — run generation if needed, finalize, claim ``generated ->
  deploying`` and register, then delete the session and release the label.
* ``generate`` — run generation and stop at the ``generated`` review gate; a
  later Deploy updates the SAME record to ``deploy`` and continues at the claim.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..database import SessionLocal
from . import artifact_finalize, label_reservations, pre_deploy_registry as pdr
from .generation_jobs import GenerationCapacityError, GenerationJobError, generation_jobs

_log = logging.getLogger("gateway.deploy_workflow")

# Fields that exist only on the gateway side of the intent; never forwarded to
# the -api register contract. `_actor_user_id` is kept in the stored request
# (the follow-up grants need it) and stripped by `_api_payload`.
_GATEWAY_ONLY = {
    "selection", "prompt", "env_reference", "allowed_target_service_ids",
    "use_global_proxy", "fix", "job_type", "owner_username",
    "skip_env", "category", "service_value", "_actor_user_id",
}
# The replayable deploy request keeps the gateway-only fields the continuation
# needs (`allowed_target_service_ids`, `use_global_proxy`, `_actor_user_id`);
# only the generation-only context is dropped.
_RECORD_STRIP = {
    "selection", "prompt", "env_reference", "fix", "job_type", "owner_username",
    "skip_env", "category", "service_value",
}

LIVE_JOB_STATUSES = ("queued", "running", "unknown")


class WorkflowError(ValueError):
    """A structured refusal (the router maps it to a 4xx/503)."""

    def __init__(self, code: str, detail: str, status: int = 422):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _now():
    return datetime.now(timezone.utc)


def _https(intent: dict) -> bool:
    return bool(intent.get("tls") if intent.get("tls") is not None else intent.get("https"))


def _selection(intent: dict) -> dict:
    sel = intent.get("selection")
    if isinstance(sel, dict):
        return sel
    # Compatibility: derive the selection from the -api-shaped intent fields.
    return {
        "compose": intent.get("compose_file_paths") or [],
        "nginx": (intent.get("nginx_confs") or {}).get("http"),
        "stream_conf": (intent.get("nginx_confs") or {}).get("stream"),
        "mail_conf": (intent.get("nginx_confs") or {}).get("mail"),
        "env": intent.get("env_files") or [],
        "profiles": intent.get("profiles") or [],
        "serving_ports": intent.get("serving_ports"),
        "serving_ports_none": bool(intent.get("serving_ports_none")),
    }


def _recipe_dir(service_name: str, recipe_path: str) -> Path:
    from .file_sets import _recipe_dir as resolve
    return resolve(Path(settings.SOURCE_PROJECTS_DIR) / service_name, recipe_path)


def _validate_selected_files(intent: dict, selection: dict) -> None:
    """R6: every SELECTED file must exist inside the recipe (traversal-safe).

    A slot left at "generate new" is empty here; generation supplies it.
    """
    from .file_sets import FileSetError, _validate_paths

    service_name = intent.get("service_name") or ""
    recipe_path = intent.get("recipe_path") or ""
    if not service_name:
        raise WorkflowError("invalid_intent", "'service_name' is required")
    project_dir = Path(settings.SOURCE_PROJECTS_DIR) / service_name
    try:
        target = _recipe_dir(service_name, recipe_path)
    except FileSetError as exc:
        raise WorkflowError("invalid_recipe_path", str(exc)) from exc

    names: list[str] = [n for n in (selection.get("compose") or []) if isinstance(n, str)]
    for key in ("nginx", "stream_conf", "mail_conf", "serving_ports"):
        if isinstance(selection.get(key), str) and selection[key]:
            names.append(selection[key])
    names.extend(n for n in (selection.get("env") or []) if isinstance(n, str))
    if not names:
        return
    try:
        checked = _validate_paths(project_dir, recipe_path, names)
    except FileSetError as exc:
        raise WorkflowError("invalid_path", str(exc)) from exc
    missing = [name for name in checked if not (target / name).is_file()]
    if missing:
        raise WorkflowError(
            "selected_file_missing",
            f"selected file(s) do not exist in the recipe: {', '.join(missing)}",
        )


def _validate_policy(intent: dict, selection: dict, *, require_ready: bool) -> None:
    """R6. `require_ready` is True once generation has produced the artifacts."""
    https = _https(intent)
    nginx = selection.get("nginx")
    stream = selection.get("stream_conf")
    if require_ready and not nginx:
        raise WorkflowError(
            "nginx_required",
            "an HTTP nginx conf is required — an instance without one deploys unroutable",
        )
    if stream and not https:
        raise WorkflowError(
            "stream_requires_tls",
            "a stream conf requires TLS; send it only with TLS enabled",
        )
    # Profiles must exist in the selected compose (the -api would tolerate an
    # unknown profile; the gateway refuses it before any -api call).
    profiles = selection.get("profiles") or []
    if profiles and not selection.get("compose"):
        raise WorkflowError(
            "profiles_without_compose",
            "profiles require a selected or generated compose",
        )
    if not require_ready and not selection.get("compose") and not intent.get("prompt"):
        # A deploy with nothing selected and no prompt has no path to a compose.
        raise WorkflowError(
            "compose_required",
            "a compose must be selected or a prompt supplied for generation",
        )


def generation_needed(intent: dict) -> bool:
    """The gateway decides, never the client (R1)."""
    selection = _selection(intent)
    missing = generation_jobs._recipe_missing({
        "service_name": intent.get("service_name") or "",
        "recipe_path": intent.get("recipe_path") or "",
    })
    if missing:
        return True
    if not (selection.get("compose") or []):
        return True
    if not selection.get("nginx"):
        return True
    # A slot left at "generate new" is a generation reason, exactly as the old
    # panel treated it (G13/G20): the disk may already hold some artifact, but
    # the operator asked for a new one. `serving_ports_none` is the deliberate
    # no-file state and is honoured.
    if not selection.get("serving_ports") and not selection.get("serving_ports_none"):
        return True
    if _https(intent) and not selection.get("stream_conf"):
        return True
    # The per-user env.raw is generated when no env is selected (R3/R6).
    if not (selection.get("env") or []):
        return True
    return False


def _generation_request(intent: dict, selection: dict) -> dict:
    # Generation is profile-BLIND: profiles are deploy-time options, not
    # generation context. They travel on the intent and are merged back into
    # the final selection before the register payload is built.
    gen_selection = dict(selection)
    gen_selection.pop("profiles", None)
    return {
        "service_name": intent.get("service_name"),
        "recipe_path": intent.get("recipe_path") or "",
        "job_type": "generate_missing",
        "prompt": intent.get("prompt") or "",
        "env_reference": intent.get("env_reference") or [],
        "selection": gen_selection,
        "user_name": intent.get("user_name") or intent.get("owner_username") or "",
        "label": str(intent.get("label") or "0"),
        # The deploy metadata is generation context (URL and credential the
        # refusal/retry record needs); credentials are redacted in the job row.
        "deploy_metadata": {
            "service_name": intent.get("service_name"),
            "user_name": intent.get("user_name") or intent.get("owner_username") or "",
            "label": str(intent.get("label") or "0"),
            "domain": intent.get("domain") or "localhost",
            "https": _https(intent),
            "passwd": intent.get("passwd") or "",
        },
        "use_global_proxy": bool(intent.get("use_global_proxy")),
        "service_id": intent.get("service_id"),
        "owner_api_user_id": intent.get("owner_api_user_id"),
        **({"fix": True} if intent.get("fix") else {}),
    }


def _api_payload(intent: dict, selection: dict, env_name: str | None) -> dict:
    """The existing -api register contract, assembled server-side (R2)."""
    payload = {k: v for k, v in intent.items()
               if k not in _GATEWAY_ONLY and not k.startswith("_")}
    payload.pop("tls", None)
    payload["tls"] = _https(intent)
    for key in ("compose_file_path", "compose_template_path"):
        payload.pop(key, None)
    payload["compose_file_paths"] = list(selection.get("compose") or [])

    nginx: dict[str, str] = {}
    if selection.get("nginx"):
        nginx["http"] = selection["nginx"]
    if _https(intent) and selection.get("stream_conf"):
        nginx["stream"] = selection["stream_conf"]
    if selection.get("mail_conf"):
        nginx["mail"] = selection["mail_conf"]
    if nginx:
        payload["nginx_confs"] = nginx
    else:
        payload.pop("nginx_confs", None)

    # The interpolation env handoff is ONLY the generated per-user env.raw; the
    # selected env is generation context and is never forwarded (design §2).
    if env_name:
        payload["env_files"] = [env_name]
    else:
        payload.pop("env_files", None)
    payload.pop("env_file_path", None)

    payload["profiles"] = list(selection.get("profiles") or [])
    if not payload["profiles"]:
        payload.pop("profiles", None)
    return payload


def _record_deploy_intent(intent: dict) -> tuple[dict, dict]:
    """Split the deploy intent into its non-secret half and encrypted secrets."""
    secrets = pdr.secret_option_values(intent)
    request = {k: v for k, v in intent.items()
               if k not in pdr.SECRET_OPTION_KEYS and k not in _RECORD_STRIP}
    return request, secrets


async def start_deploy(intent: dict, *, actor_user_id=None) -> dict:
    """`POST /api/services/deploy` — intake for a deploy workflow (R1)."""
    return await _start(intent, operation=pdr.OPERATION_DEPLOY, actor_user_id=actor_user_id)


async def start_generate(intent: dict) -> dict:
    """`POST /api/services/generate` — intake for a generate workflow (R1)."""
    return await _start(intent, operation=pdr.OPERATION_GENERATE)


async def _start(intent: dict, *, operation: str, actor_user_id=None) -> dict:
    if not intent.get("service_name"):
        raise WorkflowError("invalid_intent", "'service_name' is required")
    intent = dict(intent)
    intent.setdefault("user_name", intent.get("owner_username") or "")
    intent.setdefault("recipe_path", "")
    if actor_user_id is not None:
        intent["_actor_user_id"] = str(actor_user_id)

    selection = _selection(intent)
    _validate_selected_files(intent, selection)
    _validate_policy(intent, selection, require_ready=False)

    owner_id = intent.get("owner_api_user_id")
    if not owner_id:
        raise WorkflowError("invalid_intent", "'owner_api_user_id' is required")

    # An existing session for this tuple carries the identity across the
    # asynchronous lifetime (a reviewed Generate -> Deploy, or a retry).
    existing = None
    requested = intent.get("label")
    if requested not in (None, "", "auto"):
        existing = pdr.find_by_tuple(owner_id, intent["service_name"], str(requested))
    if existing is not None:
        # The record's identity is the tuple's identity: never renumber it.
        requested = existing.get("label") or requested

    service_id = intent.get("service_id") or (
        existing["service_id"] if existing else str(uuid.uuid4()))
    procedure_id = intent.get("procedure_id") or (
        existing["procedure_id"] if existing else str(uuid.uuid4()))
    intent["service_id"] = service_id
    intent["procedure_id"] = procedure_id

    if existing and existing.get("state") == pdr.STATE_PAUSED:
        # A GUIDED RESTART (fix=true) is the one deploy intent allowed on a
        # paused record: the LLM-fix analysis left it paused with the guide,
        # and the operator's confirmation restarts the generation with the
        # fix naming, then continues to deploy (R4).
        if not intent.get("fix"):
            raise WorkflowError(
                "workflow_paused",
                "the workflow is paused — answer the pending pause before deploying",
                409,
            )
    if existing and existing.get("state") == pdr.STATE_GENERATING:
        raise WorkflowError(
            "workflow_busy", "the workflow is already generating", 409)

    # Reviewed-session Deploy: files exist, the record already carries them.
    # A refused DEPLOY (capacity) takes the same branch — the panel's Retry
    # replays the stored request without a generation run.
    if existing and existing.get("state") in (
            pdr.STATE_GENERATED, pdr.STATE_FAILED, pdr.STATE_REFUSED) \
            and operation == pdr.OPERATION_DEPLOY \
            and (not generation_needed(intent) or _session_ready(existing, intent)):
        seeded = _seed_from_session(existing, selection)
        label = _reserve(
            owner_api_user_id=owner_id, service_name=intent["service_name"],
            service_id=service_id, requested_label=str(requested),
        )
        deploy_request, deploy_secrets = _record_deploy_intent(intent)
        pdr.create_or_replace(
            service_id=service_id, procedure_id=procedure_id,
            owner_api_user_id=owner_id, service_name=intent["service_name"],
            service_label=label, recipe_path=intent["recipe_path"],
            state=pdr.STATE_GENERATED, operation=pdr.OPERATION_DEPLOY,
            deploy_request=deploy_request, deploy_secret_values=deploy_secrets,
            selection=seeded,
        )
        return await _claim_and_register(
            service_id, intent, seeded, existing.get("env_name"))

    # Reserve the identity under the tuple lock (R5).
    label = _reserve(
        owner_api_user_id=owner_id, service_name=intent["service_name"],
        service_id=service_id, requested_label=str(requested),
    )
    intent["label"] = label

    need_generation = generation_needed(intent)
    initial_state = pdr.STATE_GENERATING if need_generation else pdr.STATE_GENERATED
    deploy_request, deploy_secrets = (
        _record_deploy_intent(intent) if operation == pdr.OPERATION_DEPLOY else (None, None)
    )
    pdr.create_or_replace(
        service_id=service_id, procedure_id=procedure_id,
        owner_api_user_id=owner_id, service_name=intent["service_name"],
        service_label=label, recipe_path=intent["recipe_path"],
        state=initial_state, operation=operation,
        deploy_request=deploy_request, deploy_secret_values=deploy_secrets,
        selection=selection, env_name=None,
    )

    if not need_generation:
        if operation == pdr.OPERATION_GENERATE:
            pdr.set_selection(service_id, selection)
            return _handle(service_id, intent, label, pdr.STATE_GENERATED,
                           operation=operation)
        return await _claim_and_register(service_id, intent, selection, None)

    # Generation is needed: the gateway starts the leg itself (R1/R2).
    db = SessionLocal()
    try:
        try:
            job = generation_jobs.create_job(
                db, intent["service_name"], intent["recipe_path"],
                "generate_missing", _generation_request(intent, selection),
            )
        except GenerationCapacityError as exc:
            pdr.record_refusal(
                service_id=service_id, reason=exc.code, detail=str(exc),
                request=_generation_request(intent, selection),
                use_global_proxy=bool(intent.get("use_global_proxy")),
            )
            raise WorkflowError(exc.code, str(exc), 503) from exc
        except GenerationJobError as exc:
            pdr.remove_session(service_id)
            label_reservations.release(service_id)
            raise WorkflowError("generation_rejected", str(exc), 422) from exc
        # The leg must be linked to the record it will continue.
        pdr.create_or_replace(
            service_id=service_id, procedure_id=job.procedure_id,
            owner_api_user_id=owner_id, service_name=intent["service_name"],
            service_label=label, recipe_path=intent["recipe_path"],
            state=pdr.STATE_GENERATING, generation_task_id=job.task_id,
            operation=operation,
        )
        return _handle(service_id, intent, label, pdr.STATE_GENERATING,
                       generation_task_id=job.task_id, operation=operation)
    finally:
        db.close()


def _seed_from_session(existing: dict, selection: dict) -> dict:
    """A reviewed session's stored selection wins over a bare re-post."""
    stored = existing.get("selection") if isinstance(existing.get("selection"), dict) else None
    return stored or selection


def _reserve(*, owner_api_user_id, service_name: str, service_id: str,
             requested_label: str | None) -> str:
    """Reserve, mapping the pinned-label conflict to a structured 409."""
    try:
        return label_reservations.reserve(
            owner_api_user_id=owner_api_user_id, service_name=service_name,
            service_id=service_id, requested_label=requested_label,
        )
    except label_reservations.LabelConflict as exc:
        raise WorkflowError("label_taken", str(exc), 409) from exc


def _session_ready(existing: dict, intent: dict) -> bool:
    """A reviewed record is deployable as-is: finalize already ran (R2).

    The panel's posted selection may still show `env: []` / a `generate new`
    slot, but the RECORD holds the generated artifacts and the env name. The
    stored record is the authority for a reviewed session.
    """
    selection = existing.get("selection") if isinstance(existing.get("selection"), dict) else {}
    if not (selection.get("compose") and selection.get("nginx")):
        return False
    if not selection.get("serving_ports") and not selection.get("serving_ports_none"):
        return False
    if _https(intent) and not selection.get("stream_conf"):
        return False
    if not (selection.get("env") or existing.get("env_name")):
        return False
    return True


def _handle(service_id: str, intent: dict, label: str, stage: str,
            generation_task_id: str | None = None,
            operation: str = pdr.OPERATION_DEPLOY) -> dict:
    return {
        "workflow_id": service_id,
        "service_id": service_id,
        "procedure_id": intent.get("procedure_id"),
        "user_name": intent.get("user_name") or "",
        "service_name": intent.get("service_name"),
        "label": label,
        "recipe_path": intent.get("recipe_path") or "",
        "operation": operation,
        "stage": stage,
        "generation_task_id": generation_task_id,
    }


async def _claim_and_register(service_id: str, intent: dict,
                              selection: dict,
                              env_name: str | None) -> dict:
    """Claim `generated -> deploying` and call the existing -api register."""
    record = pdr.session_record(service_id)
    if record is None:
        raise WorkflowError("session_missing", "the workflow record is gone", 409)
    if not pdr.claim_for_deploy(service_id):
        # Another completer owns it; a no-op, never a second deploy.
        return _handle(service_id, intent, record.get("label") or "", "deploying")

    resolved = dict(record.get("deploy_request") or {})
    resolved.update(record.get("deploy_secrets") or {})
    resolved.update(intent)
    resolved.update({
        "owner_api_user_id": record["owner_api_user_id"],
        "service_id": service_id,
        "procedure_id": record["procedure_id"],
        "service_name": record["service_name"],
        "recipe_path": record["recipe_path"],
        "label": record["label"],
    })
    payload = _api_payload(resolved, selection, env_name)
    from .provision_service import provision_service
    attempt_label = record["label"]
    result = None
    for attempt in range(2):
        payload["label"] = attempt_label
        try:
            result = await provision_service.register_user(**payload)
            break
        except Exception as exc:  # noqa: BLE001
            http_exc = provision_service.as_http_error(exc)
            # A concurrent external registration can win the label race after
            # our reservation. Rebind to the next free label and retry ONCE;
            # the identity is never the wrong one (R5).
            if getattr(http_exc, "status_code", None) == 409 and attempt == 0:
                candidate = label_reservations.reserve(
                    owner_api_user_id=record["owner_api_user_id"],
                    service_name=record["service_name"], service_id=service_id)
                if candidate != attempt_label:
                    label_reservations.rebind(service_id=service_id, label=candidate)
                    pdr.set_label(service_id, candidate)
                    attempt_label = candidate
                    continue
            detail = str(getattr(http_exc, "detail", None) or exc)
            at_capacity = (
                getattr(http_exc, "status_code", None) == 503
                and isinstance(getattr(http_exc, "detail", None), dict)
                and http_exc.detail.get("error") == "at_capacity"
            )
            if at_capacity:
                pdr.record_refusal(
                    service_id=service_id, reason="at_capacity",
                    detail=str(http_exc.detail.get("detail") or ""),
                    request=payload,
                    use_global_proxy=bool(resolved.get("use_global_proxy")),
                )
                raise WorkflowError("at_capacity", str(http_exc.detail.get("detail") or ""), 503) from exc
            pdr.mark_deploy_failed(service_id, detail)
            label_reservations.release(service_id)
            raise WorkflowError("deploy_failed", detail, 502) from exc
    if result is None:
        raise WorkflowError("deploy_failed", "the register call did not complete", 502)

    task_id = result.get("task_id")
    pdr.set_deploying(service_id, deploy_task_id=task_id)
    # Add-then-remove: the -api has the instance (202) before the record goes.
    pdr.remove_session(service_id)
    label_reservations.release(service_id)
    target_ids = [str(t) for t in (resolved.get("allowed_target_service_ids") or []) if t]
    actor_id = resolved.get("_actor_user_id") or record.get("_actor_user_id")
    asyncio.create_task(_after_deploy(service_id, task_id, target_ids, actor_id))
    return {"workflow_id": service_id, "service_id": service_id,
            "procedure_id": record["procedure_id"],
            "user_name": record.get("user_name"), "service_name": record["service_name"],
            "label": record["label"], "recipe_path": record["recipe_path"],
            "operation": pdr.OPERATION_DEPLOY, "stage": "deploying",
            "deploy_task_id": task_id, "task_id": task_id}


async def _after_deploy(source_service_id: str, task_id: str | None,
                        target_ids: list[str], actor_id) -> None:
    """Grants after the task, then the cross-path sync (existing calls)."""
    from . import service_grants
    from .cross_orchestrator import cross_orchestrator
    from .provision_service import provision_service
    if not task_id:
        return
    try:
        while True:
            data = await provision_service.get_task(task_id)
            status = str(data.get("status") or "")
            if status in ("completed", "failed", "cancelled", "unknown"):
                break
            await asyncio.sleep(2.0)
        if status == "completed" and target_ids:
            db = SessionLocal()
            try:
                for target_id in target_ids:
                    try:
                        service_grants.set_grant(
                            db, source_service_id, target_id,
                            actor_user_id=actor_id,
                        )
                    except ValueError as exc:
                        _log.warning("deploy-panel grant %s -> %s skipped: %s",
                                     source_service_id, target_id, exc)
            finally:
                db.close()
        await cross_orchestrator.sync_all()
    except Exception:  # noqa: BLE001 — the deploy already succeeded
        _log.exception("deploy follow-ups failed for %s", source_service_id)


async def on_generation_completed(*, service_id: str, request: dict,
                                  result: dict) -> None:
    """The server-owned continuation (R2): finalize, then deploy or stop."""
    record = pdr.session_record(service_id)
    if record is None:
        return
    # Only the leg that owns the record may continue it; a stale completion
    # (a cancelled/answered leg, a refused record) is a no-op.
    if record.get("state") not in (pdr.STATE_GENERATING, pdr.STATE_GENERATED):
        return
    stored_selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
    selection = dict(result.get("selection") or request.get("selection") or stored_selection)
    # Profiles were stripped from the generation request (profile-blind); they
    # are deploy-time options and must ride the final selection.
    if stored_selection.get("profiles") and not selection.get("profiles"):
        selection["profiles"] = stored_selection["profiles"]
    files = result.get("files") or {}
    env_name = result.get("env_name")
    intent = dict(record.get("deploy_request") or {})
    intent.update(record.get("deploy_secrets") or {})
    intent.update({
        "service_id": service_id,
        "procedure_id": record["procedure_id"],
        "owner_api_user_id": record["owner_api_user_id"],
        "user_name": record.get("user_name"),
        "service_name": record["service_name"],
        "recipe_path": record["recipe_path"],
    })

    # The generation pipeline may produce a stream conf even when TLS is off
    # (the slot exists for a later TLS deploy). A DEPLOY without TLS must not
    # carry one — the same rule the old panel applied — so drop it from the
    # final selection before the ready-check and the register payload.
    if not _https(intent) and selection.get("stream_conf"):
        selection = {**selection, "stream_conf": None}

    try:
        finalized = artifact_finalize.finalize_files(
            record["service_name"], record["recipe_path"], files, selection,
            persist_selection=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, never hand on nothing
        pdr.mark_deploy_failed(service_id, f"artifact finalization failed: {exc}")
        label_reservations.release(service_id)
        return

    final_selection = finalized["selection"] or selection
    pdr.set_selection(service_id, final_selection, env_name)

    if record.get("operation") != pdr.OPERATION_DEPLOY:
        # The review gate: the operator decides whether to deploy later.
        return

    try:
        _validate_policy(intent, final_selection, require_ready=True)
        await _claim_and_register(service_id, intent, final_selection, env_name)
    except WorkflowError as exc:
        # _claim_and_register already recorded a capacity refusal / failure;
        # a policy failure here is a fail-closed deploy failure.
        if exc.code not in ("at_capacity", "deploy_failed"):
            pdr.mark_deploy_failed(service_id, f"{exc.code}: {exc.detail}")
            label_reservations.release(service_id)


async def on_generation_failed(*, service_id: str, error: str) -> None:
    """Fail closed: the session is removed and the job exposes the failure."""
    pdr.remove_session(service_id)
    label_reservations.release(service_id)


async def retry(service_id: str) -> dict:
    """Retry a refusal or a reviewed session (R2/R5).

    A generation-capacity refusal re-runs generation; a deploy-capacity
    refusal (or a failed deploy) replays the stored register request; a
    `generated` deploy record is claimed now. A failed GENERATION leaves no
    session (C7), so it is not retryable here.
    """
    record = pdr.session_record(service_id)
    if record is None:
        raise WorkflowError("session_missing", "no pre-deploy session for that service", 404)
    state = record.get("state")
    reason = record.get("reason") or ""
    operation = record.get("operation") or pdr.OPERATION_DEPLOY
    if state == pdr.STATE_PAUSED:
        raise WorkflowError("workflow_paused", "answer the pending pause first", 409)
    if state not in (pdr.STATE_REFUSED, pdr.STATE_GENERATED, pdr.STATE_FAILED):
        raise WorkflowError("workflow_not_retryable", f"session is {state}", 409)
    if state == pdr.STATE_GENERATED and operation != pdr.OPERATION_DEPLOY:
        raise WorkflowError(
            "workflow_generated", "the workflow is at the review gate; deploy it instead", 409)

    selection = record.get("selection") or {}
    intent = dict(record.get("deploy_request") or {})
    intent.update(record.get("deploy_secrets") or {})
    intent.update({
        "owner_api_user_id": record["owner_api_user_id"],
        "service_id": service_id,
        "procedure_id": record["procedure_id"],
        "service_name": record["service_name"],
        "user_name": record.get("user_name"),
        "recipe_path": record.get("recipe_path") or "",
        "label": record.get("label"),
    })

    deploy_replay = (
        operation == pdr.OPERATION_DEPLOY and bool(intent) and (
            state == pdr.STATE_GENERATED or state == pdr.STATE_FAILED
            or reason == "at_capacity"
        )
    )
    if deploy_replay:
        pdr.create_or_replace(
            service_id=service_id, procedure_id=record["procedure_id"],
            owner_api_user_id=record["owner_api_user_id"],
            service_name=record["service_name"], service_label=record["label"],
            recipe_path=record.get("recipe_path") or "",
            state=pdr.STATE_GENERATED, operation=pdr.OPERATION_DEPLOY,
            selection=selection,
        )
        return await _claim_and_register(
            service_id, intent, selection, record.get("env_name"))

    generation_request = record.get("options") or _generation_request(intent, selection)
    db = SessionLocal()
    try:
        try:
            job = generation_jobs.create_job(
                db, record["service_name"], record.get("recipe_path") or "",
                "generate_missing", generation_request,
            )
        except GenerationCapacityError as exc:
            pdr.record_refusal(
                service_id=service_id, reason=exc.code, detail=str(exc),
                request=generation_request,
                use_global_proxy=bool(generation_request.get("use_global_proxy")),
            )
            raise WorkflowError(exc.code, str(exc), 503) from exc
        pdr.create_or_replace(
            service_id=service_id, procedure_id=job.procedure_id,
            owner_api_user_id=record["owner_api_user_id"],
            service_name=record["service_name"], service_label=record["label"],
            recipe_path=record.get("recipe_path") or "",
            state=pdr.STATE_GENERATING, generation_task_id=job.task_id,
            operation=operation,
        )
        return _handle(service_id, intent, record["label"], pdr.STATE_GENERATING,
                       generation_task_id=job.task_id, operation=operation)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

_STAGE_BY_SESSION = {
    pdr.STATE_GENERATING: "generating",
    pdr.STATE_PAUSED: "paused",
    pdr.STATE_REFUSED: "refused",
    pdr.STATE_GENERATED: "generated",
    pdr.STATE_DEPLOYING: "deploying",
    pdr.STATE_FAILED: "failed",
}


def _job_dict(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    db = SessionLocal()
    try:
        job = generation_jobs.get_job(db, job_id)
        return job.to_dict(include_result=True) if job else None
    finally:
        db.close()


def _latest_job_for_service(service_id: str) -> dict | None:
    from sqlalchemy import text
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT task_id FROM gateway.tasks WHERE service_id=:sid "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"sid": service_id},
        ).first()
        return _job_dict(row[0]) if row else None
    finally:
        db.close()


def _pause_of(job: dict | None) -> dict | None:
    if not job:
        return None
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    pause = (result or {}).get("pause")
    if isinstance(pause, dict):
        return pause
    pending = job.get("pending_input")
    return pending if isinstance(pending, dict) else None


def _job_live(task_id: str | None) -> bool:
    if not task_id:
        return False
    from sqlalchemy import text
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT status FROM gateway.tasks WHERE task_id=:id"),
            {"id": task_id},
        ).first()
        return bool(row and row[0] in LIVE_JOB_STATUSES)
    finally:
        db.close()


async def stages(user_name: str | None = None) -> dict:
    """`GET /api/services/stages` — the unified stage projection (R9).

    One entry per (user, service, label). The instance wins when both stores
    have the tuple; the client no longer joins them.
    """
    from .provision_service import provision_service
    try:
        payload = await provision_service.list_users()
    except Exception:  # noqa: BLE001 — the session half is still readable
        payload = {"user_status": []}

    sessions = {str(s["service_id"]): s for s in pdr.list_sessions()}
    groups_out: list[dict] = []
    used_sessions: set[str] = set()

    for group in payload.get("user_status") or []:
        uname = group.get("user_name")
        if user_name and uname != user_name:
            continue
        entries: list[dict] = []
        instance_keys: set[tuple] = set()
        for bucket in ("healthy_services", "unhealthy_services",
                       "missing_services", "failed_services"):
            for svc in group.get(bucket) or []:
                key = (uname, svc.get("service_name"), str(svc.get("label")))
                instance_keys.add(key)
                entries.append({
                    **svc,
                    "user_name": uname,
                    "stage": svc.get("status") or "unknown",
                    "source": "instance",
                    "job_live": False,
                    "session": None,
                })
        for sid, sess in sessions.items():
            if sess.get("user_name") != uname:
                continue
            key = (uname, sess.get("service_name"), str(sess.get("label")))
            used_sessions.add(sid)
            if key in instance_keys:
                # Instance wins: attach the session to the instance entry for
                # context, but the stage stays the instance's.
                for entry in entries:
                    if (entry.get("user_name"), entry.get("service_name"),
                            str(entry.get("label"))) == key:
                        entry["session"] = {**sess, "job_live": _job_live(sess.get("generation_task_id"))}
                        break
                continue
            entries.append({
                **sess,
                "user_name": uname,
                "stage": _STAGE_BY_SESSION.get(sess.get("state"), "unknown"),
                "source": "session",
                "job_live": _job_live(sess.get("generation_task_id")),
            })
        groups_out.append({"user_name": uname, "entries": entries})

    # Sessions whose owner is not in the -api payload still belong in the view.
    leftover: dict[str, list[dict]] = {}
    for sid, sess in sessions.items():
        if sid in used_sessions:
            continue
        uname = sess.get("user_name")
        if user_name and uname != user_name:
            continue
        leftover.setdefault(uname or "", []).append({
            **sess, "user_name": uname,
            "stage": _STAGE_BY_SESSION.get(sess.get("state"), "unknown"),
            "source": "session",
            "job_live": _job_live(sess.get("generation_task_id")),
        })
    for uname, entries in leftover.items():
        groups_out.append({"user_name": uname, "entries": entries})

    groups_out.sort(key=lambda g: g.get("user_name") or "")
    return {"groups": groups_out,
            "count": sum(len(g["entries"]) for g in groups_out),
            "orphans": payload.get("orphans") or []}


def workflow_status(workflow_id: str) -> dict:
    """`GET /api/services/workflows/{id}` — one read-only projection (R8)."""
    record = pdr.session_record(workflow_id)
    job = _job_dict(record.get("generation_task_id")) if record else None
    if job is None:
        job = _latest_job_for_service(workflow_id)
    if record is None:
        from .registry import Registry
        entry = Registry().get_by_id(workflow_id)
        if entry is not None:
            return {
                "workflow_id": workflow_id, "service_id": workflow_id,
                "user_name": entry.get("user_name"),
                "service_name": entry.get("service_name"),
                "label": entry.get("label"),
                "recipe_path": entry.get("recipe_path") or "",
                "operation": None, "stage": entry.get("status") or "unknown",
                "generation": job, "pause": _pause_of(job),
                "selection": None, "env_name": "", "detail": "",
                "deploy_task_id": None,
            }
        if job is not None:
            snap = job.get("target_snapshot") or {}
            return {
                "workflow_id": workflow_id, "service_id": workflow_id,
                "user_name": snap.get("user_name"),
                "service_name": snap.get("service_name"),
                "label": snap.get("label"), "recipe_path": snap.get("recipe_path") or "",
                "operation": "deploy" if job.get("origin") == "deploy" else "generate",
                "stage": "failed" if job.get("status") == "failed" else job.get("status"),
                "generation": job, "pause": _pause_of(job),
                "selection": None, "env_name": "", "detail": job.get("error") or "",
                "deploy_task_id": None,
            }
        raise KeyError(workflow_id)

    stage = _STAGE_BY_SESSION.get(record.get("state"), "unknown")
    return {
        "workflow_id": workflow_id,
        "service_id": workflow_id,
        "procedure_id": record.get("procedure_id"),
        "user_name": record.get("user_name"),
        "service_name": record.get("service_name"),
        "label": record.get("label"),
        "recipe_path": record.get("recipe_path") or "",
        "operation": record.get("operation") or pdr.OPERATION_DEPLOY,
        "stage": stage,
        "generation": job,
        "pause": _pause_of(job),
        "selection": record.get("selection"),
        "env_name": record.get("env_name") or "",
        "detail": record.get("detail") or "",
        "reason": record.get("reason") or "",
        "deploy_task_id": record.get("deploy_task_id"),
    }

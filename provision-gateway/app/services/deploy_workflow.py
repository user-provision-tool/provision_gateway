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
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..database import SessionLocal
from . import artifact_finalize, label_reservations, pre_deploy_registry as pdr
from .dependency_model import (
    dependency_entries as _dependency_entries,
    duplicate_target_services,
    entries_for_ids as _entries_for_ids,
    relation_snapshot_filename,
    serving_ports_snapshot_name,
    snapshot_used_artifacts,
    serving_url,
)
from .generation_jobs import (
    CLASSIFICATION_GENERATE_NEW,
    CLASSIFICATION_NONE,
    GenerationCapacityError,
    GenerationJobError,
    classification_mode,
    generation_jobs,
    relation_mode,
)
from .stream_conf import artifact_has_stream_rows

_log = logging.getLogger("gateway.deploy_workflow")

# Fields that exist only on the gateway side of the intent; never forwarded to
# the -api register contract. `_actor_user_id` is kept in the stored request
# (the follow-up grants need it) and stripped by `_api_payload`.
_GATEWAY_ONLY = {
    "selection", "prompt", "env_reference", "allowed_target_service_ids",
    "dependency_service_ids",
    "use_global_proxy", "fix", "job_type", "owner_username",
    "skip_env", "category", "service_value", "_actor_user_id",
    # R5/R26–R28 — generation-only context and the classification/relation
    # selections travel on the gateway request but are never part of the -api
    # register contract. The per-deploy force control is retired (C21).
    "base_refs", "context", "classification", "relations",
    "classification_fingerprint", "classify_only",
    # §14.3 — the LLM env guard flag travels with the deploy request/session
    # but is never part of the -api register contract.
    "llm_validate_env", "env_none",
}
# The replayable deploy request keeps the gateway-only fields the continuation
# needs (`allowed_target_service_ids`, `use_global_proxy`, `_actor_user_id`);
# only the generation-only context is dropped.
_RECORD_STRIP = {
    "selection", "prompt", "env_reference", "fix", "job_type", "owner_username",
    "skip_env", "category", "service_value",
    "base_refs", "context", "classification", "relations",
    "classification_fingerprint", "classify_only",
}

LIVE_JOB_STATUSES = ("queued", "running", "unknown")

# G101/C67 — the cross-resolver supervisor compares the config content on a
# one-second poll; the start waits this long after the grant-driven sync so the
# app's first call sees the new wildcard.
PRE_START_SYNC_WAIT_S = 1.2


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
        "env_none": bool(intent.get("env_none")),
        "profiles": intent.get("profiles") or [],
        "serving_ports": intent.get("serving_ports"),
        "serving_ports_none": bool(intent.get("serving_ports_none")),
    }


def _recipe_dir(service_name: str, recipe_path: str) -> Path:
    from .file_sets import _recipe_dir as resolve
    return resolve(Path(settings.SOURCE_PROJECTS_DIR) / service_name, recipe_path)


def _base_refs(intent: dict) -> dict:
    refs = intent.get("base_refs")
    return refs if isinstance(refs, dict) else {}


def _context(intent: dict) -> dict:
    ctx = intent.get("context")
    if not isinstance(ctx, dict):
        return {"files": [], "links": []}
    return {
        "files": [str(p) for p in (ctx.get("files") or []) if isinstance(p, str)],
        "links": [str(u) for u in (ctx.get("links") or []) if isinstance(u, str)],
    }


def _validate_base_refs(intent: dict, selection: dict) -> None:
    """R6 — every base ref exists, is that slot's candidate, and its slot is
    on ``generate new``. A ref on a filled slot is refused, never ignored.
    """
    from .file_sets import (
        FileSetError, _is_compose_candidate, _is_env_candidate,
        _is_mail_conf_candidate, _is_nginx_candidate,
        _is_serving_ports_candidate, _is_stream_conf_candidate,
        _validate_paths,
    )

    refs = _base_refs(intent)
    if not refs:
        return
    service_name = intent.get("service_name") or ""
    recipe_path = intent.get("recipe_path") or ""
    project_dir = Path(settings.SOURCE_PROJECTS_DIR) / service_name
    try:
        recipe_dir = _recipe_dir(service_name, recipe_path)
    except FileSetError as exc:
        raise WorkflowError("invalid_recipe_path", str(exc)) from exc

    def _check(names: list[str], *, slot: str, filled: bool, label: str, pred) -> None:
        names = [n for n in names if isinstance(n, str) and n]
        if not names:
            return
        if filled:
            raise WorkflowError(
                "base_ref_on_selected_slot",
                f"the {label} slot is filled — a base ref applies only to "
                f"`generate new`",
            )
        try:
            rel = _validate_paths(project_dir, recipe_path, names)
        except FileSetError as exc:
            raise WorkflowError("invalid_base_ref", str(exc)) from exc
        missing = [n for n in rel if not (recipe_dir / n).is_file()]
        if missing:
            raise WorkflowError(
                "base_ref_missing",
                f"base ref file(s) do not exist: {', '.join(missing)}",
            )
        not_candidates = [n for n in rel if not pred(Path(n).name)]
        if not_candidates:
            raise WorkflowError(
                "base_ref_not_a_candidate",
                f"not a {label} candidate: {', '.join(not_candidates)}",
            )

    _check(
        refs.get("compose") or [], slot="compose",
        filled=bool(selection.get("compose")), label="compose",
        pred=_is_compose_candidate,
    )
    _check(
        [refs.get("serving_ports")] if refs.get("serving_ports") else [],
        slot="serving_ports",
        filled=bool(selection.get("serving_ports") or selection.get("serving_ports_none")),
        label="serving-ports", pred=_is_serving_ports_candidate,
    )
    # C15 — `base_refs.nginx` is a per-category set: one file per category,
    # each validated against its own candidate predicate. A legacy string is
    # read as the http category.
    nginx_refs = refs.get("nginx")
    if isinstance(nginx_refs, str):
        nginx_refs = {"http": nginx_refs} if nginx_refs else {}
    if nginx_refs not in (None, {}):
        if not isinstance(nginx_refs, dict):
            raise WorkflowError(
                "invalid_base_ref",
                "base_refs.nginx must be a mapping of http/stream/mail to a file",
            )
        unknown = sorted(set(nginx_refs) - {"http", "stream", "mail"})
        if unknown:
            raise WorkflowError(
                "invalid_base_ref",
                "unknown nginx base-ref category: " + ", ".join(unknown),
            )
        categories = (
            ("http", _is_nginx_candidate, "http nginx"),
            ("stream", _is_stream_conf_candidate, "stream nginx"),
            ("mail", _is_mail_conf_candidate, "mail nginx"),
        )
        for cat, pred, label in categories:
            name = nginx_refs.get(cat)
            if not name:
                continue
            if not isinstance(name, str):
                raise WorkflowError(
                    "invalid_base_ref",
                    f"base_refs.nginx.{cat} must be a file name",
                )
            _check(
                [name], slot="nginx",
                filled=bool(selection.get("nginx") or selection.get("stream_conf")
                            or selection.get("mail_conf")),
                label=label, pred=pred,
            )
    _check(
        refs.get("env") or [], slot="env",
        filled=bool(selection.get("env") or selection.get("env_none")),
        label="env", pred=_is_env_candidate,
    )


def _validate_context(intent: dict) -> None:
    """R6 — context files stay inside the project root; links are http(s)."""
    ctx = _context(intent)
    service_name = intent.get("service_name") or ""
    project_root = (Path(settings.SOURCE_PROJECTS_DIR) / service_name).resolve()
    for name in ctx["files"]:
        raw = str(name).strip()
        if (not raw or raw.startswith("/") or "\\" in raw
                or ".." in raw.split("/")):
            raise WorkflowError(
                "context_file_invalid",
                f"context file must be project-root-relative: {name!r}",
            )
        path = (project_root / raw).resolve()
        if not str(path).startswith(str(project_root) + "/"):
            raise WorkflowError(
                "context_file_invalid", f"context file is outside the project root: {name!r}")
        if not path.is_file():
            raise WorkflowError(
                "context_file_missing", f"context file does not exist: {name!r}")
    for url in ctx["links"]:
        if not url.lower().startswith(("http://", "https://")):
            raise WorkflowError(
                "context_link_invalid", f"context link must be http(s): {url!r}")


def _classification_fingerprint(intent: dict, selection: dict) -> dict:
    """R17/R29 — hash the structural inputs once, at intake.

    The Env base used by the classifier is the ROOT TEMPLATE (R27): a SELECTED
    env file, else the explicit Env refs, else the recipe's own env files.
    Deploy-varying values and the selected dependency identities are
    deliberately excluded.
    """
    from .env_classification import implicit_env_names, structural_fingerprint

    service_name = intent.get("service_name") or ""
    try:
        recipe_dir = _recipe_dir(service_name, intent.get("recipe_path") or "")
    except Exception:  # noqa: BLE001 — the caller's own validation names this
        return {}
    refs = _base_refs(intent)
    env_bases = [] if selection.get("env_none") else [
        p for p in (selection.get("env") or []) if isinstance(p, str)]
    if not env_bases:
        env_bases = [p for p in (refs.get("env") or []) if isinstance(p, str)]
    if not env_bases:
        env_bases = [p for p in (intent.get("env_reference") or []) if isinstance(p, str)]
    if not env_bases:
        env_bases = implicit_env_names(recipe_dir)
    compose = [p for p in (selection.get("compose") or []) if isinstance(p, str)]
    if not compose:
        compose = [p for p in (refs.get("compose") or []) if isinstance(p, str)]
    if not compose:
        # The env build reads the recipe's compose candidates when the slot is
        # `generate new` with no ref (`_env_compose_paths`); the fingerprint
        # must hash the same inputs.
        from .file_sets import _is_compose_candidate
        try:
            compose = [f.name for f in sorted(recipe_dir.iterdir())
                       if f.is_file() and _is_compose_candidate(f.name)]
        except OSError:
            compose = []
    return structural_fingerprint(
        recipe_dir,
        compose_names=compose,
        profiles=list(selection.get("profiles") or []),
        env_base_names=env_bases,
    )


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
    # `env_none` is the deliberate no-env state: a stale list alongside it is
    # never sent, so it is not checked either.
    if not selection.get("env_none"):
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


def _selection_stream_rows(intent: dict, selection: dict) -> bool:
    """Whether the SELECTED serving-ports artifact declares stream rows.

    Selection values are recipe-relative names (an absolute path is honoured
    for a caller that supplies one); the artifact is read from the source
    project's recipe dir, which is where the panel's candidates live.
    """
    name = selection.get("serving_ports")
    if not isinstance(name, str) or not name:
        return False
    path = Path(name)
    if not path.is_absolute():
        path = (settings.SOURCE_PROJECTS_DIR
                / (intent.get("service_name") or "")
                / (intent.get("recipe_path") or "") / name)
    return artifact_has_stream_rows(str(path))


def generation_needed(intent: dict) -> bool:
    """The gateway decides, never the client (R1)."""
    selection = _selection(intent)
    missing = generation_jobs._recipe_missing({
        "service_name": intent.get("service_name") or "",
        "recipe_path": intent.get("recipe_path") or "",
    })
    # A phase the recipe lacks is a generation reason ONLY when the operator
    # did not fill that slot. An explicit selection wins: `serving_ports_none`
    # is a deliberate no-file state (§14.1), so a recipe that never had a
    # serving-ports artifact must not force a generation run over it. Without
    # this, a validation-only deploy (S12) could never be reached for such a
    # recipe and the LLM env guard would run only after a generation job.
    if "docker-compose" in missing and not (selection.get("compose") or []):
        return True
    if "nginx.conf" in missing and not selection.get("nginx"):
        return True
    if "serving_ports" in missing and not (
            selection.get("serving_ports") or selection.get("serving_ports_none")):
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
    # 2026-11-19 — TLS alone no longer demands a stream slot: the requirement
    # is derived from the SELECTED artifact's rows (T4). An HTTP-only artifact
    # (or a missing/unreadable one) declares none, so the stream term never
    # starts a generation run for a conf that cannot be produced.
    if (_https(intent) and not selection.get("stream_conf")
            and _selection_stream_rows(intent, selection)):
        return True
    # The per-user env.raw is generated when no env is selected (R3/R6).
    # `env_none` is the deliberate no-env state (§14.1) and suppresses it the
    # same way a selected list does.
    if not (selection.get("env") or []) and not selection.get("env_none"):
        return True
    # R26–R28 — a classification on a selected env builds the injected
    # `.env.raw`; for an existing variant that phase is model-free, but it is
    # still the only writer of the handoff. `cls None` sends the env as-is.
    if not selection.get("env_none"):
        cls = classification_mode(intent)
        if cls != CLASSIFICATION_NONE and (selection.get("env")
                                           or cls == CLASSIFICATION_GENERATE_NEW):
            return True
    return False


def _generation_request(intent: dict, selection: dict) -> dict:
    # R9 — profiles are GENERATION context (the classifier may choose a
    # profile-matching example) AND deploy-time options. They ride the request
    # and the final selection; the register payload still carries them only as
    # the deploy option.
    gen_selection = dict(selection)
    return {
        "service_name": intent.get("service_name"),
        "recipe_path": intent.get("recipe_path") or "",
        "job_type": "generate_missing",
        "prompt": intent.get("prompt") or "",
        "env_reference": intent.get("env_reference") or [],
        # R5/R6 — the per-slot refs and the arbitrary context are generation
        # only; they never reach the -api.
        "base_refs": _base_refs(intent),
        "context": _context(intent),
        "profiles": list(selection.get("profiles") or []),
        # R26–R28 — the explicit classification/relation selections; the force
        # control is retired (C21). They never reach the -api.
        "classification": classification_mode(intent),
        "relations": (intent.get("relations") if isinstance(intent.get("relations"), dict)
                      else {}),
        "classify_only": bool(intent.get("classify_only")),
        # R17 — computed once at intake so reuse and regeneration agree.
        "classification_fingerprint": intent.get("classification_fingerprint") or {},
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
        # G102 — the classifier's context travels with the generation request.
        # G103 — the duplicate / None modes remove the dependencies from the
        # GENERATION context only (the grants still use the intent's ids). A
        # guided deploy restart KEEPS them: its `.env.raw` is the deploy env.
        "dependency_service_ids": [] if (
            intent.get("_dependency_env_disabled")
        ) else [
            str(x) for x in (intent.get("dependency_service_ids")
                             or intent.get("allowed_target_service_ids") or []) if x
        ],
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

    # §14.1/S11/S17 — the deploy env payload, in precedence order:
    #   1. the generated per-user `.raw` when one exists;
    #   2. the SELECTED list (ordered; later files win) when non-empty and the
    #      operator did not choose `None`;
    #   3. nothing for an explicit `None` or an empty list.
    # The env REFERENCE is generation context only and never appears here.
    selected_env = [n for n in (selection.get("env") or []) if isinstance(n, str)]
    if env_name:
        payload["env_files"] = [env_name]
    elif selected_env and not selection.get("env_none"):
        payload["env_files"] = selected_env
    else:
        payload.pop("env_files", None)
    payload.pop("env_file_path", None)

    payload["profiles"] = list(selection.get("profiles") or [])
    if not payload["profiles"]:
        payload.pop("profiles", None)

    # G101/G102 — a deploy with dependencies is prepared in one step and
    # started in another: the register payload carries the cross resolver
    # NAME/network so the -api configures the proxy before the app starts; the
    # deploy workflow writes the pre-start grant rows and then calls start.
    dep_ids = [str(x) for x in (intent.get("dependency_service_ids")
                                or intent.get("allowed_target_service_ids") or []) if x]
    if dep_ids:
        payload["extra_resolver"] = settings.CROSS_RESOLVER_NAME
        payload["extra_network"] = settings.CROSS_NETWORK
        payload["prepare"] = True
    else:
        payload.pop("extra_resolver", None)
        payload.pop("extra_network", None)
        payload["prepare"] = False
    return payload


def _ensure_env_raw(intent: dict, selection: dict) -> str | None:
    """R7.2 — the deploy always sends a per-user `.env.raw`, never a recipe file.

    When the env build produced one, ``env_name`` already names it. When the
    operator selected an existing env file and nothing needed tuning, the
    selected content is copied into the raw here; the payload then has the
    same one-path shape as a generated env.
    """
    if selection.get("env_none"):
        return None
    names = [n for n in (selection.get("env") or []) if isinstance(n, str)]
    if not names:
        return None
    service = intent.get("service_name") or ""
    user = str(intent.get("user_name") or intent.get("owner_username") or "user")
    label = str(intent.get("label") or "0")
    try:
        rdir = _recipe_dir(service, intent.get("recipe_path") or "")
    except Exception:  # noqa: BLE001
        return None
    parts: list[str] = []
    for name in names:
        path = rdir / name.rsplit("/", 1)[-1]
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace").rstrip("\n"))
    if not parts:
        return None
    text = "\n".join(parts) + "\n"
    raw_name = f".env.raw.{user}.{label}"
    from .generated_marker import write_marker_pair
    try:
        write_marker_pair(rdir / raw_name, text, cls="ins",
                          description=f"per-user env raw ({raw_name})",
                          validation="selected env, no tuning")
    except Exception:  # noqa: BLE001 — a copy failure never blocks the deploy
        _log.exception("env raw write failed for %s/%s", service, label)
        return None
    return raw_name


def _validate_env_selection(intent: dict, selection: dict) -> None:
    """R26–R28 — validate the classification/relation selections at intake.

    Unknown modes and unknown targets are 4xx (T0). The classification state
    pins the relation pickers: `cls None` means every relation is `None`,
    `cls generate new` means every relation is `generate new`. With
    `cls existing` the per-target pickers are live; `None` is refused for a
    non-empty projection, and a selected relation variant must be a readable,
    matched file for its own target. A red classification is NOT refused here:
    Generate/Deploy regenerates it (R26).
    """
    from .dependency_model import (
        RELATION_VARIANT_RE,
        duplicate_target_services,
        matched_relation_variant,
        read_relation_variant,
        relation_filename,
    )
    from .env_classification import (
        ARTIFACT_PREFIX,
        projection,
        read_variant_any,
        target_allowlist_violations,
    )

    ids = [str(x) for x in (intent.get("dependency_service_ids")
                            or intent.get("allowed_target_service_ids") or []) if x]
    entries = _entries_for_ids(ids) if ids else []
    targets = sorted({str(e.get("service_name") or "") for e in entries} - {""})
    # G103 — two selected dependencies with the SAME service name are removed
    # from the env context (grants only); the classification may not point at
    # them (M-B9/M-B13).
    if entries and duplicate_target_services(entries):
        intent["_dependency_env_disabled"] = True
        targets = []
    if selection.get("env_none"):
        intent["_dependency_env_disabled"] = True
        intent["classification"] = CLASSIFICATION_NONE
        intent["relations"] = {}
        return

    cls = classification_mode(intent)
    if cls == CLASSIFICATION_NONE:
        intent["classification"] = CLASSIFICATION_NONE
        intent["relations"] = {}
        return
    if cls == CLASSIFICATION_GENERATE_NEW:
        intent["classification"] = CLASSIFICATION_GENERATE_NEW
        intent["relations"] = {}
        return
    if not cls.startswith(ARTIFACT_PREFIX):
        raise WorkflowError(
            "unknown_classification_mode",
            f"unknown classification mode {cls!r}: expected none, generate new, "
            "or a matched env.classification.<fingerprint>.yaml variant",
        )
    recipe_dir = _recipe_dir(intent.get("service_name") or "",
                             intent.get("recipe_path") or "")
    data = read_variant_any(recipe_dir, cls)
    if data is None:
        raise WorkflowError(
            "classification_variant_missing",
            f"classification variant is missing or unreadable: {cls}",
        )
    violations = target_allowlist_violations(data, targets)
    if violations:
        # R26 — a target-allowlist failure is regenerated on Generate/Deploy
        # when an LLM is active; with no LLM there is no way to repair it, so
        # the intake refuses with the same named cause.
        llm_on = False
        try:
            from ..database import SessionLocal
            db = SessionLocal()
            try:
                llm_on = generation_jobs._llm_active(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — an unreadable config is "no LLM"
            llm_on = False
        if not llm_on:
            raise WorkflowError(
                "classification_target_not_selected",
                "the classification points dependency variable(s) at target(s) that "
                "are not selected: "
                + ", ".join(f"{v['variable']}->{v['target']}" for v in violations)
                + " — reclassify or edit the classification",
                409,
            )
    relations = intent.get("relations")
    if relations is not None and not isinstance(relations, dict):
        raise WorkflowError("invalid_relations", "'relations' must be a mapping")
    out: dict[str, str] = {}
    for key, raw in (relations or {}).items():
        target = str(key)
        if target not in targets:
            raise WorkflowError(
                "unknown_relation_target",
                f"relation target is not a selected dependency: {target!r}",
            )
        mode = relation_mode(intent, target)
        if mode is None:
            continue
        if mode == CLASSIFICATION_NONE:
            expected = projection(data, target)
            if expected:
                raise WorkflowError(
                    "relation_none_not_allowed",
                    "relation 'None' is not allowed for target "
                    f"'{target}': the classification assigns "
                    + ", ".join(expected)
                    + " — choose generate new, a valid relation, or edit the classification",
                )
            out[target] = CLASSIFICATION_NONE
            continue
        if mode == CLASSIFICATION_GENERATE_NEW:
            out[target] = CLASSIFICATION_GENERATE_NEW
            continue
        source = intent.get("service_name") or ""
        match = RELATION_VARIANT_RE.match(mode)
        legacy_accepted = False
        if not match and mode == relation_filename(source, target):
            # R29 back-compat: a rev-1 relation file is a candidate only when
            # its recorded fingerprint matches; its name is never rewritten.
            matched = matched_relation_variant(
                recipe_dir, source, target, intent.get("classification_fingerprint"))
            legacy_accepted = bool(matched and matched[0] == mode)
        if not legacy_accepted and (
                not match or match.group("source") != source
                or match.group("target") != target):
            raise WorkflowError(
                "unknown_relation_mode",
                f"unknown relation mode for target '{target}': {mode!r}",
            )
        if read_relation_variant(recipe_dir, intent.get("service_name") or "",
                                 target, mode) is None:
            raise WorkflowError(
                "relation_variant_missing",
                f"relation variant is missing or unreadable: {mode}",
            )
        out[target] = mode
    intent["classification"] = cls
    intent["relations"] = out


DEPLOY_RECORD_VERSION = 1


def deploy_record_filename(user: str, label: str) -> str:
    return f"deploy.user-{user}.{label}.json"


def write_deploy_record(recipe_dir: Path, user: str, label: str,
                        copies: list[dict]) -> Path:
    """R11.6 — one gateway-private record naming this deployment's copies."""
    import json
    path = Path(recipe_dir) / deploy_record_filename(user, str(label))
    payload = {
        "version": DEPLOY_RECORD_VERSION,
        "deployed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "copies": copies or [],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_deploy_record(recipe_dir: Path, user: str, label: str) -> dict | None:
    import json
    path = Path(recipe_dir) / deploy_record_filename(user, str(label))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fix_copy_name(name: str, user: str, label: str, job: str) -> str | None:
    """`<stem>.fix.<job>.<ext>` -> `<stem>.fix.user-<u>.<l>.<ext>` (R11.3)."""
    token = f".fix.{job}"
    if job and token in name:
        return name.replace(token, f".fix.user-{user}.{label}", 1)
    return None


_FIX_TOKEN_RE = re.compile(r"\.fix\.([^.]+)(?:\.|$)")


def _selection_fix_job(recipe_dir: Path, selection: dict) -> str | None:
    """The fix run a reviewed selection belongs to, when no job record exists.

    The reviewed deploy's selection names the `.fix.<job>` files the run
    produced. When the deploy carries no ``env_meta.job_id`` (a direct deploy
    of a reviewed fix), the NEWEST selected fix file's token is that run.
    """
    newest: tuple[float, str] | None = None
    names: list[str] = []
    for key in ("compose", "env"):
        names.extend(p for p in (selection.get(key) or []) if isinstance(p, str))
    for key in ("nginx", "stream_conf", "mail_conf", "serving_ports"):
        if isinstance(selection.get(key), str):
            names.append(selection[key])
    for name in names:
        base = name.rsplit("/", 1)[-1]
        match = _FIX_TOKEN_RE.search(base)
        if not match:
            continue
        path = Path(recipe_dir) / base
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, match.group(1))
    return newest[1] if newest else None


def copy_run_fixes(recipe_dir: Path, selection: dict, user: str, label: str,
                   job: str | None) -> list[dict]:
    """Copy only the CURRENT run's `.fix` files to per-instance `ins` copies."""
    if not job:
        return []
    from .generated_marker import write_marker_pair
    names: list[str] = []
    for key in ("compose", "env"):
        names.extend(p for p in (selection.get(key) or []) if isinstance(p, str))
    for key in ("nginx", "stream_conf", "mail_conf", "serving_ports"):
        if isinstance(selection.get(key), str):
            names.append(selection[key])
    out: list[dict] = []
    for name in dict.fromkeys(names):
        dest_name = _fix_copy_name(name.rsplit("/", 1)[-1], user, label, str(job))
        if not dest_name:
            continue
        src = Path(recipe_dir) / name.rsplit("/", 1)[-1]
        if not src.is_file():
            continue
        try:
            write_marker_pair(
                Path(recipe_dir) / dest_name,
                src.read_text(encoding="utf-8", errors="replace"),
                cls="ins",
                description=f"fix copy for this deploy ({dest_name})",
                job=job, source=src.name)
        except Exception:  # noqa: BLE001 — a copy never blocks the deploy
            _log.exception("fix copy failed for %s", src.name)
            continue
        out.append({"kind": "fix", "path": dest_name})
    return out


def teardown_deploy_copies(entry: dict) -> int:
    """R11.6 — delete a deployment's copies and its record (explicit teardown)."""
    from .generated_marker import remove_pair
    service = str(entry.get("service_name") or "")
    user = str(entry.get("user_name") or "")
    label = str(entry.get("label") or "")
    if not service or not user:
        return 0
    try:
        rdir = _recipe_dir(service, entry.get("recipe_path") or "")
    except Exception:  # noqa: BLE001
        return 0
    record = read_deploy_record(rdir, user, label)
    removed = 0
    for copy in (record or {}).get("copies") or []:
        if not isinstance(copy, dict) or not isinstance(copy.get("path"), str):
            continue
        path = rdir / copy["path"].rsplit("/", 1)[-1]
        try:
            remove_pair(path)
            removed += 1
        except OSError:
            pass
    try:
        (rdir / deploy_record_filename(user, label)).unlink(missing_ok=True)
    except OSError:
        pass
    return removed


def _snapshot_rows(entry: dict) -> dict:
    """R11.2/R11.3 — the card's rows read the deployment record, never existence."""
    service = str(entry.get("service_name") or "")
    user = str(entry.get("user_name") or "")
    label = str(entry.get("label") or "")
    if not service or not user:
        return {}
    try:
        recipe_dir = _recipe_dir(service, entry.get("recipe_path") or "")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    record = read_deploy_record(recipe_dir, user, label)
    copies = (record or {}).get("copies") or []
    deps: list[dict] = []
    fixes: list[str] = []
    for copy in copies:
        if not isinstance(copy, dict):
            continue
        path = copy.get("path")
        kind = copy.get("kind")
        if not isinstance(path, str) or not path:
            continue
        if kind == "serving_ports":
            out["serving_ports_snapshot"] = path
        elif kind == "env_classification":
            out["env_classification_snapshot"] = path
        elif kind == "dependency":
            deps.append({"target_service": copy.get("target_service") or "",
                         "path": path})
        elif kind == "fix":
            fixes.append(path)
    if deps:
        out["dependency_snapshots"] = deps
    if fixes:
        out["fix_copies"] = fixes
    return out


def _env_meta_for_substitution(intent: dict) -> dict:
    """The used set when no generation job carried one (rev-1 record replay).

    Rev 7 has no deploy-time substitution: a path without a job's env_meta read
    or wrote nothing, so it snapshots nothing (R27).
    """
    return {
        "classification_read": False,
        "classification_written": False,
        "relations": {},
    }


def _snapshot_for_deploy(intent: dict, selection: dict, label: str,
                         env_meta: dict | None = None) -> None:
    """C12/R11 — copy exactly what the env build used, then record the copies.

    Serving ports keep their independent rule. The classification and the
    relations are copied only when the env build touched them: a selected env
    with no dependencies takes no relationship snapshot, and a mechanical env
    (no LLM) takes no classification snapshot. The current run's ``.fix`` files
    are copied by their job token. The record is written even when there are no
    copies, so the card never falls back to an older deployment's file.
    """
    service = intent.get("service_name") or ""
    user = str(intent.get("user_name") or intent.get("owner_username") or "user")
    try:
        recipe_dir = _recipe_dir(service, intent.get("recipe_path") or "")
    except Exception:  # noqa: BLE001 — a snapshot failure never blocks the deploy
        _log.exception("dependency snapshot dir failed for %s/%s", service, label)
        return
    sp = None if selection.get("serving_ports_none") else selection.get("serving_ports")
    meta = env_meta if isinstance(env_meta, dict) else _env_meta_for_substitution(intent)
    targets = sorted(t for t, status in (meta.get("relations") or {}).items()
                     if status in ("read", "written"))
    classification_used = bool(
        meta.get("classification_read") or meta.get("classification_written"))
    copies: list[dict] = []
    try:
        snap = snapshot_used_artifacts(
            recipe_dir, source_service=service, user=user, label=str(label),
            serving_ports=sp if isinstance(sp, str) else None,
            target_services=targets,
            include_classification=classification_used,
            relation_variants=meta.get("relation_variants") or {},
            classification_name=meta.get("classification_name"),
        ) or {}
        if snap.get("serving_ports"):
            copies.append({"kind": "serving_ports",
                           "path": snap["serving_ports"]})
        if snap.get("env_classification"):
            copies.append({"kind": "env_classification",
                           "path": snap["env_classification"]})
        for dep in snap.get("dependencies") or []:
            copies.append({"kind": "dependency", "path": dep.get("path"),
                           "target_service": dep.get("target_service")})
        job = str(meta.get("job_id") or "") or _selection_fix_job(recipe_dir, selection)
        copies.extend(copy_run_fixes(recipe_dir, selection, user, str(label), job))
    except Exception:  # noqa: BLE001 — the card degrades, the deploy does not
        _log.exception("deployment copies failed for %s/%s", service, label)
    try:
        write_deploy_record(recipe_dir, user, str(label), copies)
    except OSError:
        _log.exception("deployment record write failed for %s/%s", service, label)


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
    # R6 — every per-slot base ref exists, is that slot's candidate, and its
    # slot is on `generate new`; arbitrary context stays inside the project.
    _validate_base_refs(intent, selection)
    _validate_context(intent)
    # R17 — the structural fingerprint is computed ONCE, before any reuse
    # decision, so "reuse" and "regenerate" cannot disagree.
    intent["classification_fingerprint"] = _classification_fingerprint(intent, selection)
    # R26–R28 — the classification/relation selections are validated once, at
    # intake: unknown modes/targets, a selected variant that is unreadable, and
    # a relation `None` over a non-empty projection are structured refusals.
    _validate_env_selection(intent, selection)

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
        # S12 — a reviewed/refused deploy skips generation, so the gateway
        # runs the mechanical validators now. A failure fails the deploy task
        # closed (no generation job, no popup).
        try:
            await _validate_selected_artifacts(
                intent, seeded, existing.get("env_name"))
        except WorkflowError as exc:
            pdr.mark_deploy_failed(
                service_id, f"{exc.code}: {exc.detail}", reason=exc.code)
            label_reservations.release(service_id)
            raise
        deploy_request, deploy_secrets = _record_deploy_intent(intent)
        pdr.create_or_replace(
            service_id=service_id, procedure_id=procedure_id,
            owner_api_user_id=owner_id, service_name=intent["service_name"],
            service_label=label, recipe_path=intent["recipe_path"],
            state=pdr.STATE_GENERATED, operation=pdr.OPERATION_DEPLOY,
            deploy_request=deploy_request, deploy_secret_values=deploy_secrets,
            selection=seeded,
        )
        stored_env_meta = (existing.get("options") or {}).get("env_meta") \
            if isinstance(existing.get("options"), dict) else None
        return await _claim_and_register(
            service_id, intent, seeded, existing.get("env_name"),
            env_meta=stored_env_meta)

    # Reserve the identity under the tuple lock (R5).
    label = _reserve(
        owner_api_user_id=owner_id, service_name=intent["service_name"],
        service_id=service_id, requested_label=str(requested),
    )
    intent["label"] = label

    need_generation = generation_needed(intent)
    # S12 — validation-only deploy: every slot is filled, so no generation job
    # exists. The gateway still validates exactly what it will register before
    # writing a session or claiming the label. A failure is a structured
    # refusal with the validator's message; the label is released.
    if operation == pdr.OPERATION_DEPLOY and not need_generation:
        try:
            await _validate_selected_artifacts(intent, selection, None)
        except WorkflowError:
            label_reservations.release(service_id)
            raise
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
    if (_https(intent) and not selection.get("stream_conf")
            and _selection_stream_rows(intent, selection)):
        return False
    # S11 — an explicit `env_none` fills the env slot just like a selected
    # list or a recorded generated name does.
    if not (selection.get("env") or existing.get("env_name")
            or selection.get("env_none")):
        return False
    return True


def _validation_error(code: str, label: str, errors: list[str]) -> WorkflowError:
    detail = "; ".join(e for e in errors if e) or f"{label} validation failed"
    return WorkflowError(code, f"{label} validation failed: {detail}", 422)


async def _validate_selected_artifacts(intent: dict, selection: dict,
                                       env_name: str | None) -> None:
    """S12 — the mechanical validator pass for a validation-only deploy.

    When every slot is filled no generation job exists, but the gateway still
    checks exactly what it is about to register: the compose (``docker compose
    config``), the serving-ports artifact against that compose, every selected
    nginx conf (proxy targets + ``nginx -t`` + serving-port agreement), and the
    env content that will be sent (the selected ordered list, or the generated
    per-user ``.raw``). The validation context is exactly the env list sent;
    an explicit ``env_none`` has nothing to validate. The policy runs with
    ``require_ready=True`` so a half-ready selection is refused too.
    """
    from . import config_validation, file_sets

    _validate_policy(intent, selection, require_ready=True)

    service_name = intent.get("service_name") or ""
    recipe_path = intent.get("recipe_path") or ""
    try:
        recipe_dir = _recipe_dir(service_name, recipe_path)
    except Exception as exc:  # noqa: BLE001 — map to the structured refusal
        raise WorkflowError("invalid_recipe_path", str(exc)) from exc

    # 1. Compose — resolved under the recipe dir, exactly what the -api reads.
    compose_names = [n for n in (selection.get("compose") or []) if isinstance(n, str)]
    compose_paths = [recipe_dir / n for n in compose_names]
    composed = config_validation.validate_compose(compose_paths)
    if not composed.get("valid"):
        raise _validation_error(
            "compose_invalid", "compose", composed.get("errors") or [])

    # 2. Serving ports — the artifact as it will be sent, against that compose.
    ports = None
    sp_name = selection.get("serving_ports")
    if isinstance(sp_name, str) and sp_name:
        sp_path = recipe_dir / sp_name
        try:
            sp_text = sp_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise _validation_error(
                "serving_ports_invalid", "serving_ports", [str(exc)]) from exc
        res = config_validation.validate_serving_ports(sp_text, compose_paths)
        if not res.get("valid"):
            raise _validation_error(
                "serving_ports_invalid", "serving_ports", res.get("errors") or [])
        try:
            import yaml
            ports = yaml.safe_load(sp_text) if sp_text else None
        except Exception:  # noqa: BLE001 — agreement alone cannot help
            ports = None

    # 3. Nginx — every selected conf in the category it will register under.
    confs: list[tuple[str, str]] = []
    if selection.get("nginx"):
        confs.append(("http", selection["nginx"]))
    if _https(intent) and selection.get("stream_conf"):
        confs.append(("stream", selection["stream_conf"]))
    if selection.get("mail_conf"):
        confs.append(("mail", selection["mail_conf"]))
    service_names = file_sets.compose_service_names(compose_paths)
    from .serving_ports import validate_conf_agreement
    nginx_errors: list[str] = []
    for category, name in confs:
        conf_path = recipe_dir / name
        checked = await config_validation.validate_nginx(conf_path, service_names)
        nginx_errors.extend(f"{name}: {e}" for e in (checked.get("errors") or []))
        if ports is not None and conf_path.is_file():
            text = conf_path.read_text(encoding="utf-8", errors="replace")
            agreement = validate_conf_agreement(
                ports, text,
                service_name=service_name,
                user_name=str(intent.get("user_name") or "x"),
                label=str(intent.get("label") or "0"),
                domain=str(intent.get("domain") or "localhost"),
                category=category,
            )
            nginx_errors.extend(
                f"{name}: {e}" for e in (agreement.get("errors") or []))
    if nginx_errors:
        raise _validation_error("nginx_invalid", "nginx", nginx_errors)

    # 4. Env — the exact list that will be sent. `env_none` has nothing to
    #    check; a generated `.raw` is validated as the file the -api receives.
    if selection.get("env_none"):
        return
    if env_name:
        env_path = recipe_dir / env_name
        if env_path.is_file():
            content = env_path.read_text(encoding="utf-8", errors="replace")
            res = config_validation.validate_env(content, compose_paths)
            if not res.get("valid"):
                raise _validation_error("env_invalid", "env", res.get("errors") or [])
        return
    env_names = [n for n in (selection.get("env") or []) if isinstance(n, str)]
    if not env_names:
        return
    try:
        content = "\n".join(
            (recipe_dir / n).read_text(encoding="utf-8", errors="replace")
            for n in env_names
        )
    except OSError as exc:
        raise _validation_error("env_invalid", "env", [str(exc)]) from exc
    res = config_validation.validate_env(content, compose_paths)
    if not res.get("valid"):
        raise _validation_error("env_invalid", "env", res.get("errors") or [])


async def _run_llm_env_guard(intent: dict, selection: dict,
                             env_name: str | None = None) -> None:
    """S13 — the opt-in LLM validation of the SELECTED env list.

    Applies only when the operator checked ``llm_validate_env`` and a selected
    env list exists. It never runs for `env_none`, for a generated `.raw`
    (`env_name` — the generation path already guards it, and that file is what
    is actually sent), or for the env REFERENCE. It blocks on findings *and*
    on an LLM-call failure: the operator explicitly asked for this check, and
    the only bypass is to uncheck the box. The guard never mutates a file.
    """
    if not intent.get("llm_validate_env") or env_name:
        return
    env_names = [n for n in (selection.get("env") or []) if isinstance(n, str)]
    if selection.get("env_none") or not env_names:
        return

    service_name = intent.get("service_name") or ""
    recipe_path = intent.get("recipe_path") or ""
    try:
        recipe_dir = _recipe_dir(service_name, recipe_path)
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError("invalid_recipe_path", str(exc)) from exc

    env_files: list[tuple[str, str]] = []
    for name in env_names:
        path = recipe_dir / name
        try:
            env_files.append(
                (name, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            raise WorkflowError(
                "llm_env_validation_blocked",
                f"selected env file {name!r} cannot be read ({exc}); the deploy "
                "is blocked because LLM validation was requested",
            ) from exc

    compose_files: list[tuple[str, str]] = []
    for name in (selection.get("compose") or []):
        if not isinstance(name, str):
            continue
        path = recipe_dir / name
        if path.is_file():
            compose_files.append(
                (name, path.read_text(encoding="utf-8", errors="replace")))

    from .llm_service import llm_service
    db = SessionLocal()
    try:
        result = await llm_service.validate_env_selection(
            db,
            env_files=env_files,
            compose_files=compose_files,
            service_name=service_name,
            profiles=list(selection.get("profiles") or []),
        )
    finally:
        db.close()

    if result.get("valid"):
        return
    findings = list(result.get("findings") or [])
    guide = str(result.get("guide") or "")
    if result.get("error"):
        detail = (
            "LLM env validation could not run: "
            f"{result['error']}. The deploy is blocked because LLM validation "
            "was requested; uncheck 'Run LLM validation before deploy' to "
            "deploy without it."
        )
    else:
        detail = (
            "LLM env validation found issue(s): "
            + "; ".join(findings or ["the env was rejected"])
            + (f". Guided fix: {guide}" if guide else
               ". Fix the selected env file(s) and deploy again, or uncheck "
               "the LLM validation option.")
        )
    exc = WorkflowError("llm_env_validation_blocked", detail, 422)
    # The structured half the caller (and the audit) can read.
    exc.findings = findings
    exc.guide = guide
    exc.llm_error = result.get("error")
    raise exc


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
                              env_name: str | None,
                              env_meta: dict | None = None) -> dict:
    """Claim `generated -> deploying` and call the existing -api register."""
    record = pdr.session_record(service_id)
    if record is None:
        raise WorkflowError("session_missing", "the workflow record is gone", 409)

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

    # R27 — there is no deploy-time substitution path any more: the env phase
    # writes the injected `.env.raw` from the classification (or the selected
    # env is sent as-is under `cls None`). `env_name` is that handoff when one
    # was built.

    # R7.2 — the deploy always sends the per-user raw. A selected env with
    # nothing to tune is copied into it here (the env build already wrote one
    # when it ran).
    if not env_name:
        env_name = _ensure_env_raw(resolved, selection)

    # C12 — snapshot exactly what the env build used or wrote: the artefact
    # iff read/written, each relation iff read/written, and the serving-ports
    # artifact by its own independent rule.
    _snapshot_for_deploy(resolved, selection, str(record["label"]), env_meta)

    # S13 — the opt-in LLM env guard runs BEFORE the claim/register. A
    # finding or an LLM-call failure blocks the deploy; the record fails
    # closed so the operator can uncheck the box and deploy again.
    try:
        await _run_llm_env_guard(resolved, selection, env_name)
    except WorkflowError as exc:
        findings = list(getattr(exc, "findings", None) or [])
        if findings:
            # S22 — a finding pauses through the existing LLM-fix flow. The
            # session keeps its label and replayable intent: the card shows the
            # guard's guide, Check reopens the panel pre-filled, and the
            # confirming restart (fix=true, env -> generate new) regenerates the
            # env and resumes. A call failure carries no fixing prompt and
            # still fails closed below; so does an unreadable selected file.
            guide = (getattr(exc, "guide", "") or "").strip() or (
                "Fix the selected env file(s) so they pass LLM validation.")
            prompt = f"{guide}\n\nFindings: " + "; ".join(findings)
            pdr.pause_for_fix(
                service_id, guide=prompt, slot="env",
                detail=f"{exc.code}: {exc.detail}")
            raise WorkflowError(
                "workflow_paused",
                "the LLM env guard paused this deploy — open Check on the service "
                "card, confirm the fixing prompt and deploy again",
                409,
            ) from exc
        pdr.mark_deploy_failed(
            service_id, f"{exc.code}: {exc.detail}", reason=exc.code)
        label_reservations.release(service_id)
        raise

    if not pdr.claim_for_deploy(service_id):
        # Another completer owns it; a no-op, never a second deploy.
        return _handle(service_id, intent, record.get("label") or "", "deploying")

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
            detail_json = getattr(http_exc, "detail", None)
            refusal = (detail_json.get("error")
                       if getattr(http_exc, "status_code", None) == 503
                       and isinstance(detail_json, dict) else None)
            if refusal in ("at_capacity", "queue_full", "queue_unavailable"):
                pdr.record_refusal(
                    service_id=service_id, reason=refusal,
                    detail=str(detail_json.get("detail") or ""),
                    request=payload,
                    use_global_proxy=bool(resolved.get("use_global_proxy")),
                )
                raise WorkflowError(
                    refusal, str(detail_json.get("detail") or ""), 503) from exc
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
    target_ids = [str(t) for t in (resolved.get("dependency_service_ids")
                                   or resolved.get("allowed_target_service_ids") or []) if t]
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
    """Pre-start grants, the resolver sync, then start (G102/G101).

    With dependencies the register task ran in *prepare* mode: the containers
    exist (stopped), the proxy is up and configured.  This follow-up writes one
    ordinary add-only grant row per selected dependency, triggers the
    grant-driven cross sync (the dnsmasq wildcard and the proxy membership),
    waits out the resolver supervisor's one-second content poll, and only then
    starts the app.  Without dependencies the register task already started the
    app and the old lifecycle pass runs.
    """
    from . import registry_tasks, service_grants
    from .cross_orchestrator import OUTCOME_OK, cross_orchestrator
    from .provision_service import provision_service
    if not task_id:
        return
    try:
        # R9.2: read the task row directly; submit/cancel are the only HTTP
        # calls to the -api and they already happened.
        while True:
            data = await asyncio.to_thread(registry_tasks.get, task_id) or {}
            status = str(data.get("status") or "")
            if status in ("completed", "failed", "cancelled", "unknown"):
                break
            await asyncio.sleep(2.0)
        if status != "completed":
            return
        if not target_ids:
            await cross_orchestrator.sync_all()
            return
        db = SessionLocal()
        try:
            for target_id in target_ids:
                try:
                    service_grants.ensure_grant(
                        db, source_service_id, target_id, actor_user_id=actor_id,
                    )
                except ValueError as exc:
                    _log.warning("deploy-panel grant %s -> %s skipped: %s",
                                 source_service_id, target_id, exc)
        finally:
            db.close()
        # R13 — the panel wrote rows for ONE known source: refresh that source,
        # never the whole stack.  The sync writes the dnsmasq wildcard and
        # re-ensures the proxy membership; the app starts after it lands.
        action = await cross_orchestrator.sync_source(source_service_id)
        if action.get("outcome") != OUTCOME_OK:
            cross_orchestrator.schedule_retry_sync([source_service_id])
            _log.info(
                "deploy-with-dependencies sync for %s is %s; targeted retry scheduled",
                source_service_id, action.get("outcome"))
        # The resolver's supervisor compares content on a one-second poll
        # (G101/C67); the first call must see the new wildcard.
        await asyncio.sleep(PRE_START_SYNC_WAIT_S)
        try:
            await provision_service.start_instance(source_service_id)
        except Exception:  # noqa: BLE001 — the instance stays `building` and is visible
            _log.exception("start after pre-start grants failed for %s",
                           source_service_id)
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
    # C12 — carry the used-or-written set to the deploy that claims a reviewed
    # session, so its snapshot matrix is the generation's, not a guess.
    if isinstance(result.get("env_meta"), dict):
        pdr.set_env_meta(service_id, result.get("env_meta"))

    if record.get("operation") != pdr.OPERATION_DEPLOY:
        # The review gate: the operator decides whether to deploy later.
        return

    try:
        _validate_policy(intent, final_selection, require_ready=True)
        await _claim_and_register(service_id, intent, final_selection, env_name,
                                  env_meta=result.get("env_meta"))
    except WorkflowError as exc:
        # _claim_and_register already recorded a capacity refusal / failure;
        # a policy failure here is a fail-closed deploy failure. A guard pause
        # is the same kind of already-recorded state: never overwrite it.
        if exc.code not in ("at_capacity", "queue_full", "queue_unavailable",
                            "deploy_failed", "workflow_paused"):
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
                    # T9/T10 — the exact per-instance snapshot paths, present
                    # only when the snapshot exists (never a guessed name).
                    **_snapshot_rows({**svc, "user_name": uname}),
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

    # G92 R8.1 — a session card shows `queued`/`planned` while its generation
    # job waits in the gateway queue. One query for every referenced job.
    statuses = _generation_statuses(
        [s.get("generation_task_id") for s in sessions.values()
         if s.get("generation_task_id")]
    )
    for group in groups_out:
        for entry in group.get("entries") or []:
            task_id = entry.get("generation_task_id")
            if task_id:
                entry["job_status"] = statuses.get(str(task_id))
            nested = entry.get("session")
            if isinstance(nested, dict) and nested.get("generation_task_id"):
                nested["job_status"] = statuses.get(str(nested["generation_task_id"]))

    groups_out.sort(key=lambda g: g.get("user_name") or "")
    return {"groups": groups_out,
            "count": sum(len(g["entries"]) for g in groups_out),
            "orphans": payload.get("orphans") or []}


def _generation_statuses(task_ids: list[str]) -> dict[str, str]:
    ids = [str(t) for t in task_ids if t]
    if not ids:
        return {}
    from sqlalchemy import text as _text
    db = SessionLocal()
    try:
        rows = db.execute(
            _text("SELECT task_id, status FROM gateway.tasks "
                  "WHERE task_id = ANY(:ids)"),
            {"ids": ids},
        ).mappings().all()
        return {str(r["task_id"]): r["status"] for r in rows}
    except Exception:  # noqa: BLE001 — a card must render without this
        return {}
    finally:
        db.close()


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

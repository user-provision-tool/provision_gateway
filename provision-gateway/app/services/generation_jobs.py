"""Async LLM generation jobs — gateway-side job mechanism persisted in SQLite.

Design §Generation (async execution) + §Implementation notes:

- Jobs persist in the gateway SQLite DB (``generation_jobs`` table).
- ``POST`` creates a queued job and schedules an asyncio task; ``GET`` lists
  (with TTL prune of finished jobs older than N days); ``GET /{id}`` polls
  progress; ``DELETE /{id}`` cancels (the in-flight LLM call's result is
  discarded, the job marked cancelled).
- Concurrent generations for the same recipe serialize on a per-(service,
  recipe) asyncio lock; different recipes run as independent jobs.
- "Generate all missing" is ONE job with sequential phases
  (compose → nginx → env), progress per phase.
- DB-session discipline: no session held across awaits (short-lived sessions
  per write).
- Restart recovery: jobs left ``queued``/``running`` by a crashed process are
  marked ``failed`` ("interrupted by restart") on startup.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from ..config import settings
from ..models.generation_job import (
    JOB_ORIGIN_DEPLOY,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_AWAITING_INPUT,
    JOB_STATUS_PAUSED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    GenerationJob,
    job_origin,
)
from .llm_service import llm_service

ProgressFn = Callable[[str], Awaitable[None]]

# Multi-phase order: compose is the root; nginx is blocked until compose
# exists; env comes from the var-scan of the resolved compose (design
# §Dependency graph).
# The standalone/generate order. `serving_ports` sits BETWEEN compose and nginx
# because it is derived FROM the compose (the ports it mentions) and the nginx
# conf is derived from it in turn (GRILLING G37's "generated after compose and
# before nginx").
PHASE_ORDER = ("compose", "serving_ports", "validate_ports", "stream_conf", "nginx", "validate_conf", "env")

# Phase → config_type used by the LLM.
PHASE_CONFIG_TYPE = {
    "compose": "docker_compose",
    "serving_ports": "serving_ports",
    "nginx": "nginx_conf",
    "env": "env_file",
}

# Phase → the name a `generate new` output is written under, as a TEMPLATE: the
# compose and nginx entries carry a `{token}` slot, filled with the generation
# JOB's id (`_phase_names(job_id)`).
#
# The `.base` tag is what keeps generation from impersonating a repo file:
# naming the output `docker-compose.yml` risked overwriting a file the operator
# owns, and made a generated file indistinguishable from a shipped one
# (deploy-panel-selection-and-generation-todo.md §3). The tag goes BEFORE the
# token, and both before the extension, so the platform's own candidate
# predicates still match (`docker-compose*` + `.yml`; `*.conf`) and the files
# stay selectable everywhere they were.
#
# WHY THE TOKEN (GRILLING G33). These two artifacts are RECIPE-LEVEL and
# user-agnostic — `.base`/`.fix` compose and nginx names live in the recipe dir,
# while the reservation key is `user|service|recipe_path|label`. Two generations
# whose keys differ only by user or label therefore address the SAME path, and a
# parallel pair interleaves: last writer wins, and one run validates a compose
# another just replaced. The token is the JOB ID only because it is unique and
# already at hand.
#
# WHAT THE TOKEN IS NOT. It is NOT a link to the job: no ownership, no
# lifecycle, no collection keyed on the job's existence. These are the RECIPE's
# artifacts and they outlive the job that wrote them — **the generated files are
# NEVER auto-deleted**, not by the cleanup contract, not by a TTL/cap sweep, and
# not when the job is pruned or gone. Selection, the readiness scan, the
# pickers, the Generated Files column and the -api's render all identify them by
# PATTERN, never by exact name, so nothing else has to know the token.
#
# env is the exception, on both counts: it is per-user (`.env.raw.{user}.{label}`,
# already collision-free) or recipe-level for a genuine fix job, and its name
# carries agreed meaning (`.raw` vs `.fix`) — see §3's table.
PHASE_FILENAME = {
    "compose": "docker-compose.base.{token}.yml",
    # The serving-ports artifact follows the same convention the other two base
    # files do: stem, `.base`, the job-id token, then the EXTENSION (task-doc
    # R1.1) — so the platform's existing pattern predicates keep matching it.
    "serving_ports": "serving_ports.base.{token}.yml",
    "nginx": "nginx.base.{token}.conf",
    # G56 — the stream conf is RECIPE-LEVEL exactly like the http conf: the
    # generated artifact is a stream TEMPLATE (`server_name` carries
    # `{{ hostname_stem }}--<sub>.{{ domain_name }}`), and the -api renders it
    # into the per-instance `*.stream.conf`. No user/label in the name, because
    # the file is the recipe's, not the deployment's.
    "stream_conf": "nginx.stream.base.{token}.conf",
    "env": ".env",
}

# The GUIDED RESTART's names, next to the original they correct, with the `.fix`
# tag — following the `.raw` pattern (tag directly after the stem). They carry
# the same `{token}` as their `.base` siblings, and for the same reason: the fix
# family is recipe-level too, so a guided restart and a parallel `generate new`
# used to be able to overwrite each other.
#
# The TRIGGER picks these, not the slot (G25): a path restarted under guidance
# generates `.fix`, a normal `generate new` generates `.base` — which is why
# `_fix_mode` reads the request's `fix` flag rather than the phase.
#
# NOTE (G26): `LLM based fix` no longer writes anything. It is the ANALYSIS step
# — it reads the log and returns a guide. These names are used by the restart
# that `check` triggers afterwards, which is the only thing that writes `.fix`.
FIX_FILENAME = {
    "compose": "docker-compose.fix.{token}.yml",
    "serving_ports": "serving_ports.fix.{token}.yml",
    "nginx": "nginx.fix.{token}.conf",
    "stream_conf": "nginx.stream.fix.{token}.conf",
    "env": ".env.fix",
}


def _phase_names(job_id: int, *, fix: bool) -> dict[str, str]:
    """The literal output names for one job — `PHASE_FILENAME`/`FIX_FILENAME`
    with the job's collision token filled in (GRILLING G33).

    One place resolves the token, so the writer, the progress lines and the
    "did I produce this file?" test in the validate phase cannot disagree about
    a name. `env` has no slot and passes through untouched.
    """
    family = FIX_FILENAME if fix else PHASE_FILENAME
    return {phase: name.format(token=job_id) for phase, name in family.items()}


def _instance_used_global_proxy(user: str, service: str, label: str) -> bool:
    """Did the FAILED instance deploy with the global proxy's build args?

    The LLM-fix analysis request carries no deploy options, so the failed
    instance's own registry entry is the only record of the proxy setting that
    `check` has to replay (G20: a resumed path replays the options it was
    created with). The gateway mounts the same registry the -api writes, so this
    is a read, not an -api call — and it runs BEFORE the handoff's delete, which
    is what removes the entry that carries the answer.
    """
    try:
        from .registry import Registry
        from .proxy_service import build_args_carry_global_proxy

        entry = Registry(settings.REGISTRY_FILE).get_entry(user, service, label)
        return build_args_carry_global_proxy((entry or {}).get("build_args"))
    except Exception:  # noqa: BLE001 — a missing/unreadable registry is "no proxy"
        return False


def _unfilled_externals(kvs: dict[str, Any], external_values: dict[str, str] | None) -> list[str]:
    """Names of `external` variables for which no value was supplied.

    The pause makes this empty on the normal path (the operator must fill every
    required field before Submit is enabled). It exists so a bypassed popup — an
    API-only caller, or a future path that writes without pausing — is reported
    rather than silent: the value falls back to whatever the base files hold
    (see `_apply_env_kvs`) and the caller records a warning naming the variable.
    """
    supplied = external_values or {}
    out: list[str] = []
    for k, v in (kvs or {}).items():
        if isinstance(v, dict) and v.get("env_type") == "external" and not str(supplied.get(str(k)) or "").strip():
            out.append(str(k))
    return sorted(out)


def _apply_env_kvs(
    mech: str, kvs: dict[str, Any], external_values: dict[str, str] | None = None
) -> str:
    """Assemble the final interpolation env programmatically: start from the
    mechanical merge (which already carries every compose-referenced variable)
    and apply the LLM's ``necessary_env_kvs`` overrides on top (design §2 env
    redesign). New keys from the map are appended; newlines inside values are
    stripped (KEY=VALUE only).

    ``external_values`` carries what the OPERATOR supplied for variables the
    model classified as third-party credentials. Those must never be minted: a
    random string for an externally-issued key looks configured and fails at
    first use, which is worse than an empty one. A name in ``external_values``
    wins over whatever the map carried; a name with no supplied value is written
    empty (the pause that collects it happens before this runs)."""
    vals: dict[str, str] = {}
    order: list[str] = []
    for raw in (mech or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k not in vals:
            order.append(k)
        vals[k] = v
    for k, v in (kvs or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if isinstance(v, dict):
            # Decision-4 schema: {env_type: secret|non-secret|external, value}.
            env_type = v.get("env_type")
            raw = v.get("value")
            if env_type == "external":
                # Operator-supplied, never minted. Taken from the value they
                # typed into the pause popup. When nothing was supplied — the
                # API-only path that bypasses the popup — keep whatever the base
                # files already had rather than blanking it: an existing value
                # may well be correct, and blanking it silently breaks the
                # instance. The caller records a warning naming the variable
                # (see `_unfilled_externals`).
                supplied = str((external_values or {}).get(k, "") or "")
                val = supplied if supplied else vals.get(k, "")
            elif env_type == "secret" or (env_type == "non-secret" and raw is None):
                # Secret (or an empty-required non-secret) → the GATEWAY mints
                # the value as a PLAIN random secret (env files expect a plain
                # secret). Never base64-encode; never touch secrets that already
                # carry a value (those are not sent here as 'secret').
                import secrets as _secrets
                val = _secrets.token_urlsafe(18)  # plain, ~24 chars
            else:
                val = str(raw if raw is not None else "")
        else:
            val = str(v)  # back-compat: plain string value
        val = val.replace("\r", "").replace("\n", "")
        if k not in vals:
            order.append(k)
        vals[k] = val
    return "\n".join(f"{k}={vals[k]}" for k in order) + ("\n" if order else "")


class JobCancelled(Exception):
    """Raised inside a runner to abort when the operator cancelled the job."""


class GenerationJobError(ValueError):
    """Raised for invalid job requests (router → 400)."""


def _int_setting(name: str, default: int) -> int:
    """An int setting that HONOURS an explicit 0.

    `int(getattr(settings, name, default) or default)` silently discards a
    configured 0 — and 0 is meaningful for every caller here: a cap of 0 keeps no
    finished records, a TTL of 0 prunes them immediately, and a generation limit
    of 0 refuses every generation (a maintenance mode). Only a MISSING or
    unparseable value falls back to the default.
    """
    raw = getattr(settings, name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


class GenerationCapacityError(RuntimeError):
    """The GATEWAY's own generation pool is full (GRILLING G29).

    A sibling of the -api's `TaskCapacityError`, with a different reason: that
    one guards the HOST against concurrent docker work, this one guards the LLM
    endpoint and cost. Both are refusals the operator must see, and they are
    distinguished by a stable code so the card can show the right remedy —
    `at_capacity` ("cancel a hanging task and retry") versus
    `generation_at_capacity` ("wait for a generation, or cancel one").
    """

    code = "generation_at_capacity"

    def __init__(self, in_flight: int, capacity: int):
        self.in_flight = in_flight
        self.capacity = capacity
        super().__init__(
            f"generation at capacity: {in_flight} job(s) running (limit {capacity}); "
            "wait for one to finish, or cancel one"
        )


class GenerationJobManager:
    """Creates, runs, polls, cancels and prunes generation jobs.

    RACE WINDOW (documented, not mitigated — design §Implementation notes
    L270-272): the per-(service, recipe) lock above serializes *generation
    jobs within this process only*.  Generation-save vs deploy is
    cross-process (gateway save → provision-api register, which reads at
    render time) with no shared lock; saves are atomic (write then marker)
    and deploys read at render time, leaving the narrow acceptable race
    window documented in ``save_generated_files`` — it is NOT mitigated.
    """

    # The terminal set of G30's cleanup contract — the ONLY statuses either
    # sweep may touch. `paused`/`awaiting_input` are absent on purpose: a record
    # waiting for the operator is never pruned.
    _TERMINAL = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)

    # The statuses a DELETE may act on. `paused` IS one of them: G27 made a pause
    # a TERMINAL LEG whose job has STOPPED but still EXISTS, and G34 requires both
    # the card's Delete and the Source Projects row's Cancel to end such a leg.
    # Without it the DELETE was a silent no-op answering 200 with
    # `status: "paused"`, so the record could never become sweep-eligible.
    # Declared as a CLASS ATTRIBUTE so `routers/pre_deploy.py` reads the same
    # tuple — one home, so the two guards cannot drift apart.
    CANCELLABLE_STATUSES = (
        JOB_STATUS_QUEUED, JOB_STATUS_RUNNING,
        JOB_STATUS_AWAITING_INPUT, JOB_STATUS_PAUSED,
    )

    # The subset whose leg has a RUNNER somewhere: only these need the in-process
    # discard flag. A paused leg's runner has already returned, so flagging it
    # would leave an id in `_cancelled` that nothing clears — the id is dropped
    # only when a terminal status is written, and a paused leg writes nothing
    # afterwards. A stale id there would cancel a LATER job that reused the
    # number (ids restart at 1 in a fresh DB).
    _RUNNING_LEG_STATUSES = (
        JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_AWAITING_INPUT,
    )

    def __init__(self) -> None:
        self._locks_guard = asyncio.Lock()
        self._project_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancelled: set[int] = set()
        # job_id -> monotonic time of the last ROW check (D1). Keeps the
        # authoritative check off the per-token path.
        self._cancel_polled: dict[int, float] = {}
        # In-flight (fire-and-forget) session-log appends per custom task id.
        # Drained before finish so the last lines land BEFORE the terminal
        # status (feature 5 ordering).

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _lock_for(self, service_name: str, recipe_path: str) -> asyncio.Lock:
        # Called from within the running loop (no await on the guard itself
        # is safe here; the dict is only mutated from the loop).
        key = (service_name, recipe_path or ".")
        lock = self._project_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[key] = lock
        return lock

    def create_job(
        self, db: Session, service_name: str, recipe_path: str,
        job_type: str, request: dict[str, Any],
    ) -> GenerationJob:
        """Persist a queued job and schedule its execution."""
        phases = self._compute_phases(job_type, request)
        job = GenerationJob(
            service_name=service_name,
            recipe_path=recipe_path or "",
            job_type=job_type,
            status=JOB_STATUS_QUEUED,
            phase=phases[0] if phases else "pending",
            phase_index=0,
            phase_total=len(phases) or 1,
            progress="queued",
            request=json.dumps(request, ensure_ascii=False),
        )
        # THE COUNT CAP, ENFORCED ON SUBMIT (G30). The TTL sweep runs on reads
        # (`list_jobs` and the single-job GET); the cap runs here, which is the
        # same split the -api uses (`_cleanup_excess` from `submit`,
        # `_cleanup_stale` from `get`/`list_all`). Both engines agreeing on the
        # TRIGGER rule is the point — D3's retraction, and what actually keeps
        # the two stores from drifting. It cannot affect the capacity count
        # below: only finished rows are eligible.
        self.prune_excess(db)

        # CAPACITY, COUNTED IN THE DATABASE (GRILLING G31). Not from an
        # in-memory map: the gateway runs `uvicorn --workers 4`, so a per-process
        # count would let four workers each admit `limit` jobs and the effective
        # limit would be four times the configured one. Counting here, inside the
        # same transaction as the insert, is what makes the number true.
        #
        # `awaiting_input` counts as in-flight: a parked leg still holds its
        # slot's work (G27 will make a pause TERMINAL, at which point it stops
        # counting — the two changes go together).
        from ..config import settings as _settings
        limit = _int_setting("GATEWAY_MAX_GENERATIONS", 4)
        in_flight = (
            db.query(GenerationJob)
            .filter(GenerationJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING,
                                              JOB_STATUS_AWAITING_INPUT]))
            .count()
        )
        if in_flight >= limit:
            db.rollback()
            raise GenerationCapacityError(in_flight, limit)

        db.add(job)
        db.commit()
        db.refresh(job)
        loop = asyncio.get_running_loop()
        self._tasks[job.id] = loop.create_task(self._run_job(job.id))
        return job

    def claim_job(self, job_id: int) -> bool:
        """Claim a queued job for THIS process — atomically (GRILLING G31).

        `UPDATE ... WHERE id = ? AND status = 'queued'` and a rowcount check:
        exactly one caller can win, whoever it is and whichever process it runs
        in. Without it two workers can both see a `queued` row and both run the
        job — the same job, twice, writing the same files.

        Returns True when this caller owns the job."""
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            n = (
                db.query(GenerationJob)
                .filter(GenerationJob.id == job_id,
                        GenerationJob.status == JOB_STATUS_QUEUED)
                .update({GenerationJob.status: JOB_STATUS_RUNNING},
                        synchronize_session=False)
            )
            db.commit()
            return n == 1
        finally:
            db.close()

    @classmethod
    def _compute_phases(cls, job_type: str, request: dict[str, Any]) -> list[str]:
        if job_type == "fix":
            # `LLM based fix` no longer WRITES anything (GRILLING G26). It is the
            # ANALYSIS step: it reads the failed task's log plus the deployment
            # context and returns a FIXING GUIDE. The `.fix` files come from the
            # restarted path that `check` triggers (G25), which is also the only
            # route that passes the operator's confirmation — so a single phase,
            # and it produces no files.
            return ["analyze"]
        if job_type == "generate_missing" and request.get("resume_env"):
            # G27: answering a CREDENTIAL pause is a new leg that has only the
            # env left to do — the mechanical text and the LLM's key map were
            # carried by the pause, so there is no LLM call and no re-scan.
            return ["env"]
        if job_type == "generate_missing":
            # Deploy-time auto-generation runs the phases whose target is
            # missing — a project with an existing compose/nginx does NOT re-run
            # the LLM on those (design §8.3: "first deploy with existing
            # compose/nginx still shows the env step"). The recipe missing-scan
            # mirrors the provision-api check_missing_files.
            missing = cls._recipe_missing(request)
            sel = request.get("selection") or {}
            # G13/G20/G25 — `generate new` means NOT SELECTED, and the SELECTION
            # SET is what decides the work, not the disk. On a DEPLOY-scoped run
            # every slot must come out NAMED, because all three paths are what the
            # -api is sent and a refused deployment must carry all three (G20).
            # So an unselected slot is generated even when a file of that kind
            # already sits in the recipe.
            #
            # Without this the nginx phase was skipped whenever the recipe
            # already had an nginx.conf: `selection.nginx` stayed null, the
            # deploy payload carried NO nginx path, and the -api was asked to
            # deploy an unroutable instance. Found live on `bob|probe-interp||3`,
            # whose refused options held compose + env and no nginx at all.
            #
            # Scoped to deploy-scoped runs. `user_name` is what the deploy panel
            # sends and the standalone Generate panel does not — for that panel
            # "missing" is exactly what its action means, so it keeps the
            # disk-based reading. `conflict_unselected` (G21) marks a resumed
            # conflict path, which is deploy-scoped by construction.
            _deploy_scoped = (
                bool(request.get("user_name"))
                or request.get("conflict_unselected") is not None
            )
            _gen_compose = "docker-compose" in missing or (
                _deploy_scoped and not sel.get("compose")
            )
            _gen_nginx = "nginx.conf" in missing or (
                _deploy_scoped and not sel.get("nginx")
            )
            # The serving-ports artifact (task-doc R1.1/T12), by the SAME rule as
            # its siblings: deploy-scoped runs demand every slot be NAMED (G20),
            # so an unselected one is generated even if a file of that kind
            # already sits in the recipe. Positioned after the compose's
            # validation — it is derived from that compose — and before nginx.
            # R1.3's `None` is a first-class RECORDED state: "deliberately no file".
            # It suppresses the generation exactly as it suppresses both
            # validations — and the recording is what tells this apart from "no
            # file exists", both here and on the card.
            _sp_none = bool(sel.get("serving_ports_none"))
            _gen_serving_ports = (not _sp_none) and (
                "serving_ports" in missing or (
                    _deploy_scoped and not sel.get("serving_ports")
                )
            )
            phases: list[str] = []
            if _gen_compose:
                phases.append("compose")
                # §3b: validate the compose BEFORE anything is built on it.
                phases.append("validate")
            elif sel.get("compose"):
                # A compose that already exists is still the one this deploy
                # will USE — and the conflict §3b guards against lives in the
                # operator's own selected file just as readily as in a generated
                # one. So validate whenever a compose is in play, not only when
                # this run generated it.
                phases.append("validate")
            if _gen_serving_ports:
                phases.append("serving_ports")
            # T9 — the internal stream conf, produced from the serving-ports rows
            # whose kind is a STREAM protocol, and **conditional on the TLS toggle**
            # (R8.4): with TLS off the slot is not generated, not demanded by "every
            # slot must come out named", and not carried in the refusal record — so
            # a replay reproduces the same shape. A `serving_ports` row may still be
            # declared with TLS off; that row simply has no conf, no route and no
            # exposure.
            # T14 / R1.3: the two validations are STEPS, and the skip rule is
            # stated over ARTIFACTS, not phases — a step runs when its input
            # artifact is IN PLAY (selected, or produced by this run), and is
            # skipped when the artifact is deliberately absent. That is what makes
            # "a selected compose is still validated" and "a deliberately absent
            # ports file skips both checks" the same rule rather than two.
            _sp_in_play = "serving_ports" in phases or bool(sel.get("serving_ports"))
            if _sp_in_play and not sel.get("serving_ports_none"):
                phases.append("validate_ports")

            _tls_on = bool((request.get("deploy_metadata") or {}).get("https")
                           or request.get("https") or request.get("tls"))
            # G56 — two sources, ONE rule, mirroring HTTP: a SELECTED stream conf
            # is used as-is (generation bypassed); absent → generated. The slot
            # is applicable only when TLS is on and a serving-ports artifact is in
            # play, because the stream rows come from it.
            _gen_stream_conf = bool(
                _tls_on
                and not sel.get("stream_conf")
                and ("serving_ports" in phases or sel.get("serving_ports"))
            )
            if _gen_stream_conf:
                phases.append("stream_conf")
            if _gen_nginx:
                phases.append("nginx")
            # The conf's agreement with the ports file is checkable whenever BOTH
            # are in play — and "in play" includes a conf the operator SELECTED,
            # not only one this run generated. Nesting this under the generation
            # was a real gap, caught by the test for exactly this rule (R1.3 states
            # it for the compose: "a selected artifact is still validated").
            if (_sp_in_play and not sel.get("serving_ports_none")
                    and (_gen_nginx or sel.get("nginx")
                         or _gen_stream_conf or sel.get("stream_conf"))):
                phases.append("validate_conf")
            # The Source Projects panel passes skip_env (env is deploy-time
            # only — design §8.7).
            if not request.get("skip_env"):
                # env always last — the env phase itself short-circuits when
                # the compose references no ${VAR}s (empty pass).
                phases.append("env")
            return phases
        cat = request.get("category")
        if cat in PHASE_ORDER:
            return [cat]
        if cat in ("docker_compose", "nginx_conf", "env_file"):
            return {"docker_compose": ["compose"], "nginx_conf": ["nginx"], "env_file": ["env"]}[cat]
        raise GenerationJobError(f"Unknown generation category: {cat}")

    @classmethod
    def _recipe_missing(cls, request: dict[str, Any]) -> list[str]:
        """Mirror the provision-api ``check_missing_files`` presence scan on the
        recipe dir (compose / serving_ports / nginx). Returns which of them are
        ABSENT from the recipe, i.e. which LLM phases a generate-missing run must
        include.

        The THREE parts must agree or the deploy has a phase that never runs and
        no file to deploy — the failure mode `_is_compose_name`'s comment warns
        about, now for the serving-ports artifact too (task-doc R1.1/G20: a
        recipe without one cannot deploy).
        """
        from ..services.file_sets import (
            _is_compose_name, _is_serving_ports_candidate, _recipe_dir,
        )

        service_name = request.get("service_name") or ""
        recipe_path = request.get("recipe_path") or ""
        try:
            recipe_dir = _recipe_dir(Path(settings.SOURCE_PROJECTS_DIR) / service_name, recipe_path)
        except Exception:
            return ["docker-compose", "serving_ports", "nginx.conf"]  # unknown recipe — assume missing
        missing: list[str] = []
        try:
            files = [f.name for f in recipe_dir.iterdir() if f.is_file()]
        except OSError:
            return ["docker-compose", "serving_ports", "nginx.conf"]
        # Same definition the candidate scan uses (a template still counts as
        # present) — if these two disagree, the compose phase is skipped while
        # the panel offers nothing to select and the deploy has no compose.
        has_compose = any(_is_compose_name(f, allow_template=True) for f in files)
        has_serving_ports = any(_is_serving_ports_candidate(f) for f in files)
        has_nginx = any(
            f.endswith(".conf.j2") or (f.endswith(".conf") and not f.endswith(".j2"))
            for f in files
        )
        if not has_compose:
            missing.append("docker-compose")
        if not has_serving_ports:
            missing.append("serving_ports")
        if not has_nginx:
            missing.append("nginx.conf")
        return missing

    # ------------------------------------------------------------------
    # Env-phase helpers (design §2)
    # ------------------------------------------------------------------

    def _recipe_dir_for(self, service_name: str, recipe_path: str) -> Path:
        from ..services.file_sets import _recipe_dir
        return _recipe_dir(Path(settings.SOURCE_PROJECTS_DIR) / service_name, recipe_path)

    @staticmethod
    def _llm_active(db: Session) -> bool:
        from ..models.llm_config import LLMConfig
        cfg = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        return bool(cfg and cfg.mode == "byok" and cfg.byok_api_key_enc)

    def _mechanical_env(
        self, service_name: str, recipe_path: str, request: dict[str, Any]
    ) -> str | None:
        """Mechanical per-user env content (design §2 cases 1/3/4).

        Merge the selected env files (selection order, later wins); when none
        selected, fill from the compose ``${VAR}`` surface defaults. Returns
        None when the compose references no vars (no generation at all).
        """
        from ..utils.var_scan import scan_compose_files

        recipe_dir = self._recipe_dir_for(service_name, recipe_path)
        selection = request.get("selection") or {}
        sel_env = [p for p in selection.get("env", []) if isinstance(p, str)]
        sel_compose = [p for p in selection.get("compose", []) if isinstance(p, str)]
        compose_paths = [recipe_dir / p for p in sel_compose if (recipe_dir / p).is_file()]

        scan = scan_compose_files(compose_paths) if compose_paths else None
        if scan is None or not scan.refs:
            # No compose ${VAR} surface → an EMPTY per-user env, not "no file".
            # The phase no longer skips (deploy-panel-selection-and-generation-
            # todo.md §4): the gateway is the single producer of the per-user
            # env in every case, and the empty file is what keeps compose's
            # auto-read of the recipe `.env` overridden at deploy time.
            return ""

        lines: dict[str, str] = {}
        for p in sel_env:
            fp = recipe_dir / p
            if not fp.is_file():
                continue
            try:
                raw = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                lines[k] = v
        for ref in scan.refs:
            if not ref.has_default:
                continue
            # PRECEDENCE (GRILLING G16, task doc §2b): an in-file NON-EMPTY value wins and is
            # never overwritten by the compose default. An EMPTY value (`VAR=`) counts as NOT
            # SET — `:-` semantics — so the compose's own default is written in.
            #
            # The guard used to be presence (`ref.name not in lines`), which left `VAR=` empty
            # while compose would have substituted the default anyway: the env file then
            # disagreed with what the instance actually ran on.
            if str(lines.get(ref.name, "")).strip():
                continue
            lines[ref.name] = ref.default_raw or ""
        return "\n".join(f"{k}={v}" for k, v in lines.items()) + ("\n" if lines else "")

    # ------------------------------------------------------------------
    # The job's own step-log file (GRILLING G28)
    # ------------------------------------------------------------------
    #
    # Every generation writes `gt-<id>.log` under the gateway's OWN task_logs
    # dir — the same convention the -api uses for ITS tasks (`task-<id>.log`
    # under its TASK_LOG_DIR). This replaces the `record` mirror: the step log
    # used to be POSTed line-by-line into the -api's store so the generation
    # would appear there, which is what made a generation consume a capacity it
    # never used (and, before the `kind: "record"` exclusion, refuse every
    # deploy). The gateway owns the record now, so it owns the log — and the log
    # is deleted WITH the record (G30's cleanup contract).

    def _log_dir(self) -> Path:
        d = Path(settings.GATEWAY_DATA_DIR) / "task_logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d

    def _log_path(self, job_id: int) -> Path:
        return self._log_dir() / f"gt-{job_id}.log"

    def _log_append(self, job_id: int, entry: dict) -> None:
        """Append one JSON line. Best-effort — a log never fails a job."""
        try:
            with open(self._log_path(job_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _log_delete(self, job_id: int) -> None:
        try:
            self._log_path(job_id).unlink()
        except OSError:
            pass

    def prune_finished(self, db: Session) -> int:
        """Delete finished jobs older than the TTL. Returns count pruned.

        The per-task log goes WITH the record (G30's cleanup contract, and the
        same rule the -api applies to its own store) — a log whose record is
        gone is unreachable anyway, and leaving it behind is how a log directory
        grows without bound.

        The TTL is CONFIGURED, not hard-coded: G30's contract says one TTL
        number with one default, shared with the -api (whose `TASK_TTL_SECONDS`
        defaults to the same week). A gateway-only constant would let the two
        stores keep records for different lengths of time and nobody would see
        it. `paused`/`awaiting_input` are deliberately NOT in the terminal set.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=_int_setting("GATEWAY_JOB_TTL_SECONDS", 604800)
        )
        return self._delete_finished(
            db, GenerationJob.updated_at < cutoff
        )

    def prune_excess(self, db: Session) -> int:
        """Evict the OLDEST finished jobs beyond the count cap. Returns count.

        The second half of G30's contract (`_cleanup_excess` on the -api side),
        and the same trigger rule: the cap is enforced on SUBMIT, while the TTL
        sweep runs on READS. Both engines must agree on that rule, not only on
        the terminal set and the TTL.

        Only FINISHED records are eligible — the same eligibility rule as the
        TTL sweep. The -api's version sorts every task by `created_at` without
        a status filter, so at its cap it can evict a RUNNING task; this one
        cannot, which matters more here because a gateway job whose row has been
        evicted has no row left for its runner to write its terminal status to.
        """
        cap = _int_setting("GATEWAY_MAX_JOBS", 1000)
        finished = (
            db.query(GenerationJob)
            .filter(GenerationJob.status.in_(self._TERMINAL))
        )
        excess = finished.count() - cap
        if excess <= 0:
            return 0
        oldest = [
            j.id for j in finished
            .order_by(GenerationJob.created_at.asc(), GenerationJob.id.asc())
            .limit(excess)
            .all()
        ]
        if not oldest:
            return 0
        return self._delete_finished(db, GenerationJob.id.in_(oldest))

    def _delete_finished(self, db: Session, criterion) -> int:
        """Delete the finished rows matching `criterion`, logs first."""
        doomed = [
            j.id for j in db.query(GenerationJob)
            .filter(GenerationJob.status.in_(self._TERMINAL), criterion)
            .all()
        ]
        for jid in doomed:
            self._log_delete(jid)
        if not doomed:
            return 0
        rows = (
            db.query(GenerationJob)
            .filter(GenerationJob.id.in_(doomed))
            .delete(synchronize_session=False)
        )
        db.commit()
        return rows or 0

    def list_jobs(self, db: Session, include_result: bool = False) -> list[dict]:
        self.prune_finished(db)
        jobs = db.query(GenerationJob).order_by(GenerationJob.id.desc()).limit(200).all()
        return [j.to_dict(include_result=include_result) for j in jobs]

    def list_fix_analyses(self, db: Session) -> list[dict[str, Any]]:
        """The fix analyses currently running (GRILLING G26).

        `failed + analyzing` is computed GATEWAY-SIDE, from the gateway's own
        analysis job: while one runs, the instance is still `failed` in the -api
        registry and no pre-deploy session exists yet, so neither store can
        express that state on its own. This is the gateway's own view, exposed
        for the card — not an inbound call from the -api.
        """
        rows = (
            db.query(GenerationJob)
            .filter(
                GenerationJob.job_type == "fix",
                GenerationJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING]),
            )
            .order_by(GenerationJob.id.desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for j in rows:
            try:
                req = json.loads(j.request or "{}")
            except ValueError:
                req = {}
            out.append({
                "job_id": j.id,
                "status": j.status,
                "user_name": req.get("user_name") or "",
                "service_name": j.service_name,
                "recipe_path": j.recipe_path or "",
                "label": str(req.get("label") or "0"),
            })
        return out

    def get_job(self, db: Session, job_id: int) -> GenerationJob | None:
        return db.query(GenerationJob).filter(GenerationJob.id == job_id).first()

    def job_status(self, job_id: int) -> str | None:
        """One job's status, read with its own short-lived session.

        Exists so callers that hold only a job ID cannot get this wrong. Both of
        them used to call ``get_job(job_id)`` without the ``db`` argument, and
        BOTH swallowed the resulting ``TypeError`` in their own
        ``except Exception`` — so the pre-deploy sweep silently never dropped a
        stale session (their combinations stayed reserved forever, refusing
        every later deploy with 409) and the session-delete guard silently never
        refused a live job, which is the exact orphaning it exists to prevent.
        Found live in T13, chasing a `paused` session the sweep should have
        dropped minutes earlier.
        """
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            job = self.get_job(db, int(job_id))
            return job.status if job is not None else None
        except Exception:
            return None
        finally:
            db.close()

    def cancel_job(self, db: Session, job_id: int) -> GenerationJob | None:
        job = self.get_job(db, job_id)
        if not job:
            return None
        # `awaiting_input` MUST be cancellable. It is neither queued nor running,
        # so the old guard silently did nothing: the job stayed parked forever.
        # `paused` is cancellable for the same reason under G27/G34 — see
        # `CANCELLABLE_STATUSES`.
        #
        # There is nothing to release on the -api side any more (GRILLING G28):
        # the `record` mirror is gone, so a generation holds no -api task and
        # cannot leak one. The old code also DELETEd the mirrored task here to
        # free a pool slot — a leak class that no longer exists.
        if job.status in self.CANCELLABLE_STATUSES:
            _had_runner = job.status in self._RUNNING_LEG_STATUSES
            if _had_runner:
                self._cancelled.add(job_id)
            job.status = JOB_STATUS_CANCELLED
            # A live leg's runner writes the final progress itself as it unwinds;
            # a paused leg has no runner, so this write is its LAST word and must
            # not promise a further one ("cancelling…" would stay forever).
            job.progress = "cancelling…" if _had_runner else "cancelled"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
            # This write is the OTHER way a job reaches a terminal status, and
            # the rule is about the status, not about who wrote it: a cancelled
            # leg owns no session either. `None` = the leg produced nothing to
            # hand on, so its record goes. `finish_leg` never touches a
            # `paused`/`refused` session, which is what makes this safe to call
            # unconditionally — the delete path's own cancel must not destroy the
            # record the operator is looking at.
            from ..services import pre_deploy_registry as _pdr
            _pdr.finish_leg(job_id, None)
        return job

    def recover_stale_jobs(self, db: Session) -> int:
        """Mark queued/running jobs as failed after a restart. Returns count."""
        rows = (
            db.query(GenerationJob)
            .filter(GenerationJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING]))
            .update(
                {
                    "status": JOB_STATUS_FAILED,
                    "error": "interrupted by gateway restart",
                    "updated_at": datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        self._tasks.clear()
        self._cancelled.clear()
        self._cancel_polled.clear()
        return rows or 0

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _set_status(self, job_id: int, status: str, progress: str = "", error: str | None = None,
                    phase: str | None = None, phase_index: int | None = None,
                    result: dict | None = None, usage: dict | None = None,
                    events: list | None = None, pending_input: dict | None = None) -> None:
        """Persist job state with a short-lived session (no session across awaits)."""
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
            if not job:
                return
            # CANCELLED is STICKY. A cancellation lands on whichever worker the
            # load balancer picks, while the job runs in another worker's event
            # loop; without this guard that runner finishes, writes COMPLETED,
            # and the row the operator just cancelled reads "completed" (D1,
            # found live: `generation_jobs.py` cancel is process-local). Refuse
            # to overwrite a cancelled row with a terminal status.
            if job.status == JOB_STATUS_CANCELLED and status != JOB_STATUS_CANCELLED:
                # The ROW keeps the cancel (D1) — but the LEG is over either way,
                # so its reservation is released here too. A cancel landing just
                # as the run finishes is exactly when this fires, and returning
                # without it leaves `generating` behind: the same stale
                # reservation the terminal branch below exists to drop.
                from ..services import pre_deploy_registry as _pdr
                _pdr.finish_leg(job_id, None)
                return
            job.status = status
            if status in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED):
                # The job is over: its cancel bookkeeping is moot. Both maps only
                # ever grew before this, and a stale id would cancel a LATER job
                # that happened to reuse it (ids restart at 1 in a fresh DB).
                self._cancelled.discard(job_id)
                self._cancel_polled.pop(job_id, None)
            if progress:
                job.progress = progress
            if error is not None:
                job.error = error
            if phase is not None:
                job.phase = phase
            if phase_index is not None:
                job.phase_index = phase_index
            if result is not None:
                job.result = json.dumps(result, ensure_ascii=False)
            if usage is not None:
                job.usage = json.dumps(usage, ensure_ascii=False)
            if events is not None:
                job.events = json.dumps(events, ensure_ascii=False)
            if pending_input is not None:
                job.pending_input = json.dumps(pending_input, ensure_ascii=False)
            job.updated_at = datetime.now(timezone.utc)
            # §8b: the pre-deploy session mirrors the job's own state, so a card
            # can say `pre-deploy generating…` or `paused (<reason>)`. The three
            # pause sub-cases resume on different surfaces, hence the reason.
            # G27: the leg's pause is now TERMINAL (`paused`), so the session
            # mirrors BOTH the legacy parked status and the new one; the reason
            # comes from the pause payload, wherever it lives.
            _paused = status in (JOB_STATUS_AWAITING_INPUT, JOB_STATUS_PAUSED)
            _pause_payload = (result or {}).get("pause") if isinstance(result, dict) else None
            _reason = (
                (_pause_payload or {}).get("kind")
                or (pending_input or {}).get("kind")
                or "input"
            ) if _paused else None
            db.commit()
            if _paused:
                # `set_state_for_job` — NOT list → mutate → `_save`. The old
                # shape rewrote the whole JSON store from four processes, which
                # is the lost update G31 removes (D2); the store is a table now,
                # and this is one row's UPDATE.
                from ..services import pre_deploy_registry as _pdr
                _pdr.set_state_for_job(job_id, "paused", _reason)
            elif status in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED):
                # ...and the mirror has a SECOND half: a leg that ENDS must stop
                # holding the combination. The pause branch above was the whole
                # of it, so a session whose job merely COMPLETED stayed
                # `generating` — reserving a path nothing was working on — and the
                # step that must follow was refused 409 until the 90 s sweep
                # (measured 2026-10-14).
                #
                # COMPLETED gets the state, not the delete: its files are ready
                # and the deploy has not been sent, so the record must survive —
                # it is what the deploy panel's auto-run asks for once a second,
                # and the operator's card. FAILED/CANCELLED produced nothing to
                # hand on, so theirs goes. See `finish_leg` for both.
                from ..services import pre_deploy_registry as _pdr
                _pdr.finish_leg(
                    job_id,
                    _pdr.STATE_GENERATED if status == JOB_STATUS_COMPLETED else None,
                )
        finally:
            db.close()

    def _check_cancelled(self, job_id: int) -> None:
        """Raise if this job was cancelled — checked ON THE ROW (D1).

        The in-process set is a fast path only. Under `uvicorn --workers 4` a
        `DELETE /llm/jobs/{id}` is handled by ONE worker while the job runs in
        ANOTHER worker's event loop, and an in-process set is invisible across
        that boundary: the runner never learned it had been cancelled, kept
        calling the LLM, and its own terminal write then overwrote the row.

        The row is therefore authoritative. It is consulted at most once a
        second per job — this is called from the streaming callbacks, so an
        unthrottled read would put a query behind every token — and a hit is
        promoted into the local set so the remaining checks are free.
        """
        if job_id in self._cancelled:
            raise JobCancelled()
        import time as _time
        now = _time.monotonic()
        if now - self._cancel_polled.get(job_id, 0.0) < 1.0:
            return
        self._cancel_polled[job_id] = now
        if self.job_status(job_id) == JOB_STATUS_CANCELLED:
            self._cancelled.add(job_id)
            raise JobCancelled()

    async def _run_job(self, job_id: int) -> None:
        from ..database import SessionLocal

        # Load the job + request.
        db = SessionLocal()
        try:
            job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
            if not job:
                return
            request = json.loads(job.request or "{}")
            service_name = job.service_name
            recipe_path = job.recipe_path or ""
        finally:
            db.close()

        # ONLY THE CLAIMANT RUNS IT (GRILLING G31). A second process that sees
        # the same `queued` row must not run it too — the atomic UPDATE inside
        # `claim_job` decides, and the loser returns quietly.
        if not self.claim_job(job_id):
            return

        try:
            self._check_cancelled(job_id)
            self._set_status(job_id, JOB_STATUS_RUNNING, progress="starting…")

            # Serialize same-recipe generations on the per-project lock.
            async with self._lock_for(service_name, recipe_path):
                self._check_cancelled(job_id)
                try:
                    result_files: dict[str, str] = {}
                    phases = self._compute_phases(job.job_type, request)
                    validations: dict[str, Any] = {}
                    warnings: list[str] = []
                    usage_total: dict[str, int] = {"prompt": 0, "completion": 0, "reasoning": 0}
                    events: list[dict[str, Any]] = []
                    env_name: str | None = None
                    # The `analyze` phase's output (G26). Empty on every other
                    # job type — the guide belongs to the fix analysis alone.
                    fix_guide: str = ""
                    fix_slot: str = ""

                    # Job totals from the phases ALREADY finished; the LLM
                    # reports its own cumulative totals per call, so the mirror
                    # must add them onto this base (a bare set would double-count
                    # when the caller adds the final usage again).
                    _usage_base: dict[str, int] = {"prompt": 0, "completion": 0, "reasoning": 0}

                    def _usage_sync(u: dict) -> None:
                        """Mirror the LLM's running token totals into the job usage
                        so the Deploying… popup's per-step in/out/rsn counters move
                        WHILE the step runs (not only when it finishes)."""
                        for k in ("prompt", "completion", "reasoning"):
                            try:
                                usage_total[k] = int(_usage_base.get(k) or 0) + int(u.get(k) or 0)
                            except (TypeError, ValueError):
                                pass

                    # Last time this job's events were flushed to the row (G31).
                    _last_flush: list[float] = [0.0]

                    async def _event(fname: str, status: str, extra: dict | None = None) -> None:
                        ev: dict[str, Any] = {
                            "file": fname, "status": status,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "usage": dict(usage_total),
                        }
                        if extra:
                            ev.update(extra)
                        events.append(ev)
                        # COALESCE THE HIGH-FREQUENCY WRITES (GRILLING G31).
                        # `_live_est` calls this for EVERY streaming delta, so an
                        # unconditional write meant a row UPDATE per token —
                        # dozens a second, each a contended transaction on the one
                        # SQLite file that four workers share. The in-memory list
                        # is the truth; the ROW is a view. So: always write a
                        # TRANSITION (anything that is not "running" — done,
                        # failed, awaiting_input counts, and those are what the
                        # panel and the resume path act on), and otherwise write
                        # at most twice a second.
                        import time as _t
                        _now = _t.monotonic()
                        if status != "running" or (_now - _last_flush[0]) >= 0.5:
                            _last_flush[0] = _now
                            self._set_status(job_id, JOB_STATUS_RUNNING, progress=f"{fname}: {status}", usage=dict(usage_total), events=list(events))

                    def session_log(entry: dict) -> None:
                        self._log_append(job_id, entry)

                    # `LLM based fix` writes `.fix` variants instead of the
                    # originals (llm-based-fix-todo.md §2).
                    #
                    # The `fix` flag has TWO meanings that used to share this one
                    # variable, and they must be split:
                    #
                    # 1. the NAMING of the base files. `.base` on a normal path,
                    #    `.fix` when the path was triggered by a guided fix —
                    #    the trigger decides, not the slot (G25). A guided
                    #    RESTART of a deploy path sets `fix` for exactly this.
                    # 2. the ENV output being recipe-level `.env.fix` rather than
                    #    the per-user `.env.raw.<user>.<label>`. That rule exists
                    #    so a FIX JOB's output can never become a user's
                    #    deployment env without passing the review gate
                    #    (llm-based-fix-todo.md §2) — so it belongs to a genuine
                    #    `job_type == 'fix'` job ONLY.
                    #
                    # A guided deploy restart is a DEPLOY: its env phase must
                    # produce the per-user env as any deploy does. Folding the two
                    # together made it write a shared recipe-level `.env.fix` and
                    # hand THAT to the -api as the deployment env.
                    _fix_mode = bool(request.get("fix"))
                    # The literal names for THIS job — the family's templates with
                    # this job's collision token filled in (GRILLING G33).
                    _names = _phase_names(job_id, fix=_fix_mode)
                    _recipe_level_env = _fix_mode and job.job_type == "fix"
                    # The ENV phase's output name is decided INSIDE the phase
                    # (the per-user `.env.raw.<user>.<label>`, or the
                    # recipe-level `.env.fix` for a real fix job), so
                    # `_names['env']` is only ever the progress line's
                    # placeholder. Leaving it on the `.fix` family for a path
                    # whose env is per-user makes that line name a file that will
                    # never exist — seen live in T12, whose progress read
                    # `.env.fix: running` while it wrote `.env.raw.alice.1`.
                    if not _recipe_level_env:
                        _names = {**_names, "env": PHASE_FILENAME["env"]}

                    for idx, phase in enumerate(phases):
                        self._check_cancelled(job_id)
                        fname = _names.get(phase, phase)

                        # The per-phase helpers are defined BEFORE the phase
                        # branches, not after them. The ANALYSIS branch is the
                        # FIRST one and takes `progress`/`on_delta` like any other,
                        # but these used to be defined further down the loop body —
                        # so for a `fix` job, whose ONLY phase is `analyze`,
                        # `progress` was still unbound when the call was made and
                        # every `LLM based fix` died instantly with
                        # `NameError: cannot access local variable 'progress'`
                        # (found live in T13, before it could reach the model).
                        step_seq = 0

                        def _live_est(est_out: int) -> None:
                            """Streaming progress: update ONE in-place row with the
                            estimated output tokens as the response arrives (the
                            exact usage replaces it when the call completes)."""
                            try:
                                asyncio.get_running_loop().create_task(_event(
                                    fname, "running", {
                                        "purpose": (f"{phase}: receiving response… "
                                                    f"(out ≈ {est_out} tok, streaming)"),
                                        "step": f"{phase}#live",
                                        "usage": {**usage_total, "completion": est_out,
                                                  "estimated": True},
                                    }
                                ))
                            except RuntimeError:
                                pass

                        async def progress(msg: str) -> None:
                            nonlocal step_seq
                            step_seq += 1
                            self._check_cancelled(job_id)
                            self._set_status(job_id, JOB_STATUS_RUNNING, progress=msg)
                            # Each progress message is its OWN append-only step row
                            # (keyed by step, not file) carrying the token usage
                            # known at that moment — so the popup's list grows
                            # instead of one row being overwritten.
                            await _event(fname, "running", {
                                "purpose": msg, "step": f"{phase}#{step_seq}",
                            })

                        # --- G26: the ANALYSIS phase (`LLM based fix`) -------
                        # Reads the failed task's log WHOLE plus the deployment
                        # context and returns a FIXING GUIDE. It writes NO files:
                        # the `.fix` variants come from the restarted path that
                        # `check` triggers, under the operator's confirmation —
                        # which is also the only route past the review gate.
                        # On success the gateway performs the failed→paused
                        # handoff ITSELF (no -api → gateway interaction): the
                        # pre-deploy session is written FIRST, then the -api's
                        # registry entry is removed — ADD THEN DELETE, so the log
                        # stays findable through the session that names it.
                        # The removal is the FULL delete; the TASK is not deleted.
                        if phase == "analyze":
                            _ctx: dict[str, Any] = {
                                "service_name": service_name,
                                "recipe_path": recipe_path,
                                "task_log": request.get("task_log") or "",
                                "failure": request.get("failure") or {},
                                "deploy_metadata": request.get("deploy_metadata") or {},
                                "user_name": request.get("user_name") or "",
                                "label": str(request.get("label") or "0"),
                                "base_files": self._analysis_files(
                                    service_name, recipe_path, request
                                ),
                            }
                            gen = await llm_service.generate_fix_guide(
                                db, _ctx, progress=progress, session_log=session_log,
                                usage_cb=_usage_sync, on_delta=_live_est,
                            )
                            warnings.extend(gen.get("warnings") or [])
                            _guide = (gen.get("guide") or "").strip()
                            if not _guide:
                                raise GenerationJobError(
                                    "the analysis produced no fixing guide"
                                )
                            fix_guide = _guide
                            fix_slot = gen.get("slot") or "compose"
                            # --- the gateway-internal handoff (G26) ----------
                            # Nothing has moved until this point: while the
                            # analysis ran, the -api entry stayed `failed`, so a
                            # cancel or a failure here is a clean no-op.
                            _w = await self._llm_fix_handoff(
                                user=str(request.get("user_name") or
                                         (request.get("deploy_metadata") or {}).get("user_name") or ""),
                                service=service_name,
                                recipe_path=recipe_path,
                                label=str(request.get("label") or
                                          (request.get("deploy_metadata") or {}).get("label") or "0"),
                                guide=_guide,
                                task_id=str(request.get("task_id") or ""),
                                job_id=job_id,
                                # The slot the guide is ABOUT. Without it the
                                # session cannot say which file the fix targets,
                                # and `check` restarts with the default
                                # (`compose`) even when the fault is nginx/env.
                                slot=fix_slot,
                                # The proxy the path was created with, so the
                                # `check` restart replays it (G20). The request
                                # normally carries none, so the handoff derives
                                # it from the failed instance's build args.
                                use_global_proxy=request.get("use_global_proxy"),
                            )
                            if _w:
                                warnings.append(_w)
                            await _event(fname, "done", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final",
                                "guide": _guide, "slot": fix_slot,
                            })
                            continue

                        # --- §3b: compose validation -------------------------
                        # One variable name at two sites with two different
                        # defaults is a compose bug: no single value satisfies
                        # both, and the compose is only correct while the
                        # variable is unset. Pause — same machinery as the
                        # external-credential pause — and hand the operator
                        # guidance derived from the scan itself.
                        if phase == "validate":
                            from ..utils.var_scan import conflicting_defaults
                            _sel = (request.get("selection") or {})
                            _rdir = self._recipe_dir_for(service_name, recipe_path)
                            # A FIX job must validate the compose IT produced, not
                            # the pre-existing selection that carried the conflict
                            # — otherwise it re-detects the same conflict and
                            # pauses forever on its own output. Its name is THIS
                            # job's token-bearing one (GRILLING G33), so the test
                            # is "did I write it", read off this run's own
                            # `result_files` and never off the recipe dir.
                            _fix_compose = _names["compose"]
                            _produced = (
                                [_fix_compose]
                                if _fix_mode and _fix_compose in result_files
                                else []
                            )
                            _cpaths = [
                                _rdir / p for p in (_produced or (_sel.get("compose") or []))
                                if isinstance(p, str) and (_rdir / p).is_file()
                            ]
                            _conflicts = conflicting_defaults(_cpaths) if _cpaths else []
                            if not _conflicts:
                                await _event(fname, "done", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final", "conflicts": [],
                                })
                                continue
                            _lines = []
                            for _c in _conflicts:
                                _where = " and ".join(
                                    f"`{s['default']}` at {s['file']}" for s in _c["sites"]
                                )
                                _suggest = ", ".join(
                                    f"{_c['name']}_{i + 1}={d}"
                                    for i, d in enumerate(_c["defaults"])
                                )
                                _lines.append(
                                    f"`{_c['name']}` is used with {_where}. "
                                    f"Use distinct names: {_suggest}."
                                )
                            _guidance = "\n".join(_lines)
                            await _event(fname, "paused", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#input", "conflicts": _conflicts,
                            })
                            # G27 — THE LEG ENDS HERE. The conflict makes this
                            # leg terminal (`paused`) and carries everything the
                            # NEXT leg needs: the path's state (the faulty
                            # compose UNSELECTED, so the restart regenerates it —
                            # G21's rule, now expressed as input rather than as a
                            # row rewritten in place) and the guidance, which the
                            # operator may edit before confirming. Answering it
                            # creates a NEW job (post /llm/jobs with this
                            # payload); there is no endpoint that mutates a job.
                            self._set_status(
                                job_id, JOB_STATUS_PAUSED,
                                progress=(
                                    "compose conflict(s): "
                                    + ", ".join(c["name"] for c in _conflicts)
                                ),
                                result={
                                    "pause": {
                                        "kind": "compose_fix",
                                        "ask": {"conflicts": _conflicts,
                                                "guidance": _guidance},
                                        # The carry IS the next job's request.
                                        "request": {
                                            **request,
                                            "selection": {**_sel, "compose": []},
                                            "prompt": _guidance,
                                            "conflict_unselected": list(_sel.get("compose") or []),
                                            "fix": True,
                                        },
                                        "warnings": warnings,
                                    }
                                },
                            )
                            return
                        self._set_status(
                            job_id, JOB_STATUS_RUNNING,
                            progress=f"phase {idx + 1}/{len(phases)}: {phase}",
                            phase=phase, phase_index=idx,
                        )
                        # Announce the step immediately (purpose + usage snapshot)
                        # so the popup's append-only step list shows it as soon as
                        # it starts, not only when it finishes.
                        await _event(fname, "running", {
                            "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                            "step": f"{phase}#0",
                        })
                        _usage_base.update(dict(usage_total))

                        # ---- ENV phase: mechanical-first (design §2) ----
                        if phase == "env" and request.get("resume_env"):
                            # G27: finish the env from the CARRY. No LLM call,
                            # no re-scan — the paused leg already did both, and
                            # the operator's answer is the only missing input.
                            _re = request.get("resume_env") or {}
                            _vals = request.get("resume_values") or {}
                            _content = _apply_env_kvs(
                                _re.get("mech") or "", _re.get("kvs") or {},
                                external_values=_vals,
                            )
                            from ..services.config_validation import validate_env
                            validations[phase] = validate_env(
                                _content, _re.get("compose_paths") or []
                            )
                            _rd = self._recipe_dir_for(service_name, recipe_path)
                            _rd.mkdir(parents=True, exist_ok=True)
                            _name = _re.get("env_name") or ".env"
                            (_rd / _name).write_text(_content, encoding="utf-8")
                            (_rd / f"{_name}.generated").write_text("")
                            result_files[_name] = _content
                            env_name = _name
                            warnings.extend(
                                f"external variable '{n}' had no supplied value — kept the base value"
                                for n in _unfilled_externals(_re.get("kvs") or {}, _vals)
                            )
                            await _event(_name, "done", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final",
                                "validation": validations[phase],
                            })
                            continue
                        if phase == "env":
                            # Always produces content — an empty string when the
                            # compose references no ${VAR}s (§4), so the
                            # "skipped (no ${VAR} interpolation)" outcome is gone.
                            mech = self._mechanical_env(service_name, recipe_path, request) or ""
                            # Serving-URL contract: ask the -api for the hostname
                            # this instance will be served on (pre-deploy). The
                            # gateway then appends the real external port below.
                            try:
                                from ..services.provision_service import provision_service as _ps
                                dm = request.get("deploy_metadata") or {}
                                _base = await _ps.get_service_url_base(
                                    service_name,
                                    dm.get("user_name") or request.get("user_name") or "",
                                    dm.get("label") or request.get("label") or "0",
                                    dm.get("domain") or request.get("domain") or "localhost",
                                )
                                if _base.get("hostname"):
                                    request.setdefault("deploy_metadata", {})["hostname"] = _base["hostname"]
                            except Exception:
                                pass  # fall back to the local hostname derivation

                            context = self._build_context(service_name, recipe_path, phase, request)
                            context["base_files"]["__mechanical_env__.txt"] = mech
                            # LLM fill = two-turn conversation (design §2 env
                            # redesign): turn 1 identifies the vars needing
                            # values, turn 2 (same conversation) maps them to
                            # values from deployment_meta; the gateway then
                            # assembles the .env programmatically from the
                            # returned map over the mechanical merge. Mechanical
                            # output stands when the LLM is off/empty.
                            llm_on = self._llm_active(db)
                            gen = None
                            kvs: dict[str, str] = {}
                            if llm_on:
                                for attempt in (1, 2):
                                    self._check_cancelled(job_id)
                                    gen = await llm_service.generate_env_two_turn(
                                        db, context,
                                        progress=progress, session_log=session_log,
                                        usage_cb=_usage_sync,
                                        on_delta=_live_est,
                                    )
                                    if gen and gen.get("kvs"):
                                        break
                            if gen and gen.get("kvs"):
                                kvs = gen["kvs"]
                                warnings.extend(gen.get("warnings") or [])
                                u = gen.get("usage") or {}
                                # (_usage_sync already folded these into the totals)

                            # ---- PAUSE for operator-supplied credentials -------
                            # Turn 1 classified these as third-party credentials:
                            # values the platform cannot invent (a random string
                            # for an externally-issued key looks configured and
                            # fails at first use). Generation stops HERE — before
                            # the env file is written — and the job parks in
                            # `awaiting_input` until the operator supplies them
                            # through POST /jobs/{id}/input.
                            #
                            # `resume` carries the mechanical base and the final
                            # kvs so the write can be completed WITHOUT re-running
                            # the LLM: that is what makes the pause restart-safe —
                            # a gateway restart must not throw away work already
                            # paid for.
                            _ext_vars = list(gen.get("external") or []) if gen else []
                            if kvs and _ext_vars:
                                _pu_name = (
                                    request.get("user_name")
                                    or (request.get("deploy_metadata") or {}).get("user_name")
                                    or "user"
                                )
                                _pu_label = str(
                                    request.get("label")
                                    or (request.get("deploy_metadata") or {}).get("label")
                                    or "0"
                                )
                                _pu_env = f".env.raw.{_pu_name}.{_pu_label}"
                                # ORDER MATTERS: `_event` persists the job with a
                                # hardcoded RUNNING status (it is the generic
                                # progress reporter). Emitting the event FIRST and
                                # setting the parked status LAST is what actually
                                # leaves the job in `awaiting_input` — the other
                                # order was silently overwritten back to `running`,
                                # so the job never paused and the popup never
                                # appeared (found live: job 83 stuck "running").
                                await _event(_pu_env, "paused", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#input",
                                    "vars": _ext_vars,
                                })
                                # G27 — the leg ends. The carry is the WHOLE
                                # previous request plus the env-phase resume data
                                # (mechanical text + the LLM's key map), so the
                                # answering job can finish the env WITHOUT a
                                # second LLM call: `_compute_phases` returns
                                # `["env"]` and the env branch writes the file
                                # from the carried `resume_env`.
                                self._set_status(
                                    job_id, JOB_STATUS_PAUSED,
                                    progress=(
                                        f"waiting for {len(_ext_vars)} external credential(s): "
                                        + ", ".join(_ext_vars)
                                    ),
                                    result={
                                      "pause": {
                                        "kind": "input",
                                        "ask": {"vars": _ext_vars},
                                        "request": {
                                            **request,
                                            "resume_env": {
                                                "env_name": (".env.fix" if _recipe_level_env else _pu_env),
                                                "vars": _ext_vars,
                                                "mech": mech,
                                                "kvs": kvs,
                                                "compose_paths": context.get("compose_paths") or [],
                                            },
                                        },
                                        "warnings": warnings,
                                      }
                                    },
                                )
                                return
                            # §4: bind `content` BEFORE the branch. When the
                            # compose has no ${VAR} surface AND the LLM produced
                            # no kvs, neither arm below used to run and the
                            # `if not content:` fallback raised UnboundLocalError
                            # — a regression introduced by making
                            # `_mechanical_env` return "" instead of None (the
                            # old `is None` guard `continue`d past this point).
                            content = ""
                            if kvs:
                                content = _apply_env_kvs(mech, kvs)
                                # Normally every `external` variable pauses before we reach
                                # this write; if one arrives here with no value, report it and
                                # keep the base value rather than blanking it silently.
                                for _name in _unfilled_externals(kvs, None):
                                    warnings.append(
                                        f"external variable '{_name}' has no value — kept the base value"
                                    )
                                from ..services.config_validation import validate_env
                                compose_paths = context.get("compose_paths") or []
                                validations[phase] = validate_env(content, compose_paths)
                                if not validations[phase].get("valid", True):
                                    # LLM map produced an incomplete env → fall
                                    # back to the mechanical output.
                                    content = ""
                            if not content:
                                content = mech
                                from ..services.config_validation import validate_env
                                compose_paths = context.get("compose_paths") or []
                                validations[phase] = validate_env(content, compose_paths)
                            # Write the per-user RAW env file (+ marker).
                            recipe_dir = self._recipe_dir_for(service_name, recipe_path)
                            recipe_dir.mkdir(parents=True, exist_ok=True)
                            uname = (
                                request.get("user_name")
                                or (request.get("deploy_metadata") or {}).get("user_name")
                                or "user"
                            )
                            ulabel = str(
                                request.get("label")
                                or (request.get("deploy_metadata") or {}).get("label")
                                or "0"
                            )
                            # The recipe-level `.env.fix` belongs to a genuine
                            # `fix` JOB only (llm-based-fix-todo.md §2) — see the
                            # `_recipe_level_env` note above. A guided deploy
                            # restart keeps the per-user `.env.raw.*` artifact;
                            # it is a deploy, and the -api needs a per-user env.
                            env_name = (
                                ".env.fix" if _recipe_level_env
                                else f".env.raw.{uname}.{ulabel}"
                            )
                            if validations[phase].get("valid"):
                                fpath = recipe_dir / env_name
                                fpath.write_text(content, encoding="utf-8")
                                (recipe_dir / f"{fpath.name}.generated").write_text("")
                                result_files[env_name] = content
                                await _event(env_name, "done", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final",
                                    "validation": validations[phase],
                                })
                            else:
                                errs = validations[phase].get("errors") or []
                                await _event(env_name, "failed", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final",
                                    "errors": errs,
                                })
                                raise GenerationJobError(
                                    f"phase 'env' produced no valid output: {'; '.join(errs)}"
                                )
                            continue

                        # ---- T14: the two validations (MECHANICAL) ----
                        # No LLM: they CHECK artifacts, they do not produce any.
                        # What a FAILURE does is two different things, and R1.3
                        # states why that is not decoration: a pause KEEPS the
                        # run and the artifacts it has already produced, while a
                        # raised failure discards both and re-pays for them on
                        # the retry. So the deploy-scoped path pauses — a pause
                        # IS a session, and one exists there — with this check's
                        # OWN kind, so the guided fix names the right file; the
                        # standalone Generate panel (no user, no label, no
                        # session — which is why that path is Tasks-page-only by
                        # design) raises, with the same precise errors and the
                        # same named file.
                        if phase in ("validate_ports", "validate_conf"):
                            # `validate_serving_ports` lives with the other
                            # mechanical validators; `validate_conf_agreement` with
                            # the artifact's own semantics. Both are imported here
                            # because this branch is where they are used.
                            from ..services.config_validation import validate_serving_ports
                            from ..services.serving_ports import validate_conf_agreement

                            # R2-F40/F41 — this branch needs its OWN `recipe_dir`
                            # and `context`. Both are locals of `_run_job` assigned
                            # in OTHER branches, so reading them here raised
                            # `UnboundLocalError` — the second and third unbound
                            # name this phase body has now failed on (after
                            # `eff_compose`, R2-F38), because no test executes a
                            # phase body. `_build_context` is the same call the
                            # other phases make, so the compose paths it resolves
                            # are the ones this check is about.
                            recipe_dir = self._recipe_dir_for(service_name, recipe_path)
                            context = self._build_context(service_name, recipe_path, phase, request)

                            _sel = dict(request.get("selection") or {})
                            _sp_name = _names.get("serving_ports")
                            _sp_path = None
                            # WHICH file is being checked — recorded, not
                            # re-derived: a pause must name the file that FAILED,
                            # and `_names.get("serving_ports")` is the phase's
                            # prospective name for this job, not the artifact in
                            # play whenever the row came from the selection.
                            # Measured: naming it from `_names` first reported
                            # `serving_ports.base.<job>.yml` as the culprit while
                            # the operator's own `serving_ports.yml` was the file
                            # the check had actually rejected.
                            _sp_source = ""
                            # Initialised HERE, not inside the `validate_conf`
                            # branch that fills it: the pause below reads it from
                            # another branch, and a name bound only in a sibling
                            # branch is NOT bound for that load. Bound at this
                            # level it is bound on every path, which is what the
                            # static net (TestPhaseBodiesBindTheirOwnNames) and
                            # the runtime both want.
                            _conf_source = ""
                            if _sp_name and _sp_name in result_files:
                                # produced by THIS run (not yet on disk)
                                _sp_text = result_files[_sp_name]
                                _sp_source = _sp_name
                            elif _sel.get("serving_ports"):
                                _p = recipe_dir / _sel["serving_ports"]
                                _sp_text = _p.read_text(encoding="utf-8", errors="replace") if _p.is_file() else ""
                                _sp_source = str(_sel["serving_ports"])
                            else:
                                _sp_text = ""
                            _ports = None
                            try:
                                import yaml as _yaml
                                _ports = _yaml.safe_load(_sp_text) if _sp_text else None
                            except Exception as _e:  # noqa: BLE001
                                _ports = None
                                warnings.append(f"serving_ports could not be parsed: {_e}")

                            _domain = (request.get("deploy_metadata") or {}).get("domain") or "localhost"
                            if phase == "validate_ports":
                                # R2-F38 — `eff_compose` belonged to a LATER
                                # function and was never a name in this scope, so
                                # the first real run of this phase died with
                                # `NameError` and the whole generation job failed
                                # (measured 2026-10-10, job 362, phase 3/6). The
                                # compose in play is the one the run resolved:
                                # `context["compose_paths"]` when the compose phase
                                # ran, else the selected list. The unit suite
                                # covers the phase LISTS, not the phase bodies, so
                                # nothing caught it — the same hole the round
                                # already recorded for a wrong module path.
                                _vp_compose = [
                                    str(p) for p in (context.get("compose_paths") or [])
                                ] or [
                                    str(recipe_dir / p) for p in (_sel.get("compose") or [])
                                ]
                                _res = validate_serving_ports(_sp_text, _vp_compose)
                            else:
                                # G56 — BOTH categories, one name-agnostic check.
                                # For each category in play: this run's produced
                                # conf first, else the selected one. The blocks are
                                # matched by their `--<sub>` suffix and their
                                # upstream/port; NAMES ARE NOT COMPARED (the -api
                                # renders them).
                                _cat_confs: list[tuple[str, str, str]] = []
                                for _cat, _slot in (("http", "nginx"), ("stream", "stream_conf")):
                                    _conf_name = _names.get(_slot)
                                    if _conf_name and _conf_name in result_files:
                                        _cat_confs.append((_cat, result_files[_conf_name], _conf_name))
                                    elif _sel.get(_slot):
                                        _p = recipe_dir / _sel[_slot]
                                        _text = _p.read_text(encoding="utf-8", errors="replace") if _p.is_file() else ""
                                        _cat_confs.append((_cat, _text, str(_sel[_slot])))
                                _errors_all: list[str] = []
                                _warnings_all: list[str] = []
                                for _cat, _conf_text, _src in _cat_confs:
                                    _one = validate_conf_agreement(
                                        _ports, _conf_text,
                                        service_name=service_name,
                                        user_name=str(request.get("user_name") or "x"),
                                        label=str(request.get("label") or "0"),
                                        domain=_domain,
                                        category=_cat,
                                    )
                                    _errors_all.extend(_one.get("errors") or [])
                                    _warnings_all.extend(_one.get("warnings") or [])
                                    if not _one.get("valid") and not _conf_source:
                                        # The pause names the file that FAILED.
                                        _conf_source = _src
                                _res = {
                                    "valid": not _errors_all,
                                    "errors": _errors_all,
                                    "warnings": _warnings_all,
                                }

                            warnings.extend(_res.get("warnings") or [])
                            if not _res.get("valid"):
                                _errs = _res.get("errors") or []
                                await _event(fname, "failed", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final", "errors": _errs,
                                })
                                # R1.3 — the deploy-scoped path PAUSES; the
                                # standalone panel RAISES. `job_origin` is the
                                # one home for "which path created this job"
                                # (the deploy's own generation, a resumed leg, a
                                # fix, or the Source Projects panel), read off
                                # what the job RECORDS rather than inferred.
                                if job_origin(request, job.job_type) == JOB_ORIGIN_DEPLOY:
                                    # The file the operator must fix, and the
                                    # slot that produces it. The restart has to
                                    # REGENERATE the artifact the check rejected,
                                    # so that slot travels UNSELECTED — the same
                                    # rule the compose-conflict pause uses (G21),
                                    # for the same reason: re-running the path
                                    # with the bad file still selected would
                                    # judge it again and pause forever on its own
                                    # output. `user_name` is already in the
                                    # carried request, which is what keeps the
                                    # resumed leg deploy-scoped.
                                    if phase == "validate_ports":
                                        _fix_file = _sp_source or "serving_ports"
                                        _fix_unsel = {"serving_ports": None}
                                    else:
                                        _fix_file = _conf_source or "nginx.conf"
                                        # Re-generate the SLOT that failed: the
                                        # stream conf is its own slot now (G56).
                                        _stream_failed = (
                                            ".stream." in (_conf_source or "")
                                            or (_conf_source or "").endswith(".stream.conf")
                                        )
                                        _fix_unsel = (
                                            {"stream_conf": None} if _stream_failed
                                            else {"nginx": None}
                                        )
                                    _guidance = (
                                        f"{_fix_file} failed the {phase} check: "
                                        + "; ".join(_errs)
                                        + f". Confirm to regenerate {_fix_file} and resume."
                                    )
                                    await _event(fname, "paused", {
                                        "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                        "step": f"{phase}#input", "errors": _errs,
                                    })
                                    self._set_status(
                                        job_id, JOB_STATUS_PAUSED,
                                        progress=f"{phase}: {_fix_file}",
                                        result={
                                            "pause": {
                                                # Its OWN kind per check, so the
                                                # prompt the operator answers is
                                                # the one that names the right
                                                # file (T14's rule, restated).
                                                "kind": f"{phase}_fix",
                                                "ask": {
                                                    "file": _fix_file,
                                                    "check": phase,
                                                    "errors": _errs,
                                                    "guidance": _guidance,
                                                },
                                                "request": {
                                                    **request,
                                                    "selection": {**_sel, **_fix_unsel},
                                                    "prompt": _guidance,
                                                    "fix": True,
                                                },
                                                "warnings": warnings,
                                            }
                                        },
                                    )
                                    return
                                raise GenerationJobError(
                                    f"{phase} failed: " + "; ".join(_errs)
                                )
                            await _event(fname, "done", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final", "errors": [], "warnings": _res.get("warnings") or [],
                            })
                            continue

                        # ---- T9: the internal stream conf (MECHANICAL) ----
                        # No LLM: the judgment already happened in the
                        # serving_ports phase (which ports exist, what each
                        # speaks). What remains is arithmetic — pick the stream
                        # rows, name each, write a block the internal nginx
                        # selects by `server_name`.
                        if phase == "stream_conf":
                            from ..services.stream_conf import (
                                render_stream_conf, stream_rows,
                            )
                            from ..services.serving_ports import load_kind_categories  # noqa: F401

                            # R2-F42 — its own `recipe_dir`, for the reason the
                            # validate branch needed one (F40): the name is bound
                            # in the `env` branch only, so reading it here would
                            # have been the FOURTH `UnboundLocalError` in these
                            # phase bodies. Found by the static net, not by a run.
                            recipe_dir = self._recipe_dir_for(service_name, recipe_path)

                            _sel = dict(request.get("selection") or {})
                            _sp_name = _names.get("serving_ports")
                            _sp_path = None
                            if _sp_name and _sp_name in result_files:
                                _sp_path = recipe_dir / _sp_name
                            elif _sel.get("serving_ports"):
                                _sp_path = recipe_dir / _sel["serving_ports"]

                            _rows: list[dict[str, Any]] = []
                            if _sp_path and _sp_path.is_file():
                                try:
                                    import yaml as _yaml
                                    _rows = stream_rows(
                                        _yaml.safe_load(_sp_path.read_text(encoding="utf-8", errors="replace"))
                                    )
                                except Exception as _e:  # noqa: BLE001
                                    warnings.append(f"could not read serving_ports: {_e}")

                            if not _rows:
                                # R1.4 by construction: nothing to expose, so no
                                # file is written at all.
                                await _event(fname, "done", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final", "stream_rows": 0,
                                })
                                continue

                            _domain = (request.get("deploy_metadata") or {}).get("domain") or "localhost"
                            _ssl_dir = Path(getattr(settings, "SSL_DIR", ""))
                            content = render_stream_conf(
                                _rows,
                                service_name=service_name,
                                user_name=str(request.get("user_name") or ""),
                                label=str(request.get("label") or "0"),
                                domain=_domain,
                                cert_pem=str(_ssl_dir / _domain / "fullchain.pem"),
                                cert_key=str(_ssl_dir / _domain / "privkey.pem"),
                            )
                            # G56 — the generated artifact is RECIPE-LEVEL, like
                            # `nginx.base.<token>.conf`: a stream TEMPLATE whose
                            # names the -api renders per instance. No user/label in
                            # the name — the file belongs to the recipe.
                            sc_name = _names.get("stream_conf") or (
                                f"nginx.stream.base.{job_id}.conf"
                            )
                            result_files[sc_name] = content
                            _swapped = dict(request.get("selection") or {})
                            _swapped["stream_conf"] = sc_name
                            request["selection"] = _swapped

                            # Persist the swap (the same inline pattern the LLM
                            # phases use): the in-memory dict dies with this run,
                            # and the request is what a later resume — and the
                            # deploy payload — reads. Without it the conf is
                            # generated and then not sent.
                            try:
                                _db = SessionLocal()
                                try:
                                    _row = _db.query(GenerationJob).filter(
                                        GenerationJob.id == job_id
                                    ).first()
                                    if _row:
                                        _row.request = json.dumps(request, ensure_ascii=False)
                                        _db.commit()
                                finally:
                                    _db.close()
                            except Exception:  # noqa: BLE001
                                pass
                            # Write it to the recipe dir immediately, like the other
                            # phases: the payload references a PATH, so the file has
                            # to exist before this run finishes.
                            try:
                                _rd = self._recipe_dir_for(service_name, recipe_path)
                                _rd.mkdir(parents=True, exist_ok=True)
                                _fp = _rd / sc_name
                                _fp.write_text(content, encoding="utf-8")
                                (_rd / f"{_fp.name}.generated").write_text("")
                            except OSError:
                                pass
                            await _event(sc_name, "done", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final", "stream_rows": len(_rows),
                            })
                            continue

                        # ---- compose / nginx phases (LLM) ----
                        context = self._build_context(service_name, recipe_path, phase, request)
                        gen = None
                        for attempt in (1, 2):
                            self._check_cancelled(job_id)
                            if attempt > 1:
                                self._set_status(
                                    job_id, JOB_STATUS_RUNNING,
                                    progress=f"empty output — retrying phase ({attempt}/2)",
                                )
                            gen = await llm_service.generate_with_agent(
                                db, PHASE_CONFIG_TYPE[phase], context,
                                progress=progress, session_log=session_log,
                                usage_cb=_usage_sync,
                                on_delta=_live_est,
                            )
                            if (gen.get("generated_content") or "").strip():
                                break
                        self._check_cancelled(job_id)

                        content = gen.get("generated_content") or ""
                        validations[phase] = gen.get("validation") or {}
                        warnings.extend(gen.get("warnings") or [])
                        u = gen.get("usage") or {}
                        # (_usage_sync already folded these into the totals)
                        if content and (gen.get("validation") or {}).get("valid", True):
                            result_files[fname] = content
                            # G21 (task doc §8b-4 rule 2/3): the file this phase
                            # just produced REPLACES whatever was selected for
                            # that slot — it replaces, it does not add alongside.
                            # Two consequences, both required: the later phases
                            # in THIS run (validate / nginx / env) read the
                            # corrected compose, and the final result carries the
                            # swapped selection so the client's deploy payload
                            # names it — no second deploy.
                            if phase in ("compose", "nginx", "serving_ports"):
                                _swapped = dict(request.get("selection") or {})
                                # compose is a LIST slot, nginx a SINGLE-value
                                # one — the shape mirrors the panel's own
                                # selection model.
                                if phase == "compose":
                                    _swapped["compose"] = [fname]
                                elif phase == "serving_ports":
                                    _swapped["serving_ports"] = fname
                                else:
                                    _swapped["nginx"] = fname
                                request["selection"] = _swapped
                                # PERSIST it. The in-memory dict dies with this
                                # run, and the request is what a LATER resume
                                # reads — a credential pause unwinds out of
                                # `_run_job` entirely, so without this the swap is
                                # lost and the deploy payload keeps the original
                                # compose. Found live by T11: the `.base` file was
                                # generated but the -api rendered `docker-compose.yml`.
                                # nginx was swapped nowhere at all, so a generated
                                # nginx never reached the payload (P4).
                                try:
                                    _db = SessionLocal()
                                    try:
                                        _row = _db.query(GenerationJob).filter(
                                            GenerationJob.id == job_id
                                        ).first()
                                        if _row:
                                            _row.request = json.dumps(request, ensure_ascii=False)
                                            _db.commit()
                                    finally:
                                        _db.close()
                                except Exception:
                                    pass
                            # Persist the phase output IMMEDIATELY (like the env
                            # phase does): a later phase validates against the
                            # recipe dir on disk, so a compose generated in this
                            # same run must exist there before the nginx phase
                            # runs (otherwise "Compose services: []" and the
                            # nginx conf is rejected after all repair rounds).
                            try:
                                _rd = self._recipe_dir_for(service_name, recipe_path)
                                _rd.mkdir(parents=True, exist_ok=True)
                                _fp = _rd / fname
                                _fp.write_text(content, encoding="utf-8")
                                (_rd / f"{_fp.name}.generated").write_text("")
                            except OSError:
                                pass
                            await _event(fname, "done", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final",
                                "validation": validations[phase],
                            })
                        else:
                            errs = (gen.get("validation") or {}).get("errors") or gen.get("warnings") or []
                            await _event(fname, "failed", {
                                "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                "step": f"{phase}#final",
                                "errors": errs,
                            })
                            raise GenerationJobError(
                                f"phase '{phase}' produced no valid output: {'; '.join(errs) if errs else 'LLM unavailable?'}"
                            )

                    result = {
                        "files": result_files,
                        "validations": validations,
                        "warnings": warnings,
                        "env_name": env_name,
                        # G26: the analysis step's output. `guide` is what `check`
                        # pre-fills and what the restarted generation receives as
                        # its prompt; `slot` names the slot the restart should
                        # leave at `generate new`, so the operator does not have
                        # to guess which file the guide is about. Both are empty
                        # for every job type except the fix analysis.
                        "guide": fix_guide,
                        "slot": fix_slot,
                        # G21 rule 3: the selection AS THE PATH LEFT IT — the
                        # generated artifacts replace their slots, so the client
                        # adopts this and its deploy payload names the corrected
                        # files. Without it a resumed deploy would still send the
                        # compose the pipeline just replaced.
                        "selection": request.get("selection") or {},
                        "usage": usage_total,
                        "events": events,
                    }
                    self._set_status(job_id, JOB_STATUS_COMPLETED, progress="completed", result=result, usage=usage_total, events=events)
                except JobCancelled:
                    self._set_status(job_id, JOB_STATUS_CANCELLED, progress="cancelled")
                except GenerationJobError as exc:
                    self._set_status(job_id, JOB_STATUS_FAILED, progress="failed", error=str(exc))
                except Exception as exc:  # noqa: BLE001 — job must not kill the loop
                    self._set_status(job_id, JOB_STATUS_FAILED, progress="failed", error=str(exc))
        finally:
            self._tasks.pop(job_id, None)

    # ------------------------------------------------------------------
    # Context construction (decision 5a)
    # ------------------------------------------------------------------

    def _analysis_files(
        self, service_name: str, recipe_path: str, request: dict[str, Any]
    ) -> dict[str, str]:
        """The deployment files the analysis reads (G26).

        The selected compose/nginx/env plus — when nothing is selected — the
        recipe's compose candidates, so a failed deploy whose compose was
        generated can still be reasoned about. Read-only context: the analysis
        writes nothing, so nothing here can be clobbered by it.
        """
        from ..services.file_sets import _is_compose_candidate, _recipe_dir

        recipe_dir = _recipe_dir(settings.SOURCE_PROJECTS_DIR / service_name, recipe_path)
        selection = request.get("selection") or {}
        names: list[str] = []
        for p in (selection.get("compose") or []):
            if isinstance(p, str):
                names.append(p)
        if isinstance(selection.get("nginx"), str):
            names.append(selection["nginx"])
        for p in (selection.get("env") or []):
            if isinstance(p, str):
                names.append(p)
        if not any(isinstance(p, str) for p in (selection.get("compose") or [])):
            try:
                names.extend(
                    f.name for f in sorted(recipe_dir.iterdir())
                    if f.is_file() and _is_compose_candidate(f.name)
                )
            except OSError:
                pass
        out: dict[str, str] = {}
        for n in names:
            fp = recipe_dir / n
            try:
                if fp.is_file():
                    out[n] = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return out

    async def _llm_fix_handoff(
        self, *, user: str, service: str, recipe_path: str, label: str,
        guide: str, task_id: str, job_id: int, slot: str = "",
        use_global_proxy: bool | None = None,
    ) -> str:
        """The failed→paused handoff, entirely inside the gateway (G26).

        ADD THEN DELETE:

        1. the pre-deploy session is written FIRST, carrying the guide AND the
           failed task's id — that ordering is the only thing that keeps the log
           reachable once the registry entry that named it is gone;
        2. the removal is then requested of the -api: the FULL delete
           (containers, volumes, rendered files), exactly as the Delete button
           does. The TASK is not deleted, so the log survives it.

        No `-api → gateway` interaction is involved: this is an outbound call the
        gateway already makes, on its own initiative.

        Returns a warning string when the removal could not be requested, else "".
        """
        from . import pre_deploy_registry as pdr

        # `check` restarts this path with the deploy panel's options, so the
        # session has to carry the proxy the path was created with (G20). The
        # fix ANALYSIS request carries no such option, so fall back to the
        # failed instance's OWN registry `build_args` — still present here,
        # because the DELETE below has not run yet. Read it BEFORE the write.
        if use_global_proxy is None:
            use_global_proxy = _instance_used_global_proxy(user, service, label)

        pdr.add_session(
            user=user, service=service, recipe_path=recipe_path, label=label,
            state="paused", reason="llm_fix", job_id=job_id,
            guide=guide, task_id=task_id, slot=slot,
            use_global_proxy=use_global_proxy,
        )
        try:
            from .provision_service import provision_service
            await provision_service.remove_user(user, service, label)
        except Exception as exc:  # noqa: BLE001 — reported, never silent
            return (
                "the failed instance could not be removed after the analysis "
                f"succeeded ({exc}) — remove it from its card; the fixing guide "
                "is recorded on the pre-deploy session"
            )
        return ""

    def _build_context(
        self, service_name: str, recipe_path: str, phase: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the generation context for one phase.

        Selected base files raw contents only + prompt + deploy metadata +
        activated profiles; app-manifest carve-out when compose itself is
        missing. ``compose_paths`` are included for the env completeness check.
        """
        from ..services.file_sets import (
            _is_compose_candidate,
            _recipe_dir,
            compose_service_names as _names,
        )

        project_dir = settings.SOURCE_PROJECTS_DIR / service_name
        recipe_dir = _recipe_dir(project_dir, recipe_path)
        selection = request.get("selection") or {}
        sel_compose = [p for p in selection.get("compose", []) if isinstance(p, str)]
        # The compose files this run is actually configuring. Prefer the
        # operator's selection; when there is none, use the recipe's compose
        # candidates. A compose GENERATED earlier in this same run is not in
        # the selection — that is precisely why the compose phase ran — and
        # looking only at the selection then starves the other phases:
        # nginx validation gets an empty service list (so every proxy_pass
        # host reads as unknown and the phase fails) and the env phase gets an
        # empty ${VAR} surface. Only for reading, never for the rendered
        # per-user compose (the candidate rule excludes those).
        eff_compose = list(sel_compose) or [
            f.name for f in sorted(recipe_dir.iterdir())
            if f.is_file() and _is_compose_candidate(f.name)
        ]
        sel_nginx = selection.get("nginx")
        sel_env = [p for p in selection.get("env", []) if isinstance(p, str)]
        profiles = [p for p in selection.get("profiles", []) if isinstance(p, str)]

        # R1.5 / T16 — the serving-ports artifact in play. The nginx phase needs
        # its `kind: http` rows to know which EXTRA endpoints the conf must serve
        # (R1.3 makes a missing block for one an error). The SELECTION comes
        # first — the artifact the deploy actually used — then the newest
        # candidate, the same precedence the mint settled on (round 2's F35);
        # without it the model sees only the compose and writes one block.
        from .file_sets import _is_serving_ports_candidate

        _sel_sp = selection.get("serving_ports")
        if not _sel_sp:
            _sp_candidates = sorted(
                (f.name for f in recipe_dir.iterdir()
                 if f.is_file() and _is_serving_ports_candidate(f.name)),
            )
            _sel_sp = _sp_candidates[-1] if _sp_candidates else None
        serving_ports_path = str(recipe_dir / _sel_sp) if _sel_sp else None

        base_files: dict[str, str] = {}
        # Compose files are the base for the nginx phase; the compose phase
        # itself only gets the manifests (or a selected compose being replaced).
        # The ENV phase gets the mechanically scanned ${VAR} surface instead of
        # the raw compose: huge inputs make reasoning models spend the whole
        # token budget on chain-of-thought and return EMPTY content
        # (finish_reason=length) — the var list is all the phase needs.
        if phase in ("nginx", "serving_ports"):
            # Both are DERIVED from the compose: nginx proxies what it declares,
            # serving_ports classifies the ports it mentions (which is why the
            # artifact lists every `ports:` AND every `expose:` entry — R1.1).
            for p in eff_compose:
                fp = recipe_dir / p
                if fp.is_file():
                    try:
                        base_files[p] = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
        elif phase == "env":
            from ..utils.var_scan import scan_compose_files

            compose_paths = [recipe_dir / p for p in eff_compose if (recipe_dir / p).is_file()]
            if compose_paths:
                scan = scan_compose_files(compose_paths)
                if scan.refs:
                    required = sorted({r.name for r in scan.refs if not r.has_default})
                    optional = sorted({r.name for r in scan.refs if r.has_default})
                    base_files["__compose_variables__.txt"] = (
                        "# Mechanically scanned ${VAR} surface of the selected compose files\n"
                        "REQUIRED (no default — each MUST be present in the output):\n"
                        + ("\n".join(required) or "(none)")
                        + "\n\nOPTIONAL (have defaults; include only when relevant):\n"
                        + ("\n".join(optional) or "(none)")
                    )
        if phase == "nginx" and sel_nginx:
            fp = recipe_dir / sel_nginx
            if fp.is_file():
                try:
                    base_files[sel_nginx] = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        if phase == "env":
            # §5: when env is `generate new` the operator may supply REFERENCE
            # files — base/example envs the model reads to produce the `.raw`.
            # They are context only and are never handed to the deployment; a
            # selected handoff env (sel_env) makes the reference irrelevant.
            _env_ctx = list(sel_env) or [
                p for p in (request.get("env_reference") or []) if isinstance(p, str)
            ]
            for p in _env_ctx:
                fp = recipe_dir / p
                if fp.is_file():
                    try:
                        raw = fp.read_text(encoding="utf-8", errors="replace")
                        # Pass the FULL file INCLUDING comments: the model reads
                        # each variable's meaning (often only documented in
                        # comments) to decide how to fill it (design §2 — the
                        # gateway never guesses variable names). Blank lines are
                        # dropped; comment lines are kept.
                        base_files[p] = "\n".join(
                            ln for ln in raw.splitlines() if ln.strip()
                        )
                    except OSError:
                        continue

        # --- `LLM based fix`: the failed task's log, fed WHOLE (§1) ---------
        # Appended AFTER everything else so the standard context is unchanged
        # and the failure evidence is the last, most salient input. Deliberately
        # not truncated or summarised: the whole point is that the model sees
        # the real error. It is NOT a durable store — when the task log is gone
        # the fix is unavailable rather than degraded (§5).
        _task_log = request.get("task_log")
        if request.get("fix") and _task_log:
            base_files["__failed_task_log__.txt"] = (
                "# You are repairing a FAILED deployment by correcting its DEPLOYMENT\n"
                "# FILES. Read the failure below and repair its CAUSE.\n"
                "#\n"
                "# 1. The change may belong in ANY phase of this pipeline — the compose,\n"
                "#    the nginx conf, or the env — whichever owns the file the failure\n"
                "#    actually lives in. Nothing privileges the compose; decide from the\n"
                "#    failure itself.\n"
                "# 2. Do NOT restyle or rewrite files you were not asked to change: a\n"
                "#    plausible-looking rewrite that leaves the CAUSE untouched is the\n"
                "#    failure mode this instruction exists to prevent.\n"
                "# 3. If the failure is NOT expressible in a deployment file (a broken\n"
                "#    Dockerfile, a missing tool, a bad build-arg VALUE the operator\n"
                "#    supplies rather than the recipe), say so plainly and change\n"
                "#    nothing — an honest no-fix beats a plausible one.\n"
                "#\n"
                "# No separate verification step runs: the operator reviews these files\n"
                "# and the next DEPLOYMENT is the real test. A fix that does not work\n"
                "# simply fails again, producing another failed card.\n"
                f"# task_id: {request.get('task_id') or '(unknown)'}\n"
                f"# recorded failure: {json.dumps(request.get('failure') or {}, ensure_ascii=False)}\n"
                "\n"
                + str(_task_log)
            )

        has_compose = bool(eff_compose) and all((recipe_dir / p).is_file() for p in eff_compose)

        context: dict[str, Any] = {
            "repo_description": f"Service: {service_name}{' @ ' + recipe_path if recipe_path else ''}",
            "prompt": request.get("prompt") or "",
            "base_files": base_files,
            "profiles": profiles,
            "compose_paths": [str(recipe_dir / p) for p in eff_compose],
            "serving_ports_path": serving_ports_path,
            "project_dir": str(project_dir),
            "recipe_path": recipe_path,
            "empty_recipe": not has_compose and not sel_nginx and not sel_env,
            "deploy_metadata": dict(request.get("deploy_metadata") or {}),
        }
        context["deploy_metadata"].setdefault("service_name", service_name)
        context["deploy_metadata"].setdefault("user_name", request.get("user_name") or "")
        context["deploy_metadata"].setdefault("label", request.get("label") or "0")
        context["deploy_metadata"].setdefault("domain", request.get("domain") or "localhost")

        # Fixed deployment handoff for the LLM (design §2): the gateway knows
        # the origin the -api will serve this instance on (the shared nginx /
        # ACL-edge server_name convention + edge ports). The model never
        # guesses these — it reads variable meanings from the env base files and
        # fills reachability/identity variables from this handoff.
        dm = context["deploy_metadata"]
        svc = dm.get("service_name") or service_name
        usr = dm.get("user_name") or ""
        lbl = dm.get("label") or "0"
        dom = dm.get("domain") or "localhost"
        https = bool(dm.get("https") or request.get("https") or False)
        # Prefer the hostname the -api reported (serving-URL contract); derive
        # only as a fallback when the pre-deploy lookup was unavailable.
        hostname = dm.get("hostname") or (f"{svc}-{usr}-{lbl}.{dom}" if usr else dom)
        http_port = int(getattr(settings, "NGINX_HTTP_PORT", 80))
        https_port = int(getattr(settings, "NGINX_HTTPS_PORT", 443))

        def _serving(scheme: str, port: int) -> str:
            base = f"{scheme}://{hostname}"
            std = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
            return base if std else f"{base}:{port}"

        dm["hostname"] = hostname
        dm["http_port"] = http_port
        dm["https_port"] = https_port
        dm["scheme"] = "https" if https else "http"
        dm["serving_url"] = _serving(dm["scheme"], https_port if https else http_port)

        # Compose service names (merged set incl. profile-gated services) —
        # used for nginx validation + generation hints.
        context["compose_service_names"] = _names([recipe_dir / p for p in eff_compose])

        # App-manifest carve-out (compose missing → shallow RepoContext scan).
        if not has_compose:
            manifests: dict[str, str] = {}
            from ..utils.file_scanner import RepoContext

            scan_ctx = self._scan_manifests(recipe_dir)
            manifests.update(scan_ctx)
            if recipe_path not in ("", ".") and recipe_dir != project_dir:
                manifests.update(self._scan_manifests(project_dir, include_recipe_subdirs=False))
            context["manifests"] = manifests

        return context

    @staticmethod
    def _scan_manifests(directory: Path, include_recipe_subdirs: bool = True) -> dict[str, str]:
        """Shallow scan for the closed manifest filename set (nested NOT included)."""
        from ..services.llm_service import MANIFEST_FILENAMES

        result: dict[str, str] = {}
        if not directory.is_dir():
            return result
        try:
            entries = list(directory.iterdir())
        except OSError:
            return result
        # migrations/ presence indicator for alembic projects.
        has_migrations = (directory / "migrations").is_dir() or (directory / "alembic").is_dir()
        for entry in entries:
            if entry.is_file() and entry.name in MANIFEST_FILENAMES:
                try:
                    content = entry.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 128 * 1024:
                        content = content[:128 * 1024]
                    result[entry.name] = content
                except OSError:
                    continue
        if has_migrations:
            result.setdefault("_migrations_present", "true")
        return result


# Singleton
generation_jobs = GenerationJobManager()

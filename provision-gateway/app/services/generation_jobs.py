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
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_TTL_DAYS,
    GenerationJob,
)
from .llm_service import llm_service

ProgressFn = Callable[[str], Awaitable[None]]

# Multi-phase order: compose is the root; nginx is blocked until compose
# exists; env comes from the var-scan of the resolved compose (design
# §Dependency graph).
PHASE_ORDER = ("compose", "nginx", "env")

# Phase → config_type used by the LLM.
PHASE_CONFIG_TYPE = {
    "compose": "docker_compose",
    "nginx": "nginx_conf",
    "env": "env_file",
}

# Phase → generated filename suggestion.
PHASE_FILENAME = {
    "compose": "docker-compose.yml",
    "nginx": "nginx.conf",
    "env": ".env",
}


def _apply_env_kvs(mech: str, kvs: dict[str, Any]) -> str:
    """Assemble the final interpolation env programmatically: start from the
    mechanical merge (which already carries every compose-referenced variable)
    and apply the LLM's ``necessary_env_kvs`` overrides on top (design §2 env
    redesign). New keys from the map are appended; newlines inside values are
    stripped (KEY=VALUE only)."""
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
            # Decision-4 schema: {env_type: secret|non-secret, value}.
            env_type = v.get("env_type")
            raw = v.get("value")
            if env_type == "secret" or (env_type == "non-secret" and raw is None):
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

    def __init__(self) -> None:
        self._locks_guard = asyncio.Lock()
        self._project_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancelled: set[int] = set()
        # In-flight (fire-and-forget) session-log appends per custom task id.
        # Drained before finish so the last lines land BEFORE the terminal
        # status (feature 5 ordering).
        self._session_pending_appends: dict[str, list[asyncio.Task]] = {}

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
        db.add(job)
        db.commit()
        db.refresh(job)
        loop = asyncio.get_running_loop()
        self._tasks[job.id] = loop.create_task(self._run_job(job.id))
        return job

    @classmethod
    def _compute_phases(cls, job_type: str, request: dict[str, Any]) -> list[str]:
        if job_type == "generate_missing":
            # Deploy-time auto-generation runs ONLY the phases whose target is
            # actually missing — a project with an existing compose/nginx does
            # NOT re-run the LLM on those (design §8.3: "first deploy with
            # existing compose/nginx still shows the env step"). The recipe
            # missing-scan mirrors the provision-api check_missing_files.
            missing = cls._recipe_missing(request)
            phases = []
            if "docker-compose" in missing:
                phases.append("compose")
            if "nginx.conf" in missing:
                phases.append("nginx")
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
        recipe dir (compose/nginx only). Returns which of ``docker-compose`` /
        ``nginx.conf`` are ABSENT from the recipe, i.e. which LLM phases a
        generate-missing run must include.
        """
        from ..services.file_sets import _recipe_dir

        service_name = request.get("service_name") or ""
        recipe_path = request.get("recipe_path") or ""
        try:
            recipe_dir = _recipe_dir(Path(settings.SOURCE_PROJECTS_DIR) / service_name, recipe_path)
        except Exception:
            return ["docker-compose", "nginx.conf"]  # unknown recipe — assume missing
        missing: list[str] = []
        try:
            files = [f.name for f in recipe_dir.iterdir() if f.is_file()]
        except OSError:
            return ["docker-compose", "nginx.conf"]
        has_compose = any(
            f.endswith(".yml.j2") or f.endswith(".yaml.j2")
            or (f.endswith(".yml") and not f.endswith(".j2"))
            or (f.endswith(".yaml") and not f.endswith(".j2"))
            for f in files
        )
        has_nginx = any(
            f.endswith(".conf.j2") or (f.endswith(".conf") and not f.endswith(".j2"))
            for f in files
        )
        if not has_compose:
            missing.append("docker-compose")
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
            # No compose ${VAR} surface → nothing to generate (empty pass).
            return None

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
            if ref.has_default and ref.name not in lines:
                lines[ref.name] = ref.default_raw or ""
        return "\n".join(f"{k}={v}" for k, v in lines.items()) + ("\n" if lines else "")

    # ------------------------------------------------------------------
    # Session task record (Tasks page — design §2)
    # ------------------------------------------------------------------

    def _session_endpoint(self, path: str) -> str:
        return f"{settings.PROVISION_API_URL.rstrip('/')}{path}"

    async def _session_create(self, service_name: str, recipe_path: str, request: dict[str, Any]) -> str | None:
        try:
            import httpx
            target = f"{service_name} @ {recipe_path or '(root)'} / {request.get('user_name') or '-'}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._session_endpoint("/tasks/custom"),
                    json={"type": "generate-missing-files", "target": target, "detail": request},
                )
            if resp.status_code < 400:
                return (resp.json() or {}).get("task_id")
        except Exception:
            pass
        return None

    def _session_append(self, task_id: str | None, entry: dict) -> None:
        if not task_id:
            return
        try:
            import httpx
            line = json.dumps(entry, ensure_ascii=False)
            task = asyncio.create_task(self._session_append_async(task_id, line))
            self._session_pending_appends.setdefault(task_id, []).append(task)
        except Exception:
            pass

    async def _session_append_async(self, task_id: str, line: str) -> None:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    self._session_endpoint(f"/tasks/{task_id}/log"), json={"line": line}
                )
        except Exception:
            pass
        finally:
            # Drop the finished task from the pending set so it cannot leak.
            pending = self._session_pending_appends.get(task_id)
            if pending:
                try:
                    pending.remove(asyncio.current_task())
                except (ValueError, KeyError):
                    pass

    async def _session_drain(self, task_id: str | None) -> None:
        """Await all in-flight log appends for a session (bounded) so the
        terminal ``finish`` lands after the final lines."""
        if not task_id:
            return
        pending = self._session_pending_appends.pop(task_id, [])
        for t in pending:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=10)
            except Exception:
                pass

    async def _session_finish(self, task_id: str | None, status: str, error: str = "") -> None:
        if not task_id:
            return
        await self._session_drain(task_id)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    self._session_endpoint(f"/tasks/{task_id}/finish"),
                    json={"status": status, "error": error},
                )
        except Exception:
            pass

    def prune_finished(self, db: Session) -> int:
        """Delete finished jobs older than JOB_TTL_DAYS. Returns count pruned."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=JOB_TTL_DAYS)
        rows = (
            db.query(GenerationJob)
            .filter(
                GenerationJob.status.in_([JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED]),
                GenerationJob.updated_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return rows or 0

    def list_jobs(self, db: Session, include_result: bool = False) -> list[dict]:
        self.prune_finished(db)
        jobs = db.query(GenerationJob).order_by(GenerationJob.id.desc()).limit(200).all()
        return [j.to_dict(include_result=include_result) for j in jobs]

    def get_job(self, db: Session, job_id: int) -> GenerationJob | None:
        return db.query(GenerationJob).filter(GenerationJob.id == job_id).first()

    def cancel_job(self, db: Session, job_id: int) -> GenerationJob | None:
        job = self.get_job(db, job_id)
        if not job:
            return None
        if job.status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING):
            self._cancelled.add(job_id)
            job.status = JOB_STATUS_CANCELLED
            job.progress = "cancelling…"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)
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
        return rows or 0

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _set_status(self, job_id: int, status: str, progress: str = "", error: str | None = None,
                    phase: str | None = None, phase_index: int | None = None,
                    result: dict | None = None, usage: dict | None = None,
                    events: list | None = None) -> None:
        """Persist job state with a short-lived session (no session across awaits)."""
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            job = db.query(GenerationJob).filter(GenerationJob.id == job_id).first()
            if not job:
                return
            job.status = status
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
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

    def _check_cancelled(self, job_id: int) -> None:
        if job_id in self._cancelled:
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
                    session_task_id = await self._session_create(service_name, recipe_path, request)

                    async def _event(fname: str, status: str, extra: dict | None = None) -> None:
                        ev: dict[str, Any] = {
                            "file": fname, "status": status,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "usage": dict(usage_total),
                        }
                        if extra:
                            ev.update(extra)
                        events.append(ev)
                        self._set_status(job_id, JOB_STATUS_RUNNING, progress=f"{fname}: {status}", usage=dict(usage_total), events=list(events))

                    def session_log(entry: dict) -> None:
                        self._session_append(session_task_id, entry)

                    for idx, phase in enumerate(phases):
                        self._check_cancelled(job_id)
                        fname = PHASE_FILENAME.get(phase, phase)
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
                        step_seq = 0
                        _usage_base.update(dict(usage_total))

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

                        # ---- ENV phase: mechanical-first (design §2) ----
                        if phase == "env":
                            mech = self._mechanical_env(service_name, recipe_path, request)
                            if mech is None:
                                # Compose references no ${VAR}s → no generation.
                                await _event(fname, "skipped (no ${VAR} interpolation)", {
                                    "purpose": f"phase {idx + 1}/{len(phases)}: {phase}",
                                    "step": f"{phase}#final",
                                })
                                validations[phase] = {"valid": True, "errors": [], "skipped": True}
                                continue
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
                            content = ""
                            if kvs:
                                content = _apply_env_kvs(mech, kvs)
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
                            env_name = f".env.raw.{uname}.{ulabel}"
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
                        "usage": usage_total,
                        "events": events,
                        "session_task_id": session_task_id,
                    }
                    self._set_status(job_id, JOB_STATUS_COMPLETED, progress="completed", result=result, usage=usage_total, events=events)
                    await self._session_finish(session_task_id, "completed")
                except JobCancelled:
                    self._set_status(job_id, JOB_STATUS_CANCELLED, progress="cancelled")
                    if session_task_id:
                        await self._session_finish(session_task_id, "failed", error="cancelled")
                except GenerationJobError as exc:
                    self._set_status(job_id, JOB_STATUS_FAILED, progress="failed", error=str(exc))
                    if session_task_id:
                        await self._session_finish(session_task_id, "failed", error=str(exc))
                except Exception as exc:  # noqa: BLE001 — job must not kill the loop
                    self._set_status(job_id, JOB_STATUS_FAILED, progress="failed", error=str(exc))
                    if session_task_id:
                        await self._session_finish(session_task_id, "failed", error=str(exc))
        finally:
            self._tasks.pop(job_id, None)

    # ------------------------------------------------------------------
    # Context construction (decision 5a)
    # ------------------------------------------------------------------

    def _build_context(
        self, service_name: str, recipe_path: str, phase: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the generation context for one phase.

        Selected base files raw contents only + prompt + deploy metadata +
        activated profiles; app-manifest carve-out when compose itself is
        missing. ``compose_paths`` are included for the env completeness check.
        """
        from ..services.file_sets import _recipe_dir, compose_service_names as _names

        project_dir = settings.SOURCE_PROJECTS_DIR / service_name
        recipe_dir = _recipe_dir(project_dir, recipe_path)
        selection = request.get("selection") or {}
        sel_compose = [p for p in selection.get("compose", []) if isinstance(p, str)]
        sel_nginx = selection.get("nginx")
        sel_env = [p for p in selection.get("env", []) if isinstance(p, str)]
        profiles = [p for p in selection.get("profiles", []) if isinstance(p, str)]

        base_files: dict[str, str] = {}
        # Compose files are the base for the nginx phase; the compose phase
        # itself only gets the manifests (or a selected compose being replaced).
        # The ENV phase gets the mechanically scanned ${VAR} surface instead of
        # the raw compose: huge inputs make reasoning models spend the whole
        # token budget on chain-of-thought and return EMPTY content
        # (finish_reason=length) — the var list is all the phase needs.
        if phase == "nginx":
            for p in sel_compose:
                fp = recipe_dir / p
                if fp.is_file():
                    try:
                        base_files[p] = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
        elif phase == "env":
            from ..utils.var_scan import scan_compose_files

            compose_paths = [recipe_dir / p for p in sel_compose if (recipe_dir / p).is_file()]
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
            for p in sel_env:
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

        has_compose = bool(sel_compose) and all((recipe_dir / p).is_file() for p in sel_compose)

        context: dict[str, Any] = {
            "repo_description": f"Service: {service_name}{' @ ' + recipe_path if recipe_path else ''}",
            "prompt": request.get("prompt") or "",
            "base_files": base_files,
            "profiles": profiles,
            "compose_paths": [str(recipe_dir / p) for p in sel_compose],
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
        context["compose_service_names"] = _names([recipe_dir / p for p in sel_compose])

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

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

    @staticmethod
    def _compute_phases(job_type: str, request: dict[str, Any]) -> list[str]:
        if job_type == "generate_missing":
            return list(PHASE_ORDER)
        if job_type == "per_user_env":
            return ["env"]
        cat = request.get("category")
        if cat in PHASE_ORDER:
            return [cat]
        if cat in ("docker_compose", "nginx_conf", "env_file"):
            return {"docker_compose": ["compose"], "nginx_conf": ["nginx"], "env_file": ["env"]}[cat]
        raise GenerationJobError(f"Unknown generation category: {cat}")

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
                    result: dict | None = None) -> None:
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
                    for idx, phase in enumerate(phases):
                        self._check_cancelled(job_id)
                        self._set_status(
                            job_id, JOB_STATUS_RUNNING,
                            progress=f"phase {idx + 1}/{len(phases)}: {phase}",
                            phase=phase, phase_index=idx,
                        )

                        async def progress(msg: str) -> None:
                            self._check_cancelled(job_id)
                            self._set_status(job_id, JOB_STATUS_RUNNING, progress=msg)

                        context = self._build_context(service_name, recipe_path, phase, request)
                        if job.job_type == "per_user_env" and phase == "env":
                            gen = await llm_service.generate_per_user_env(db, context)
                        else:
                            gen = await llm_service.generate_with_agent(db, PHASE_CONFIG_TYPE[phase], context, progress=progress)
                        self._check_cancelled(job_id)

                        content = gen.get("generated_content") or ""
                        validations[phase] = gen.get("validation") or {}
                        warnings.extend(gen.get("warnings") or [])
                        if content:
                            result_files[PHASE_FILENAME[phase]] = content
                        else:
                            errs = (gen.get("validation") or {}).get("errors") or gen.get("warnings") or []
                            raise GenerationJobError(
                                f"phase '{phase}' produced no output: {'; '.join(errs) if errs else 'LLM unavailable?'}"
                            )

                    result = {
                        "files": result_files,
                        "validations": validations,
                        "warnings": warnings,
                        "per_user_env_name": gen.get("per_user_env_name"),
                    }
                    self._set_status(job_id, JOB_STATUS_COMPLETED, progress="completed", result=result)
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
        # Compose files are the base for nginx + env phases; the compose phase
        # itself only gets the manifests (or a selected compose being replaced).
        if phase in ("nginx", "env"):
            for p in sel_compose:
                fp = recipe_dir / p
                if fp.is_file():
                    try:
                        base_files[p] = fp.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
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
                        base_files[p] = fp.read_text(encoding="utf-8", errors="replace")
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

"""The gateway's bounded durable task queue (GRILLING G92; T2/T3/T5/T6/T10).

``gateway.tasks`` is the queue. This module owns the parts that are NOT the
generation runner:

* admission — 20 queued, plus the planner-heartbeat liveness check;
* the serialized planner — plan exhaustion trigger, 300 s window, per-user
  deficit round-robin, a ``plan_size``-deep plan that is never refilled;
* the scheduler — claims ``planned -> running`` on the lowest ``plan_seq`` and
  spawns the generation runner (the runner only executes);
* the watchdog — per-kind run timeouts and cancellation settling;
* telemetry — counts, positions, per-kind medians, pinned workers, heartbeat;
* the read-only projection of the ``-api`` queue (R9.2).

The -api's queue is never written here; submit/cancel remain HTTP calls to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import queue_contract as qc

_log = logging.getLogger("gateway.queue")

_PLAN_LOCK = 7_412_015
_ADMIT_CLAIM_LOCK = 7_412_016

_MEDIAN_CACHE_TTL = 5.0
_CONFIG_TTL = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _fair_select(rows: list[dict], plan_size: int) -> list[dict]:
    """Per-user deficit round-robin with per-user FIFO (R4.1)."""
    pending: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: (r["created_at"], r["task_id"])):
        pending.setdefault(row.get("user_key") or "", []).append(row)
    selected: list[dict] = []
    while len(selected) < plan_size and pending:
        order = sorted(
            (k for k, v in pending.items() if v),
            key=lambda k: (pending[k][0]["created_at"], k),
        )
        if not order:
            break
        for user in order:
            if len(selected) >= plan_size:
                break
            queue = pending[user]
            if queue:
                selected.append(queue.pop(0))
        pending = {k: v for k, v in pending.items() if v}
    return selected


class GatewayQueue:
    def __init__(self) -> None:
        self._config_cache: tuple[float, dict] | None = None
        # Per-table caches: the gateway and the -api queues have different
        # histories and must never share an estimate.
        self._median_cache: dict[str, tuple[float, dict, dict, float | None]] = {}
        self._last_plan = 0.0
        self._stop = False
        self._loop_task: asyncio.Task | None = None
        # The generation runner to spawn for a claimed row. None = the module
        # singleton; tests substitute a fresh manager so per-loop locks never
        # cross event loops.
        self.runner = None

    # ------------------------------------------------------------------
    # Configuration / heartbeat / admission
    # ------------------------------------------------------------------
    def config(self, db: Session | None = None, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._config_cache and now - self._config_cache[0] < _CONFIG_TTL:
            return self._config_cache[1]
        ref = db
        if ref is None:
            from ..database import SessionLocal
            ref = SessionLocal()
        try:
            row = ref.execute(
                text("SELECT * FROM gateway.queue_config WHERE id=1")
            ).mappings().first()
        finally:
            if db is None:
                ref.close()
        cfg = qc.config_from_row(dict(row) if row else None)
        self._config_cache = (now, cfg)
        return cfg

    def _heartbeat(self, db: Session) -> tuple[datetime | None, bool, int]:
        cfg = self.config(db)
        row = db.execute(
            text("SELECT last_dispatch_at FROM gateway.queue_state WHERE id=1")
        ).mappings().first()
        last = row["last_dispatch_at"] if row else None
        stale = int(cfg["heartbeat_stale_s"])
        if last is None:
            return None, False, stale
        return last, (_now() - last).total_seconds() <= stale, stale

    def admit(self, db: Session) -> None:
        """Check the admission boundary in the caller's transaction (R1).

        ``create_job`` calls this before ``db.add(job)``: the advisory lock makes
        the count and the insert one critical section across the four workers.
        """
        # Fresh read: the cap check is a refusal boundary, not a hot path — a
        # changed configuration must take effect on the next submission.
        cfg = self.config(db, force=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMIT_CLAIM_LOCK})
        queued = db.execute(
            text("SELECT count(*) FROM gateway.tasks "
                 "WHERE status='queued' AND deleted_at IS NULL")
        ).scalar_one()
        if queued >= int(cfg["queue_cap"]):
            raise qc.QueueFullError(int(queued), int(cfg["queue_cap"]))
        last, alive, stale = self._heartbeat(db)
        if last is None:
            raise qc.QueueUnavailableError(None, stale)
        age = (_now() - last).total_seconds()
        if age > stale:
            raise qc.QueueUnavailableError(age, stale)

    # ------------------------------------------------------------------
    # Planner (T2/R3)
    # ------------------------------------------------------------------
    def planner_tick(self) -> None:
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            got = db.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _PLAN_LOCK}
            ).scalar()
            if not got:
                db.rollback()
                return
            db.execute(
                text(
                    "INSERT INTO gateway.queue_state (id, last_dispatch_at, planner_id, "
                    "updated_at) VALUES (1, now(), :p, now()) "
                    "ON CONFLICT (id) DO UPDATE SET last_dispatch_at=now(), "
                    "planner_id=EXCLUDED.planner_id, updated_at=now()"
                ),
                {"p": str(os.getpid())},
            )
            planned = db.execute(
                text("SELECT count(*) FROM gateway.tasks "
                     "WHERE status='planned' AND deleted_at IS NULL")
            ).scalar_one()
            if planned:
                db.commit()  # heartbeat only; the plan is not refilled (D2)
                return
            # The lane is exhausted and its plan is (re)formed: mark the
            # per-lane plan time. A stuck plan stops advancing it (G94 R4.3).
            db.execute(
                text("UPDATE gateway.queue_state SET last_plan_operator_at=now(), "
                     "updated_at=now() WHERE id=1")
            )
            self._write_plan(db, self.config(db))
            db.commit()
        except Exception:  # noqa: BLE001 — the queue outlives one bad tick
            db.rollback()
            _log.exception("gateway planner tick failed")
        finally:
            db.close()

    def _write_plan(self, db: Session, cfg: dict) -> int:
        rows = db.execute(
            text(
                "SELECT t.task_id, t.created_at, "
                "       COALESCE(t.target_snapshot->>'user_name', u.username, '') "
                "           AS user_key "
                "FROM gateway.tasks t "
                "LEFT JOIN registry.services s ON s.id = t.service_id "
                "LEFT JOIN registry.api_users u ON u.id = s.owner_api_user_id "
                "WHERE t.status='queued' AND t.deleted_at IS NULL "
                "ORDER BY t.created_at, t.task_id"
            )
        ).mappings().all()
        if not rows:
            return 0
        plan_size = int(cfg["plan_size"])
        span = int(cfg["span_s"])
        anchor = rows[0]["created_at"]
        extended = 0
        while True:
            horizon = anchor + timedelta(seconds=span * (extended + 1))
            candidates = [r for r in rows if r["created_at"] < horizon]
            if len(candidates) >= plan_size or len(candidates) >= len(rows):
                break
            extended += 1
        selected = _fair_select([dict(r) for r in candidates], plan_size)
        for seq, row in enumerate(selected, start=1):
            db.execute(
                text("UPDATE gateway.tasks SET status='planned', plan_seq=:seq, "
                     "updated_at=now() WHERE task_id=:id AND status='queued'"),
                {"seq": seq, "id": row["task_id"]},
            )
        if selected:
            _log.info("gateway planned %d task(s)", len(selected))
        return len(selected)

    # ------------------------------------------------------------------
    # Scheduler / claims (T3/R4)
    # ------------------------------------------------------------------
    def claim_next(self) -> str | None:
        from ..config import settings
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            cfg = self.config(db)
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADMIT_CLAIM_LOCK})
            running = db.execute(
                text("SELECT count(*) FROM gateway.tasks "
                     "WHERE status='running' AND deleted_at IS NULL")
            ).scalar_one()
            bound = min(int(cfg["workers"]), int(settings.GATEWAY_MAX_GENERATIONS))
            if running >= bound:
                db.commit()
                return None
            row = db.execute(
                text(
                    "SELECT task_id FROM gateway.tasks "
                    "WHERE status='planned' AND deleted_at IS NULL "
                    "ORDER BY plan_seq NULLS LAST, created_at, task_id "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            ).mappings().first()
            if not row:
                db.commit()
                return None
            db.execute(
                text("UPDATE gateway.tasks SET status='running', started_at=now(), "
                     "updated_at=now() WHERE task_id=:id"),
                {"id": row["task_id"]},
            )
            db.commit()
            return row["task_id"]
        except Exception:  # noqa: BLE001
            db.rollback()
            _log.exception("gateway claim failed")
            return None
        finally:
            db.close()

    async def dispatch(self) -> int:
        """Claim every free slot and start its runner; returns claims made."""
        from .generation_jobs import generation_jobs

        runner = self.runner or generation_jobs._run_job
        claimed = 0
        while True:
            task_id = await asyncio.to_thread(self.claim_next)
            if not task_id:
                return claimed
            claimed += 1
            asyncio.create_task(runner(task_id))

    # ------------------------------------------------------------------
    # Watchdog (T4/R5)
    # ------------------------------------------------------------------
    def watchdog_tick(self) -> None:
        from ..database import SessionLocal

        db = SessionLocal()
        try:
            # Fresh: an operator's timeout change must apply to the next tick.
            cfg = self.config(db, force=True)
            rows = db.execute(
                text(
                    "SELECT task_id, operation, started_at, cancel_requested_at, error "
                    "FROM gateway.tasks WHERE status='running' AND deleted_at IS NULL"
                )
            ).mappings().all()
            now = _now()
            for row in rows:
                kind = "fix_analysis" if row["operation"] == "fix" else "generate"
                requested = row["cancel_requested_at"]
                if requested is None:
                    timeout = int(cfg.get(f"run_timeout_{kind}",
                                          qc.RUN_TIMEOUTS[kind]))
                    if row["started_at"] is not None and \
                            (now - row["started_at"]).total_seconds() > timeout:
                        db.execute(
                            text(
                                "UPDATE gateway.tasks SET cancel_requested_at=now(), "
                                "error=:e, updated_at=now() WHERE task_id=:id "
                                "AND status='running' AND cancel_requested_at IS NULL"
                            ),
                            {"e": f"run timeout after {timeout}s", "id": row["task_id"]},
                        )
                        self._signal(row["task_id"])
                    continue
                timeout = int(cfg.get(f"cancel_timeout_{kind}",
                                      qc.CANCEL_TIMEOUTS[kind]))
                if (now - requested).total_seconds() <= timeout:
                    continue
                # The runner did not settle: the supervisor takes the row over.
                db.execute(
                    text(
                        "UPDATE gateway.tasks SET status='failed', error=:e, "
                        "finished_at=now(), updated_at=now() WHERE task_id=:id "
                        "AND status='running'"
                    ),
                    {
                        "e": f"cancellation did not settle within {timeout}s; "
                             "interrupted, runtime cleanup attempted",
                        "id": row["task_id"],
                    },
                )
                try:
                    from . import pre_deploy_registry as pdr
                    pdr.finish_leg(row["task_id"], None)
                except Exception:  # noqa: BLE001
                    pass
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            _log.exception("gateway watchdog tick failed")
        finally:
            db.close()

    @staticmethod
    def _signal(task_id: str) -> None:
        """Best-effort in-process wake; the row is the cross-process authority."""
        try:
            from .generation_jobs import generation_jobs
            generation_jobs.signal_cancel(task_id)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Loop (one per uvicorn worker; the advisory locks make them safe)
    # ------------------------------------------------------------------
    async def run(self) -> None:
        from ..database import SessionLocal

        # Beat immediately, so a just-started stack admits work.
        try:
            await asyncio.to_thread(self.planner_tick)
        except Exception:  # noqa: BLE001
            _log.exception("initial planner tick failed")
        while not self._stop:
            try:
                await self.dispatch()
                now = time.monotonic()
                interval = float(self.config().get("planner_interval_s", 2))
                if now - self._last_plan >= interval:
                    self._last_plan = now
                    await asyncio.to_thread(self.planner_tick)
                await asyncio.to_thread(self.watchdog_tick)
            except Exception:  # noqa: BLE001
                _log.exception("gateway queue loop tick failed")
            await asyncio.sleep(1.0)

    def start(self) -> asyncio.Task:
        self._stop = False
        self._loop_task = asyncio.create_task(self.run())
        return self._loop_task

    def stop(self) -> None:
        self._stop = True
        if self._loop_task:
            self._loop_task.cancel()

    # ------------------------------------------------------------------
    # Telemetry (T6/R7)
    # ------------------------------------------------------------------
    def counts(self) -> dict[str, Any]:
        """Cheap status counts from both queues, split by lane (the cards).

        Top-level keys are the combined totals (the gateway generation queue +
        both -api lanes); ``operator`` and ``maintenance`` carry the split the
        Dashboard and the Services bar need (G94 R5.2). The gateway queue is
        operator-only.
        """
        from ..database import ReaderSessionLocal, SessionLocal

        def one(db: Session, table: str) -> dict[str, Any]:
            out = {
                "queued": 0, "planned": 0, "running": 0,
                "operator": {"queued": 0, "planned": 0, "running": 0},
                "maintenance": {"queued": 0, "planned": 0, "running": 0},
            }
            for row in db.execute(
                text(f"SELECT lane, status, count(*) AS n FROM {table} "
                     "WHERE deleted_at IS NULL AND status IN "
                     "('queued','planned','running') GROUP BY lane, status")
            ).mappings():
                lane = row["lane"] if row["lane"] in qc.LANES else qc.OPERATOR
                out[row["status"]] += row["n"]
                out[lane][row["status"]] += row["n"]
            return out

        gw = SessionLocal()
        try:
            g = one(gw, "gateway.tasks")
        finally:
            gw.close()
        rd = ReaderSessionLocal()
        try:
            r = one(rd, "registry.tasks")
        finally:
            rd.close()
        out: dict[str, Any] = {
            key: g[key] + r[key] for key in ("queued", "planned", "running")
        }
        for lane in qc.LANES:
            out[lane] = {
                key: g[lane][key] + r[lane][key]
                for key in ("queued", "planned", "running")
            }
        return out

    def stats(self) -> dict[str, Any]:
        from ..database import ReaderSessionLocal, SessionLocal

        gw_db = SessionLocal()
        try:
            gateway = self._stats_for(gw_db, "gateway",
                                      "gateway.tasks", "gateway.queue_config",
                                      "gateway.queue_state")
        finally:
            gw_db.close()
        rd_db = ReaderSessionLocal()
        try:
            registry = self._stats_for(rd_db, "registry",
                                       "registry.tasks", "registry.queue_config",
                                       "registry.queue_state")
            registry["hold_alerts"] = self._hold_alerts(rd_db)
        finally:
            rd_db.close()
        return {"gateway": gateway, "registry": registry,
                "generated_at": _now().isoformat()}

    @staticmethod
    def _hold_alerts(db: Session, limit: int = 10) -> list[dict[str, Any]]:
        """Recent stream-conf hold alerts, read from the registry task rows (S9).

        A hold that used an index records ``result.hold`` on the -api task
        (``index``, ``previous_paused_present``, ``alert``, plus the §15.3
        evidence: the existing ``.paused`` copy path(s) with mtime/size, the
        path the active conf was held as, and the active conf path). The
        Services page renders these as a warning naming the service, the index
        and the task; a normal hold records nothing, so it produces no
        notification. The new fields are rendered defensively — an older -api
        row simply has none of them.
        """
        rows = db.execute(
            text(
                "SELECT t.task_id, t.operation, t.finished_at, t.result, "
                "       s.service_name, s.label, a.username AS user_name "
                "FROM registry.tasks t "
                "LEFT JOIN registry.services s ON s.id = t.service_id "
                "LEFT JOIN registry.api_users a ON a.id = s.owner_api_user_id "
                "WHERE t.deleted_at IS NULL "
                "  AND t.result ? 'hold' "
                "  AND t.result->'hold'->>'alert' IS NOT NULL "
                "ORDER BY t.finished_at DESC NULLS LAST "
                "LIMIT :n"
            ),
            {"n": int(limit)},
        ).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            hold = (row["result"] or {}).get("hold") or {}
            # `existing` is the held-over `.paused` copy list: [{path,mtime,size}].
            existing: list[dict[str, Any]] = []
            raw_existing = hold.get("existing")
            if isinstance(raw_existing, dict):
                raw_existing = [raw_existing]
            if isinstance(raw_existing, list):
                for item in raw_existing:
                    if isinstance(item, dict):
                        existing.append({
                            "path": item.get("path"),
                            "mtime": item.get("mtime"),
                            "size": item.get("size"),
                        })
                    elif isinstance(item, str):
                        existing.append(
                            {"path": item, "mtime": None, "size": None})
            out.append({
                "task_id": row["task_id"],
                "operation": row["operation"],
                "user_name": row["user_name"],
                "service_name": row["service_name"],
                "label": row["label"],
                "index": hold.get("index"),
                "message": hold.get("alert"),
                # §15.3 — the active conf and the indexed destination, plus
                # the pre-existing `.paused` copies and their evidence.
                "existing": existing,
                "held_as": hold.get("held_as"),
                "active": hold.get("active"),
                "finished_at": (
                    row["finished_at"].isoformat() if row["finished_at"] else None
                ),
            })
        return out

    def _stats_for(self, db: Session, queue: str, tasks_table: str,
                   config_table: str, state_table: str) -> dict[str, Any]:
        cfg_row = db.execute(
            text(f"SELECT * FROM {config_table} WHERE id=1")
        ).mappings().first()
        cfg = qc.config_from_row(dict(cfg_row) if cfg_row else None)

        lane_counts = {lane: {state: 0 for state in qc.STATES}
                       for lane in qc.LANES}
        for row in db.execute(
            text(f"SELECT lane, status, count(*) AS n FROM {tasks_table} "
                 "WHERE deleted_at IS NULL GROUP BY lane, status")
        ).mappings():
            lane = row["lane"] if row["lane"] in lane_counts else qc.OPERATOR
            lane_counts[lane][row["status"]] = row["n"]

        state = db.execute(
            text(f"SELECT * FROM {state_table} WHERE id=1")
        ).mappings().first()
        last = state["last_dispatch_at"] if state else None
        stale = int(cfg["heartbeat_stale_s"])
        alive = last is not None and (_now() - last).total_seconds() <= stale
        age = None if last is None else round(max(0.0, (_now() - last).total_seconds()), 1)

        medians, samples, global_median = self._medians(db, tasks_table)

        active = db.execute(
            text(f"SELECT task_id, kind, operation, lane, status, plan_seq, "
                 f"created_at, started_at, finished_at FROM {tasks_table} "
                 "WHERE status IN ('queued','planned','running') "
                 "AND deleted_at IS NULL ORDER BY created_at, task_id")
        ).mappings().all()
        queued_by_lane: dict[str, list[dict]] = {lane: [] for lane in qc.LANES}
        for row in active:
            if row["status"] == qc.QUEUED:
                lane = row["lane"] if row["lane"] in queued_by_lane else qc.OPERATOR
                queued_by_lane[lane].append(dict(row))
        lane_tasks: dict[str, dict[str, Any]] = {lane: {} for lane in qc.LANES}
        now = _now()
        for row in active:
            lane = row["lane"] if row["lane"] in lane_tasks else qc.OPERATOR
            kind = self._kind(row)
            estimate = medians.get(kind, global_median) or 0.0
            workers = max(1, int(qc.lane_config(cfg, lane)["workers"]))
            position = None
            if row["status"] == qc.QUEUED:
                order = [r["task_id"] for r in queued_by_lane.get(lane, [])]
                position = order.index(row["task_id"]) if row["task_id"] in order else None
                start = (position or 0) / workers * estimate
                finish = start + estimate
            elif row["status"] == qc.PLANNED:
                start, finish = 0.0, estimate
            else:
                elapsed = 0.0
                if row["started_at"]:
                    elapsed = max(0.0, (now - row["started_at"]).total_seconds())
                start, finish = 0.0, max(0.0, estimate - elapsed)
            lane_tasks.setdefault(lane, {})[row["task_id"]] = {
                "status": row["status"],
                "kind": kind,
                "lane": lane,
                "plan_seq": row["plan_seq"],
                "position": position,
                "eta_start_s": round(start, 1),
                "eta_finish_s": round(finish, 1),
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            }

        pinned_by_lane = {lane: 0 for lane in qc.LANES}
        for row in db.execute(
            text(f"SELECT lane, kind, operation, cancel_requested_at FROM {tasks_table} "
                 "WHERE status='running' AND cancel_requested_at IS NOT NULL "
                 "AND deleted_at IS NULL")
        ).mappings():
            kind = self._kind(row)
            lane = row["lane"] if row["lane"] in pinned_by_lane else qc.OPERATOR
            limit = int(cfg.get(f"cancel_timeout_{kind}", qc.CANCEL_TIMEOUTS[kind]))
            if (now - row["cancel_requested_at"]).total_seconds() > limit:
                pinned_by_lane[lane] += 1

        lanes: dict[str, Any] = {}
        for lane in qc.LANES:
            limits = qc.lane_config(cfg, lane)
            plan_at = (state.get(f"last_plan_{lane}_at") if state else None) or last
            lanes[lane] = {
                "counts": lane_counts[lane],
                "workers": int(limits["workers"]),
                "cap": int(limits["cap"]),
                "plan_size": int(limits["plan_size"]),
                "span_s": limits["span_s"],
                "pinned_workers": pinned_by_lane[lane],
                "last_plan_at": plan_at.isoformat() if plan_at else None,
                "tasks": lane_tasks.get(lane, {}),
            }

        operator = lanes[qc.OPERATOR]
        return {
            "queue": queue,
            "counts": operator["counts"],
            "workers": operator["workers"],
            "pinned_workers": operator["pinned_workers"],
            "cap": operator["cap"],
            "span_s": operator["span_s"],
            "plan_size": operator["plan_size"],
            "heartbeat": {
                "last_dispatch_at": last.isoformat() if last else None,
                "age_s": age,
                "alive": alive,
                "stale_after_s": stale,
            },
            "planner_interval_s": int(cfg["planner_interval_s"]),
            "tasks": operator["tasks"],
            "kinds": {
                kind: {"median_s": round(value, 1), "samples": samples.get(kind, 0)}
                for kind, value in medians.items()
            },
            "config": cfg,
            "lanes": lanes,
        }

    @staticmethod
    def _kind(row: dict) -> str:
        if row.get("kind"):
            return row["kind"]
        return "fix_analysis" if row.get("operation") == "fix" else "generate"

    def _medians(self, db: Session, tasks_table: str
                 ) -> tuple[dict[str, float], dict[str, int], float | None]:
        now = time.monotonic()
        cached = self._median_cache.get(tasks_table)
        if cached and now - cached[0] < _MEDIAN_CACHE_TTL:
            return cached[1], cached[2], cached[3]
        rows = db.execute(
            text(
                "SELECT kind, operation, "
                "       EXTRACT(EPOCH FROM (finished_at - started_at)) AS secs, "
                "       row_number() OVER (PARTITION BY kind ORDER BY finished_at DESC) AS rn "
                f"FROM {tasks_table} "
                "WHERE status IN ('completed','failed') AND started_at IS NOT NULL "
                "AND finished_at IS NOT NULL AND deleted_at IS NULL"
            )
        ).mappings().all()
        by_kind: dict[str, list[float]] = {}
        global_vals: list[float] = []
        for row in rows:
            if row["secs"] is None:
                continue
            secs = float(row["secs"])
            global_vals.append(secs)
            if int(row["rn"]) <= 50:
                kind = row["kind"] or ("fix_analysis" if row["operation"] == "fix"
                                       else "generate")
                by_kind.setdefault(kind, []).append(secs)
        medians = {kind: _median(vals) for kind, vals in by_kind.items() if vals}
        samples = {kind: len(vals) for kind, vals in by_kind.items()}
        global_median = _median(global_vals[-50:]) if global_vals else None
        self._median_cache[tasks_table] = (now, medians, samples, global_median)
        return medians, samples, global_median


gateway_queue = GatewayQueue()

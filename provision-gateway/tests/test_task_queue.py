"""The gateway's bounded durable queue (G92 T2/T3/T5/T6/T10, R3–R7).

Exercises the real PostgreSQL store: plan window/fairness, claim atomicity,
admission, telemetry and the watchdog.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


def _now():
    return datetime.now(timezone.utc)


def make_task(db, task_id, *, status="queued", user="alice", operation="generate_missing",
              kind="generate", created_at=None, started_at=None,
              cancel_requested_at=None):
    db.execute(
        text(
            "INSERT INTO gateway.tasks (task_id, collision_token, procedure_id, service_id, "
            "executor, kind, operation, origin, status, target_snapshot, created_at, "
            "updated_at, started_at, cancel_requested_at) "
            "VALUES (:tid, :ct, :pid, NULL, 'gateway', :kind, :op, 'deploy', :status, "
            ":snap, :created, :created, :started, :cancel)"
        ),
        {
            "tid": task_id, "ct": uuid.uuid4().hex[:12], "pid": uuid.uuid4(),
            "kind": kind, "op": operation, "status": status,
            "snap": json.dumps({"user_name": user, "service_name": task_id,
                                "label": "0"}),
            "created": created_at or _now(),
            "started": started_at,
            "cancel": cancel_requested_at,
        },
    )
    db.commit()
    return task_id


def set_queue_config(**values):
    from app.database import SessionLocal
    from app.services.task_queue import gateway_queue
    gateway_queue._config_cache = None
    db = SessionLocal()
    try:
        sets = ", ".join(f"{k}=:v{i}" for i, k in enumerate(values))
        db.execute(text(f"UPDATE gateway.queue_config SET {sets} WHERE id=1"),
                   {f"v{i}": v for i, v in enumerate(values.values())})
        db.commit()
    finally:
        db.close()


def planned(db):
    return [dict(r) for r in db.execute(
        text("SELECT task_id, plan_seq, status FROM gateway.tasks "
             "WHERE status='planned' ORDER BY plan_seq")).mappings().all()]


class TestContract:
    def test_both_services_use_the_same_contract(self):
        """T0: state names, error codes, bounds and stats keys are identical."""
        import importlib.util
        import pg_test_db
        from app.services import queue_contract as gw

        path = (pg_test_db.REPO_ROOT / "_users_provision" / "user_provision_tool"
                / "lib" / "queue_contract.py")
        spec = importlib.util.spec_from_file_location("api_queue_contract", path)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)

        assert tuple(gw.STATES) == tuple(api.STATES)
        assert tuple(gw.ERROR_CODES) == tuple(api.ERROR_CODES)
        assert tuple(gw.KINDS) == tuple(api.KINDS)
        assert gw.RUN_TIMEOUTS == api.RUN_TIMEOUTS
        assert gw.CANCEL_TIMEOUTS == api.CANCEL_TIMEOUTS
        assert tuple(gw.CONFIG_KEYS) == tuple(api.CONFIG_KEYS)
        assert tuple(gw.STATS_TOP_KEYS) == tuple(api.STATS_TOP_KEYS)
        assert tuple(gw.STATS_HEARTBEAT_KEYS) == tuple(api.STATS_HEARTBEAT_KEYS)
        assert tuple(gw.STATS_TASK_KEYS) == tuple(api.STATS_TASK_KEYS)
        assert tuple(gw.STATS_KIND_KEYS) == tuple(api.STATS_KIND_KEYS)
        for key in ("DEFAULT_QUEUE_CAP", "DEFAULT_WORKERS", "DEFAULT_PLAN_SIZE",
                    "DEFAULT_SPAN_S", "DEFAULT_HEARTBEAT_STALE_S"):
            assert getattr(gw, key) == getattr(api, key)


class TestPlanner:
    def test_window_and_per_user_fairness(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            t0 = _now()
            # alice1 t0, bob1 t1, alice2 t2, bob2 t3, carol1 t4, alice3 t5
            make_task(db, "gt-a1", user="alice", created_at=t0)
            make_task(db, "gt-b1", user="bob", created_at=t0 + timedelta(seconds=1))
            make_task(db, "gt-a2", user="alice", created_at=t0 + timedelta(seconds=2))
            make_task(db, "gt-b2", user="bob", created_at=t0 + timedelta(seconds=3))
            make_task(db, "gt-c1", user="carol", created_at=t0 + timedelta(seconds=4))
            make_task(db, "gt-a3", user="alice", created_at=t0 + timedelta(seconds=5))
        finally:
            db.close()

        gateway_queue.planner_tick()
        db = SessionLocal()
        try:
            rows = planned(db)
        finally:
            db.close()
        # Round 1 gives each user one slot in enqueue order; round 2 continues.
        assert [r["task_id"] for r in rows] == ["gt-a1", "gt-b1", "gt-c1", "gt-a2"]
        assert [r["plan_seq"] for r in rows] == [1, 2, 3, 4]

    def test_window_extends_when_too_few_candidates(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            t0 = _now()
            make_task(db, "gt-a1", user="alice", created_at=t0)
            # Three more beyond the first 300 s span.
            for idx in range(3):
                make_task(db, f"gt-b{idx}", user="bob",
                          created_at=t0 + timedelta(seconds=600 + idx))
        finally:
            db.close()

        gateway_queue.planner_tick()
        db = SessionLocal()
        try:
            rows = planned(db)
        finally:
            db.close()
        assert len(rows) == 4, "the window extended to fill the plan"

    def test_plan_is_not_refilled_until_exhausted(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            for idx in range(6):
                make_task(db, f"gt-t{idx}", user=f"u{idx}",
                          created_at=_now() + timedelta(seconds=idx))
        finally:
            db.close()
        gateway_queue.planner_tick()
        # One planned row is claimed -> 3 planned remain: no new plan (D2).
        gateway_queue.claim_next()
        gateway_queue.planner_tick()
        db = SessionLocal()
        try:
            assert len(planned(db)) == 3
        finally:
            db.close()

    def test_plan_forms_again_when_the_last_planned_is_claimed(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            for idx in range(6):
                make_task(db, f"gt-t{idx}", user=f"u{idx}",
                          created_at=_now() + timedelta(seconds=idx))
        finally:
            db.close()
        gateway_queue.planner_tick()
        for _ in range(4):
            assert gateway_queue.claim_next()
        # Running count is now 4 (the worker bound), so the plan can form but
        # no claim can happen — the plan itself is written by the planner.
        gateway_queue.planner_tick()
        db = SessionLocal()
        try:
            assert len(planned(db)) == 2, "the remaining queued tasks form the next plan"
        finally:
            db.close()


class TestAdmission:
    def test_twenty_queued_admitted_and_the_21st_refused(self):
        from app.database import SessionLocal
        from app.services.generation_jobs import GenerationJobManager
        from app.services.queue_contract import QueueFullError

        db = SessionLocal()
        try:
            for i in range(20):
                make_task(db, f"gt-cap-{i}", user=f"u{i}")
        finally:
            db.close()
        db = SessionLocal()
        try:
            with pytest.raises(QueueFullError):
                GenerationJobManager().create_job(
                    db, "probe", "", "generate_missing", {})
        finally:
            db.close()
        db = SessionLocal()
        try:
            assert db.execute(text(
                "SELECT count(*) FROM gateway.tasks WHERE status='queued'"
            )).scalar_one() == 20
        finally:
            db.close()

    def test_stale_heartbeat_refuses(self):
        from app.database import SessionLocal
        from app.services.generation_jobs import GenerationJobManager
        from app.services.queue_contract import QueueUnavailableError

        db = SessionLocal()
        try:
            db.execute(text(
                "UPDATE gateway.queue_state SET last_dispatch_at = now() - interval '1 hour' "
                "WHERE id=1"))
            db.commit()
            with pytest.raises(QueueUnavailableError):
                GenerationJobManager().create_job(
                    db, "probe", "", "generate_missing", {})
        finally:
            db.close()


class TestClaim:
    def test_two_claimants_one_winner(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            make_task(db, "gt-one", user="alice")
        finally:
            db.close()
        gateway_queue.planner_tick()

        winners = []
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            winners.append(gateway_queue.claim_next())

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert winners.count("gt-one") == 1
        assert winners.count(None) == 1


class TestWatchdog:
    def test_run_timeout_fails_the_row(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        set_queue_config(run_timeout_generate=1, cancel_timeout_generate=1)
        db = SessionLocal()
        try:
            make_task(db, "gt-slow", status="running", operation="generate_missing",
                      started_at=_now() - timedelta(seconds=30))
        finally:
            db.close()
        gateway_queue.watchdog_tick()
        db = SessionLocal()
        try:
            row = db.execute(text(
                "SELECT status, error, cancel_requested_at FROM gateway.tasks "
                "WHERE task_id='gt-slow'")).mappings().first()
            assert row["status"] == "running"
            assert row["error"].startswith("run timeout")
            assert row["cancel_requested_at"] is not None
            # Age the request past the cancellation bound: the supervisor fails it.
            db.execute(text(
                "UPDATE gateway.tasks SET cancel_requested_at = now() - interval '1 hour' "
                "WHERE task_id='gt-slow'"))
            db.commit()
        finally:
            db.close()
        gateway_queue.watchdog_tick()
        db = SessionLocal()
        try:
            row = db.execute(text(
                "SELECT status, error FROM gateway.tasks WHERE task_id='gt-slow'"
            )).mappings().first()
            assert row["status"] == "failed"
            assert "did not settle" in row["error"]
        finally:
            db.close()


class TestTelemetry:
    def test_positions_eta_and_median(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue

        db = SessionLocal()
        try:
            t0 = _now()
            make_task(db, "gt-q0", user="u0", created_at=t0)
            make_task(db, "gt-q1", user="u1", created_at=t0 + timedelta(seconds=1))
            make_task(db, "gt-q2", user="u2", created_at=t0 + timedelta(seconds=2))
            # Two finished generations, 100 s and 200 s: median 150 s.
            for idx, secs in ((0, 100), (1, 200)):
                make_task(db, "gt-done%d" % idx, status="completed",
                          created_at=t0, started_at=t0,
                          operation="generate_missing")
                db.execute(text(
                    "UPDATE gateway.tasks SET finished_at = started_at + "
                    "make_interval(secs => :s) WHERE task_id=:id"),
                    {"s": secs, "id": "gt-done%d" % idx})
            db.commit()
        finally:
            db.close()

        stats = gateway_queue.stats()["gateway"]
        assert stats["counts"]["queued"] == 3
        assert stats["counts"]["completed"] == 2
        assert stats["tasks"]["gt-q0"]["position"] == 0
        assert stats["tasks"]["gt-q1"]["position"] == 1
        assert stats["tasks"]["gt-q2"]["position"] == 2
        assert stats["kinds"]["generate"]["median_s"] == 150.0
        # workers 4: start ETA = position / 4 * 150; finish adds the estimate.
        assert stats["tasks"]["gt-q1"]["eta_start_s"] == 37.5
        assert stats["tasks"]["gt-q2"]["eta_finish_s"] == 225.0

    def test_counts_include_the_registry_queue(self):
        from app.database import SessionLocal
        from app.services.task_queue import gateway_queue
        from support import registry_engine

        db = SessionLocal()
        try:
            make_task(db, "gt-q", user="u0")
        finally:
            db.close()
        with registry_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO registry.tasks (task_id, procedure_id, service_id, executor, "
                "kind, operation, status, created_at, updated_at) "
                "VALUES ('api-q', :p, NULL, 'api', 'deploy', 'register', 'queued', "
                "now(), now())"), {"p": uuid.uuid4()})
        counts = gateway_queue.counts()
        assert counts["queued"] == 2

    def test_read_model_reads_registry_tasks(self):
        from app.services import registry_tasks
        from support import registry_engine

        with registry_engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO registry.tasks (task_id, procedure_id, service_id, executor, "
                "kind, operation, status, created_at, updated_at) "
                "VALUES ('api-read', :p, NULL, 'api', 'deploy', 'register', 'planned', "
                "now(), now())"), {"p": uuid.uuid4()})
        row = registry_tasks.get("api-read")
        assert row is not None and row["status"] == "planned"

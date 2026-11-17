"""R13/S10 — a deploy-with-grants refreshes only its own source."""

from __future__ import annotations

import asyncio

import pytest


class _FakeSession:
    def close(self):
        pass


def _patch(monkeypatch):
    from app.services import cross_orchestrator as co
    from app.services import deploy_workflow as dw
    from app.services import registry_tasks, service_grants

    calls = {"sync_source": [], "sync_all": 0, "retry": [], "grants": []}
    monkeypatch.setattr(dw, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        registry_tasks, "get", lambda task_id: {"status": "completed"})
    monkeypatch.setattr(
        service_grants, "set_grant",
        lambda db, source, target, actor_user_id=None: calls["grants"].append((source, target)))

    async def fake_sync_source(sid):
        calls["sync_source"].append(sid)
        return {"ok": True, "outcome": "ok", "reason": None}

    async def fake_sync_all():
        calls["sync_all"] += 1
        return {}

    monkeypatch.setattr(co.cross_orchestrator, "sync_source", fake_sync_source)
    monkeypatch.setattr(co.cross_orchestrator, "sync_all", fake_sync_all)
    monkeypatch.setattr(
        co.cross_orchestrator, "schedule_retry_sync",
        lambda ids=None, **kw: calls["retry"].append(ids))
    return calls


def test_a_deploy_with_grants_syncs_only_the_deployed_source(monkeypatch):
    from app.services.deploy_workflow import _after_deploy
    calls = _patch(monkeypatch)

    asyncio.run(_after_deploy("source-1", "task-1", ["target-1"], None))

    assert calls["grants"] == [("source-1", "target-1")]
    assert calls["sync_source"] == ["source-1"]
    assert calls["sync_all"] == 0
    assert calls["retry"] == []


def test_a_deploy_without_grants_keeps_the_lifecycle_pass(monkeypatch):
    from app.services.deploy_workflow import _after_deploy
    calls = _patch(monkeypatch)

    asyncio.run(_after_deploy("source-1", "task-1", [], None))

    assert calls["grants"] == []
    assert calls["sync_source"] == []
    assert calls["sync_all"] == 1


def test_a_non_ok_targeted_sync_schedules_a_targeted_retry(monkeypatch):
    from app.services import cross_orchestrator as co
    from app.services.deploy_workflow import _after_deploy
    calls = _patch(monkeypatch)

    async def deferred(sid):
        calls["sync_source"].append(sid)
        return {"ok": False, "outcome": "deferred", "reason": "queue down"}

    monkeypatch.setattr(co.cross_orchestrator, "sync_source", deferred)
    asyncio.run(_after_deploy("source-1", "task-1", ["target-1"], None))

    assert calls["retry"] == [["source-1"]]
    assert calls["sync_all"] == 0


def test_the_route_level_helpers_do_not_global_sync_an_empty_set(monkeypatch):
    from app.services import cross_orchestrator as co

    called = []
    monkeypatch.setattr(
        co.cross_orchestrator, "sync_all",
        lambda: called.append("global"))
    monkeypatch.setattr(
        co.cross_orchestrator, "sync_sources",
        lambda ids: called.append(("targeted", list(ids))))

    co.schedule_sync([])
    co.schedule_sync_threadsafe([])
    assert called == []

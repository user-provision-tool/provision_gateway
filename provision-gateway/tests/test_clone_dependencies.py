"""G102 R5 — the clone's dependency order, artifact remap and mapped grants."""

from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.routers import users as users_router


def _svc(name, sid, user):
    return {
        "service_name": name, "label": "0", "service_id": sid,
        "recipe_path": "", "compose_template_path": f"/tpl/{name}.yml.j2",
        "nginx_conf_template_path": f"/tpl/{name}.nginx.conf.j2",
        "profiles": [],
    }


def test_clone_registers_dependencies_first_and_remaps_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    for name in ("A", "B"):
        (tmp_path / name).mkdir()
    # The source artifact: A depends on B.
    (tmp_path / "A" / "dependencies.src.0.json").write_text(json.dumps({
        "version": 1,
        "generated_at": "2026-11-19T00:00:00Z",
        "source": "src/A/0",
        "services": {
            "src/A/0": {"user": "src", "service": "A", "label": "0",
                        "env": {"DEP_URL": {"depends_on": "src/B/0"}}},
            "src/B/0": {"user": "src", "service": "B", "label": "0", "env": {}},
        },
    }))

    source = {"user_name": "src", "user_status": [
        {"healthy_services": [_svc("A", "old-A", "src"), _svc("B", "old-B", "src")]},
    ]}
    target_services: dict[str, dict] = {}
    order: list[str] = []
    grants: list[tuple[str, str]] = []

    async def get_user(username):
        if username == "src":
            return source
        return {"user_name": username, "user_status": [
            {"healthy_services": list(target_services.values())},
        ]}

    async def register_user(**payload):
        name = payload["service_name"]
        order.append(name)
        new_id = f"new-{name}"
        target_services[name] = _svc(name, new_id, "tgt")
        return {"service_id": new_id, "task_id": f"task-{name}"}

    async def get_task(task_id):
        return {"status": "completed"}

    class _Session:
        def close(self):
            pass

    def set_grant(db, source_id, target_id, actor_user_id=None):
        grants.append((source_id, target_id))
        return {"target_service_id": target_id}

    monkeypatch.setattr(users_router.provision_service, "get_user", get_user)
    monkeypatch.setattr(users_router.provision_service, "register_user", register_user)
    monkeypatch.setattr(users_router.provision_service, "get_task", get_task)
    monkeypatch.setattr(users_router, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(users_router.service_grants, "set_grant", set_grant)

    result = asyncio.run(users_router._clone_user_impl("src", "tgt", {}))

    assert order == ["B", "A"]          # dependencies before their source
    assert grants == [("new-A", "new-B")]
    assert all("error" not in t for t in result["tasks"])
    artifact = json.loads((tmp_path / "A" / "dependencies.tgt.0.json").read_text())
    assert artifact["source"] == "tgt/A/0"
    assert artifact["services"]["tgt/A/0"]["env"] == {
        "DEP_URL": {"depends_on": "tgt/B/0"}}
    assert "tgt/B/0" in artifact["services"]

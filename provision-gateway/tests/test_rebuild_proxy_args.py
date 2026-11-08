"""The Redeploy modal's "use global proxy" tick must reach the build args.

The -api's ``RebuildRequest`` has no ``use_global_proxy`` field, so the gateway
expands the tick into the four proxy build args before forwarding the rebuild.
Without it a context build (the example-s2s recipe) reaches no package index.
"""

from __future__ import annotations

import asyncio

from app.routers import users as users_router
from app.services import proxy_service


def test_rebuild_expands_use_global_proxy_into_build_args(monkeypatch):
    captured = {}

    async def fake_rebuild(service_id, **kwargs):
        captured["service_id"] = service_id
        captured.update(kwargs)
        return {"task_id": None}  # no cross-orchestrator follow-up

    class _Db:
        def close(self):
            pass

    monkeypatch.setattr(users_router.provision_service, "rebuild_instance", fake_rebuild)
    monkeypatch.setattr(users_router, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(
        proxy_service, "inject_proxy_build_args",
        lambda db, build_args, use: {
            "HTTP_PROXY": "http://proxy:3128",
            "HTTPS_PROXY": "http://proxy:3128",
        },
    )

    asyncio.run(users_router._lifecycle(
        "sid", "rebuild", {"use_global_proxy": True}, None))

    assert captured["service_id"] == "sid"
    assert captured["build_args"] == {
        "HTTP_PROXY": "http://proxy:3128",
        "HTTPS_PROXY": "http://proxy:3128",
    }
    assert "use_global_proxy" not in captured

"""Regression test for parallel request handling (tasks-21072026 #2.1-2.2, GAP-3).

The production gateway runs uvicorn with ``--workers 4`` (see Dockerfile CMD)
and serves many concurrent requests. ``tests/test_load.py`` exercises this
against a live gateway, but that is not part of the default pytest suite.

This test runs concurrent requests against the FastAPI app IN-PROCESS using
``httpx.ASGITransport`` — no live gateway is required — so parallel request
handling is covered by the default test suite (GAP-3).
"""

import asyncio

import httpx


CONCURRENCY = 20


def _app():
    from app.main import app
    return app


def test_concurrent_health_requests_succeed():
    """20 concurrent GET /health requests should all return 200 ok."""
    app = _app()

    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as client:
            async def one(_: int) -> httpx.Response:
                return await client.get("/health")
            return await asyncio.gather(*[one(i) for i in range(CONCURRENCY)])

    responses = asyncio.run(run())
    assert len(responses) == CONCURRENCY
    for resp in responses:
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
        assert resp.json()["status"] == "ok"


def test_concurrent_requests_have_no_5xx_errors():
    """None of the concurrent /health responses should be a 5xx error."""
    app = _app()

    async def run() -> tuple[list[int], int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as client:
            async def one(_: int) -> int:
                resp = await client.get("/health")
                return resp.status_code
            statuses = await asyncio.gather(*[one(i) for i in range(CONCURRENCY)])
            server_errors = sum(1 for s in statuses if s >= 500)
            return statuses, server_errors

    statuses, server_errors = asyncio.run(run())
    assert server_errors == 0, f"{server_errors} concurrent request(s) returned 5xx: {statuses}"


def test_dockerfile_uses_multiple_workers():
    """Dockerfile CMD should keep --workers 4 for parallel request handling (GAP-3)."""
    from pathlib import Path
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    content = dockerfile.read_text()
    assert "--workers" in content, "Dockerfile missing --workers flag"
    workers_value = content.split("--workers")[1].split()[0].strip('"')
    assert int(workers_value) >= 2, (
        f"Dockerfile --workers should be >= 2 for parallel handling, got {workers_value}"
    )


def test_health_responds_while_service_scan_in_flight(monkeypatch):
    """DB1: /health must respond while a slow /api/services scan is in flight.

    list_services is a sync ``def`` handler now, so FastAPI runs it in a
    worker thread; the event loop stays free and /health answers instantly
    instead of queueing behind the (potentially huge) scan.
    """
    import time

    from app.main import app
    from app.middleware import require_admin
    from app.services.service_manager import service_manager

    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin"}

    original = service_manager.list_services

    def slow_list_services():
        time.sleep(1.5)
        return original()

    monkeypatch.setattr(service_manager, "list_services", slow_list_services)

    async def run() -> tuple[httpx.Response, float, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as client:
            services_task = asyncio.create_task(client.get("/api/services"))
            await asyncio.sleep(0.05)  # let the scan handler enter the threadpool
            start = time.monotonic()
            health = await client.get("/health")
            elapsed = time.monotonic() - start
            services = await services_task
            return health, elapsed, services

    try:
        health, elapsed, services = asyncio.run(run())
    finally:
        app.dependency_overrides.clear()

    assert health.status_code == 200, "expected 200 from /health during in-flight scan"
    assert elapsed < 1.0, (
        f"/health took {elapsed:.2f}s while a scan was in flight — "
        "the blocking scan is stalling the event loop"
    )
    assert services.status_code == 200, "scan request itself must still succeed"

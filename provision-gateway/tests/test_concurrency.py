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

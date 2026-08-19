#!/usr/bin/env python3
"""
Provision Gateway — Parallel Load Test (Iteration 2, F1)

Tests the gateway's ability to handle concurrent requests, especially deploy
operations which involve multiple async stages (check → generate → save → deploy).

Usage:
    python tests/test_load.py [--base-url http://localhost:8771] [--concurrency 20]

Environment variables:
    BASE_URL       — Gateway URL (default: http://localhost:8771)
    CONCURRENCY    — Number of concurrent requests (default: 20)
    AUTH_EMAIL     — Admin email for login (default: admin@example.com)
    AUTH_PASSWORD  — Admin password for login (default: admin123)
"""

import asyncio
import json
import os
import sys
import time
from statistics import mean, median, stdev

try:
    import httpx
except ImportError:
    print("httpx is required. Install with: pip install httpx")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8771")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "20"))
AUTH_EMAIL = os.environ.get("AUTH_EMAIL", "admin@example.com")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "admin123")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def report(label: str, latencies: list[float], errors: int):
    """Print load test results."""
    if not latencies:
        print(f"  {label}: ALL {errors} REQUESTS FAILED")
        return
    p50 = median(latencies) if len(latencies) >= 2 else latencies[0]
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 20 else max(latencies)
    p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) >= 100 else max(latencies)
    print(f"  {label}:")
    print(f"    Requests: {len(latencies)} OK, {errors} errors")
    print(f"    Latency  min/avg/p50/p95/p99: "
          f"{min(latencies)*1000:.1f}/{mean(latencies)*1000:.1f}/{p50*1000:.1f}/"
          f"{p95*1000:.1f}/{p99*1000:.1f} ms")
    if len(latencies) >= 2:
        print(f"    StdDev:  {stdev(latencies)*1000:.1f} ms")
    print(f"    Throughput: {len(latencies) / sum(latencies):.1f} req/s")


async def run_concurrent(
    label: str,
    concurrency: int,
    client: httpx.AsyncClient,
    url: str,
    method: str = "GET",
    **kwargs,
) -> tuple[list[float], int]:
    """Send concurrent requests and measure latency."""
    latencies = []
    errors = 0

    async def single():
        nonlocal errors
        start = time.monotonic()
        try:
            resp = await client.request(method, url, **kwargs)
            elapsed = time.monotonic() - start
            if resp.status_code < 500:
                latencies.append(elapsed)
            else:
                errors += 1
                print(f"    [ERROR] {url} returned {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            errors += 1
            print(f"    [EXCEPTION] {url}: {e}")

    tasks = [single() for _ in range(concurrency)]
    await asyncio.gather(*tasks)

    report(label, latencies, errors)
    return latencies, errors


# ---------------------------------------------------------------------------
# Load Test Scenarios
# ---------------------------------------------------------------------------
async def test_health(client: httpx.AsyncClient, concurrency: int):
    """Test concurrent requests to the unauthenticated health endpoint."""
    print("\n[1/5] Health endpoint (GET /health) — no auth required")
    await run_concurrent("GET /health", concurrency, client, f"{BASE_URL}/health")


async def test_services_list(client: httpx.AsyncClient, concurrency: int, token: str):
    """Test concurrent authenticated requests to list services."""
    print("\n[2/5] Services list (GET /api/services) — authenticated")
    headers = {"Authorization": f"Bearer {token}"}
    await run_concurrent(
        "GET /api/services", concurrency, client,
        f"{BASE_URL}/api/services", headers=headers,
    )


async def test_system_status(client: httpx.AsyncClient, concurrency: int, token: str):
    """Test concurrent requests to system status (read-only, cached)."""
    print("\n[3/5] System status (GET /api/system/status) — authenticated")
    headers = {"Authorization": f"Bearer {token}"}
    await run_concurrent(
        "GET /api/system/status", concurrency, client,
        f"{BASE_URL}/api/system/status", headers=headers,
    )


async def test_services_detail(client: httpx.AsyncClient, concurrency: int, token: str):
    """Test concurrent requests to fetch all services in detail."""
    print("\n[4/5] Services detail (GET /api/services/{name}) — authenticated")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = await client.get(f"{BASE_URL}/api/services", headers=headers)
        services = resp.json().get("services", [])
    except Exception as e:
        print(f"  [SKIP] Could not fetch services: {e}")
        return

    if not services:
        print("  [SKIP] No services available for detail test")
        return

    # Rotate through available services for concurrent detail requests
    async def single(idx: int):
        svc = services[idx % len(services)]
        url = f"{BASE_URL}/api/services/{svc['name']}"
        start = time.monotonic()
        try:
            resp = await client.get(url, headers=headers)
            elapsed = time.monotonic() - start
            return elapsed, resp.status_code
        except Exception:
            return None, 500

    tasks = [single(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks)
    latencies = [r[0] for r in results if r and r[1] < 500 and r[0] is not None]
    errors = sum(1 for r in results if r and r[1] >= 500)
    report("GET /api/services/{name}", latencies, errors)


async def test_deploy_parallel(
    client: httpx.AsyncClient, token: str,
    services: list[dict], users: list[str],
    concurrency: int,
):
    """
    Test concurrent deploy requests.

    This is the most important test. Deploy involves:
    1. Gateway receives POST /api/users/deploy
    2. Gateway injects proxy build args (if enabled)
    3. Gateway auto-registers user in DB
    4. Gateway proxies to provision-api POST /users
    5. Provision-api creates async task and returns task_id

    We measure how the gateway handles multiple concurrent deploy requests.
    """
    print("\n[5/5] Deploy requests (POST /api/users/deploy) — concurrent")
    headers = {"Authorization": f"Bearer {token}"}

    if not services or not users:
        print("  [SKIP] Need at least 1 service and 1 user for deploy test")
        return

    labels_used = set()  # avoid label conflicts
    latencies = []
    errors = 0

    async def deploy(idx: int):
        nonlocal errors
        svc = services[idx % len(services)]["name"]
        user = users[idx % len(users)]
        label = str(idx % 3)  # 0, 1, or 2
        # If label already used by another user/service combo, add prefix
        payload = {
            "user_name": user,
            "service_name": svc,
            "project_root": svc,
            "compose_template_path": "docker-compose.yml.j2",
            "nginx_conf_template_path": "nginx.conf.j2",
            "label": label,
            "domain": "localhost",
            "passwd": "loadtest",
        }
        start = time.monotonic()
        try:
            resp = await client.post(
                f"{BASE_URL}/api/users/deploy",
                json=payload,
                headers=headers,
            )
            elapsed = time.monotonic() - start
            if resp.status_code in (202, 200):
                latencies.append(elapsed)
            elif resp.status_code == 409:
                # Conflict (label exists) — still tests gateway handling
                latencies.append(elapsed)
                print(f"    [CONFLICT] label={label} already used for {user}/{svc} — normal")
            else:
                errors += 1
                detail = resp.text[:120]
                print(f"    [ERROR] deploy #{idx}: {resp.status_code} {detail}")
        except Exception as e:
            errors += 1
            print(f"    [EXCEPTION] deploy #{idx}: {e}")

    tasks = [deploy(i) for i in range(concurrency)]
    await asyncio.gather(*tasks)
    report("POST /api/users/deploy", latencies, errors)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("Provision Gateway — Parallel Load Test")
    print(f"  Target:     {BASE_URL}")
    print(f"  Concurrency: {CONCURRENCY} requests")
    print(f"  Auth:       {AUTH_EMAIL}")
    print("=" * 60)

    # Create a shared connection pool for efficiency
    limits = httpx.Limits(max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        # ---- Test 1: Health (unauthenticated) ----
        await test_health(client, CONCURRENCY)

        # ---- Authenticate ----
        print("\n--- Authenticating ---")
        try:
            resp = await client.post(
                f"{BASE_URL}/api/auth/login",
                json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
            )
            if resp.status_code != 200:
                print(f"  [FATAL] Login failed: {resp.status_code} {resp.text[:200]}")
                print("  Cannot proceed with authenticated tests.")
                return
            token = resp.json().get("access_token")
            print(f"  Token acquired: {token[:20]}...")
        except Exception as e:
            print(f"  [FATAL] Login exception: {e}")
            return

        # ---- Test 2: Services list ----
        await test_services_list(client, CONCURRENCY, token)

        # ---- Test 3: System status ----
        await test_system_status(client, CONCURRENCY, token)

        # ---- Test 4: Services detail ----
        await test_services_detail(client, CONCURRENCY, token)

        # ---- Test 5: Deploy ----
        headers = {"Authorization": f"Bearer {token}"}
        services = []
        users = []
        try:
            resp_services = await client.get(f"{BASE_URL}/api/services", headers=headers)
            services = resp_services.json().get("services", [])
            # First try deployable users from gateway auth
            resp_users = await client.get(f"{BASE_URL}/api/auth/users/deployable", headers=headers)
            users = [u["username"] for u in resp_users.json().get("users", [])]
            # Fallback: use usernames from /api/users (provision-api) — deploy auto-registers them
            if not users:
                resp_all = await client.get(f"{BASE_URL}/api/users", headers=headers)
                all_users = resp_all.json().get("users", [])
                users = [u["user_name"] for u in all_users if u.get("user_name")]
            print(f"\n  Available services: {len(services)}, deployable users: {len(users)}")
        except Exception as e:
            print(f"  [WARN] Could not fetch deploy prerequisites: {e}")

        await test_deploy_parallel(client, token, services, users, CONCURRENCY)

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("LOAD TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

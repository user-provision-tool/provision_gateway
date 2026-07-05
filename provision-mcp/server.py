"""Provision MCP Server — bridges external agents with provision-gateway.

Streamable HTTP with session IDs for back-and-forth communication.
Authentication via Gateway JWT (admin-only).

Flow:
  external-agent → POST /deploy (with JWT) → gateway → MCP polls status
  gateway needs files → POST /request-generation → external-agent generates
  external-agent → POST /submit-generation → gateway uses files → deploys
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from jose import jwt

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://provision-gateway:8770")
GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET_KEY", "dev-secret-change-me-in-production-32chars!")
JWT_ALGORITHM = "HS256"

app = FastAPI(title="Provision MCP Server", version="1.0.0")

# In-memory session store
sessions: dict[str, dict] = {}


def verify_admin_token(token: str) -> dict:
    """Verify JWT and ensure admin role."""
    try:
        payload = jwt.decode(token, GATEWAY_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(401, "Not an access token")
        if payload.get("role") != "admin":
            raise HTTPException(403, "Admin role required")
        return payload
    except Exception:
        raise HTTPException(401, "Invalid token")


async def call_gateway(method: str, path: str, token: str, json_data: dict = None) -> dict:
    """Make an authenticated call to the gateway."""
    async with httpx.AsyncClient(timeout=300) as client:
        headers = {"Authorization": f"Bearer {token}"}
        if method == "GET":
            resp = await client.get(f"{GATEWAY_URL}{path}", headers=headers)
        elif method == "POST":
            resp = await client.post(f"{GATEWAY_URL}{path}", headers=headers, json=json_data)
        else:
            raise ValueError(f"Unsupported method: {method}")
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, detail=resp.text)
        return resp.json()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "gateway": GATEWAY_URL}


# ---------------------------------------------------------------------------
# POST /deploy — start a deployment with streaming status
# ---------------------------------------------------------------------------

@app.post("/deploy")
async def deploy_service(request: Request):
    """Deploy a service via the gateway. Returns SSE stream with session ID."""
    auth = request.headers.get("Authorization", "").replace("Bearer ", "")
    admin = verify_admin_token(auth)
    body = await request.json()

    session_id = uuid.uuid4().hex[:16]
    sessions[session_id] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "admin": admin.get("email"),
        "status": "starting",
        "events": [],
    }

    async def event_stream():
        yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"

        # Step 1: Check deploy readiness
        yield f"event: status\ndata: {json.dumps({'step': 'checking', 'msg': 'Checking deploy readiness...'})}\n\n"
        try:
            service_name = body.get("service_name", "")
            check = await call_gateway("POST", f"/api/services/check-deploy", auth,
                                       {"service_name": service_name})
            ready = check.get("ready", False)
            yield f"event: status\ndata: {json.dumps({'step': 'checked', 'ready': ready, 'missing': check.get('missing', []), 'generated': check.get('generated', {})})}\n\n"

            # Step 2: If generated files exist, request external agent to confirm/generate
            if check.get("needs_confirmation"):
                sessions[session_id]["pending_generation"] = check.get("generated", {})
                sessions[session_id]["service_name"] = service_name
                sessions[session_id]["deploy_body"] = body
                yield f"event: request_generation\ndata: {json.dumps({'session_id': session_id, 'files_needed': check.get('missing', []), 'generated_preview': check.get('generated', {}), 'instruction': 'POST /submit-generation with session_id and files'})}\n\n"
                yield f"event: done\ndata: {json.dumps({'status': 'waiting_for_generation'})}\n\n"
                return

            # Step 3: Deploy
            yield f"event: status\ndata: {json.dumps({'step': 'deploying', 'msg': 'Submitting deployment...'})}\n\n"
            result = await call_gateway("POST", "/api/users/deploy", auth, body)
            task_id = result.get("task_id", "")
            yield f"event: deployed\ndata: {json.dumps({'task_id': task_id, 'status': 'queued'})}\n\n"

            # Step 4: Poll task
            for _ in range(60):
                await asyncio.sleep(2)
                try:
                    task = await call_gateway("GET", f"/api/tasks/{task_id}", auth)
                    st = task.get("status", "unknown")
                    yield f"event: task_update\ndata: {json.dumps({'task_id': task_id, 'status': st})}\n\n"
                    if st in ("completed", "failed"):
                        break
                except Exception:
                    pass

            sessions[session_id]["status"] = "completed"
            yield f"event: done\ndata: {json.dumps({'status': 'completed', 'task_id': task_id})}\n\n"

        except Exception as e:
            sessions[session_id]["status"] = "failed"
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# POST /submit-generation — external agent submits generated files
# ---------------------------------------------------------------------------

@app.post("/submit-generation")
async def submit_generation(request: Request):
    """External agent submits generated files. Gateway deploys with them."""
    auth = request.headers.get("Authorization", "").replace("Bearer ", "")
    admin = verify_admin_token(auth)
    body = await request.json()

    session_id = body.get("session_id")
    files = body.get("files", {})
    if not session_id or session_id not in sessions:
        raise HTTPException(404, "Session not found")

    session = sessions[session_id]

    async def event_stream():
        yield f"event: status\ndata: {json.dumps({'session_id': session_id, 'msg': 'Saving generated files...'})}\n\n"

        # Save generated files via gateway
        try:
            svc = session.get("service_name", "")
            await call_gateway("POST", "/api/services/save-generated", auth,
                              {"service_name": svc, "files": files})
            yield f"event: status\ndata: {json.dumps({'msg': 'Files saved'})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            return

        # Now deploy
        deploy_body = session.get("deploy_body", {})
        try:
            result = await call_gateway("POST", "/api/users/deploy", auth, deploy_body)
            task_id = result.get("task_id", "")
            yield f"event: deployed\ndata: {json.dumps({'task_id': task_id})}\n\n"

            for _ in range(60):
                await asyncio.sleep(2)
                task = await call_gateway("GET", f"/api/tasks/{task_id}", auth)
                st = task.get("status", "unknown")
                yield f"event: task_update\ndata: {json.dumps({'task_id': task_id, 'status': st})}\n\n"
                if st in ("completed", "failed"):
                    break

            yield f"event: done\ndata: {json.dumps({'status': 'completed', 'task_id': task_id})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# GET /session/{session_id} — query session status
# ---------------------------------------------------------------------------

@app.get("/session/{session_id}")
def get_session(session_id: str, request: Request):
    auth = request.headers.get("Authorization", "").replace("Bearer ", "")
    verify_admin_token(auth)
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session

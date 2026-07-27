"""Example MCP server — streamable HTTP MCP that interacts with the example service.

This MCP:
1. Has a tool to GET the current content from the example service API
2. Has a tool to PUT new content to the example service, changing its output

Uses streamable HTTP (SSE) transport — compatible with MCP clients.
"""

import asyncio
import json
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI(title="Example MCP")

# Configuration — the example service URL (set by provision system at deploy time)
EXAMPLE_SERVICE_URL = "http://example-service:8000"


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "get_content",
        "description": "Get the current hello-world content from the example service.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "set_content",
        "description": "Set the hello-world content to a new value on the example service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The new content string to display.",
                }
            },
            "required": ["content"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def tool_get_content() -> dict:
    """GET the current content from the example service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{EXAMPLE_SERVICE_URL}/content")
        resp.raise_for_status()
        return resp.json()


async def tool_set_content(content: str) -> dict:
    """PUT new content to the example service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.put(
            f"{EXAMPLE_SERVICE_URL}/content",
            json={"content": content},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# MCP Streamable HTTP endpoints
# ---------------------------------------------------------------------------

@app.get("/mcp")
async def mcp_info():
    """Return MCP server info."""
    return {
        "name": "example-mcp",
        "version": "1.0.0",
        "description": "MCP for interacting with the example hello-world service",
        "transport": "streamable-http",
    }


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """Main MCP endpoint — handles JSON-RPC requests via POST with SSE response."""
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        })

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "get_content":
            result = await tool_get_content()
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            })

        if tool_name == "set_content":
            new_content = arguments.get("content", "")
            result = await tool_set_content(new_content)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            })

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }, status_code=400)

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }, status_code=400)


# SSE streaming endpoint for MCP streamable HTTP transport
@app.get("/mcp/sse")
async def mcp_sse(request: Request):
    """SSE endpoint for MCP streamable HTTP transport."""
    session_id = str(uuid.uuid4())

    async def event_stream():
        # Send endpoint event with session ID
        yield f"event: endpoint\ndata: /mcp/sse/{session_id}\n\n"
        # Keep connection alive
        while True:
            if await request.is_disconnected():
                break
            yield f": heartbeat\n\n"
            await asyncio.sleep(30)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/mcp/sse/{session_id}")
async def mcp_sse_message(session_id: str, request: Request):
    """Handle MCP JSON-RPC messages for a given SSE session."""
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        })

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "get_content":
            result = await tool_get_content()
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            })

        if tool_name == "set_content":
            new_content = arguments.get("content", "")
            result = await tool_set_content(new_content)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            })

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }, status_code=400)

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }, status_code=400)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "example-mcp"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)

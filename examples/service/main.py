"""Example service — simple REST API returning configurable hello-world content.

Has a Dockerfile but NO docker-compose.yml or nginx.conf — designed to test
the LLM-based file generation flow in the provision gateway.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Example Service")

# Controllable content — can be changed via the MCP or directly via API
_content: str = "Hello World!"


class ContentUpdate(BaseModel):
    content: str


@app.get("/")
async def root():
    """Return the current hello-world content."""
    return {"message": f"hello world: {_content}"}


@app.get("/content")
async def get_content():
    """Get the current controllable content value."""
    return {"content": _content}


@app.put("/content")
async def set_content(body: ContentUpdate):
    """Update the controllable content."""
    global _content
    _content = body.content
    return {"content": _content, "updated": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

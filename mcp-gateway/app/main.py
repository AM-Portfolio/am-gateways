"""
am-ai-gateway — thin L2 proxy for AI chat.

Deploy / image name: am-ai-gateway
Code folder: mcp-gateway

Forwards chat to am-agents/fin-portfolio-agent (L3). Does not own intent or tools.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

FINANCE_AGENT_BASE_URL = os.getenv(
    "FINANCE_AGENT_BASE_URL", "http://localhost:8101"
).rstrip("/")
CHAT_PATH = os.getenv("FINANCE_AGENT_CHAT_PATH", "/api/v1/ai/chat")
MCP_PATH = os.getenv("FINANCE_AGENT_MCP_PATH", "/ai/mcp")
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:9000,http://127.0.0.1:9000,https://am.asrax.in,https://am-dev.asrax.in",
    ).split(",")
    if o.strip()
]

app = FastAPI(title="am-ai-gateway", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _header(request: Request, *names: str) -> str | None:
    for name in names:
        val = request.headers.get(name)
        if val and val.strip():
            return val.strip()
    return None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "am-ai-gateway"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{FINANCE_AGENT_BASE_URL}/health")
            agent_ok = r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "finance_agent": FINANCE_AGENT_BASE_URL,
            "error": str(exc),
        }
    return {
        "status": "ok" if agent_ok else "degraded",
        "finance_agent": FINANCE_AGENT_BASE_URL,
        "agent_healthy": agent_ok,
    }


@app.post("/api/v1/ai/chat")
@app.post("/api/v1/chat")
@app.post("/v1/ai/chat")
async def chat_proxy(request: Request) -> Response:
    """Proxy chat to fin-portfolio-agent; preserve status and JSON body."""
    body = await request.body()
    request_id = _header(request, "x-request-id", "X-Request-Id") or str(uuid.uuid4())
    session_id = _header(request, "x-session-id", "X-Session-Id") or str(uuid.uuid4())

    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "X-Request-Id": request_id,
        "X-Session-Id": session_id,
    }
    auth = _header(request, "authorization", "Authorization")
    if auth:
        headers["Authorization"] = auth

    url = f"{FINANCE_AGENT_BASE_URL}{CHAT_PATH}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        upstream = await client.post(url, content=body, headers=headers)

    response_headers = {
        "X-Request-Id": request_id,
        "X-Session-Id": session_id,
    }
    upstream_trace = upstream.headers.get("x-trace-id") or upstream.headers.get("X-Trace-Id")
    if upstream_trace:
        response_headers["X-Trace-Id"] = upstream_trace

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=response_headers,
    )


@app.api_route("/mcp", methods=["GET", "POST"])
@app.api_route("/mcp/{subpath:path}", methods=["GET", "POST"])
async def mcp_proxy(request: Request, subpath: str = "") -> Response:
    """Stream authenticated MCP SSE traffic to fin-portfolio-agent."""
    suffix = f"/{subpath}" if subpath else ""
    query = f"?{request.url.query}" if request.url.query else ""
    url = f"{FINANCE_AGENT_BASE_URL}{MCP_PATH}{suffix}{query}"
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower()
        in {"authorization", "accept", "content-type", "last-event-id"}
    }

    client = httpx.AsyncClient(timeout=None)
    upstream_request = client.build_request(
        request.method,
        url,
        headers=headers,
        content=await request.body(),
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        return Response(
            content=f"MCP upstream unavailable: {exc}",
            status_code=502,
            media_type="text/plain",
        )

    if request.method == "GET" and upstream.status_code < 400:
        content_type = upstream.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers={"Content-Type": content_type},
            background=BackgroundTask(
                _close_upstream_response, upstream, client
            ),
        )

    content = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    response_headers = {}
    if content_type := upstream.headers.get("content-type"):
        response_headers["Content-Type"] = content_type
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


async def _close_upstream_response(
    response: httpx.Response, client: httpx.AsyncClient
) -> None:
    await response.aclose()
    await client.aclose()


@app.get("/api/v1/agents")
async def list_agents() -> dict[str, Any]:
    """Coarse registry — finance only for now."""
    return {
        "agents": [
            {
                "id": "finance",
                "name": "fin-portfolio-agent",
                "baseUrl": FINANCE_AGENT_BASE_URL,
                "chatPath": CHAT_PATH,
            }
        ]
    }

"""
am-ai-gateway — Unified L2 AI Edge Gateway.

Deploy / image name: am-ai-gateway
Code folder: mcp-gateway

Exposes:
  - POST /v1/ai/chat, /api/v1/ai/chat (one-shot chat proxy)
  - GET & POST /v1/ai/chat/stream, /api/v1/ai/chat/stream (SSE streaming proxy)
  - POST /v1/ai/feedback (feedback collector)
  - POST /v1/ai/actions/confirm (HITL action confirmation stub)
  - GET /v1/ai/health, /health, /ready (aggregated health: gateway + agent + MCP)
  - MCP SSE proxy routes (/mcp)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

logger = logging.getLogger("am.ai.gateway")

FINANCE_AGENT_BASE_URL = os.getenv(
    "FINANCE_AGENT_BASE_URL", "http://localhost:8101"
).rstrip("/")
CHAT_PATH = os.getenv("FINANCE_AGENT_CHAT_PATH", "/api/v1/ai/chat")
STREAM_PATH = os.getenv("FINANCE_AGENT_STREAM_PATH", "/api/v1/ai/chat/stream")
MCP_PATH = os.getenv("FINANCE_AGENT_MCP_PATH", "/ai/mcp")
MCP_SERVER_URL = os.getenv("MCP_BASE_URL", os.getenv("AM_MCP_SERVER_URL", "https://am-dev.asrax.in/mcp")).rstrip("/")

# Feature Flags
AI_CHAT_ENABLED = os.getenv("AI_CHAT_ENABLED", "true").lower() in {"1", "true", "yes"}
AI_STREAMING_ENABLED = os.getenv("AI_STREAMING_ENABLED", "true").lower() in {"1", "true", "yes"}
AI_WRITE_TOOLS_ENABLED = os.getenv("AI_WRITE_TOOLS_ENABLED", "false").lower() in {"1", "true", "yes"}
AI_MCP_REQUIRED = os.getenv("AI_MCP_REQUIRED", "false").lower() in {"1", "true", "yes"}

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:9000,http://127.0.0.1:9000,https://am.asrax.in,https://am-dev.asrax.in,*",
    ).split(",")
    if o.strip()
]

app = FastAPI(
    title="AM AI Gateway",
    description="Unified edge API gateway for conversational AI, portfolio agents, and MCP tools",
    version="1.0.0",
)

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


# ─── Inbound Edge GuardRail ───────────────────────────────────────────────────

_EDGE_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore your instructions",
    "you are now",
    "forget your instructions",
    "jailbreak",
]


def _check_edge_guardrail(raw_body: bytes, trace_id: str) -> tuple[bool, str | None]:
    try:
        data = json.loads(raw_body.decode("utf-8"))
        msg = str(data.get("message") or "").lower()
        for p in _EDGE_INJECTION_PATTERNS:
            if p in msg:
                return True, "Potential prompt injection blocked at AI Gateway edge."
    except Exception:
        pass
    return False, None


# ─── Health & Readiness ───────────────────────────────────────────────────────

@app.get("/health")
@app.get("/v1/ai/health")
async def health() -> dict[str, Any]:
    """Aggregated health check of Gateway, Agent, and MCP."""
    agent_status = "unknown"
    mcp_status = "unknown"

    async with httpx.AsyncClient(timeout=3.0) as client:
        # Check Finance Agent
        try:
            r = await client.get(f"{FINANCE_AGENT_BASE_URL}/health")
            agent_status = "ok" if r.status_code == 200 else f"degraded ({r.status_code})"
        except Exception as exc:
            agent_status = f"down ({type(exc).__name__})"

        # Check MCP
        try:
            r = await client.get(f"{MCP_SERVER_URL}/health")
            mcp_status = "ok" if r.status_code in {200, 204} else f"degraded ({r.status_code})"
        except Exception as exc:
            mcp_status = f"down ({type(exc).__name__})"

    overall_ok = agent_status == "ok" and (mcp_status == "ok" or not AI_MCP_REQUIRED)
    return {
        "status": "ok" if overall_ok else "degraded",
        "service": "am-ai-gateway",
        "finance_agent": {"url": FINANCE_AGENT_BASE_URL, "status": agent_status},
        "mcp_server": {"url": MCP_SERVER_URL, "status": mcp_status, "required": AI_MCP_REQUIRED},
        "flags": {
            "ai_chat_enabled": AI_CHAT_ENABLED,
            "ai_streaming_enabled": AI_STREAMING_ENABLED,
            "ai_write_tools_enabled": AI_WRITE_TOOLS_ENABLED,
        },
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    return await health()


# ─── Chat One-Shot Proxy ──────────────────────────────────────────────────────

@app.post("/v1/ai/chat")
@app.post("/api/v1/ai/chat")
@app.post("/api/v1/chat")
async def chat_proxy(request: Request) -> Response:
    """Proxy one-shot chat to fin-portfolio-agent."""
    if not AI_CHAT_ENABLED:
        raise HTTPException(status_code=503, detail="AI chat is currently disabled by feature flag.")

    body = await request.body()
    request_id = _header(request, "x-request-id", "X-Request-Id") or str(uuid.uuid4())
    session_id = _header(request, "x-session-id", "X-Session-Id") or str(uuid.uuid4())

    blocked, reason = _check_edge_guardrail(body, request_id)
    if blocked:
        return Response(
            content=json.dumps({
                "message": f"Request blocked: {reason}",
                "widgetId": "ERROR",
                "widgetParams": {"reason": reason, "traceId": request_id},
                "sessionId": session_id,
                "toolsUsed": [],
                "traceId": request_id,
            }),
            status_code=200,
            media_type="application/json",
            headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
        )

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


# ─── Chat SSE Streaming Proxy ─────────────────────────────────────────────────

@app.post("/v1/ai/chat/stream")
@app.get("/v1/ai/chat/stream")
@app.post("/api/v1/ai/chat/stream")
@app.get("/api/v1/ai/chat/stream")
async def chat_stream_proxy(request: Request) -> Response:
    """Proxy SSE chat stream from fin-portfolio-agent to client without buffering."""
    if not AI_STREAMING_ENABLED:
        raise HTTPException(status_code=503, detail="AI streaming is disabled by feature flag.")

    request_id = _header(request, "x-request-id", "X-Request-Id") or str(uuid.uuid4())
    session_id = _header(request, "x-session-id", "X-Session-Id") or str(uuid.uuid4())
    body = await request.body() if request.method == "POST" else None

    if body:
        blocked, reason = _check_edge_guardrail(body, request_id)
        if blocked:
            err_payload = json.dumps({"type": "error", "content": f"Request blocked: {reason}", "trace_id": request_id})
            return StreamingResponse(
                iter([f"data: {err_payload}\n\n"]),
                media_type="text/event-stream",
                headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
            )

    query = f"?{request.url.query}" if request.url.query else ""
    url = f"{FINANCE_AGENT_BASE_URL}{STREAM_PATH}{query}"

    headers = {
        "Accept": "text/event-stream",
        "X-Request-Id": request_id,
        "X-Session-Id": session_id,
    }
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    auth = _header(request, "authorization", "Authorization")
    if auth:
        headers["Authorization"] = auth

    client = httpx.AsyncClient(timeout=None)
    upstream_req = client.build_request(
        request.method,
        url,
        headers=headers,
        content=body,
    )

    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        err_msg = json.dumps({"type": "error", "content": f"Agent upstream connection failed: {exc}", "trace_id": request_id})
        return StreamingResponse(
            iter([f"data: {err_msg}\n\n"]),
            media_type="text/event-stream",
            status_code=502,
        )

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Trace-Id": request_id,
            "X-Session-Id": session_id,
        },
        background=BackgroundTask(_close_upstream_response, upstream, client),
    )


# ─── Actions & Feedback ───────────────────────────────────────────────────────

@app.post("/v1/ai/feedback")
@app.post("/api/v1/ai/feedback")
async def feedback_proxy(request: Request) -> Response:
    body = await request.body()
    url = f"{FINANCE_AGENT_BASE_URL}/api/v1/ai/feedback"
    async with httpx.AsyncClient(timeout=10.0) as client:
        upstream = await client.post(url, content=body, headers={"Content-Type": "application/json"})
    return Response(content=upstream.content, status_code=upstream.status_code, media_type="application/json")


@app.post("/v1/ai/actions/confirm")
@app.post("/api/v1/ai/actions/confirm")
async def confirm_action(payload: dict) -> dict[str, Any]:
    """Phase 4 HITL action confirmation stub."""
    confirm_token = payload.get("confirmToken")
    if not confirm_token:
        raise HTTPException(status_code=400, detail="Missing confirmToken in payload")
    return {
        "status": "confirmed",
        "confirmToken": confirm_token,
        "message": "Action confirmed (HITL execution enabled in Phase 4).",
    }


# ─── MCP SSE Proxy ────────────────────────────────────────────────────────────

@app.api_route("/mcp", methods=["GET", "POST"])
@app.api_route("/mcp/{subpath:path}", methods=["GET", "POST"])
async def mcp_proxy(request: Request, subpath: str = "") -> Response:
    """Stream authenticated MCP SSE traffic."""
    suffix = f"/{subpath}" if subpath else ""
    query = f"?{request.url.query}" if request.url.query else ""
    url = f"{FINANCE_AGENT_BASE_URL}{MCP_PATH}{suffix}{query}"
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in {"authorization", "accept", "content-type", "last-event-id"}
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
            background=BackgroundTask(_close_upstream_response, upstream, client),
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
    return {
        "agents": [
            {
                "id": "finance",
                "name": "fin-portfolio-agent",
                "baseUrl": FINANCE_AGENT_BASE_URL,
                "chatPath": CHAT_PATH,
                "streamPath": STREAM_PATH,
            }
        ]
    }


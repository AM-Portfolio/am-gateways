"""
am-ai-gateway — Unified L2 AI Edge Gateway.

Deploy / image name: am-ai-gateway
Code folder: mcp-gateway

Exposes:
  - POST /v1/ai/chat, /api/v1/ai/chat (one-shot chat proxy)
  - GET & POST /v1/ai/chat/stream, /api/v1/ai/chat/stream (SSE streaming proxy)
  - GET/POST/PATCH/DELETE /v1/ai/sessions* (user-platform AI session proxy)
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

from app.subscription_client import (
    QuotaExceeded,
    SubscriptionUnavailable,
    check_ai_chat_quota,
    extract_user_id,
    meter_ai_chat_tokens,
    new_idempotency_key,
    parse_tokens_used_from_chat_body,
    parse_tokens_used_from_sse_chunk,
    quota_error_payload,
    subscription_configured,
    unavailable_error_payload,
)

logger = logging.getLogger("am.ai.gateway")

FINANCE_AGENT_BASE_URL = os.getenv(
    "FINANCE_AGENT_BASE_URL", "http://localhost:8101"
).rstrip("/")
CHAT_PATH = os.getenv("FINANCE_AGENT_CHAT_PATH", "/api/v1/ai/chat")
STREAM_PATH = os.getenv("FINANCE_AGENT_STREAM_PATH", "/api/v1/ai/chat/stream")
MCP_PATH = os.getenv("FINANCE_AGENT_MCP_PATH", "/ai/mcp")
MCP_SERVER_URL = os.getenv("MCP_BASE_URL", os.getenv("AM_MCP_SERVER_URL", "https://am-dev.asrax.in/mcp")).rstrip("/")
USER_PLATFORM_URL = os.getenv("USER_PLATFORM_URL", "").rstrip("/")
USER_PLATFORM_AI_PREFIX = "/v1/user-platform/ai"

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


@app.options("/{full_path:path}")
async def options_preflight(full_path: str) -> Response:
    """Ensure browser preflight never 405s when CORSMiddleware does not short-circuit."""
    return Response(status_code=200)


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
            "user_platform_configured": bool(USER_PLATFORM_URL),
            "subscription_configured": subscription_configured(),
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
    auth = _header(request, "authorization", "Authorization")
    user_id = extract_user_id(auth, body) or "anonymous"
    turn_key = new_idempotency_key(f"ai-chat-{user_id}")

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

    try:
        await check_ai_chat_quota(user_id, idempotency_key=f"{turn_key}-check")
    except QuotaExceeded as exc:
        return Response(
            content=json.dumps(quota_error_payload(session_id=session_id, request_id=request_id, exc=exc)),
            status_code=429,
            media_type="application/json",
            headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
        )
    except SubscriptionUnavailable as exc:
        return Response(
            content=json.dumps(unavailable_error_payload(session_id=session_id, request_id=request_id, exc=exc)),
            status_code=503,
            media_type="application/json",
            headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
        )

    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "X-Request-Id": request_id,
        "X-Session-Id": session_id,
    }
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

    if upstream.status_code == 200:
        tokens = parse_tokens_used_from_chat_body(upstream.content)
        meter_key = upstream_trace or turn_key
        await meter_ai_chat_tokens(user_id, tokens, idempotency_key=f"meter-{meter_key}")

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
    auth = _header(request, "authorization", "Authorization")
    user_id = extract_user_id(auth, body) or "anonymous"
    turn_key = new_idempotency_key(f"ai-stream-{user_id}")

    if body:
        blocked, reason = _check_edge_guardrail(body, request_id)
        if blocked:
            err_payload = json.dumps({"type": "error", "content": f"Request blocked: {reason}", "trace_id": request_id})
            return StreamingResponse(
                iter([f"data: {err_payload}\n\n"]),
                media_type="text/event-stream",
                headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
            )

    try:
        await check_ai_chat_quota(user_id, idempotency_key=f"{turn_key}-check")
    except QuotaExceeded as exc:
        err = quota_error_payload(session_id=session_id, request_id=request_id, exc=exc)
        err_payload = json.dumps(
            {
                "type": "error",
                "content": err["message"],
                "trace_id": request_id,
                "session_id": session_id,
                "code": "QUOTA_EXCEEDED",
                "error": err.get("error"),
            }
        )
        return StreamingResponse(
            iter([f"data: {err_payload}\n\n"]),
            media_type="text/event-stream",
            status_code=429,
            headers={"X-Trace-Id": request_id, "X-Session-Id": session_id},
        )
    except SubscriptionUnavailable as exc:
        err = unavailable_error_payload(session_id=session_id, request_id=request_id, exc=exc)
        err_payload = json.dumps(
            {
                "type": "error",
                "content": err["message"],
                "trace_id": request_id,
                "session_id": session_id,
                "code": "SUBSCRIPTION_UNAVAILABLE",
                "error": err.get("error"),
            }
        )
        return StreamingResponse(
            iter([f"data: {err_payload}\n\n"]),
            media_type="text/event-stream",
            status_code=503,
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

    async def _tee_and_meter():
        tokens_used = 0
        content_chars = 0
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    try:
                        text = chunk.decode("utf-8", errors="ignore")
                        parsed = parse_tokens_used_from_sse_chunk(text)
                        if parsed is not None:
                            tokens_used = parsed
                        # Accumulate streamed token text for estimate fallback
                        for line in text.splitlines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(obj, dict) and obj.get("type") == "token":
                                content_chars += len(str(obj.get("content") or ""))
                    except Exception:
                        pass
                yield chunk
        finally:
            qty = tokens_used if tokens_used > 0 else max(1, content_chars // 4) if content_chars else 0
            if qty > 0:
                await meter_ai_chat_tokens(
                    user_id,
                    qty,
                    idempotency_key=f"meter-{turn_key}",
                )
            await _close_upstream_response(upstream, client)

    return StreamingResponse(
        _tee_and_meter(),
        status_code=upstream.status_code,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Trace-Id": request_id,
            "X-Session-Id": session_id,
        },
    )


async def _close_upstream_response(
    response: httpx.Response, client: httpx.AsyncClient
) -> None:
    await response.aclose()
    await client.aclose()


def _require_user_bearer(request: Request) -> str:
    auth = _header(request, "authorization", "Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token is required")
    return auth


def _normalize_feedback_body(raw: bytes) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw
    rating = str(data.get("rating") or data.get("Rating") or "").lower()
    if rating in {"thumbs_up", "up", "1", "+1"}:
        rating = "up"
    elif rating in {"thumbs_down", "down", "-1"}:
        rating = "down"
    out = {
        "session_id": data.get("session_id") or data.get("sessionId"),
        "message_id": data.get("message_id") or data.get("messageId"),
        "agent_type": data.get("agent_type") or data.get("agentType") or "fin_portfolio",
        "rating": rating or data.get("rating"),
        "comment": data.get("comment"),
        "trace_id": data.get("trace_id") or data.get("traceId"),
    }
    return json.dumps({k: v for k, v in out.items() if v is not None}).encode()


async def _proxy_user_platform(
    request: Request,
    suffix: str,
    *,
    rewrite_body: bytes | None = None,
) -> Response:
    if not USER_PLATFORM_URL:
        raise HTTPException(status_code=503, detail="User platform is not configured")
    auth = _require_user_bearer(request)
    query = f"?{request.url.query}" if request.url.query else ""
    url = f"{USER_PLATFORM_URL}{USER_PLATFORM_AI_PREFIX}{suffix}{query}"
    headers = {
        "Authorization": auth,
        "Accept": "application/json",
        "User-Agent": request.headers.get("user-agent") or "am-ai-gateway",
    }
    body = rewrite_body
    if body is None and request.method not in {"GET", "DELETE", "HEAD"}:
        body = await request.body()
    if body:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            upstream = await client.request(request.method, url, headers=headers, content=body)
        except httpx.RequestError as exc:
            logger.error("user-platform proxy failed: %s", exc)
            raise HTTPException(status_code=502, detail="User platform unavailable") from exc
    media = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=media,
    )


@app.get("/v1/ai/sessions")
@app.get("/api/v1/ai/sessions")
async def list_sessions(request: Request) -> Response:
    return await _proxy_user_platform(request, "/sessions")


@app.post("/v1/ai/sessions")
@app.post("/api/v1/ai/sessions")
async def create_session(request: Request) -> Response:
    return await _proxy_user_platform(request, "/sessions")


@app.get("/v1/ai/sessions/{session_id}")
@app.get("/api/v1/ai/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> Response:
    return await _proxy_user_platform(request, f"/sessions/{session_id}")


@app.patch("/v1/ai/sessions/{session_id}")
@app.patch("/api/v1/ai/sessions/{session_id}")
async def patch_session(session_id: str, request: Request) -> Response:
    return await _proxy_user_platform(request, f"/sessions/{session_id}")


@app.delete("/v1/ai/sessions/{session_id}")
@app.delete("/api/v1/ai/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> Response:
    return await _proxy_user_platform(request, f"/sessions/{session_id}")


# ─── Actions & Feedback ───────────────────────────────────────────────────────

@app.post("/v1/ai/feedback")
@app.post("/api/v1/ai/feedback")
async def feedback_proxy(request: Request) -> Response:
    body = _normalize_feedback_body(await request.body())
    if USER_PLATFORM_URL:
        return await _proxy_user_platform(request, "/feedback", rewrite_body=body)
    url = f"{FINANCE_AGENT_BASE_URL}/api/v1/ai/feedback"
    async with httpx.AsyncClient(timeout=10.0) as client:
        upstream = await client.post(url, content=body, headers={"Content-Type": "application/json"})
    return Response(content=upstream.content, status_code=upstream.status_code, media_type="application/json")


@app.post("/v1/ai/actions/confirm")
@app.post("/api/v1/ai/actions/confirm")
async def confirm_action(payload: dict, request: Request) -> dict[str, Any]:
    """Phase 4 HITL action confirmation endpoint. Forwards to agent when reachable."""
    confirm_token = payload.get("confirmToken")
    if not confirm_token:
        raise HTTPException(status_code=400, detail="Missing confirmToken in payload")
    headers = {}
    auth = _header(request, "authorization", "Authorization")
    if auth:
        headers["Authorization"] = auth
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.post(
                f"{FINANCE_AGENT_BASE_URL}/api/v1/ai/actions/confirm",
                json=payload,
                headers=headers,
            )
        if upstream.status_code < 500:
            try:
                return upstream.json()
            except Exception:
                pass
    except httpx.RequestError:
        pass
    return {
        "status": "confirmed",
        "confirmToken": confirm_token,
        "message": "Action confirmed.",
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


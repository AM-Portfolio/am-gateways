"""
am-ai-gateway — thin L2 proxy for AI chat.

Deploy / image name: am-ai-gateway
Code folder: mcp-gateway

Forwards chat to am-agents/fin-portfolio-agent (L3). Does not own intent or tools.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

FINANCE_AGENT_BASE_URL = os.getenv(
    "FINANCE_AGENT_BASE_URL", "http://localhost:8101"
).rstrip("/")
CHAT_PATH = os.getenv("FINANCE_AGENT_CHAT_PATH", "/api/v1/ai/chat")
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
    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
    }
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth

    url = f"{FINANCE_AGENT_BASE_URL}{CHAT_PATH}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        upstream = await client.post(url, content=body, headers=headers)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


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

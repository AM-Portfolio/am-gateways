"""
AM API Gateway — Dynamic Service Proxy v2.0
============================================
Single catch-all route eliminates the need to write per-service router files.
To add a new microservice, add one entry to SERVICES_REGISTRY — no code changes required.

Security hardening (v2.1):
  Fix 8  — Restricted CORS: explicit allow_headers instead of wildcard.
  Fix 9  — Security response headers middleware (X-Frame-Options, HSTS, etc.).
  Fix 10 — Error responses never leak internal service URLs or stack traces.
  Fix 11 — /health endpoint redacts registered service names in production.
"""

import os
import sys
import time
import logging
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

load_dotenv()

# Add bundled shared library to path
shared_path = Path(__file__).parent / "shared"
if str(shared_path) not in sys.path:
    sys.path.insert(0, str(shared_path))

try:
    from shared.logging import initialize_logging, get_logger, LogConfig
    from shared.logging.middleware import setup_fastapi_logging
    initialize_logging("am-api-gateway", LogConfig(service_name="am-api-gateway"))
    logger = get_logger("am-api-gateway.main")
except ImportError:
    logger = logging.getLogger("am-api-gateway.fallback")
    logger.warning("Shared logging not available, using fallback logger")
    def setup_fastapi_logging(*args, **kwargs): pass

from core.auth import get_current_user, generate_service_token, CurrentUser
from core.config import settings
from core.security_headers import SecurityHeadersMiddleware
from middleware.rate_limiter import RateLimiterMiddleware

app = FastAPI(
    title="AM API Gateway",
    description=(
        "Central Dynamic API Gateway for all AM Asset Management microservices. "
        "Routes /am/{service}/{path} without any hardcoded per-service router files."
    ),
    version="2.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── Middleware (order matters — outermost first) ─────────────────────────────

# Fix 9: security headers on every response
app.add_middleware(SecurityHeadersMiddleware)

setup_fastapi_logging(app, service_name="am-api-gateway")
app.add_middleware(RateLimiterMiddleware)

# Fix 8: explicit CORS allow_headers (no wildcard)
allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Trace-ID"],
)

# ─── Dynamic Service Registry ────────────────────────────────────────────────
# Add a new service here — zero Python code changes needed in routers.
# URLs are read from environment variables; Helm values.yaml injects them.

SERVICES_REGISTRY: dict[str, dict] = {
    "trade": {
        "url": os.getenv("TRADE_SERVICE_URL", "http://am-trade-management-service:8080"),
        "service_id": "trade-service",
        "permissions": ["trade:read", "trade:write"],
        "direct_prefixes": ["trade", "trades"],
    },
    "portfolio": {
        "url": os.getenv("PORTFOLIO_SERVICE_URL", "http://am-portfolio:8080"),
        "service_id": "portfolio-service",
        "permissions": ["portfolio:read", "portfolio:write"],
        "direct_prefixes": ["portfolio"],
    },
    "market-data": {
        "url": os.getenv("MARKET_DATA_SERVICE_URL", "http://am-market-data:8080"),
        "service_id": "market-data-service",
        "permissions": ["market-data:read"],
        "direct_prefixes": ["market", "market-data"],
    },
    "documents": {
        "url": os.getenv("DOCUMENT_PROCESSOR_URL", "http://am-document-processor:8080"),
        "service_id": "document-processor",
        "permissions": ["documents:read", "documents:write"],
        "direct_prefixes": ["documents", "doc"],
    },
    "subscriptions": {
        "url": os.getenv("SUBSCRIPTION_SERVICE_URL", "http://am-subscription:8080"),
        "service_id": "am-subscription",
        "permissions": ["subscription:read", "subscription:write"],
        "direct_prefixes": ["subscriptions"],
    },
    "notifications": {
        "url": os.getenv("NOTIFICATION_SERVICE_URL", "http://am-notification:8080"),
        "service_id": "am-notification",
        "permissions": ["notification:read", "notification:write"],
        "direct_prefixes": ["notifications"],
    },
    "analysis": {
        "url": os.getenv("ANALYSIS_SERVICE_URL", "http://am-analysis:8080"),
        "service_id": "am-analysis",
        "permissions": ["analysis:read"],
        "direct_prefixes": ["analysis"],
    },
}

# Build a flat prefix → service_name lookup at startup (O(1) dispatch)
_PREFIX_MAP: dict[str, str] = {}
for _svc_name, _svc_cfg in SERVICES_REGISTRY.items():
    _PREFIX_MAP[_svc_name] = _svc_name
    for _prefix in _svc_cfg.get("direct_prefixes", []):
        _PREFIX_MAP[_prefix] = _svc_name


# ─── Static Endpoints ─────────────────────────────────────────────────────────

_ENV = os.getenv("ENVIRONMENT", os.getenv("environment", "production")).lower()
_IS_PROD = _ENV == "production"


@app.get("/router/health", tags=["System"])
@app.get("/health", tags=["System"])
async def health_check():
    """Kubernetes liveness / readiness probe target."""
    body: dict = {
        "status": "healthy",
        "service": "AM API Gateway",
        "version": "2.1.0",
        "timestamp": time.time(),
    }
    # Fix 11: never expose internal service topology in production
    if not _IS_PROD:
        body["registered_services"] = list(SERVICES_REGISTRY.keys())
    return body


@app.get("/router", tags=["System"])
@app.get("/", tags=["System"])
async def root():
    return {
        "service": "AM API Gateway",
        "version": "2.1.0",
        "description": "Dynamic central gateway — all routes via /am/{service}/{path}",
        "docs": "/docs",
    }


# ─── Single Dynamic Catch-All Proxy Route ─────────────────────────────────────

@app.api_route(
    "/router/am/{service_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    tags=["Proxy"],
)
@app.api_route(
    "/am/{service_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    tags=["Proxy"],
)
async def dynamic_proxy(
    service_name: str,
    path: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Universal proxy handler.

    Pattern: /am/{service_name}/{path}
    Example: GET /am/subscriptions/subscriptions/me
                → GET http://am-subscription:8080/subscriptions/me
                   (with injected service JWT + X-User-ID header)

    To register a new microservice, add one entry to SERVICES_REGISTRY above.
    No new router file, no new import, no code changes.
    """
    service = SERVICES_REGISTRY.get(service_name)
    if not service:
        # Fix 10: never disclose registered service names in 404 responses
        raise HTTPException(
            status_code=404,
            detail={"error": f"Service '{service_name}' not found."},
        )

    try:
        # 1. Mint inter-service JWT
        service_token = await generate_service_token(
            user_token=current_user.token,
            service_id=service["service_id"],
            permissions=service["permissions"],
        )

        # 2. Read body (mutations only)
        body = b""
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()

        # 3. Proxy to downstream service
        target_url = f"{service['url']}/{path}"
        logger.info(
            "[PROXY] %s /am/%s/%s → %s",
            request.method, service_name, path, target_url,
            extra={"user_id": current_user.user_id, "service": service_name},
        )

        async with httpx.AsyncClient(timeout=settings.LONG_TIMEOUT) as client:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers={
                    "Authorization": f"Bearer {service_token}",
                    "X-User-ID": str(current_user.user_id),
                    "X-User-Email": str(current_user.email or ""),
                    "X-Gateway-Version": "2.1",
                    "Content-Type": request.headers.get("content-type", "application/json"),
                },
                content=body,
                params=request.query_params,
            )

        if response.status_code >= 400:
            logger.warning(
                "[PROXY] %s returned %d for %s",
                service_name, response.status_code, path,
                extra={"user_id": current_user.user_id},
            )

        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    except httpx.ConnectError:
        # Fix 10: do not echo internal service URL
        logger.error("[PROXY] Cannot connect to %s", service_name)
        raise HTTPException(status_code=503, detail=f"Service '{service_name}' is currently unavailable.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[PROXY] Unexpected error proxying to %s: %s", service_name, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ─── Direct Proxy (catch-all by first path segment) ──────────────────────────

# Segments that must never be routed to a downstream service
_SYSTEM_SEGMENTS = frozenset({"health", "docs", "redoc", "openapi.json", "am", "router"})


@app.api_route(
    "/router/{first_segment}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    tags=["Direct Proxy"],
)
@app.api_route(
    "/{first_segment}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    tags=["Direct Proxy"],
)
async def dynamic_direct_proxy(
    first_segment: str,
    path: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Dynamic direct proxy handler.
    Routes /{prefix}/{path} to the matching service via the O(1) prefix map.
    """
    if first_segment in _SYSTEM_SEGMENTS:
        raise HTTPException(status_code=404)

    target_service_name = _PREFIX_MAP.get(first_segment)
    if not target_service_name:
        raise HTTPException(
            status_code=404,
            detail=f"Path prefix '{first_segment}' does not map to any registered service.",
        )

    # Reconstruct downstream path — prevent double-prepending
    if path:
        if path.startswith(first_segment + "/") or path == first_segment:
            full_downstream_path = path
        else:
            full_downstream_path = f"{first_segment}/{path}"
    else:
        full_downstream_path = first_segment

    return await dynamic_proxy(
        service_name=target_service_name,
        path=full_downstream_path,
        request=request,
        current_user=current_user,
    )


# ─── Global Error Handler ─────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Fix 10: log full detail internally, return nothing sensitive externally
    logger.error("Unhandled exception: %s", exc, exc_info=True, extra={"path": str(request.url)})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=True, log_config=None)

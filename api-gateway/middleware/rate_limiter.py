"""
Rate Limiting Middleware for AM API Gateway v2.0
================================================
Security hardening:
  Fix 6 — Trusted IP extraction: only honour X-Real-IP from cluster-internal origins
           (Traefik pod CIDR), preventing IP spoofing via crafted headers.
  Fix 7 — Redis-backed sliding window for cluster-wide enforcement across pod replicas.
           Falls back gracefully to in-process limiter if Redis is unavailable.
"""
import os
import time
import ipaddress
import logging
from collections import defaultdict
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

from core.config import settings

logger = logging.getLogger(__name__)

# Paths exempt from rate limiting
_SKIP_PATHS = frozenset({"/health", "/", "/docs", "/redoc", "/openapi.json", "/router/health"})

# Cluster-internal CIDRs (K8s pod networks). Traefik pods originate from here.
# Override via CLUSTER_CIDR env var if your network uses a different range.
_CLUSTER_CIDRS: list[ipaddress.IPv4Network] = []
for cidr_str in os.getenv("CLUSTER_CIDR", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16").split(","):
    try:
        _CLUSTER_CIDRS.append(ipaddress.IPv4Network(cidr_str.strip(), strict=False))
    except ValueError:
        logger.warning("Invalid CLUSTER_CIDR value: %s — skipping", cidr_str)


def _is_cluster_internal(ip: str) -> bool:
    """Return True if the IP originates from a known cluster-internal CIDR."""
    try:
        addr = ipaddress.IPv4Address(ip)
        return any(addr in net for net in _CLUSTER_CIDRS)
    except ValueError:
        return False


def _get_real_ip(request: Request) -> str:
    """
    Extract the real client IP safely.
    X-Real-IP is only trusted when the direct TCP connection comes from
    a cluster-internal address (i.e., the Traefik ingress pod).
    This prevents external clients from spoofing their IP via headers.
    """
    direct_ip = request.client.host if request.client else "unknown"
    if _is_cluster_internal(direct_ip):
        # Traefik injects X-Real-IP with the original client IP
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.split(",")[0].strip()
    return direct_ip


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter.
    - Uses Redis for cluster-wide enforcement when REDIS_URL is set.
    - Falls back to in-process per-pod limiter when Redis is unavailable.
    """

    def __init__(self, app):
        super().__init__(app)
        self.max_requests: int = settings.RATE_LIMIT_REQUESTS
        self.window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS
        self._in_proc: dict = defaultdict(list)
        self._redis: Optional[object] = None
        self._redis_checked: bool = False

    async def _get_redis(self):
        """Lazily initialise Redis client with dedicated gateway credentials; returns None if unavailable."""
        if self._redis_checked:
            return self._redis
        self._redis_checked = True

        redis_host = os.getenv("REDIS_HOST")
        if not redis_host or not _REDIS_AVAILABLE:
            logger.info(
                "Redis rate limiter disabled "
                "(REDIS_HOST not set or redis package not installed — using in-process fallback)"
            )
            return None

        # Dedicated gateway credentials — separate from other services (e.g. Novu uses DB 2)
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_password = os.getenv("REDIS_PASSWORD")       # from Vault: infra/shared-api
        redis_username = os.getenv("REDIS_USERNAME", "")   # optional: ACL username
        redis_db = int(os.getenv("REDIS_DB", "3"))         # default DB 3, dedicated for gateway

        try:
            client = aioredis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password or None,
                username=redis_username or None,
                db=redis_db,
                decode_responses=True,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
            await client.ping()
            self._redis = client
            logger.info(
                "Redis rate limiter connected to %s:%d db=%d user=%s",
                redis_host, redis_port, redis_db,
                redis_username or "(no username)",
            )
        except Exception as exc:
            logger.warning(
                "Redis unavailable for rate limiter (%s) — using in-process fallback", exc
            )
            self._redis = None
        return self._redis

    async def _check_redis(self, ip: str, now: float) -> int:
        """
        Redis sliding window using a sorted set.
        Returns the current request count within the window.
        """
        r = await self._get_redis()
        if r is None:
            return -1  # signal: use in-process

        key = f"ratelimit:{ip}"
        window_start = now - self.window_seconds

        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, "-inf", window_start)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, self.window_seconds + 1)
            results = await pipe.execute()
            return results[2]  # zcard result = current count
        except Exception as exc:
            logger.warning("Redis rate limit check failed (%s) — falling back to in-process", exc)
            return -1

    def _check_in_process(self, ip: str, now: float) -> int:
        """In-process sliding window. Returns current request count."""
        self._in_proc[ip] = [ts for ts in self._in_proc[ip] if now - ts < self.window_seconds]
        self._in_proc[ip].append(now)
        return len(self._in_proc[ip])

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        client_ip = _get_real_ip(request)
        now = time.time()

        count = await self._check_redis(client_ip, now)
        if count == -1:
            count = self._check_in_process(client_ip, now)

        if count > self.max_requests:
            logger.warning("Rate limit exceeded for %s (count=%d)", client_ip, count)
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests. Limit: {self.max_requests} per {self.window_seconds}s",
            )

        response = await call_next(request)
        remaining = max(self.max_requests - count, 0)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(now + self.window_seconds))
        return response

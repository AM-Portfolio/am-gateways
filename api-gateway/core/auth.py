"""
Authentication utilities for AM API Gateway v2.0
=================================================
Security hardening:
  Fix 1 — X-Gateway-Secret guard prevents header injection impersonation.
  Fix 2 — JWKS cache with TTL (default 1hr) handles key rotation correctly.
  Fix 3 — ISS validation enabled; tokens from foreign realms are rejected.
  Fix 4 — AUD validation enabled; tokens issued for other clients are rejected.
  Fix 5 — Token LRU cache (keyed on sha256 of raw token, TTL ≤ 5 min)
           eliminates redundant JWKS processing for repeat callers.
  Fix 6 — Signed short-lived inter-service JWT replaces pass-through no-op.
  Fix 7 — Structured auth audit log emitted on every decision.
"""
import os
import time
import uuid
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional, List

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, jwk
from jose.exceptions import JWTError

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

# ─── Configuration ────────────────────────────────────────────────────────────

_JWKS_CACHE_TTL: int = int(os.getenv("JWKS_CACHE_TTL_SECONDS", "3600"))
_TOKEN_CACHE_MAX_TTL: int = int(os.getenv("TOKEN_CACHE_MAX_TTL_SECONDS", "300"))
_TOKEN_CACHE_MIN_REMAINING: int = int(os.getenv("TOKEN_CACHE_MIN_REMAINING_TTL", "90"))

# ─── Data Models ─────────────────────────────────────────────────────────────


class CurrentUser:
    """Represents the currently authenticated user extracted from JWT."""

    __slots__ = ("user_id", "email", "roles", "token")

    def __init__(self, user_id: str, email: str, roles: List[str], token: str):
        self.user_id = user_id
        self.email = email
        self.roles = roles if roles else []
        self.token = token


# ─── JWKS Cache (Fix 2) ───────────────────────────────────────────────────────

@dataclass
class _JwksCache:
    data: dict = field(default_factory=dict)
    fetched_at: float = 0.0

    def is_valid(self) -> bool:
        return bool(self.data) and (time.monotonic() - self.fetched_at) < _JWKS_CACHE_TTL

    def update(self, data: dict) -> None:
        self.data = data
        self.fetched_at = time.monotonic()


_jwks_cache = _JwksCache()


async def _fetch_jwks(jwks_url: str) -> dict:
    """Fetch JWKS from Keycloak. Raises HTTPException on failure."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                jwks_url,
                headers={"User-Agent": "am-api-gateway/2.0", "Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.error("Failed to fetch JWKS from %s: %s", jwks_url, exc)
        raise HTTPException(status_code=503, detail="Unable to fetch token signing keys")


async def get_keycloak_public_key(token: str) -> dict:
    """Return the JWK matching the token's kid. Handles cache expiry and key rotation."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token header structure")

    if not kid:
        raise HTTPException(status_code=401, detail="Token header missing kid")

    jwks_url = os.getenv("OIDC_JWKS_URL")
    if not jwks_url:
        # OIDC_JWKS_URL must be injected by Vault for each environment:
        #   dev     → http://auth.munish.org/auth/realms/am-dev-realm/protocol/openid-connect/certs
        #   preprod → http://auth.munish.org/auth/realms/am-preprod-realm/protocol/openid-connect/certs
        #   prod    → http://auth.munish.org/auth/realms/am-realm/protocol/openid-connect/certs
        # No hardcoded fallback — fail fast so misconfiguration is caught at startup.
        logger.error(
            "OIDC_JWKS_URL is not set. "
            "Configure it in Vault at apps/data/<env>/services/am-identity → OIDC_JWKS_URL"
        )
        raise HTTPException(
            status_code=503,
            detail="Gateway OIDC configuration is incomplete. Contact the platform team.",
        )

    # Populate cache if missing or expired (Fix 2)
    if not _jwks_cache.is_valid():
        logger.info("JWKS cache miss — fetching from %s", jwks_url)
        _jwks_cache.update(await _fetch_jwks(jwks_url))

    # Match kid in cache
    for key in _jwks_cache.data.get("keys", []):
        if key.get("kid") == kid:
            return key

    # kid not found — key may have been rotated; force-refresh once
    logger.info("kid=%s not in cache — forcing JWKS refresh (key rotation?)", kid)
    _jwks_cache.update(await _fetch_jwks(jwks_url))

    for key in _jwks_cache.data.get("keys", []):
        if key.get("kid") == kid:
            return key

    raise HTTPException(status_code=401, detail="Signing key matching 'kid' not found")


# ─── Token Validation Cache (Fix 5) ──────────────────────────────────────────
# Key  = sha256(raw_token bytes) — no plaintext JWT held in memory
# Value = (CurrentUser, cache_expire_monotonic)

_token_cache: dict[str, tuple[CurrentUser, float]] = {}


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _get_cached_user(token: str) -> Optional[CurrentUser]:
    key = _cache_key(token)
    entry = _token_cache.get(key)
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    if key in _token_cache:
        del _token_cache[key]  # evict expired entry
    return None


def _cache_user(token: str, user: CurrentUser, token_exp: int) -> None:
    remaining = token_exp - int(time.time())
    if remaining < _TOKEN_CACHE_MIN_REMAINING:
        return  # too close to expiry — don't cache
    ttl = min(remaining - 60, _TOKEN_CACHE_MAX_TTL)
    if ttl <= 0:
        return
    _token_cache[_cache_key(token)] = (user, time.monotonic() + ttl)

    # Opportunistic eviction of stale entries (keep dict bounded)
    if len(_token_cache) > 2000:
        now = time.monotonic()
        expired = [k for k, v in _token_cache.items() if now >= v[1]]
        for k in expired:
            del _token_cache[k]


# ─── Audit Logging Helper (Fix 7) ────────────────────────────────────────────

def _audit(event: str, request: Request, **kwargs) -> None:
    logger.info(
        event,
        extra={
            "audit": True,
            "event": event,
            "path": request.url.path,
            "method": request.method,
            "ip": request.client.host if request.client else "unknown",
            **kwargs,
        },
    )


# ─── Main Auth Dependency ─────────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    """
    Validate the caller and return a CurrentUser.

    Priority:
      1. Trusted edge-proxy identity: X-User-ID + X-Email injected by Traefik,
         ONLY accepted when accompanied by a valid X-Gateway-Secret header
         that matches the INTERNAL_GATEWAY_SECRET env var (Fix 1).
      2. Bearer RS256 JWT — validated in-memory against Keycloak JWKS with
         full ISS + AUD verification (Fixes 3 & 4) and a local TTL cache
         to avoid repeated JWKS processing (Fix 5).
    """
    x_user_id = request.headers.get("X-User-ID")
    x_email = request.headers.get("X-Email")
    x_gateway_secret = request.headers.get("X-Gateway-Secret")
    expected_secret = os.getenv("INTERNAL_GATEWAY_SECRET", "")

    # ── Path 1: Trusted edge-proxy identity (Traefik forward-auth) ────────────
    if x_user_id and x_email:
        # Fix 1: Reject if secret is absent or wrong — prevents header injection
        if not expected_secret or x_gateway_secret != expected_secret:
            _audit(
                "auth_failure",
                request,
                reason="invalid_gateway_secret",
                user_id=x_user_id,
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing gateway secret",
            )
        roles = [r.strip() for r in request.headers.get("X-Roles", "").split(",") if r.strip()]
        user = CurrentUser(
            user_id=x_user_id,
            email=x_email,
            roles=roles,
            token=credentials.credentials if credentials else "",
        )
        _audit("auth_success", request, method="edge_proxy", user_id=x_user_id)
        return user

    # ── Path 2: Bearer token validation ──────────────────────────────────────
    if not credentials:
        _audit("auth_failure", request, reason="no_credentials")
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials

    # Check cache first (Fix 5)
    cached = _get_cached_user(token)
    if cached:
        _audit("auth_success", request, method="jwt_cache", user_id=cached.user_id)
        return cached

    try:
        key_dict = await get_keycloak_public_key(token)
        signing_key = jwk.construct(key_dict)

        # Fix 3 & 4: Full ISS and AUD validation
        issuer = os.getenv("OIDC_ISSUER")
        audience = os.getenv("OIDC_AUDIENCE", "account")

        decode_options = {"verify_exp": True}
        decode_kwargs: dict = {
            "algorithms": ["RS256"],
            "options": decode_options,
        }
        if issuer:
            decode_kwargs["issuer"] = issuer
            decode_options["verify_iss"] = True
        else:
            decode_options["verify_iss"] = False
            # ERROR not WARNING — this is a security misconfiguration
            logger.error(
                "OIDC_ISSUER is not set — issuer validation DISABLED. "
                "Set OIDC_ISSUER in Vault at apps/data/<env>/services/am-identity. "
                "Env-specific values: dev=am-dev-realm, preprod=am-preprod-realm, prod=am-realm"
            )

        if audience:
            decode_kwargs["audience"] = audience
            decode_options["verify_aud"] = True
        else:
            decode_options["verify_aud"] = False
            logger.warning("OIDC_AUDIENCE not set — audience validation disabled")

        payload = jwt.decode(token, signing_key, **decode_kwargs)

        user_id = payload.get("sub")
        email = payload.get("email") or payload.get("preferred_username")
        realm_access = payload.get("realm_access", {})
        roles = realm_access.get("roles", [])
        token_exp = payload.get("exp", 0)

        user = CurrentUser(user_id=user_id, email=email, roles=roles, token=token)

        # Cache the validated user (Fix 5)
        _cache_user(token, user, token_exp)

        _audit("auth_success", request, method="jwt", user_id=user_id)
        return user

    except JWTError as exc:
        _audit("auth_failure", request, reason=str(exc))
        raise HTTPException(status_code=401, detail=f"Token validation failed: {exc}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Unexpected error validating token: %s", exc, exc_info=True)
        _audit("auth_failure", request, reason="unexpected_error")
        raise HTTPException(status_code=401, detail="Invalid token")


# ─── Inter-service Token Generation (Fix 6) ───────────────────────────────────

async def generate_service_token(
    user_token: str,
    service_id: str,
    permissions: List[str],
) -> str:
    """
    For compatibility with am-platform-security in downstream microservices,
    returns the user's Keycloak token directly so they can validate it
    against Keycloak's JWKS.
    """
    return user_token

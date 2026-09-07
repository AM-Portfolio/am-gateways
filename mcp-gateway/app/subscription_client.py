"""Subscription check + meter for AI chat token quotas."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger("am.ai.gateway.subscription")

SUBSCRIPTION_SERVICE_URL = os.getenv("SUBSCRIPTION_SERVICE_URL", "").rstrip("/")
KEYCLOAK_TOKEN_URL = os.getenv("KEYCLOAK_TOKEN_URL", "").strip()
SUBSCRIPTION_CLIENT_ID = os.getenv("SUBSCRIPTION_CLIENT_ID", "am-gateway-client")
SUBSCRIPTION_CLIENT_SECRET = os.getenv("SUBSCRIPTION_CLIENT_SECRET", "").strip()
SUBSCRIPTION_SERVICE_TOKEN = os.getenv("SUBSCRIPTION_SERVICE_TOKEN", "").strip()
# When URL is unset, skip gate (local / pre-config). When set, enforce.
SUBSCRIPTION_ENFORCE = os.getenv("SUBSCRIPTION_ENFORCE", "true").lower() in {
    "1",
    "true",
    "yes",
}

AI_CHAT_FEATURE = "ai_chat"
AI_CHAT_ACTION = "ai.chat"
AI_CHAT_METRIC = "ai_chat_tokens"

_token: str | None = None
_token_expires_at = 0.0


class QuotaExceeded(Exception):
    """Real plan quota / entitlement denial (HTTP 429)."""

    def __init__(self, details: dict[str, Any] | None = None, message: str = "Quota exceeded"):
        super().__init__(message)
        self.details = details or {}
        self.message = message


class SubscriptionUnavailable(Exception):
    """Infra/auth failure talking to subscription (HTTP 503 — not a user quota)."""

    def __init__(self, details: dict[str, Any] | None = None, message: str = "Subscription service unavailable"):
        super().__init__(message)
        self.details = details or {}
        self.message = message


def subscription_configured() -> bool:
    return bool(SUBSCRIPTION_SERVICE_URL)


def subscription_gate_ready() -> bool:
    """True when URL + credentials are present so we can call internal APIs."""
    if not subscription_configured():
        return False
    if SUBSCRIPTION_SERVICE_TOKEN and SUBSCRIPTION_SERVICE_TOKEN.count(".") == 2:
        return True
    return bool(KEYCLOAK_TOKEN_URL and SUBSCRIPTION_CLIENT_SECRET)


def extract_user_id(auth_header: str | None, body: bytes | None) -> str | None:
    """Prefer JWT `sub`, fall back to chat body `userId`."""
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        parts = token.split(".")
        if len(parts) >= 2:
            try:
                pad = "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                sub = payload.get("sub")
                if sub:
                    return str(sub)
            except Exception:
                pass
    if body:
        try:
            data = json.loads(body.decode("utf-8") or "{}")
            if isinstance(data, dict):
                uid = data.get("userId") or data.get("user_id")
                if uid:
                    return str(uid)
        except Exception:
            pass
    return None


async def _service_bearer() -> str:
    global _token, _token_expires_at
    if SUBSCRIPTION_SERVICE_TOKEN and SUBSCRIPTION_SERVICE_TOKEN.count(".") == 2:
        return SUBSCRIPTION_SERVICE_TOKEN
    if _token and time.time() < _token_expires_at - 30:
        return _token
    if not KEYCLOAK_TOKEN_URL or not SUBSCRIPTION_CLIENT_SECRET:
        raise RuntimeError(
            "subscription auth needs SUBSCRIPTION_SERVICE_TOKEN or "
            "KEYCLOAK_TOKEN_URL + SUBSCRIPTION_CLIENT_SECRET"
        )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            KEYCLOAK_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": SUBSCRIPTION_CLIENT_ID,
                "client_secret": SUBSCRIPTION_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()
    _token = body["access_token"]
    _token_expires_at = time.time() + int(body.get("expires_in", 300))
    return _token


async def ensure_user_subscription(user_id: str, *, bearer: str | None = None) -> bool:
    """Ensure free-tier (or default) subscription exists before check/meter.

    Returns True when the user has a subscription after this call.
    """
    if not user_id or not subscription_gate_ready():
        return False
    try:
        token = bearer or await _service_bearer()
    except Exception as exc:
        logger.warning("subscription bootstrap auth failed: %s", exc)
        return False
    url = f"{SUBSCRIPTION_SERVICE_URL}/subscriptions/internal/bootstrap/{user_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.RequestError as exc:
            logger.warning("subscription bootstrap request failed: %s", exc)
            return False
    if resp.status_code >= 400:
        logger.warning(
            "subscription bootstrap HTTP %s: %s",
            resp.status_code,
            resp.text[:300],
        )
        return False
    return True


async def check_ai_chat_quota(user_id: str, *, idempotency_key: str) -> None:
    """Raise QuotaExceeded when over limit; SubscriptionUnavailable on infra errors."""
    if not subscription_gate_ready() or not SUBSCRIPTION_ENFORCE:
        if subscription_configured() and not subscription_gate_ready():
            logger.warning(
                "SUBSCRIPTION_SERVICE_URL set but credentials missing — skipping quota check"
            )
        return
    try:
        token = await _service_bearer()
    except Exception as exc:
        logger.error("subscription check auth failed: %s", exc)
        raise SubscriptionUnavailable(
            message="Subscription service unavailable",
            details={"reason": "auth_failed"},
        ) from exc

    await ensure_user_subscription(user_id, bearer=token)

    payload = {
        "user_id": user_id,
        "feature": AI_CHAT_FEATURE,
        "action": AI_CHAT_ACTION,
        "quantity": 1,
        "idempotency_key": idempotency_key,
    }
    url = f"{SUBSCRIPTION_SERVICE_URL}/subscriptions/internal/check"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as exc:
            logger.error("subscription check request failed: %s", exc)
            raise SubscriptionUnavailable(
                message="Subscription service unavailable",
                details={"reason": "unreachable"},
            ) from exc

    # Race: subscription created between check attempts — bootstrap + one retry.
    if resp.status_code == 404:
        if await ensure_user_subscription(user_id, bearer=token):
            async with httpx.AsyncClient(timeout=10.0) as client:
                try:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.RequestError as exc:
                    logger.error("subscription check retry failed: %s", exc)
                    raise SubscriptionUnavailable(
                        message="Subscription service unavailable",
                        details={"reason": "unreachable"},
                    ) from exc

    if resp.status_code == 429:
        details: dict[str, Any] = {}
        try:
            err = resp.json()
            details = err.get("details") or err.get("detail") or err
            if isinstance(details, dict) and "details" in details:
                details = details["details"]
        except Exception:
            details = {"raw": resp.text[:200]}
        raise QuotaExceeded(
            message="AI chat token quota exceeded",
            details=details if isinstance(details, dict) else {},
        )

    if resp.status_code >= 400:
        logger.error("subscription check HTTP %s: %s", resp.status_code, resp.text[:300])
        raise SubscriptionUnavailable(
            message="Subscription check failed",
            details={"status": resp.status_code},
        )

    try:
        data = resp.json().get("data") or {}
    except Exception:
        data = {}
    if data.get("allowed") is False:
        reason = str(data.get("reason") or "AI chat not allowed")
        # Soft entitlement / state denials — treat as quota-style block for upgrade UX
        raise QuotaExceeded(
            message=reason,
            details=data if isinstance(data, dict) else {},
        )


async def meter_ai_chat_tokens(
    user_id: str,
    quantity: int,
    *,
    idempotency_key: str,
) -> None:
    """Record token usage after a successful turn. Best-effort; logs on failure."""
    if not subscription_gate_ready() or quantity <= 0:
        return
    try:
        token = await _service_bearer()
    except Exception as exc:
        logger.warning("subscription meter auth failed: %s", exc)
        return

    await ensure_user_subscription(user_id, bearer=token)

    payload = {
        "user_id": user_id,
        "metric_code": AI_CHAT_METRIC,
        "quantity": int(quantity),
        "idempotency_key": idempotency_key,
        "properties": {"tokens": int(quantity)},
    }
    url = f"{SUBSCRIPTION_SERVICE_URL}/subscriptions/internal/meter"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 404 and await ensure_user_subscription(
                user_id, bearer=token
            ):
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            if resp.status_code >= 400:
                logger.warning(
                    "subscription meter HTTP %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
            else:
                logger.info(
                    "subscription meter ok user_id=%s quantity=%s",
                    user_id[:12] + "…",
                    quantity,
                )
        except httpx.RequestError as exc:
            logger.warning("subscription meter request failed: %s", exc)


def quota_error_payload(
    *,
    session_id: str,
    request_id: str,
    exc: QuotaExceeded,
) -> dict[str, Any]:
    details = exc.details if isinstance(exc.details, dict) else {}
    return {
        "message": exc.message,
        "widgetId": "ERROR",
        "widgetParams": {
            "reason": "quota_exceeded",
            "code": "QUOTA_EXCEEDED",
            "metric": AI_CHAT_METRIC,
            "traceId": request_id,
            **{k: details[k] for k in ("limit", "used", "remaining") if k in details},
        },
        "sessionId": session_id,
        "toolsUsed": [],
        "traceId": request_id,
        "tokensUsed": 0,
        "error": {
            "code": "QUOTA_EXCEEDED",
            "metric": AI_CHAT_METRIC,
            "message": exc.message,
            "details": details,
        },
    }


def unavailable_error_payload(
    *,
    session_id: str,
    request_id: str,
    exc: SubscriptionUnavailable,
) -> dict[str, Any]:
    details = exc.details if isinstance(exc.details, dict) else {}
    return {
        "message": exc.message,
        "widgetId": "ERROR",
        "widgetParams": {
            "reason": "subscription_unavailable",
            "code": "SUBSCRIPTION_UNAVAILABLE",
            "traceId": request_id,
            **{k: details[k] for k in ("reason", "status") if k in details},
        },
        "sessionId": session_id,
        "toolsUsed": [],
        "traceId": request_id,
        "tokensUsed": 0,
        "error": {
            "code": "SUBSCRIPTION_UNAVAILABLE",
            "message": exc.message,
            "details": details,
        },
    }


def new_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def parse_tokens_used_from_chat_body(raw: bytes) -> int:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    for key in ("tokensUsed", "tokens_used"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    # Fallback when agent reported 0 / omitted usage (e.g. ContextVar loss, no usage blob)
    msg = data.get("message")
    if isinstance(msg, str) and msg.strip():
        return max(1, len(msg) // 4)
    return 0


def parse_tokens_used_from_sse_chunk(text: str) -> int | None:
    """Return tokens_used if this chunk contains a done event with the field."""
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
        if not isinstance(obj, dict) or obj.get("type") != "done":
            continue
        val = obj.get("tokens_used")
        if val is None:
            val = obj.get("tokensUsed")
        if isinstance(val, (int, float)) and int(val) > 0:
            return int(val)
    return None

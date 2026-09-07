"""Unit tests for AI gateway subscription helpers (Sprint B1)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.subscription_client import (
    extract_user_id,
    parse_tokens_used_from_chat_body,
    parse_tokens_used_from_sse_chunk,
    quota_error_payload,
    QuotaExceeded,
)


def _fake_jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}.sig"


def test_extract_user_id_from_jwt():
    auth = f"Bearer {_fake_jwt('user-123')}"
    assert extract_user_id(auth, None) == "user-123"


def test_extract_user_id_from_body():
    body = json.dumps({"userId": "body-user", "message": "hi"}).encode()
    assert extract_user_id(None, body) == "body-user"


def test_parse_tokens_from_chat_body():
    raw = json.dumps({"tokensUsed": 3842, "message": "ok"}).encode()
    assert parse_tokens_used_from_chat_body(raw) == 3842


def test_parse_tokens_falls_back_to_message_estimate_when_zero():
    msg = "x" * 40
    raw = json.dumps({"tokensUsed": 0, "message": msg}).encode()
    assert parse_tokens_used_from_chat_body(raw) == 10


def test_parse_tokens_from_sse_done():
    chunk = 'data: {"type": "done", "tokens_used": 120, "session_id": "s1"}\n\n'
    assert parse_tokens_used_from_sse_chunk(chunk) == 120


def test_quota_error_payload_structure():
    exc = QuotaExceeded(message="AI chat token quota exceeded", details={"limit": 100000, "used": 100000, "remaining": 0})
    payload = quota_error_payload(session_id="s1", request_id="r1", exc=exc)
    assert payload["widgetParams"]["code"] == "QUOTA_EXCEEDED"
    assert payload["error"]["code"] == "QUOTA_EXCEEDED"
    assert payload["widgetParams"]["remaining"] == 0


def test_ensure_user_subscription_posts_bootstrap(monkeypatch):
    import asyncio

    import app.subscription_client as sc

    monkeypatch.setattr(sc, "SUBSCRIPTION_SERVICE_URL", "http://sub.test")
    monkeypatch.setattr(sc, "subscription_gate_ready", lambda: True)

    captured = {}

    class _Resp:
        status_code = 200
        text = "{}"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["auth"] = (headers or {}).get("Authorization")
            return _Resp()

    monkeypatch.setattr(sc.httpx, "AsyncClient", _Client)
    ok = asyncio.run(sc.ensure_user_subscription("user-xyz", bearer="svc-token"))
    assert ok is True
    assert captured["url"].endswith("/subscriptions/internal/bootstrap/user-xyz")
    assert captured["auth"] == "Bearer svc-token"

import os
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.main import app


def test_sessions_require_bearer():
    client = TestClient(app)
    with patch.object(main, "USER_PLATFORM_URL", "http://user-platform.test"):
        response = client.get("/v1/ai/sessions")
    assert response.status_code == 401


def test_sessions_503_when_unconfigured():
    client = TestClient(app)
    with patch.object(main, "USER_PLATFORM_URL", ""):
        response = client.get(
            "/v1/ai/sessions",
            headers={"Authorization": "Bearer user-jwt"},
        )
    assert response.status_code == 503


def test_list_sessions_forwards_jwt_and_query():
    client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer user-jwt"
        assert "product_id=am_app" in str(request.url)
        return httpx.Response(200, json={"data": {"items": [], "total": 0}})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    with patch.object(main, "USER_PLATFORM_URL", "http://user-platform.test"):
        with patch("app.main.httpx.AsyncClient", factory):
            response = client.get(
                "/v1/ai/sessions?product_id=am_app",
                headers={"Authorization": "Bearer user-jwt"},
            )
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 0


def test_feedback_maps_camel_case_to_user_platform():
    client = TestClient(app)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"data": {"rating": "down"}})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    with patch.object(main, "USER_PLATFORM_URL", "http://user-platform.test"):
        with patch("app.main.httpx.AsyncClient", factory):
            response = client.post(
                "/v1/ai/feedback",
                headers={"Authorization": "Bearer user-jwt"},
                json={
                    "sessionId": "11111111-1111-1111-1111-111111111111",
                    "rating": "thumbs_down",
                    "comment": "nope",
                },
            )
    assert response.status_code == 201
    assert "/v1/user-platform/ai/feedback" in captured["url"]
    assert "session_id" in captured["body"]
    assert "down" in captured["body"]


def test_get_session_forwards_404():
    client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    with patch.object(main, "USER_PLATFORM_URL", "http://user-platform.test"):
        with patch("app.main.httpx.AsyncClient", factory):
            response = client.get(
                "/v1/ai/sessions/11111111-1111-1111-1111-111111111111",
                headers={"Authorization": "Bearer other-user"},
            )
    assert response.status_code == 404

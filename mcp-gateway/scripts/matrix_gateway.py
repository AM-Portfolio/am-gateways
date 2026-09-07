"""Live gateway session/feedback matrix against https://am.asrax.in/ai. Never prints secrets."""

from __future__ import annotations

import json
import os
import pathlib
import urllib.parse
import urllib.request
import uuid

# Reuse user-platform matrix helpers by importing after path tweak
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "am-platform" / "am-user-platform" / "scripts"))
from matrix_user_platform import (  # type: ignore
    KEYCLOAK,
    REALM,
    fetch_client_secret,  # noqa: F401
    jwt_sub,
    load_creds,
    req,
    rotate_test_user_password,
    token_form_admin,
)

GW = os.environ.get("GATEWAY_URL", "https://am.asrax.in/ai").rstrip("/")


def main() -> None:
    creds = load_creds()
    kc_url = (creds.get("KEYCLOAK_URL") or KEYCLOAK).rstrip("/")
    admin_token = token_form_admin(kc_url, creds["KEYCLOAK_ADMIN"], creds["KEYCLOAK_ADMIN_PASSWORD"])
    email = os.environ.get("TEST_EMAIL") or "test.user@example.com"
    password = os.environ.get("TEST_PASSWORD") or rotate_test_user_password(kc_url, admin_token, email)

    passed = total = 0

    def run(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, total
        total += 1
        print(f"[{'pass' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if cond:
            passed += 1

    code, body = req("GET", f"{GW}/v1/ai/health")
    if code != 200:
        code, body = req("GET", f"{GW}/health")
    run(
        "GET gateway health",
        code == 200 and isinstance(body, dict) and body.get("service") == "am-ai-gateway",
        f"HTTP {code}",
    )
    if isinstance(body, dict):
        run(
            "health flags include user_platform_configured",
            "user_platform_configured" in (body.get("flags") or {}),
            str((body.get("flags") or {}).get("user_platform_configured")),
        )

    code, _ = req("GET", f"{GW}/v1/ai/sessions")
    run("GET sessions no auth → 401", code == 401, f"got {code}")

    code, body = req(
        "POST",
        "https://am.asrax.in/identity/auth/login",
        {"Content-Type": "application/json"},
        json.dumps({"username": email, "password": password}).encode(),
    )
    user = body.get("access_token") if isinstance(body, dict) else None
    run("identity login", bool(user), f"HTTP {code}")
    auth = {"Authorization": f"Bearer {user}", "Content-Type": "application/json"} if user else {}

    created_id = None
    if user:
        code, body = req(
            "POST",
            f"{GW}/v1/ai/sessions",
            auth,
            json.dumps(
                {
                    "product_id": "am_app",
                    "agent_type": "fin_portfolio",
                    "title": "Gateway matrix",
                }
            ).encode(),
        )
        created_id = (body.get("data") or {}).get("id") if isinstance(body, dict) else None
        run("POST /v1/ai/sessions", code == 201 and bool(created_id), f"HTTP {code}")

        code, _ = req(
            "GET",
            f"{GW}/v1/ai/sessions?product_id=am_app&agent_type=fin_portfolio",
            {"Authorization": f"Bearer {user}"},
        )
        run("GET /v1/ai/sessions", code == 200, f"HTTP {code}")

        if created_id:
            code, _ = req("GET", f"{GW}/v1/ai/sessions/{created_id}", {"Authorization": f"Bearer {user}"})
            run("GET /v1/ai/sessions/{id}", code == 200, f"HTTP {code}")
            code, body = req(
                "PATCH",
                f"{GW}/v1/ai/sessions/{created_id}",
                auth,
                json.dumps({"title": "Gateway renamed"}).encode(),
            )
            title_ok = isinstance(body, dict) and (body.get("data") or {}).get("title") == "Gateway renamed"
            run("PATCH /v1/ai/sessions/{id}", code == 200 and title_ok, f"HTTP {code}")
            code, _ = req(
                "POST",
                f"{GW}/v1/ai/feedback",
                auth,
                json.dumps(
                    {
                        "sessionId": created_id,
                        "rating": "thumbs_down",
                        "comment": "gateway-matrix",
                    }
                ).encode(),
            )
            run("POST /v1/ai/feedback via platform", code in (200, 201), f"HTTP {code}")
            other = str(uuid.uuid4())
            code, _ = req("GET", f"{GW}/v1/ai/sessions/{other}", {"Authorization": f"Bearer {user}"})
            run("GET missing session → 404", code == 404, f"got {code}")
            code, _ = req("DELETE", f"{GW}/v1/ai/sessions/{created_id}", {"Authorization": f"Bearer {user}"})
            run("DELETE /v1/ai/sessions/{id}", code == 204, f"HTTP {code}")
            code, _ = req("GET", f"{GW}/v1/ai/sessions/{created_id}", {"Authorization": f"Bearer {user}"})
            run("GET after delete → 404", code == 404, f"got {code}")

    print()
    print(f"GATEWAY MATRIX: {passed}/{total} passed")
    if passed != total:
        raise SystemExit(2)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()

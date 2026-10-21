"""Per-run API token protects HTTP routes and WebSocket operations."""
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.server  # noqa: F401
from api.auth import API_TOKEN
from api.common import app


def test_api_requires_valid_token():
    client = TestClient(app)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={"X-API-Token": API_TOKEN}).status_code == 200


def test_login_sets_http_only_same_site_cookie():
    client = TestClient(app)
    response = client.post("/api/auth/session", json={"token": API_TOKEN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/status").status_code == 200


def test_token_endpoint_is_localhost_only():
    client = TestClient(app)
    assert client.get("/api/auth/token").status_code == 403


def test_localhost_can_bootstrap_token_and_cookie():
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    )
    response = client.get("/api/auth/token")
    assert response.status_code == 200
    assert response.text == API_TOKEN
    page = client.get("/")
    assert page.status_code == 200
    assert "httponly" in page.headers["set-cookie"].lower()
    assert client.get("/api/status").status_code == 200


def test_token_endpoint_rejects_rebinding_host():
    client = TestClient(
        app,
        base_url="http://attacker.example",
        client=("127.0.0.1", 12345),
    )
    assert client.get("/api/auth/token").status_code == 403


def test_local_web_dev_origin_is_accepted_but_remote_origin_is_not():
    local_client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    )
    assert local_client.get(
        "/api/status",
        headers={"Origin": "http://localhost:5173"},
    ).status_code == 200

    attacker_client = TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    )
    assert attacker_client.get(
        "/api/status",
        headers={"Origin": "http://attacker.example"},
    ).status_code == 401


def test_lan_login_page_requires_token_then_sets_session_cookie():
    client = TestClient(
        app,
        base_url="http://192.0.2.20",
        client=("192.0.2.20", 54321),
    )
    page = client.get("/")
    assert page.status_code == 200
    assert "需要本机 API 令牌" in page.text
    assert client.get("/api/auth/token").status_code == 403
    response = client.post(
        "/api/auth/session",
        headers={"Origin": "http://192.0.2.20"},
        json={"token": API_TOKEN},
    )
    assert response.status_code == 200
    assert client.get("/api/status").status_code == 200


def test_websocket_requires_api_token():
    client = TestClient(app)
    try:
        with client.websocket_connect("/ws/k8s/exec?env=dev&namespace=default&pod=p"):
            raise AssertionError("unauthenticated websocket unexpectedly connected")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401

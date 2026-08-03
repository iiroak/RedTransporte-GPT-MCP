"""Tests for MCP registration, validation and HTTP authentication."""

import asyncio
import base64
import hashlib
import importlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import red_transporte_mcp.server as server_module
from mcp.server.mcpserver.exceptions import ToolError


def test_registers_read_only_tools():
    server = server_module.build_mcp()
    tools = asyncio.run(server.list_tools())

    assert len(tools) == 15
    assert {tool.name for tool in tools} == {
        "search_stops",
        "get_stop",
        "find_nearby_stops",
        "find_closest_station",
        "get_routes_near_point",
        "find_stops_in_bbox",
        "list_routes",
        "get_route",
        "get_route_stops",
        "get_route_shape",
        "get_arrivals",
        "suggest_direct_routes",
        "plan_journey",
        "get_system_stats",
        "get_gtfs_status",
    }
    assert all(tool.annotations.read_only_hint for tool in tools)


def test_arrivals_is_open_world():
    server = server_module.build_mcp()
    tools = asyncio.run(server.list_tools())
    arrivals = next(tool for tool in tools if tool.name == "get_arrivals")
    assert arrivals.annotations.open_world_hint is True


def test_validation_rejects_invalid_code():
    with pytest.raises(ToolError):
        server_module._validate_code(" ")
    with pytest.raises(ToolError):
        server_module._validate_code("x" * 65)


def test_validation_rejects_invalid_coordinates():
    with pytest.raises(ToolError):
        server_module._validate_coords(-91, -70)
    with pytest.raises(ToolError):
        server_module._validate_coords(-33, 181)


def test_http_without_mcp_token_fails_closed(monkeypatch):
    monkeypatch.setenv("RED_TRANSPORTE_MCP_TOKEN", "")
    monkeypatch.setenv("RED_TRANSPORTE_API_TOKEN", "test-api-token")
    module = importlib.reload(server_module)

    with TestClient(module.create_app()) as client:
        health = client.get("/health")
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={},
        )

    assert health.status_code == 200
    assert response.status_code == 401
    assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]


def test_http_rejects_invalid_mcp_token(monkeypatch):
    monkeypatch.setenv("RED_TRANSPORTE_MCP_TOKEN", "expected")
    monkeypatch.setenv("RED_TRANSPORTE_API_TOKEN", "test-api-token")
    module = importlib.reload(server_module)

    with TestClient(module.create_app()) as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer wrong",
            },
            json={},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_http_accepts_configured_public_host(monkeypatch):
    monkeypatch.setenv("RED_TRANSPORTE_MCP_TOKEN", "expected")
    monkeypatch.setenv("RED_TRANSPORTE_API_TOKEN", "test-api-token")
    monkeypatch.setenv("RED_TRANSPORTE_MCP_PUBLIC_HOST", "mcp.example.com")
    module = importlib.reload(server_module)

    with TestClient(module.create_app(), base_url="https://mcp.example.com") as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer expected",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )

    assert response.status_code == 200


def test_oauth_pkce_flow_issues_mcp_access_token(monkeypatch):
    monkeypatch.setenv("RED_TRANSPORTE_MCP_TOKEN", "bootstrap-token")
    monkeypatch.setenv("RED_TRANSPORTE_API_TOKEN", "test-api-token")
    monkeypatch.setenv("RED_TRANSPORTE_MCP_PUBLIC_HOST", "mcp.example.com")
    module = importlib.reload(server_module)

    verifier = "test-code-verifier-with-enough-entropy-123456789"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    redirect_uri = "https://chatgpt.com/connector/oauth/test"

    with TestClient(module.create_app(), base_url="https://mcp.example.com") as client:
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]

        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resource.status_code == 200
        assert resource.json()["resource"] == module.MCP_RESOURCE_URL

        registration = client.post(
            "/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [redirect_uri],
                "response_types": ["code"],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
                "scope": "transit:read",
                "application_type": "web",
            },
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        authorization = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "state-123",
                "scope": "transit:read",
                "resource": module.MCP_RESOURCE_URL,
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302

        consent_location = urlparse(authorization.headers["location"])
        consent = client.post(
            consent_location.path,
            params=parse_qs(consent_location.query),
            data={"ticket": parse_qs(consent_location.query)["ticket"][0], "mcp_token": "bootstrap-token"},
            follow_redirects=False,
        )
        assert consent.status_code == 302

        callback = urlparse(consent.headers["location"])
        callback_params = parse_qs(callback.query)
        assert callback_params["state"] == ["state-123"]

        token = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": callback_params["code"][0],
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": module.MCP_RESOURCE_URL,
            },
        )
        assert token.status_code == 200
        token_data = token.json()
        assert token_data["token_type"] == "Bearer"
        assert token_data["refresh_token"]

        mcp_response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token_data['access_token']}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert mcp_response.status_code == 200

        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": client_id,
                "resource": module.MCP_RESOURCE_URL,
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != token_data["access_token"]

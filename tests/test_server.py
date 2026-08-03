"""Tests for MCP registration, validation and HTTP authentication."""

import asyncio
import importlib

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
    assert response.status_code == 503


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

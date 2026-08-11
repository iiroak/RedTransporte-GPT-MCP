from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from red_transporte_mcp.admin import RedAdminController
from red_transporte_mcp.secret_store import SecretStore


def test_admin_contract_requires_internal_token_and_masks_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RED_TRANSPORTE_ADMIN_TOKEN", "internal-admin-token")
    store = SecretStore(tmp_path)
    store.set_secret("api_token", "upstream-secret")
    store.set_secret("mcp_token", "mcp-secret")
    controller = RedAdminController(store)
    app = Starlette(routes=controller.routes())

    with TestClient(app) as client:
        assert client.get("/internal-admin/v1/manifest").status_code == 403
        manifest = client.get(
            "/internal-admin/v1/manifest",
            headers={"X-MCP-Admin-Token": "internal-admin-token"},
        )
        assert manifest.status_code == 200
        config = client.get(
            "/internal-admin/v1/config",
            headers={"X-MCP-Admin-Token": "internal-admin-token"},
        )
        assert config.status_code == 200
        body = config.text
        assert "upstream-secret" not in body
        assert "mcp-secret" not in body
        assert '"configured":true' in body


def test_secret_store_supports_explicit_empty_override(tmp_path) -> None:
    store = SecretStore(tmp_path)
    assert store.get_secret("token") is None
    store.set_secret("token", "value")
    assert store.get_secret("token") == "value"
    store.set_secret("token", "")
    assert store.get_secret("token") == ""
    assert store.secret_status("token")["configured"] is False

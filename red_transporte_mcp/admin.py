from __future__ import annotations

import hmac
import inspect
import os
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .secret_store import SecretStore, SecretStoreError


def manifest() -> dict[str, Any]:
    return {
        "protocol": "iroak.mcp-admin/v1",
        "service": {
            "id": "red",
            "name": "Red Transporte",
            "version": "0.1.0",
            "description": "MCP de consulta read-only para RedTransporteAPI.",
            "public_url": "https://mcp.iroak.dev/red/mcp",
        },
        "config": [
            {
                "id": "upstream",
                "title": "API aguas arriba",
                "description": "Conexión del MCP con RedTransporteAPI.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "api_url": {"type": "string", "title": "URL de API", "minLength": 1},
                        "api_token": {
                            "type": "string",
                            "title": "Token de API",
                            "writeOnly": True,
                            "minLength": 1,
                        },
                    },
                },
            },
            {
                "id": "security",
                "title": "Acceso MCP y OAuth",
                "description": "Secretos que pueden invalidar clientes existentes.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "mcp_token": {
                            "type": "string",
                            "title": "Token bootstrap MCP",
                            "writeOnly": True,
                            "minLength": 32,
                        },
                        "oauth_secret": {
                            "type": "string",
                            "title": "Clave de firma OAuth",
                            "writeOnly": True,
                            "minLength": 32,
                        },
                    },
                },
            },
        ],
        "actions": [
            {
                "id": "validate",
                "title": "Validar conexión",
                "description": "Comprueba API upstream y configuración OAuth.",
            },
            {
                "id": "restart",
                "title": "Reiniciar MCP",
                "description": "Aplica cambios que requieren reinicio.",
                "confirm": True,
                "impact": {"requires_restart": True},
            },
            {
                "id": "rotate-oauth",
                "title": "Rotar firma OAuth",
                "description": "Invalida clientes y tokens OAuth existentes.",
                "confirm": True,
                "impact": {
                    "destructive": True,
                    "requires_restart": True,
                    "invalidates_oauth_clients": True,
                },
            },
        ],
    }


class RedAdminController:
    def __init__(self, store: SecretStore | None):
        self.store = store
        self.token = os.getenv("RED_TRANSPORTE_ADMIN_TOKEN", "")

    def authorized(self, request: Request) -> bool:
        supplied = request.headers.get("x-mcp-admin-token", "")
        return bool(self.token) and hmac.compare_digest(supplied, self.token)

    def _server(self):
        from . import server

        return server

    def _secret(self, name: str, env_name: str) -> str:
        if self.store:
            stored = self.store.get_secret(name)
            if stored is not None:
                return stored
        return os.getenv(env_name, "")

    def _setting(self, name: str, env_name: str, default: str = "") -> str:
        if self.store:
            stored = self.store.get_setting(name)
            if stored is not None:
                return stored
        return os.getenv(env_name, default)

    def _secret_status(self, name: str, env_name: str) -> dict[str, object]:
        if self.store:
            stored = self.store.secret_status(name)
            if stored["configured"] or stored["updated_at"]:
                return stored
        return {"configured": bool(os.getenv(env_name, "")), "updated_at": None}

    def _allowed_api_urls(self) -> set[str]:
        configured = os.getenv("RED_TRANSPORTE_API_ALLOWED_URLS", "")
        values = configured.split() if configured else [os.getenv("RED_TRANSPORTE_API_URL", "")]
        return {value.rstrip("/") for value in values if value}

    async def _guard(self, request: Request, handler):
        if not self.authorized(request):
            return JSONResponse({"error": "admin token requerido"}, status_code=403)
        try:
            value = handler(request)
            return await value if inspect.isawaitable(value) else value
        except (SecretStoreError, ValueError, httpx.HTTPError) as exc:
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    async def manifest_endpoint(self, request: Request):
        return await self._guard(request, lambda _: JSONResponse(manifest()))

    async def status(self, request: Request):
        async def work(_: Request):
            service = self._server()
            configured = bool(self._secret("api_token", "RED_TRANSPORTE_API_TOKEN"))
            api_url = self._setting("api_url", "RED_TRANSPORTE_API_URL", "http://localhost:8000")
            ready = False
            allowed_url = api_url.rstrip("/") in self._allowed_api_urls()
            if configured and allowed_url:
                async with httpx.AsyncClient(
                    base_url=api_url,
                    headers={"Authorization": f"Bearer {self._secret('api_token', 'RED_TRANSPORTE_API_TOKEN')}"},
                    timeout=5,
                ) as client:
                    response = await client.get("/stats")
                    ready = response.status_code == 200
            return JSONResponse(
                {
                    "state": "healthy" if ready else ("degraded" if configured else "unconfigured"),
                    "liveness": "ok",
                    "readiness": "ready" if ready else "not_ready",
                    "upstream": api_url,
                    "message": (
                        "API upstream validada"
                        if ready
                        else ("URL de API no permitida" if not allowed_url else "API upstream no validada")
                    ),
                }
            )

        return await self._guard(request, work)

    async def config(self, request: Request):
        async def work(_: Request):
            return JSONResponse(
                {
                    "sections": {
                        "upstream": {
                            "api_url": self._setting(
                                "api_url", "RED_TRANSPORTE_API_URL", "http://localhost:8000"
                            ),
                            "api_token": self._secret_status("api_token", "RED_TRANSPORTE_API_TOKEN"),
                        },
                        "security": {
                            "mcp_token": self._secret_status("mcp_token", "RED_TRANSPORTE_MCP_TOKEN"),
                            "oauth_secret": self._secret_status(
                                "oauth_secret", "RED_TRANSPORTE_OAUTH_SECRET"
                            ),
                        },
                    }
                }
            )

        return await self._guard(request, work)

    async def update_config(self, request: Request):
        async def work(request: Request):
            if self.store is None:
                raise ValueError("RED_TRANSPORTE_DATA_DIR no está configurado")
            section = request.path_params["section"]
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("payload inválido")
            mappings = {
                "upstream": {"api_url": "api_url", "api_token": "api_token"},
                "security": {"mcp_token": "mcp_token", "oauth_secret": "oauth_secret"},
            }
            if section not in mappings:
                raise ValueError("sección no encontrada")
            for name, value in payload.items():
                if name not in mappings[section] or not isinstance(value, str) or not value:
                    raise ValueError("campo inválido")
                if name == "api_url":
                    parsed = urlparse(value)
                    if parsed.scheme not in {"http", "https"} or value.rstrip("/") not in self._allowed_api_urls():
                        raise ValueError("URL de API no permitida")
                if name in {"mcp_token", "oauth_secret"} and len(value) < 32:
                    raise ValueError("los secretos OAuth deben tener al menos 32 caracteres")
                if name == "api_url":
                    self.store.set_setting("api_url", value)
                else:
                    self.store.set_secret(mappings[section][name], value)
            return JSONResponse({"state": "stored", "requires_restart": True})

        return await self._guard(request, work)

    async def delete_config(self, request: Request):
        async def work(request: Request):
            if self.store is None:
                raise ValueError("RED_TRANSPORTE_DATA_DIR no está configurado")
            section = request.path_params["section"]
            field = request.path_params["field"]
            if field == "api_url":
                self.store.delete_setting("api_url")
            elif field in {"api_token", "mcp_token", "oauth_secret"}:
                self.store.set_secret(field, "")
            else:
                raise ValueError("campo no encontrado")
            return JSONResponse({"state": "deleted", "requires_restart": True})

        return await self._guard(request, work)

    async def action(self, request: Request):
        async def work(request: Request):
            action = request.path_params["action"]
            if action == "validate":
                response = await self.status(request)
                return response
            if action == "rotate-oauth":
                if self.store is None:
                    raise ValueError("RED_TRANSPORTE_DATA_DIR no está configurado")
                self.store.set_secret("oauth_secret", secrets.token_urlsafe(48))
                return JSONResponse(
                    {
                        "state": "rotated",
                        "requires_restart": True,
                        "invalidates_oauth_clients": True,
                    }
                )
            if action == "restart":
                return JSONResponse(
                    {
                        "state": "accepted",
                        "requires_restart": True,
                        "message": "El broker del control plane debe aplicar el reinicio.",
                    }
                )
            raise ValueError("acción no encontrada")

        return await self._guard(request, work)

    async def logs(self, request: Request):
        return await self._guard(
            request,
            lambda _: JSONResponse({"lines": [], "message": "Los logs se consultan mediante systemd."}),
        )

    def routes(self) -> list[Route]:
        prefix = "/internal-admin/v1"
        return [
            Route(prefix + "/manifest", self.manifest_endpoint, methods=["GET"]),
            Route(prefix + "/status", self.status, methods=["GET"]),
            Route(prefix + "/config", self.config, methods=["GET"]),
            Route(prefix + "/config/{section}", self.update_config, methods=["PATCH"]),
            Route(prefix + "/config/{section}/{field}", self.delete_config, methods=["DELETE"]),
            Route(prefix + "/actions/{action}", self.action, methods=["POST"]),
            Route(prefix + "/logs", self.logs, methods=["GET"]),
        ]

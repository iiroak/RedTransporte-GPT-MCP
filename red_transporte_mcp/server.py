"""Servidor MCP privado para RedTransporteAPI.

Expone las capacidades de la API REST (paraderos, recorridos, predicciones,
geoespacial, RAPTOR) como tools MCP. Pensado para uso privado: deny-by-default,
el endpoint HTTP exige un bearer token (RED_TRANSPORTE_MCP_TOKEN) y las llamadas
al upstream van con un token de API de RedTransporteAPI (RED_TRANSPORTE_API_TOKEN).

Transportes:
- streamable-http (stateless) en /mcp, para clientes remotos (ChatGPT, etc.)
- stdio, para uso local (Claude Desktop, Codex, opencode, etc.)
"""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from red_transporte_mcp.oauth import (
    OAUTH_SCOPE,
    RedTransporteOAuthProvider,
    authorization_server_metadata,
    consent_endpoint,
)

API_URL = os.getenv("RED_TRANSPORTE_API_URL", "https://api.example.com")
API_TOKEN = os.getenv("RED_TRANSPORTE_API_TOKEN", "")
MCP_TOKEN = os.getenv("RED_TRANSPORTE_MCP_TOKEN", "")
MCP_PORT = int(os.getenv("RED_TRANSPORTE_MCP_PORT", "8001"))
TIMEOUT = float(os.getenv("RED_TRANSPORTE_MCP_TIMEOUT", "30"))
MCP_PUBLIC_HOST = os.getenv("RED_TRANSPORTE_MCP_PUBLIC_HOST", "mcp.example.com")
MCP_BASE_URL = os.getenv("RED_TRANSPORTE_MCP_BASE_URL", f"https://{MCP_PUBLIC_HOST}").rstrip("/")
MCP_RESOURCE_URL = f"{MCP_BASE_URL}/mcp"
MCP_OAUTH_SECRET = os.getenv("RED_TRANSPORTE_OAUTH_SECRET", MCP_TOKEN)

MIN_QUERY = 1
MAX_QUERY = 64
MIN_LIMIT = 1
MAX_LIMIT = 100
MAX_RADIUS_KM = 5.0


@dataclass
class AppContext:
    client: httpx.AsyncClient


def _require_api_token() -> str:
    if not API_TOKEN:
        raise RuntimeError(
            "RED_TRANSPORTE_API_TOKEN no está configurado. "
            "Créelo en la API (POST /admin/tokens) y póngalo en el entorno."
        )
    return API_TOKEN


@asynccontextmanager
async def app_lifespan(server: MCPServer):
    headers = {"Authorization": f"Bearer {_require_api_token()}"}
    client = httpx.AsyncClient(
        base_url=API_URL, headers=headers, timeout=TIMEOUT, follow_redirects=True
    )
    try:
        yield AppContext(client=client)
    finally:
        await client.aclose()


def _read_only(**extra: bool) -> ToolAnnotations:
    annotations = {"read_only_hint": True, "destructive_hint": False, **extra}
    return ToolAnnotations(**annotations)


def _transport_security() -> TransportSecuritySettings:
    public_host = MCP_PUBLIC_HOST.strip().rstrip(".")
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    if public_host:
        allowed_hosts.extend([public_host, f"{public_host}:*"])
        allowed_origins.extend([f"https://{public_host}", f"https://{public_host}:*"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _oauth_provider() -> RedTransporteOAuthProvider:
    return RedTransporteOAuthProvider(
        issuer_url=MCP_BASE_URL,
        resource_url=MCP_RESOURCE_URL,
        bootstrap_token=MCP_TOKEN,
        signing_secret=MCP_OAUTH_SECRET,
    )


def build_mcp(provider: RedTransporteOAuthProvider | None = None) -> MCPServer:
    provider = provider or _oauth_provider()
    mcp = MCPServer(
        name="RedTransporte",
        title="Red Transporte Santiago",
        version="0.1.0",
        instructions=(
            "Datos de transporte público de Santiago de Chile. "
            "Para predicciones en tiempo real usa get_arrivals. Para planificar un "
            "viaje usa plan_journey con coordenadas. Los códigos de parada son "
            "identificadores alfanuméricos cortos (p. ej. PA433)."
        ),
        lifespan=app_lifespan,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(MCP_BASE_URL),
            resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL),
            required_scopes=[OAUTH_SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=False),
        ),
    )

    # ── Paraderos ────────────────────────────────────────────

    @mcp.tool(
        name="search_stops",
        title="Buscar paraderos",
        description=(
            "Buscar paraderos de Santiago por nombre o código parcial. "
            "Útil cuando el usuario menciona un lugar o intersección y hay que "
            "resolver el código del paradero."
        ),
        annotations=_read_only(),
    )
    async def search_stops(ctx: Context, query: str, limit: int = 10) -> dict[str, Any]:
        if not MIN_QUERY <= len(query) <= MAX_QUERY:
            raise ToolError(f"query debe tener entre {MIN_QUERY} y {MAX_QUERY} caracteres")
        limit = max(MIN_LIMIT, min(limit, MAX_LIMIT))
        data = await _get(ctx, "/stops/search", params={"q": query, "limit": limit})
        return {"stops": data, "count": len(data)}

    @mcp.tool(
        name="get_stop",
        title="Información de un paradero",
        description=(
            "Obtener información detallada de un paradero por su código: "
            "nombre, coordenadas, accesibilidad y servicios que pasan por él."
        ),
        annotations=_read_only(),
    )
    async def get_stop(ctx: Context, stop_code: str) -> dict[str, Any]:
        return await _get(ctx, f"/stops/{_validate_code(stop_code)}")

    @mcp.tool(
        name="find_nearby_stops",
        title="Paraderos cercanos",
        description=(
            "Encontrar paraderos dentro de un radio (km) de una coordenada, "
            "ordenados por distancia."
        ),
        annotations=_read_only(),
    )
    async def find_nearby_stops(
        ctx: Context,
        latitude: float,
        longitude: float,
        radius_km: float = 0.5,
        limit: int = 20,
    ) -> dict[str, Any]:
        _validate_coords(latitude, longitude)
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ToolError(f"radius_km debe estar entre 0 y {MAX_RADIUS_KM}")
        limit = max(MIN_LIMIT, min(limit, MAX_LIMIT))
        data = await _get(
            ctx, "/nearby/stops",
            params={"lat": latitude, "lon": longitude, "radius": radius_km, "limit": limit},
        )
        return {"stops": data, "count": len(data)}

    @mcp.tool(
        name="find_closest_station",
        title="Estación más cercana",
        description=(
            "Encontrar la estación de metro o tren más cercana a una coordenada, "
            "con sus servicios."
        ),
        annotations=_read_only(),
    )
    async def find_closest_station(
        ctx: Context, latitude: float, longitude: float
    ) -> dict[str, Any]:
        _validate_coords(latitude, longitude)
        return await _get(ctx, "/nearby/station", params={"lat": latitude, "lon": longitude})

    @mcp.tool(
        name="get_routes_near_point",
        title="Recorridos cerca de un punto",
        description="Recorridos que pasan cerca de una coordenada, dentro de un radio.",
        annotations=_read_only(),
    )
    async def get_routes_near_point(
        ctx: Context, latitude: float, longitude: float, radius_km: float = 0.3
    ) -> dict[str, Any]:
        _validate_coords(latitude, longitude)
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ToolError(f"radius_km debe estar entre 0 y {MAX_RADIUS_KM}")
        data = await _get(
            ctx, "/nearby/routes",
            params={"lat": latitude, "lon": longitude, "radius": radius_km},
        )
        return {"routes": data, "count": len(data)}

    @mcp.tool(
        name="find_stops_in_bbox",
        title="Paraderos en bounding box",
        description="Paraderos dentro de un bounding box (min/max lat/lon).",
        annotations=_read_only(),
    )
    async def find_stops_in_bbox(
        ctx: Context,
        min_latitude: float,
        min_longitude: float,
        max_latitude: float,
        max_longitude: float,
    ) -> dict[str, Any]:
        _validate_coords(min_latitude, min_longitude)
        _validate_coords(max_latitude, max_longitude)
        if min_latitude >= max_latitude or min_longitude >= max_longitude:
            raise ToolError("min_latitude < max_latitude y min_longitude < max_longitude")
        data = await _get(
            ctx, "/bbox/stops",
            params={
                "min_lat": min_latitude, "min_lon": min_longitude,
                "max_lat": max_latitude, "max_lon": max_longitude,
            },
        )
        return {"stops": data, "count": len(data)}

    # ── Recorridos ───────────────────────────────────────────

    @mcp.tool(
        name="list_routes",
        title="Listar recorridos",
        description=(
            "Listar recorridos, opcionalmente filtrados por modo (bus, metro, rail, tram)."
        ),
        annotations=_read_only(),
    )
    async def list_routes(ctx: Context, mode: str | None = None) -> dict[str, Any]:
        params = {"mode": mode} if mode else None
        data = await _get(ctx, "/routes", params=params)
        return {"routes": data, "count": len(data)}

    @mcp.tool(
        name="get_route",
        title="Detalle de un recorrido",
        description=(
            "Detalle de un recorrido: nombre, color, modo, paradas de ida/vuelta "
            "y frecuencias por día."
        ),
        annotations=_read_only(),
    )
    async def get_route(ctx: Context, route_id: str) -> dict[str, Any]:
        return await _get(ctx, f"/routes/{_validate_code(route_id)}")

    @mcp.tool(
        name="get_route_stops",
        title="Paradas de un recorrido",
        description="Secuencia de paradas de un recorrido en una dirección (0=ida, 1=vuelta).",
        annotations=_read_only(),
    )
    async def get_route_stops(
        ctx: Context, route_id: str, direction: int = 0
    ) -> dict[str, Any]:
        if direction not in (0, 1):
            raise ToolError("direction debe ser 0 (ida) o 1 (vuelta)")
        return await _get(
            ctx, f"/routes/{_validate_code(route_id)}/stops", params={"direction": direction}
        )

    @mcp.tool(
        name="get_route_shape",
        title="Geometría de un recorrido",
        description="Geometría (shape) de un recorrido como coordenadas [lon, lat].",
        annotations=_read_only(),
    )
    async def get_route_shape(
        ctx: Context, route_id: str, direction: int = 0
    ) -> dict[str, Any]:
        if direction not in (0, 1):
            raise ToolError("direction debe ser 0 (ida) o 1 (vuelta)")
        return await _get(
            ctx, f"/routes/{_validate_code(route_id)}/shape", params={"direction": direction}
        )

    # ── Predicciones ─────────────────────────────────────────

    @mcp.tool(
        name="get_arrivals",
        title="Predicciones de llegada",
        description=(
            "Predicciones de llegada en tiempo real para un paradero (iBus + RED web). "
            "Usar después de resolver el código con search_stops."
        ),
        annotations=_read_only(open_world_hint=True),
    )
    async def get_arrivals(
        ctx: Context, stop_code: str, service: str | None = None
    ) -> dict[str, Any]:
        code = _validate_code(stop_code)
        if service:
            return await _get(ctx, f"/predictions/{code}/{_validate_code(service)}")
        return await _get(ctx, f"/predictions/{code}")

    # ── Planificación ────────────────────────────────────────

    @mcp.tool(
        name="suggest_direct_routes",
        title="Recorridos directos entre dos puntos",
        description=(
            "Sugerir recorridos directos que conecten dos puntos. No planifica "
            "transbordos; para eso usar plan_journey."
        ),
        annotations=_read_only(),
    )
    async def suggest_direct_routes(
        ctx: Context,
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
        radius_km: float = 0.5,
    ) -> dict[str, Any]:
        _validate_coords(from_latitude, from_longitude)
        _validate_coords(to_latitude, to_longitude)
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ToolError(f"radius_km debe estar entre 0 y {MAX_RADIUS_KM}")
        data = await _get(
            ctx, "/routing/suggest",
            params={
                "from_lat": from_latitude, "from_lon": from_longitude,
                "to_lat": to_latitude, "to_lon": to_longitude,
                "radius": radius_km,
            },
        )
        return data

    @mcp.tool(
        name="plan_journey",
        title="Planificar viaje",
        description=(
            "Planificar un viaje en transporte público entre dos coordenadas "
            "(RAPTOR): alternativas con transbordos, tiempos y tarifa integrada. "
            "day: L=laboral, S=sábado, D=domingo. fare_type: normal, estudiante, adulto_mayor."
        ),
        annotations=_read_only(),
    )
    async def plan_journey(
        ctx: Context,
        from_latitude: float,
        from_longitude: float,
        to_latitude: float,
        to_longitude: float,
        departure_time: str = "08:00:00",
        day: str = "L",
        max_transfers: int = 2,
        fare_type: str = "normal",
    ) -> dict[str, Any]:
        _validate_coords(from_latitude, from_longitude)
        _validate_coords(to_latitude, to_longitude)
        if day not in ("L", "S", "D"):
            raise ToolError("day debe ser L (laboral), S (sábado) o D (domingo)")
        if fare_type not in ("normal", "estudiante", "adulto_mayor"):
            raise ToolError("fare_type debe ser normal, estudiante o adulto_mayor")
        if not 0 <= max_transfers <= 3:
            raise ToolError("max_transfers debe estar entre 0 y 3")
        return await _get(
            ctx, "/routing/plan",
            params={
                "from_lat": from_latitude, "from_lon": from_longitude,
                "to_lat": to_latitude, "to_lon": to_longitude,
                "departure_time": departure_time,
                "day": day,
                "max_transfers": max_transfers,
                "fare_type": fare_type,
            },
        )

    # ── Sistema ──────────────────────────────────────────────

    @mcp.tool(
        name="get_system_stats",
        title="Estadísticas del sistema",
        description="Estadísticas del dataset GTFS: paraderos, recorridos, viajes, modos.",
        annotations=_read_only(),
    )
    async def get_system_stats(ctx: Context) -> dict[str, Any]:
        return await _get(ctx, "/stats")

    @mcp.tool(
        name="get_gtfs_status",
        title="Estado del dataset GTFS",
        description="Estado del dataset GTFS: disponible, fecha de descarga, vigencia.",
        annotations=_read_only(),
    )
    async def get_gtfs_status(ctx: Context) -> dict[str, Any]:
        return await _get(ctx, "/gtfs/status")

    return mcp


async def _get(ctx: Context, path: str, params: dict | None = None) -> Any:
    client: httpx.AsyncClient = ctx.request_context.lifespan_context.client
    try:
        resp = await client.get(path, params=params)
    except httpx.HTTPError as e:
        raise ToolError(f"Error de red contra la API: {e}") from e
    if resp.status_code == 401:
        raise ToolError("El token de la API no es válido o fue revocado (401)")
    if resp.status_code == 403:
        raise ToolError("Sin permiso para este recurso en la API (403)")
    if resp.status_code == 429:
        raise ToolError("Rate limit de la API superado (429). Reintenta en un minuto.")
    if resp.status_code >= 500:
        raise ToolError(f"La API devolvió un error interno ({resp.status_code})")
    if resp.status_code == 422:
        raise ToolError(f"Parámetros inválidos para la API: {resp.text[:300]}")
    if resp.status_code == 404:
        raise ToolError("Recurso no encontrado en la API (404)")
    if resp.status_code != 200:
        raise ToolError(f"Respuesta inesperada de la API ({resp.status_code})")
    return resp.json()


def _validate_code(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 64:
        raise ToolError("El código debe tener entre 1 y 64 caracteres")
    return value


def _validate_coords(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ToolError("Coordenadas fuera de rango (lat [-90,90], lon [-180,180])")


def create_app() -> Starlette:
    """ASGI app: OAuth-protected MCP plus the ChatGPT consent endpoint."""
    provider = _oauth_provider()
    mcp = build_mcp(provider)
    inner = mcp.streamable_http_app(
        stateless_http=True,
        streamable_http_path="/mcp",
        transport_security=_transport_security(),
        host="0.0.0.0",
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # El session manager inicializa el task group que maneja las sesiones
        # Streamable HTTP; sin entrar a su contexto, cada request falla.
        async with mcp.session_manager.run():
            yield

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "red-transporte-mcp", "version": "0.1.0"})

    async def oauth_metadata(request: Request) -> JSONResponse:
        return authorization_server_metadata(provider)

    async def register_client(request: Request):
        return await provider.register_request(request)

    async def consent(request: Request):
        return await consent_endpoint(request, provider)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
            Route("/register", register_client, methods=["POST"]),
            Route("/oauth/consent", consent, methods=["GET", "POST"]),
            Mount("/", inner),
        ],
    )
    return app


app = create_app()


def main() -> None:
    """Entrypoint CLI: `--transport stdio` (default) o `--transport http`."""
    import asyncio

    parser = argparse.ArgumentParser(description="Servidor MCP de RedTransporte")
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="Transporte MCP (default: stdio para uso local)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=MCP_PORT)
    args = parser.parse_args()

    if args.transport == "http":
        uvicorn.run(create_app(), host=args.host, port=args.port)
    else:
        mcp = build_mcp()
        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()

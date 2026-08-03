# RedTransporteMCP

Servidor **MCP** (Model Context Protocol) privado para
[RedTransporteAPI](https://github.com/iiroak/RedTransporteAPI) — transporte
público de Santiago de Chile: paraderos, recorridos, predicciones en tiempo
real (iBus + RED web) y planificación RAPTOR.

El repositorio de la API se deja intacto como motor REST/CLI; este proyecto es
un adaptador MCP que la consume por HTTP. Código libre (GPL-3.0) para quien
quiera montarlo, pero el endpoint desplegado es de uso **privado** (deny-by-default).

## Capacidades

| Tool | Descripción |
|------|-------------|
| `search_stops` | Buscar paraderos por nombre o código parcial |
| `get_stop` | Detalle de un paradero (coords, accesibilidad, servicios) |
| `find_nearby_stops` | Paraderos cercanos a una coordenada |
| `find_closest_station` | Estación metro/tren más cercana |
| `get_routes_near_point` | Recorridos cerca de un punto |
| `find_stops_in_bbox` | Paraderos en un bounding box |
| `list_routes` | Listar recorridos (filtro por modo) |
| `get_route` / `get_route_stops` / `get_route_shape` | Detalle, paradas y geometría de un recorrido |
| `get_arrivals` | Predicciones en tiempo real para un paradero |
| `suggest_direct_routes` | Recorridos directos entre dos puntos |
| `plan_journey` | Planificación RAPTOR con transbordos y tarifa |
| `get_system_stats` / `get_gtfs_status` | Estado del dataset |

Todas las tools son de **solo lectura**. No hay tools de escritura ni de administración.

## Arquitectura

```text
ChatGPT / Codex / Claude / opencode
        │  (MCP streamable-http o stdio)
        ▼
┌───────────────────────┐
│  RedTransporteMCP     │  ← bearer token MCP (RED_TRANSPORTE_MCP_TOKEN)
│  deny-by-default      │
└──────────┬────────────┘
           │  HTTPS + bearer token API (RED_TRANSPORTE_API_TOKEN)
           ▼
┌───────────────────────┐
│  RedTransporteAPI     │  ← motor GTFS/RAPTOR/iBus/RED (repo aparte)
└───────────────────────┘
```

El servidor es un proxy delgado: valida parámetros, traduce errores a errores
MCP y reenvía al REST. No carga GTFS en memoria ni duplica el motor.

## Configuración

| Variable | Default | Descripción |
|----------|---------|-------------|
| `RED_TRANSPORTE_API_URL` | `https://api.example.com` | Base URL del REST |
| `RED_TRANSPORTE_API_TOKEN` | _(requerido)_ | Bearer token de la API (crear en `POST /admin/tokens`) |
| `RED_TRANSPORTE_MCP_TOKEN` | _(requerido para HTTP)_ | Bearer token exigido al cliente MCP |
| `RED_TRANSPORTE_MCP_PUBLIC_HOST` | `mcp.example.com` | Hostname HTTP permitido por la protección DNS-rebinding |
| `RED_TRANSPORTE_MCP_PORT` | `8001` | Puerto del transporte HTTP |
| `RED_TRANSPORTE_MCP_TIMEOUT` | `30` | Timeout de llamadas al REST (segundos) |

## Uso local (stdio)

```bash
uv sync
RED_TRANSPORTE_API_TOKEN=tu-token uv run red-transporte-mcp --transport stdio
```

Configura el cliente MCP con comando `uv run red-transporte-mcp` (stdio).

## Uso remoto (streamable-http)

```bash
RED_TRANSPORTE_API_TOKEN=tu-token \
RED_TRANSPORTE_MCP_TOKEN=token-privado \
uv run red-transporte-mcp --transport http --host 0.0.0.0 --port 8001
```

Endpoint: `https://<host>/mcp`.

Liveness: `GET /health` (no authentication; no application data).

- Sin `RED_TRANSPORTE_MCP_TOKEN`, el endpoint falla cerrado con `503` por
  configuración incompleta.
- Con token configurado, cualquier request sin `Authorization: Bearer <token>`
  recibe `401` (deny-by-default).

### Conexión desde ChatGPT (plugin personal)

1. Desplegar el servidor en un endpoint HTTPS estable (ver abajo).
2. En ChatGPT: *Settings → Security and login → Developer mode*.
3. *ChatGPT Plugins → +* → URL del MCP → en los detalles de conexión, agregar
   el header `Authorization: Bearer <RED_TRANSPORTE_MCP_TOKEN>`.
4. Instalar el plugin y probar con `@plugin` en un chat Work.

OAuth 2.1 (login real, sin pegar tokens) es el siguiente paso; ver Roadmap.

## Despliegue (the deployment platform / the reverse proxy)

Idea base, ajustar a tu infraestructura:

1. Aplicación Docker en the deployment platform (imagen publicada por CI de este repo).
2. Hostname `mcp.example.com` → túnel the reverse proxy → puerto publicado
   (p. ej. `published-port → 8001`).
3. Variables secretas en the deployment platform, nunca en Git: `RED_TRANSPORTE_API_TOKEN`,
   `RED_TRANSPORTE_MCP_TOKEN`.
4. Un solo worker (sin estado de sesión; `stateless_http`).
5. Rate limiting a nivel de the reverse proxy + el de la API.
6. Revisar que los logs no contengan tokens ni cuerpos de requests.

### Docker local

```bash
cp .env.example .env
# completar RED_TRANSPORTE_API_TOKEN y RED_TRANSPORTE_MCP_TOKEN en .env
docker compose up --build
```

El compose publica solo `127.0.0.1:8001`; un reverse proxy o túnel debe
terminar TLS y reenviar al puerto local.

## Seguridad

- Deny-by-default: sin token MCP válido no hay respuesta.
- El token MCP se compara en tiempo constante (`hmac.compare_digest`).
- El token de la API nunca se expone a los clientes MCP: el servidor lo usa
  solo contra el REST.
- Errores de la API se traducen a errores MCP sin filtrar detalles internos.
- Las tools son read-only y no exponen paths del servidor.

## Roadmap

- [ ] OAuth 2.1 con proveedor propio (issuer + CIMD) para login real en ChatGPT
- [ ] mTLS de OpenAI como capa adicional de identificación
- [ ] CI: tests, build de imagen, smoke test MCP
- [ ] Imagen Docker reproducible (uv.lock, semver)
- [ ] Resources MCP para shapes/geometrías grandes

## Licencia

GPL-3.0-only. Datos: GTFS DTPM, iBus y RED web — revisar términos de cada
fuente antes de un uso público amplio.

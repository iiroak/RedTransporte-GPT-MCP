# RedTransporteMCP

Servidor **MCP** (Model Context Protocol) privado para
[RedTransporteAPI](https://github.com/iiroak/RedTransporteAPI) — transporte
público de Santiago de Chile: paraderos, recorridos, predicciones en tiempo
real (iBus + RED web) y planificación RAPTOR.

El repositorio de la API se deja intacto como motor REST/CLI; este proyecto es
un adaptador MCP que la consume por HTTP. Código libre para quien quiera
montarlo; el endpoint HTTP está protegido por defecto (deny-by-default).

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

La guía completa está en [`MCP_USAGE.md`](MCP_USAGE.md): tools, transports,
OAuth, ChatGPT, OpenCode, despliegue y troubleshooting.

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
MCP y reenvía al REST. No carga GTFS en memoria ni duplica el motor. El transporte
HTTP usa OAuth 2.1 con PKCE para clientes como ChatGPT; el token MCP existente se
usa en la pantalla de consentimiento y sigue funcionando para clientes legacy.

## Configuración

| Variable | Default | Descripción |
|----------|---------|-------------|
| `RED_TRANSPORTE_API_URL` | `http://localhost:8000` | Base URL del REST |
| `RED_TRANSPORTE_API_TOKEN` | _(requerido)_ | Bearer token de la API (crear en `POST /admin/tokens`) |
| `RED_TRANSPORTE_MCP_TOKEN` | _(requerido para HTTP)_ | Bearer token exigido al cliente MCP |
| `RED_TRANSPORTE_MCP_PUBLIC_HOST` | `localhost` | Hostname HTTP permitido por la protección DNS-rebinding |
| `RED_TRANSPORTE_MCP_BASE_URL` | `http://localhost:8001` | URL canónica del issuer; puede incluir un prefijo de ruta y el recurso final es `<base>/mcp` |
| `RED_TRANSPORTE_OAUTH_SECRET` | _(deriva del MCP token)_ | Secreto HMAC opcional separado para clientes y tokens OAuth |
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

Endpoint: `https://<host>/mcp` por defecto. Si `RED_TRANSPORTE_MCP_BASE_URL` es,
por ejemplo, `https://mcp.example.com/red`, el endpoint es
`https://mcp.example.com/red/mcp` y las rutas auxiliares también quedan bajo
`/red` (`/red/health`, `/red/register`, `/red/oauth/consent` y los metadatos
OAuth).

Liveness: `GET <prefijo>/health` (sin prefijo, `GET /health`; no authentication;
no application data).

- Sin `RED_TRANSPORTE_MCP_TOKEN`, `<prefijo>/mcp` falla cerrado con `401` y la pantalla
  de consentimiento OAuth no puede autorizar usuarios.
- Con token configurado, cualquier request sin un bearer OAuth válido o el token
  legacy recibe `401` (deny-by-default).

### Conexión desde ChatGPT

1. En ChatGPT web, activa Developer mode en *Settings → Apps → Advanced Settings*.
2. Crea una app MCP desde *Apps → Create*.
3. Usa el endpoint HTTPS de tu despliegue, por ejemplo `https://mcp.example.com/mcp`, y selecciona OAuth.
4. Pulsa *Scan Tools*; el flujo redirige a la pantalla de consentimiento del MCP.
5. Introduce el valor de `RED_TRANSPORTE_MCP_TOKEN` desde tu gestor de secretos.
6. Crea/publica la app y actívala desde el menú de herramientas de un chat.

El MCP publica los metadatos en
`<prefijo>/.well-known/oauth-protected-resource/mcp` y
`<prefijo>/.well-known/oauth-authorization-server`, registra clientes
dinámicamente y requiere PKCE `S256`. Sin prefijo, `<prefijo>` es vacío. No hay
que pegar el token de la API REST en ChatGPT.

## Producción

Este repositorio no documenta un host, LXC, reverse proxy, secretos ni estado
persistente concreto. El procedimiento de producción pertenece a la documentación
privada de infraestructura del operador. Revisa allí el runbook antes de instalar
este servicio.

### Docker local

```bash
cp .env.example .env
# completar RED_TRANSPORTE_API_TOKEN y RED_TRANSPORTE_MCP_TOKEN en .env
docker compose up --build
```

El compose publica solo `127.0.0.1:8001`; un reverse proxy o túnel debe
terminar TLS y reenviar al puerto local.

## Seguridad

- Deny-by-default: `<prefijo>/mcp` requiere un bearer OAuth válido o el token MCP legacy.
- OAuth usa authorization code + PKCE `S256`, resource indicators y tokens ligados al recurso `<base>/mcp`.
- Los access tokens expiran en una hora y los refresh tokens rotan durante 30 días.
- El token MCP se compara en tiempo constante (`hmac.compare_digest`).
- El token de la API nunca se expone a los clientes MCP: el servidor lo usa
  solo contra el REST.
- Errores de la API se traducen a errores MCP sin filtrar detalles internos.
- Las tools son read-only y no exponen paths del servidor.

## Roadmap

- [ ] mTLS de OpenAI como capa adicional de identificación
- [ ] CI: tests, build de imagen, smoke test MCP
- [ ] Imagen Docker reproducible (uv.lock, semver)
- [ ] Resources MCP para shapes/geometrías grandes

## Licencia

GPL-3.0-only. Datos: GTFS DTPM, iBus y RED web — revisar términos de cada
fuente antes de un uso público amplio.

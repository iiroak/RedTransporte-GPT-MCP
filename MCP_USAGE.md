# RedTransporteMCP

Guía de instalación e integración del servidor MCP para RedTransporteAPI.

## Qué es un MCP server

MCP es un protocolo para que un cliente de IA descubra y llame herramientas.
Este repositorio es el **servidor**; ChatGPT, OpenCode, Claude y Codex son
**clientes**. Todos hablan el mismo protocolo, pero pueden usar transportes y
autenticación diferentes.

```text
ChatGPT / OpenCode / Claude / Codex
                 │ MCP
                 ▼
        RedTransporteMCP
        valida + adapta
                 │ HTTPS + API token interno
                 ▼
        RedTransporteAPI
```

El MCP no carga GTFS ni duplica RAPTOR. Convierte llamadas de tools en requests
HTTP al API y devuelve sus resultados como respuestas MCP.

Contrato REST detallado: [RedTransporteAPI/docs/API_USAGE.md](https://github.com/iiroak/RedTransporteAPI/blob/main/docs/API_USAGE.md).

## Tools disponibles

Todas las tools son de solo lectura. Ninguna puede crear tokens, actualizar GTFS,
modificar configuración o escribir datos.

| Tool | Parámetros principales | Endpoint REST que usa |
|---|---|---|
| `search_stops` | `query`, `limit` | `GET /stops/search` |
| `get_stop` | `stop_code` | `GET /stops/{code}` |
| `find_nearby_stops` | `latitude`, `longitude`, `radius_km`, `limit` | `GET /nearby/stops` |
| `find_closest_station` | `latitude`, `longitude` | `GET /nearby/station` |
| `get_routes_near_point` | `latitude`, `longitude`, `radius_km` | `GET /nearby/routes` |
| `find_stops_in_bbox` | min/max latitude/longitude | `GET /bbox/stops` |
| `list_routes` | `mode` opcional | `GET /routes` |
| `get_route` | `route_id` | `GET /routes/{id}` |
| `get_route_stops` | `route_id`, `direction` | `GET /routes/{id}/stops` |
| `get_route_shape` | `route_id`, `direction` | `GET /routes/{id}/shape` |
| `get_arrivals` | `stop_code`, `service` opcional | `GET /predictions/{code}` |
| `suggest_direct_routes` | coordenadas origen/destino, `radius_km` | `GET /routing/suggest` |
| `plan_journey` | coordenadas, hora, día, transbordos, tarifa | `GET /routing/plan` |
| `get_system_stats` | ninguno | `GET /stats` |
| `get_gtfs_status` | ninguno | `GET /gtfs/status` |

Validaciones del servidor:

- Códigos y consultas: 1-64 caracteres.
- Coordenadas: latitud `[-90, 90]`, longitud `[-180, 180]`.
- Radios: mayor que `0` y máximo `5` km.
- `direction`: `0` ida o `1` vuelta.
- `plan_journey.day`: `L`, `S` o `D`.
- `plan_journey.fare_type`: `normal`, `estudiante` o `adulto_mayor`.
- `plan_journey.max_transfers`: `0-3`.

`get_arrivals` se marca como consulta de mundo abierto porque depende de fuentes
en tiempo real. Las demás tools leen datos estáticos o el estado del sistema.

## Transportes

### `stdio`: OpenCode y clientes locales

El cliente inicia el proceso y se comunica por stdin/stdout. No se usa el OAuth
HTTP ni el `RED_TRANSPORTE_MCP_TOKEN`; el proceso solo necesita el token interno
para llamar a la API REST.

```bash
cd /ruta/a/RedTransporteMCP
RED_TRANSPORTE_API_TOKEN=token-de-la-api \
  uv run red-transporte-mcp --transport stdio
```

Configuración equivalente para OpenCode (`opencode.json`):

```json
{
  "mcp": {
    "red-transporte": {
      "type": "local",
      "enabled": true,
      "command": [
        "uv",
        "run",
        "--directory",
        "/ruta/a/RedTransporteMCP",
        "red-transporte-mcp",
        "--transport",
        "stdio"
      ],
      "environment": {
        "RED_TRANSPORTE_API_TOKEN": "{env:RED_TRANSPORTE_API_TOKEN}"
      }
    }
  }
}
```

El valor de `RED_TRANSPORTE_API_TOKEN` debe vivir en el gestor de secretos o en
el entorno de OpenCode, nunca en un commit.

### `streamable-http`: ChatGPT y clientes remotos

El endpoint desplegado es, por defecto:

```text
https://mcp.example.com/mcp
```

`RED_TRANSPORTE_MCP_BASE_URL` puede incluir un prefijo de ruta. Por ejemplo,
con `https://mcp.example.com/red`, el endpoint es
`https://mcp.example.com/red/mcp` y todas las rutas HTTP del MCP quedan bajo
`/red`.

El servidor publica:

| Endpoint | Uso |
|---|---|
| `GET <prefijo>/health` | Liveness sin autenticación |
| `GET <prefijo>/.well-known/oauth-protected-resource/mcp` | Metadata RFC 9728 |
| `GET <prefijo>/.well-known/oauth-authorization-server` | Metadata OAuth RFC 8414 |
| `POST <prefijo>/register` | Dynamic Client Registration |
| `GET <prefijo>/authorize` | Inicio del consentimiento OAuth |
| `GET/POST <prefijo>/oauth/consent` | Pantalla que valida el token MCP |
| `POST <prefijo>/token` | Authorization code y refresh token |
| `POST <prefijo>/mcp` | Transporte MCP protegido |

Sin prefijo, `<prefijo>` es vacío y las rutas mantienen sus URLs actuales.

## Autenticación HTTP

### OAuth para ChatGPT

El flujo es OAuth 2.1 authorization code con PKCE `S256`:

1. ChatGPT solicita metadata del recurso.
2. ChatGPT registra un cliente público con redirect URI propio.
3. ChatGPT genera `code_verifier` y `code_challenge`.
4. El navegador abre `<prefijo>/authorize` y luego la pantalla de consentimiento.
5. El usuario introduce el valor de `RED_TRANSPORTE_MCP_TOKEN` desde el gestor de secretos elegido.
6. El servidor devuelve un authorization code a ChatGPT.
7. ChatGPT canjea el code por access token y refresh token.
8. Cada request MCP lleva `Authorization: Bearer <oauth_access_token>`.

Los access tokens duran una hora. Los refresh tokens duran 30 días y rotan en
cada refresh. El token incluye como audiencia el recurso exacto:
`https://mcp.example.com/mcp`.

Configuración de ChatGPT web:

1. Activar **Developer mode** en *Settings → Apps → Advanced Settings*.
2. Entrar a *Apps → Create*.
3. Usar `https://mcp.example.com/mcp` como endpoint MCP.
4. Elegir OAuth y pulsar **Scan Tools**.
5. En la pantalla del MCP, introducir el token guardado en el gestor de secretos
   configurado para tu despliegue.
6. Crear/publicar la app y activarla en un chat.

No se introduce en ChatGPT el `RED_TRANSPORTE_API_TOKEN`: ese token solo vive
en el entorno privado del servidor MCP y se usa hacia `RedTransporteAPI`.

### Bearer legacy

Clientes que no implementen OAuth todavía pueden enviar directamente:

```http
Authorization: Bearer <RED_TRANSPORTE_MCP_TOKEN>
```

Es una compatibilidad para integraciones internas, no el flujo recomendado para
ChatGPT.

## Variables de entorno

| Variable | Default | Requerida | Uso |
|---|---|---:|---|
| `RED_TRANSPORTE_API_URL` | `http://localhost:8000` | no | Base URL de la API REST |
| `RED_TRANSPORTE_API_TOKEN` | vacío | sí | Token interno para la API |
| `RED_TRANSPORTE_MCP_TOKEN` | vacío | HTTP | Bootstrap OAuth y bearer legacy |
| `RED_TRANSPORTE_MCP_PUBLIC_HOST` | `localhost` | no | Allowlist DNS-rebinding |
| `RED_TRANSPORTE_MCP_BASE_URL` | `http://localhost:8001` | no | Issuer y recurso OAuth; su path opcional es el prefijo HTTP |
| `RED_TRANSPORTE_OAUTH_SECRET` | deriva del MCP token | no | Secreto HMAC separado recomendado |
| `RED_TRANSPORTE_MCP_PORT` | `8001` | no | Puerto HTTP |
| `RED_TRANSPORTE_MCP_TIMEOUT` | `30` | no | Timeout al API REST, en segundos |

En producción, `RED_TRANSPORTE_API_TOKEN`, `RED_TRANSPORTE_MCP_TOKEN` y, si se
usa, `RED_TRANSPORTE_OAUTH_SECRET` deben ser secretos del gestor de secretos
de tu plataforma.
No deben aparecer en `.env.example`, logs, README ni comandos guardados.

## Docker local

```bash
cp .env.example .env
# completar los tokens en .env
docker compose up --build
```

La configuración de producción, incluyendo el checkout, el reverse proxy, los
secretos, las units y los smoke tests, pertenece a la documentación privada de
infraestructura del operador.

El proceso usa `stateless_http=True`: no dependas de una sesión MCP persistente
entre requests. Los tokens OAuth son autocontenidos y el authorization code es
de un solo uso.

## Pruebas y smoke tests

```bash
uv sync --extra dev
uv run pytest tests/ -q
```

Comprobaciones HTTP mínimas:

```bash
BASE=https://mcp.example.com
PREFIX=
# Para un despliegue con prefijo: BASE=https://mcp.example.com; PREFIX=/red
curl -fsS "$BASE$PREFIX/health"
curl -i -X POST "$BASE$PREFIX/mcp" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}}}'
```

La primera request debe devolver `200`; la segunda debe devolver `401`. El flujo
OAuth completo debe probar DCR, redirect, PKCE, token, `tools/list`, una llamada
real y refresh.

## Errores habituales

| Síntoma | Causa probable |
|---|---|
| `<prefijo>/health` funciona pero `<prefijo>/mcp` da `401` | Falta OAuth o bearer legacy |
| ChatGPT no descubre OAuth | Revisar metadata y el endpoint exacto `<prefijo>/mcp` |
| `421 Invalid Host header` | `RED_TRANSPORTE_MCP_PUBLIC_HOST` no coincide con el hostname |
| Tool responde error 401/403 | Falta o no tiene scopes el `RED_TRANSPORTE_API_TOKEN` |
| Predicciones responden 503 | Fuentes iBus/RED web están temporalmente caídas |
| OpenCode no registra tools | Usar `stdio` y comprobar que `RED_TRANSPORTE_API_TOKEN` está en el entorno |

## Licencia

GPL-3.0-only. Ver [LICENSE](LICENSE).

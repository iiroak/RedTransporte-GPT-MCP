"""OAuth 2.1 authorization server support for the ChatGPT MCP connector."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse

from pydantic import AnyUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


OAUTH_SCOPE = "transit:read"
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
AUTHORIZATION_CODE_TTL = 300


@dataclass
class _PendingAuthorization:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float


class RedTransporteOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Small single-user OAuth provider backed by the existing MCP secret.

    Client registrations and issued tokens are self-contained HMAC-signed values, so a
    container restart does not invalidate ChatGPT's registered client or refresh token.
    Authorization codes remain one-time-use in process memory and expire quickly.
    """

    def __init__(self, issuer_url: str, resource_url: str, bootstrap_token: str, signing_secret: str):
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.bootstrap_token = bootstrap_token
        self.signing_secret = signing_secret or bootstrap_token
        self._pending: dict[str, _PendingAuthorization] = {}
        self._used_codes: set[str] = set()
        self._revoked_tokens: set[str] = set()

    def _encode(self, payload: dict[str, object]) -> str:
        if not self.signing_secret:
            raise RuntimeError("OAuth signing secret is not configured")
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        body = _b64encode(raw)
        signature = hmac.new(
            self.signing_secret.encode(), body.encode(), hashlib.sha256
        ).digest()
        return f"{body}.{_b64encode(signature)}"

    def _decode(self, value: str) -> dict[str, object] | None:
        if not self.signing_secret or "." not in value:
            return None
        body, signature = value.rsplit(".", 1)
        expected = hmac.new(
            self.signing_secret.encode(), body.encode(), hashlib.sha256
        ).digest()
        try:
            supplied = _b64decode(signature)
        except ValueError:
            return None
        if not hmac.compare_digest(expected, supplied):
            return None
        try:
            payload = json.loads(_b64decode(body).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _valid_redirect_uri(uri: AnyUrl) -> bool:
        parsed = urlparse(str(uri))
        if parsed.fragment or parsed.username or parsed.password:
            return False
        if parsed.scheme == "https" and parsed.netloc:
            return True
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "[::1]"}

    def _client_payload(self, client: OAuthClientInformationFull) -> dict[str, object]:
        return {
            "kind": "client",
            "client_id": client.client_id,
            "redirect_uris": [str(uri) for uri in (client.redirect_uris or [])],
            "client_name": client.client_name,
            "scope": client.scope or OAUTH_SCOPE,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "issued_at": int(time.time()),
        }

    def _client_from_payload(
        self, payload: dict[str, object], client_id: str
    ) -> OAuthClientInformationFull | None:
        if payload.get("kind") != "client":
            return None
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list):
            return None
        if not all(isinstance(uri, str) for uri in redirect_uris):
            return None
        try:
            return OAuthClientInformationFull(
                client_id=client_id,
                redirect_uris=redirect_uris,
                client_name=payload.get("client_name"),
                scope=payload.get("scope") or OAUTH_SCOPE,
                grant_types=payload.get("grant_types") or ["authorization_code"],
                response_types=payload.get("response_types") or ["code"],
                token_endpoint_auth_method="none",
                client_id_issued_at=payload.get("issued_at"),
            )
        except ValidationError:
            return None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        payload = self._decode(client_id)
        return self._client_from_payload(payload, client_id) if payload else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The HTTP registration endpoint below issues signed, restart-safe client IDs.
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description="Use the MCP dynamic registration endpoint",
        )

    async def register_request(self, request: Request) -> Response:
        try:
            metadata = OAuthClientMetadata.model_validate_json(await request.body())
        except ValidationError as error:
            return _oauth_error("invalid_client_metadata", str(error), status_code=400)

        if metadata.token_endpoint_auth_method not in (None, "none"):
            return _oauth_error(
                "invalid_client_metadata",
                "Only public clients with token_endpoint_auth_method=none are supported",
                status_code=400,
            )
        if "code" not in metadata.response_types:
            return _oauth_error(
                "invalid_client_metadata",
                "response_types must include code",
                status_code=400,
            )
        if "authorization_code" not in metadata.grant_types:
            return _oauth_error(
                "invalid_client_metadata",
                "grant_types must include authorization_code",
                status_code=400,
            )
        if not metadata.redirect_uris or not all(
            self._valid_redirect_uri(uri) for uri in metadata.redirect_uris
        ):
            return _oauth_error(
                "invalid_redirect_uri",
                "Redirect URIs must use HTTPS or localhost HTTP",
                status_code=400,
            )

        requested_scopes = (metadata.scope or OAUTH_SCOPE).split()
        if requested_scopes != [OAUTH_SCOPE]:
            return _oauth_error(
                "invalid_client_metadata",
                f"Only the {OAUTH_SCOPE} scope is supported",
                status_code=400,
            )

        client_id = self._encode(
            {
                "kind": "client",
                "client_id": secrets.token_urlsafe(24),
                "redirect_uris": [str(uri) for uri in metadata.redirect_uris],
                "client_name": metadata.client_name,
                "scope": OAUTH_SCOPE,
                "grant_types": [grant for grant in metadata.grant_types if grant in ("authorization_code", "refresh_token")],
                "response_types": ["code"],
                "issued_at": int(time.time()),
            }
        )
        client = await self.get_client(client_id)
        if client is None:  # pragma: no cover - the signed payload was created locally
            return _oauth_error("invalid_client_metadata", "Could not create client", status_code=500)
        body = client.model_dump(mode="json", exclude_none=True)
        return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and str(params.resource).rstrip("/") != self.resource_url:
            raise AuthorizeError(
                error="invalid_target",
                error_description="The requested resource is not this MCP server",
            )
        if not self.signing_secret:
            raise AuthorizeError(
                error="server_error",
                error_description="OAuth signing secret is not configured",
            )
        ticket = secrets.token_urlsafe(32)
        self._pending[ticket] = _PendingAuthorization(
            client=client,
            params=params,
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
        )
        return f"{self.issuer_url}/oauth/consent?{urlencode({'ticket': ticket})}"

    def pending_authorization(self, ticket: str) -> _PendingAuthorization | None:
        pending = self._pending.get(ticket)
        if pending and pending.expires_at >= time.time():
            return pending
        self._pending.pop(ticket, None)
        return None

    def approve(self, ticket: str, supplied_token: str) -> str | None:
        pending = self.pending_authorization(ticket)
        if pending is None or not self.bootstrap_token:
            return None
        if not hmac.compare_digest(supplied_token, self.bootstrap_token):
            return None
        self._pending.pop(ticket, None)
        params = pending.params
        resource = str(params.resource).rstrip("/") if params.resource else self.resource_url
        code = self._encode(
            {
                "kind": "code",
                "jti": secrets.token_urlsafe(24),
                "client_id": pending.client.client_id,
                "scope": params.scopes or [OAUTH_SCOPE],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": resource,
                "subject": "red-transporte-user",
                "exp": int(time.time()) + AUTHORIZATION_CODE_TTL,
            }
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state, iss=self.issuer_url
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        payload = self._decode(authorization_code)
        if not payload or payload.get("kind") != "code":
            return None
        jti = payload.get("jti")
        if not isinstance(jti, str) or jti in self._used_codes:
            return None
        if payload.get("client_id") != client.client_id or not _not_expired(payload):
            return None
        redirect_uri = payload.get("redirect_uri")
        challenge = payload.get("code_challenge")
        scopes = payload.get("scope")
        if not isinstance(redirect_uri, str) or not isinstance(challenge, str) or not isinstance(scopes, list):
            return None
        if not all(isinstance(scope, str) for scope in scopes):
            return None
        self._used_codes.add(jti)
        return AuthorizationCode(
            code=authorization_code,
            scopes=scopes,
            expires_at=float(payload["exp"]),
            client_id=client.client_id,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=bool(payload.get("redirect_uri_provided_explicitly")),
            resource=payload.get("resource") if isinstance(payload.get("resource"), str) else self.resource_url,
            subject=payload.get("subject") if isinstance(payload.get("subject"), str) else None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.resource != self.resource_url:
            raise TokenError(error="invalid_target", error_description="Invalid resource")
        return self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        if refresh_token in self._revoked_tokens:
            return None
        payload = self._decode(refresh_token)
        if not payload or payload.get("kind") != "refresh" or payload.get("client_id") != client.client_id:
            return None
        if not _not_expired(payload):
            return None
        scopes = payload.get("scope")
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(payload["exp"]),
            subject=payload.get("subject") if isinstance(payload.get("subject"), str) else None,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._revoked_tokens.add(refresh_token.token)
        return self._issue_tokens(client.client_id, scopes, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.bootstrap_token and hmac.compare_digest(token, self.bootstrap_token):
            return AccessToken(
                token=token,
                client_id="legacy-mcp-client",
                scopes=[OAUTH_SCOPE],
                resource=self.resource_url,
                subject="red-transporte-user",
                claims={"iss": self.issuer_url, "aud": self.resource_url},
            )
        if token in self._revoked_tokens:
            return None
        payload = self._decode(token)
        if not payload or payload.get("kind") != "access" or not _not_expired(payload):
            return None
        if payload.get("resource") != self.resource_url:
            return None
        scopes = payload.get("scope")
        client_id = payload.get("client_id")
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            return None
        if not isinstance(client_id, str):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(payload["exp"]),
            resource=self.resource_url,
            subject=payload.get("subject") if isinstance(payload.get("subject"), str) else None,
            claims={"iss": self.issuer_url, "aud": self.resource_url},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._revoked_tokens.add(token.token)

    def _issue_tokens(self, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        now = int(time.time())
        access_token = self._encode(
            {
                "kind": "access",
                "jti": secrets.token_urlsafe(24),
                "client_id": client_id,
                "scope": scopes,
                "resource": self.resource_url,
                "subject": subject,
                "iat": now,
                "exp": now + ACCESS_TOKEN_TTL,
            }
        )
        refresh_token = self._encode(
            {
                "kind": "refresh",
                "jti": secrets.token_urlsafe(24),
                "client_id": client_id,
                "scope": scopes,
                "subject": subject,
                "iat": now,
                "exp": now + REFRESH_TOKEN_TTL,
            }
        )
        return OAuthToken(
            access_token=access_token,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )


async def consent_endpoint(request: Request, provider: RedTransporteOAuthProvider) -> Response:
    ticket = request.query_params.get("ticket")
    if request.method == "POST":
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        ticket = _first(form, "ticket")
        supplied_token = _first(form, "mcp_token") or ""
        redirect_uri = provider.approve(ticket or "", supplied_token)
        if redirect_uri:
            return RedirectResponse(redirect_uri, status_code=302, headers={"Cache-Control": "no-store"})
        return _consent_page("Invalid token or expired authorization request.", ticket)

    pending = provider.pending_authorization(ticket or "")
    if pending is None:
        return HTMLResponse("Authorization request expired.", status_code=400)
    return _consent_page(None, ticket, pending.client.client_name)


def authorization_server_metadata(provider: RedTransporteOAuthProvider) -> JSONResponse:
    issuer = provider.issuer_url
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "scopes_supported": [OAUTH_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": False,
            "authorization_response_iss_parameter_supported": True,
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


def _issue_error(error: str, description: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _oauth_error(error: str, description: str, status_code: int) -> JSONResponse:
    return _issue_error(error, description, status_code)


def _consent_page(message: str | None, ticket: str | None, client_name: str | None = None) -> HTMLResponse:
    escaped_ticket = html.escape(ticket or "", quote=True)
    escaped_client = html.escape(client_name or "RedTransporte", quote=True)
    notice = f"<p class=error>{html.escape(message)}</p>" if message else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Authorize RedTransporte</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font:16px system-ui,sans-serif;max-width: thirtyrem;max-width:30rem;margin:4rem auto;padding:0 1rem;color:#202124}}input{{box-sizing:border-box;width:100%;padding:.7rem;margin:.4rem 0 1rem}}button{{padding:.7rem 1rem;background:#2457d6;color:#fff;border:0;border-radius:4px}}.error{{color:#b3261e}}</style></head>
<body><h1>Authorize RedTransporte</h1>{notice}
<p><strong>{escaped_client}</strong> requests read-only transit data.</p>
<p>Enter the MCP access token stored in the configured secret manager to authorize ChatGPT.</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="ticket" value="{escaped_ticket}">
<label for="mcp_token">MCP access token</label>
<input id="mcp_token" name="mcp_token" type="password" autocomplete="current-password" required>
<button type="submit">Authorize</button></form></body></html>"""
    return HTMLResponse(
        body,
        status_code=401 if message else 200,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        },
    )


def _not_expired(payload: dict[str, object]) -> bool:
    expiry = payload.get("exp")
    return isinstance(expiry, (int, float)) and expiry >= time.time()


def _first(values: dict[str, list[str]], key: str) -> str | None:
    items = values.get(key)
    return items[0] if items else None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

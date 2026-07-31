"""Single API-token auth stub (guide §1: no auth UI, one token).

The token is declared through FastAPI's security classes rather than a plain
`Header(...)` parameter. That matters for more than tidiness: only a declared
security scheme ends up in `components.securitySchemes`, which is what makes
Swagger UI render the **Authorize** button and the padlock icons. Read as a bare
header, the token would work over curl but be untestable from `/docs`.

Two schemes are accepted:
    Authorization: Bearer <token>     (preferred; what the UI client sends)
    X-API-Token: <token>              (convenience for tools that dislike Bearer)
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

#: `auto_error=False` on both so a miss on one scheme can fall through to the
#: other, and so we raise a single consistent 401 ourselves.
bearer_scheme = HTTPBearer(
    scheme_name="BearerToken",
    description="Paste the value of API_TOKEN from your .env (no 'Bearer ' prefix).",
    auto_error=False,
)

api_key_scheme = APIKeyHeader(
    name="X-API-Token",
    scheme_name="ApiTokenHeader",
    description="Alternative to the Authorization header. Same API_TOKEN value.",
    auto_error=False,
)


def _valid(token: str | None) -> bool:
    if not token:
        return False
    return secrets.compare_digest(token, settings.api_token)


def _extract_bearer(authorization: str | None) -> str | None:
    """Pull the token out of a raw Authorization header value."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_api_token: str | None = Depends(api_key_scheme),
) -> None:
    """REST dependency. Accepts `Authorization: Bearer <t>` or `X-API-Token: <t>`."""
    token = credentials.credentials if credentials else None
    if not token:
        token = x_api_token
    if not token:
        # Lenient fallback: a bare token in the Authorization header, with no
        # "Bearer " prefix. HTTPBearer rejects that shape, so read it directly.
        token = _extract_bearer(request.headers.get("authorization"))

    if not _valid(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def authorize_websocket(ws: WebSocket, token: str | None = None) -> bool:
    """Browsers cannot set headers on a WebSocket handshake, so the token comes
    in as `?token=`. A header is still accepted when present.

    Returns True when authorized; otherwise closes the socket and returns False.
    """
    candidate = (
        token
        or _extract_bearer(ws.headers.get("authorization"))
        or ws.headers.get("x-api-token")
    )
    if _valid(candidate):
        return True
    await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid API token")
    return False

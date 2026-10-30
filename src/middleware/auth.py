from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient

from ..config.constants import ROLE_MODERATOR
from ..config.settings import settings
from ..errors import AppError, forbidden_error, unauthorised_error

_jwk_client: PyJWKClient | None = None


def _issuer() -> str:
    """Build the Cognito user pool issuer URL used for token validation."""
    return f"https://cognito-idp.{settings.aws_region}.amazonaws.com/{settings.cognito_user_pool_id}"


def _jwks_client() -> PyJWKClient:
    """Return a cached JWKS client (reused across warm Lambda invocations)."""
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{_issuer()}/.well-known/jwks.json")
    return _jwk_client


def _extract_bearer_token(event: dict[str, Any]) -> str | None:
    """Pull the bearer token out of an API Gateway HTTP API v2 event."""
    headers = event.get("headers") or {}
    authorization = headers.get("authorization") or headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip()


def _decode(token: str) -> dict[str, Any]:
    """Verify a Cognito-issued access token and return its claims."""
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=_issuer(),
        options={"verify_aud": False},  # Cognito access tokens carry client_id, not aud
    )
    if claims.get("client_id") and claims["client_id"] != settings.cognito_client_id:
        raise unauthorised_error("Invalid token audience")
    if claims.get("token_use") != "access":
        raise unauthorised_error("Invalid token type")
    return claims


def authenticate(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the bearer token on a Lambda event and return user context."""
    token = _extract_bearer_token(event)
    if not token:
        raise unauthorised_error("Missing bearer token")
    try:
        claims = _decode(token)
    except AppError:
        raise
    except Exception as exc:  # pragma: no cover - translated to 401
        raise unauthorised_error("Invalid or expired token") from exc

    groups = claims.get("cognito:groups", []) or []
    role = ROLE_MODERATOR if ROLE_MODERATOR in groups else "user"
    return {
        "userId": claims["sub"],
        "userRole": role,
        "username": claims.get("username", claims["sub"]),
    }


def optional_authenticate(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return user context when a valid bearer token is present, else None."""
    if not _extract_bearer_token(event):
        return None
    try:
        return authenticate(event)
    except Exception:
        return None


def require_moderator(event: dict[str, Any]) -> dict[str, Any]:
    """Require moderator role before allowing access to protected handlers."""
    current_user = authenticate(event)
    if current_user["userRole"] != ROLE_MODERATOR:
        raise forbidden_error("Moderator access required")
    return current_user

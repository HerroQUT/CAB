"""Cognito token verification.

The agent forwards the end user's Cognito token on every MCP call. The server
verifies it against the pool's JWKS and takes the user id from the token's
`sub` claim. No tool ever accepts a userId argument: the model cannot ask for
another person's photos, because it has no way to name one.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import jwt
from jwt import PyJWKClient

import config

log = logging.getLogger("strobe-mcp.auth")


class AuthError(Exception):
    """Raised when a request carries no usable Cognito token."""


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    pool = config.get("cognito-user-pool-id")
    url = f"https://cognito-idp.{config.REGION}.amazonaws.com/{pool}/.well-known/jwks.json"
    return PyJWKClient(url, cache_keys=True)


def verify(token: str) -> dict:
    """Verify a Cognito access or ID token and return its claims."""
    if not token:
        raise AuthError("no token supplied")
    token = token.removeprefix("Bearer ").strip()
    try:
        key = _jwk_client().get_signing_key_from_jwt(token).key
        unverified = jwt.decode(token, options={"verify_signature": False})
        token_use = unverified.get("token_use")
        pool = config.get("cognito-user-pool-id")
        issuer = f"https://cognito-idp.{config.REGION}.amazonaws.com/{pool}"
        # Access tokens carry client_id; ID tokens carry aud. Check whichever applies.
        options = {"verify_aud": token_use == "id"}
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=issuer, options=options,
            audience=config.get("cognito-client-id") if token_use == "id" else None,
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}") from e

    if claims.get("token_use") == "access" and claims.get("client_id") != config.get("cognito-client-id"):
        raise AuthError("token issued for a different app client")
    if not claims.get("sub"):
        raise AuthError("token has no sub claim")
    return claims

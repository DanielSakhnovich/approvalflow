"""
N1: self-signed JWT auth for the demo. No external IdP -- intake mints its
own HS256 tokens against a small in-process seed-user table, and every
service can verify them locally with the same shared secret (no network
round-trip per request).

`require_role` is gated by the `AUTH_ENABLED` env var, read at *call time*
(inside the returned dependency, not at import/factory time) so a process
can flip the flag between requests -- this is what lets the harness and the
test suite run the exact same route both with and without auth enforced.
When the flag is off, the dependency is a no-op: every route behaves as it
did before N1, returning a synthetic system principal. When it is on, a
missing/invalid bearer token is a 401 and a valid token whose role is not
in the allowed set is a 403 (M15: distinguish "who are you" from "you can't
do that" rather than collapsing both to one status).
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=12)

# Used ONLY when AUTH_ENABLED is off (or unset) and no JWT_SIGNING_SECRET is
# configured -- e.g. local dev poking at routes with auth disabled. Never
# used while auth is actually enforced: _signing_secret() raises instead of
# falling back to this, so a deployment can't silently run enforced auth on
# a well-known secret (M15, fail loud).
_DEV_DEFAULT_SECRET = "afcommon-dev-only-insecure-signing-secret"  # noqa: S105

# Demo-only credentials for the three roles the workflow knows about. This
# is a stand-in for a real user store/IdP -- fine for the harness and local
# demo, not something to point at real data.
SEED_USERS: dict[str, dict] = {
    "alice": {"password": "alice-demo-pw", "role": "submitter"},
    "revi": {"password": "revi-demo-pw", "role": "approver"},
    "admin": {"password": "admin-demo-pw", "role": "admin"},
}

_bearer_scheme = HTTPBearer(auto_error=False)


def _auth_enabled() -> bool:
    return os.environ.get("AUTH_ENABLED", "").lower() == "true"


def _signing_secret() -> str:
    """Resolve the HS256 signing secret from `JWT_SIGNING_SECRET`.

    Falls back to a documented dev default ONLY when auth is off. When
    AUTH_ENABLED is on and no secret is configured, raises immediately
    rather than quietly signing/verifying tokens against a secret every
    reader of this file also knows (M15).
    """
    secret = os.environ.get("JWT_SIGNING_SECRET")
    if secret:
        return secret
    if _auth_enabled():
        raise RuntimeError(
            "AUTH_ENABLED is set but JWT_SIGNING_SECRET is not configured -- "
            "refusing to sign/verify tokens with the dev-default secret."
        )
    return _DEV_DEFAULT_SECRET


def create_access_token(sub: str, role: str) -> str:
    """Mint a self-signed HS256 token carrying `sub`, `role`, `exp` (~12h)."""
    now = datetime.now(UTC)
    claims = {"sub": sub, "role": role, "exp": now + TOKEN_TTL}
    return jwt.encode(claims, _signing_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Verify and decode a token. Raises `jwt.InvalidTokenError` (or a
    subclass, e.g. on a bad signature or expiry) on any bad/expired token."""
    return jwt.decode(token, _signing_secret(), algorithms=[ALGORITHM])


def require_role(*allowed: str) -> Callable[..., dict[str, Any]]:
    """FastAPI dependency factory: build a dependency that requires the
    caller's token to carry one of `allowed` roles.

    `AUTH_ENABLED` is read inside the dependency itself (request time), not
    here in the factory (route-registration time) -- registering routes
    happens once at import, so reading the flag here would bake in
    whatever it was at process startup and no test/harness toggle could
    ever change it afterward.
    """

    async def _dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    ) -> dict[str, Any]:
        if not _auth_enabled():
            return {"sub": "anonymous", "role": "system"}

        if credentials is None or not credentials.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
            )

        try:
            claims = decode_token(credentials.credentials)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or expired token",
            ) from exc

        if claims.get("role") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )

        return claims

    return _dependency

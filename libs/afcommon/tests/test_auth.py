"""
N1: afcommon.auth -- self-signed JWT mint/verify plus the `require_role`
FastAPI dependency. The dependency-level tests build a tiny FastAPI app with
one route gated by `require_role("approver")` and drive it through
`AUTH_ENABLED` off/on, no-header/wrong-role/right-role, to exercise the
200/401/403 split for real rather than asserting on the helper functions in
isolation.
"""

import jwt
import pytest
from afcommon.auth import SEED_USERS, create_access_token, decode_token, require_role
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def test_roundtrip_carries_role():
    tok = create_access_token("alice", "submitter")
    claims = decode_token(tok)
    assert claims["sub"] == "alice" and claims["role"] == "submitter"


def test_tampered_token_rejected():
    tok = create_access_token("alice", "submitter")
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(tok + "x")


def test_seed_users_have_the_three_roles():
    assert {u["role"] for u in SEED_USERS.values()} == {"submitter", "approver", "admin"}


_require_approver = require_role("approver")


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected(claims: dict = Depends(_require_approver)):
        return claims

    return app


@pytest.fixture
def client():
    return TestClient(make_app())


def test_auth_disabled_is_a_noop(client, monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    resp = client.get("/protected")

    assert resp.status_code == 200
    assert resp.json() == {"sub": "anonymous", "role": "system"}


def test_auth_enabled_missing_header_is_401(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-signing-secret-for-unit-tests-only")

    resp = client.get("/protected")

    assert resp.status_code == 401


def test_auth_enabled_wrong_role_is_403(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-signing-secret-for-unit-tests-only")
    tok = create_access_token("alice", "submitter")

    resp = client.get("/protected", headers={"Authorization": f"Bearer {tok}"})

    assert resp.status_code == 403


def test_auth_enabled_right_role_is_200(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-signing-secret-for-unit-tests-only")
    tok = create_access_token("revi", "approver")

    resp = client.get("/protected", headers={"Authorization": f"Bearer {tok}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sub"] == "revi" and body["role"] == "approver"


def test_auth_enabled_invalid_token_is_401(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-signing-secret-for-unit-tests-only")

    resp = client.get("/protected", headers={"Authorization": "Bearer not-a-real-token"})

    assert resp.status_code == 401


def test_auth_enabled_without_secret_fails_loud(client, monkeypatch):
    """AUTH_ENABLED on with no JWT_SIGNING_SECRET must raise rather than
    silently signing/verifying against the dev-default secret (M15)."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("JWT_SIGNING_SECRET", raising=False)

    with pytest.raises(RuntimeError):
        client.get("/protected", headers={"Authorization": "Bearer whatever"})

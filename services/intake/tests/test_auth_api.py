import pytest
from afcommon.auth import SEED_USERS, decode_token
from fastapi.testclient import TestClient

from services.intake.src.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("username", list(SEED_USERS.keys()))
def test_login_valid_seed_user_returns_role_bearing_token(client, username):
    password = SEED_USERS[username]["password"]
    role = SEED_USERS[username]["role"]
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == role
    claims = decode_token(body["access_token"])
    assert claims["sub"] == username
    assert claims["role"] == role


def test_login_bad_password_is_401(client):
    resp = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user_is_401(client):
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever"})
    assert resp.status_code == 401

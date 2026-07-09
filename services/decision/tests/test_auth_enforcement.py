"""
N1.3: role enforcement on the decision config API. Only the mutating route
(`PUT /api/config/thresholds`) is gated to `admin` -- the GET stays
readable regardless of auth state so the dashboard/harness can always see
current thresholds. `require_role` no-ops when `AUTH_ENABLED` is unset (see
`services/decision/tests/test_config.py`, which runs with no auth env at
all), so these tests explicitly flip `AUTH_ENABLED=true` to exercise the
401/403/200 split for real, mirroring `libs/afcommon/tests/test_auth.py`.
"""

import pytest
from afcommon.auth import create_access_token
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.decision.src import deps
from services.decision.src.config import ConfigRepo
from services.decision.src.main import app


@pytest.fixture
def client():
    repo = ConfigRepo(InMemoryStateStore())
    app.dependency_overrides[deps.get_config_repo] = lambda: repo
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-secret")
    return client


def auth_header(sub: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(sub, role)}"}


class TestUpdateThresholdsRoleEnforcement:
    def test_no_token_is_401(self, auth_client):
        resp = auth_client.put("/api/config/thresholds", json={"min_confidence": 0.9})
        assert resp.status_code == 401

    def test_wrong_role_is_403(self, auth_client):
        resp = auth_client.put("/api/config/thresholds", json={"min_confidence": 0.9},
                               headers=auth_header("revi", "approver"))
        assert resp.status_code == 403

    def test_admin_role_is_200(self, auth_client):
        resp = auth_client.put("/api/config/thresholds", json={"min_confidence": 0.9},
                               headers=auth_header("admin", "admin"))
        assert resp.status_code == 200
        assert resp.json()["min_confidence"] == 0.9


class TestGetThresholdsStaysOpen:
    def test_get_thresholds_no_token_is_200_even_with_auth_enabled(self, auth_client):
        resp = auth_client.get("/api/config/thresholds")
        assert resp.status_code == 200

"""
N1.3: role enforcement on the intake API's protected routes (submit +
status). `require_role` no-ops when `AUTH_ENABLED` is unset (see
`services/intake/tests/test_api.py`, which runs with no auth env at all),
so these tests explicitly flip `AUTH_ENABLED=true` to exercise the
401/403/200 split for real, mirroring `libs/afcommon/tests/test_auth.py`.
"""

import pytest
from afcommon.auth import create_access_token
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.intake.src import deps
from services.intake.src.main import app
from services.intake.src.repo import IntakeRepo
from services.intake.tests.test_models import FIXTURE


@pytest.fixture
def env():
    repo = IntakeRepo(InMemoryStateStore())
    published: list[tuple[str, dict]] = []

    async def fake_publish(topic: str, payload: dict) -> None:
        published.append((topic, payload))

    app.dependency_overrides[deps.get_repo] = lambda: repo
    app.dependency_overrides[deps.get_publisher] = lambda: fake_publish
    yield TestClient(app), repo, published
    app.dependency_overrides.clear()


@pytest.fixture
def auth_env(env, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "test-secret")
    return env


def auth_header(sub: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(sub, role)}"}


class TestSubmitRoleEnforcement:
    def test_no_token_is_401(self, auth_env):
        client, *_ = auth_env
        resp = client.post("/api/invoices", json=FIXTURE)
        assert resp.status_code == 401

    def test_wrong_role_is_403(self, auth_env):
        client, *_ = auth_env
        resp = client.post("/api/invoices", json=FIXTURE, headers=auth_header("revi", "approver"))
        assert resp.status_code == 403

    def test_submitter_role_is_202(self, auth_env):
        client, *_ = auth_env
        resp = client.post("/api/invoices", json=FIXTURE, headers=auth_header("alice", "submitter"))
        assert resp.status_code == 202

    def test_admin_role_is_202(self, auth_env):
        client, *_ = auth_env
        resp = client.post("/api/invoices", json=FIXTURE, headers=auth_header("admin", "admin"))
        assert resp.status_code == 202


class TestGetStatusRoleEnforcement:
    def test_no_token_is_401(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/invoices/inv_whatever")
        assert resp.status_code == 401

    def test_wrong_role_is_403(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/invoices/inv_whatever", headers=auth_header("revi", "approver"))
        assert resp.status_code == 403

    def test_submitter_role_is_200(self, auth_env):
        client, *_ = auth_env
        tracking = client.post(
            "/api/invoices", json=FIXTURE, headers=auth_header("alice", "submitter")
        ).json()["trackingId"]
        resp = client.get(f"/api/invoices/{tracking}", headers=auth_header("alice", "submitter"))
        assert resp.status_code == 200

    def test_admin_role_is_200(self, auth_env):
        client, *_ = auth_env
        tracking = client.post(
            "/api/invoices", json=FIXTURE, headers=auth_header("admin", "admin")
        ).json()["trackingId"]
        resp = client.get(f"/api/invoices/{tracking}", headers=auth_header("admin", "admin"))
        assert resp.status_code == 200

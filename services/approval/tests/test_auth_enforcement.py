"""
N1.3: role enforcement on the approval API's protected routes (queue +
verdict). `require_role` no-ops when `AUTH_ENABLED` is unset (see
`services/approval/tests/test_api.py`, which runs with no auth env at
all), so these tests explicitly flip `AUTH_ENABLED=true` to exercise the
401/403/200 split for real, mirroring `libs/afcommon/tests/test_auth.py`.
"""

import pytest
from afcommon.auth import create_access_token
from afcommon.state import InMemoryStateStore
from fastapi.testclient import TestClient

from services.approval.src import deps
from services.approval.src.main import app
from services.approval.src.models import Escalation, EscalationStatus
from services.approval.src.repo import ApprovalRepo


def make_escalation(invoice_id: str, escalated_at: str, status=EscalationStatus.pending,
                    **overrides) -> Escalation:
    defaults = dict(
        invoice_id=invoice_id,
        correlation_id=f"corr-{invoice_id}",
        status=status,
        usd_cents=50000,
        route_violations=["policy_a"],
        recommendation="Escalate to management",
        confidence=0.75,
        reasoning="High value and policy violation",
        escalated_at=escalated_at,
    )
    defaults.update(overrides)
    return Escalation(**defaults)


async def seed(repo: ApprovalRepo, esc: Escalation) -> None:
    await repo.save_new(esc)
    await repo.add_to_queue(esc.invoice_id)


@pytest.fixture
def env():
    repo = ApprovalRepo(InMemoryStateStore())
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


class TestQueueRoleEnforcement:
    def test_no_token_is_401(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/approvals/queue")
        assert resp.status_code == 401

    def test_wrong_role_is_403(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/approvals/queue", headers=auth_header("alice", "submitter"))
        assert resp.status_code == 403

    def test_approver_role_is_200(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/approvals/queue", headers=auth_header("revi", "approver"))
        assert resp.status_code == 200

    def test_admin_role_is_200(self, auth_env):
        client, *_ = auth_env
        resp = client.get("/api/approvals/queue", headers=auth_header("admin", "admin"))
        assert resp.status_code == 200


class TestVerdictRoleEnforcement:
    async def test_no_token_is_401(self, auth_env):
        client, repo, _ = auth_env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": "revi"})
        assert resp.status_code == 401

    async def test_wrong_role_is_403(self, auth_env):
        client, repo, _ = auth_env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": "revi"},
                           headers=auth_header("alice", "submitter"))
        assert resp.status_code == 403

    async def test_approver_role_is_200(self, auth_env):
        client, repo, _ = auth_env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": "revi"},
                           headers=auth_header("revi", "approver"))
        assert resp.status_code == 200

    async def test_admin_role_is_200(self, auth_env):
        client, repo, _ = auth_env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": "admin"},
                           headers=auth_header("admin", "admin"))
        assert resp.status_code == 200

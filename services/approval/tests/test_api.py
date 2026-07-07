"""
Task 3: the approver API -- queue view + exactly-once verdicts (F4/F5).

Verdict idempotency is M10 applied to people: double-clicks, retries, and
racing approvers must all collapse to exactly one effect. The CAS transform
in `resolve()` raises `AlreadyResolved` the moment it sees a non-pending
status, so only the first caller through the CAS loop ever gets to apply a
verdict -- everyone else, including a genuine concurrent racer under
`YieldingStateStore`, gets back a 409 carrying the existing resolution
(who/when/what), not a bare conflict.

The publish-failure rollback (F5-critical, mirrors intake's fail-loud M15)
is the other half: a resolution CAS-committed but never published would
leave the invoice hanging forever (the exact failure M11 exists to
prevent). Ordering is resolve -> publish -> remove_from_queue, so on a
publish exception the compensating CAS back to pending is the whole
rollback (the id never left the queue), the caller sees 503, and the
record must be fully re-approvable afterward -- by a different approver
with a different verdict. If the rollback itself fails, that is the one
state that must never be silent: log critical + honest 500.
"""

import asyncio

import pytest
from afcommon.state import InMemoryStateStore, YieldingStateStore
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.approval.src import deps
from services.approval.src.api import VerdictRequest, submit_verdict
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


class TestQueueView:
    async def test_queue_empty_returns_items_empty_list(self, env):
        client, *_ = env
        resp = client.get("/api/approvals/queue")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    async def test_queue_orders_oldest_first_with_full_fields(self, env):
        client, repo, _ = env
        await seed(repo, make_escalation("inv-2", "2024-01-02T00:00:00+00:00"))
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))

        resp = client.get("/api/approvals/queue")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["invoiceId"] for i in items] == ["inv-1", "inv-2"]

        first = items[0]
        assert first["recommendation"] == "Escalate to management"
        assert first["confidence"] == 0.75
        assert first["violations"] == ["policy_a"]
        assert first["reasoning"] == "High value and policy violation"
        assert first["usdCents"] == 50000
        assert first["escalatedAt"] == "2024-01-01T00:00:00+00:00"
        assert first["status"] == "pending"

    async def test_queue_self_heals_missing_ids(self, env):
        client, repo, _ = env
        await repo.add_to_queue("inv-ghost")  # index entry, no backing record
        await seed(repo, make_escalation("inv-real", "2024-01-01T00:00:00+00:00"))

        resp = client.get("/api/approvals/queue")
        items = resp.json()["items"]
        assert [i["invoiceId"] for i in items] == ["inv-real"]
        assert await repo.list_queue() == ["inv-real"]

    async def test_queue_self_heals_non_pending_ids(self, env):
        client, repo, _ = env
        resolved = make_escalation(
            "inv-resolved", "2024-01-01T00:00:00+00:00", status=EscalationStatus.approved)
        await repo.save_new(resolved)
        await repo.add_to_queue("inv-resolved")  # inconsistent: indexed but not pending

        resp = client.get("/api/approvals/queue")
        assert resp.json()["items"] == []
        assert await repo.list_queue() == []


class TestVerdict:
    async def test_verdict_approved_resolves_queue_empty_and_publishes(self, env):
        client, repo, published = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))

        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": "lena", "comment": "ok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["resolvedBy"] == "lena"
        assert body["resolutionComment"] == "ok"

        assert await repo.list_queue() == []
        record = await repo.get("inv-1")
        assert record.status == EscalationStatus.approved
        assert record.resolved_by == "lena"
        assert record.resolved_at is not None

        assert len(published) == 1
        topic, payload = published[0]
        assert topic == "approval-resolved"
        assert payload["verdict"] == "approved"
        assert payload["approver_id"] == "lena"
        assert payload["comment"] == "ok"
        assert payload["meta"]["invoice_id"] == "inv-1"
        assert payload["meta"]["correlation_id"] == "corr-inv-1"
        # I3: usd_cents rides the approval-resolved event so payment (Phase 05)
        # can size the budget reservation without re-querying.
        assert payload["usd_cents"] == 50000

    async def test_verdict_needs_info_variant(self, env):
        client, repo, published = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))

        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "needs_info", "approver_id": "lena"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "needs_info"
        assert published[0][1]["verdict"] == "needs_info"

    async def test_verdict_rejected_variant(self, env):
        client, repo, published = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))

        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "rejected", "approver_id": "lena"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        assert published[0][1]["verdict"] == "rejected"

    async def test_double_post_same_verdict_returns_409_with_resolution(self, env):
        client, repo, published = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))

        first = client.post("/api/approvals/inv-1/verdict",
                            json={"verdict": "approved", "approver_id": "lena"})
        assert first.status_code == 200

        second = client.post("/api/approvals/inv-1/verdict",
                             json={"verdict": "rejected", "approver_id": "marco"})
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert detail["resolvedBy"] == "lena"
        assert detail["status"] == "approved"
        assert len(published) == 1

    async def test_missing_approver_id_returns_422(self, env):
        client, repo, _ = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict", json={"verdict": "approved"})
        assert resp.status_code == 422

    async def test_empty_approver_id_returns_422(self, env):
        client, repo, _ = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "approved", "approver_id": ""})
        assert resp.status_code == 422

    async def test_invalid_verdict_literal_returns_422(self, env):
        client, repo, _ = env
        await seed(repo, make_escalation("inv-1", "2024-01-01T00:00:00+00:00"))
        resp = client.post("/api/approvals/inv-1/verdict",
                           json={"verdict": "maybe", "approver_id": "lena"})
        assert resp.status_code == 422

    async def test_unknown_id_returns_404(self, env):
        client, *_ = env
        resp = client.post("/api/approvals/inv-nope/verdict",
                           json={"verdict": "approved", "approver_id": "lena"})
        assert resp.status_code == 404


async def test_concurrent_racing_verdicts_exactly_one_wins():
    """Two tasks racing to resolve the SAME escalation, using
    YieldingStateStore so their CAS loops genuinely interleave (asyncio.gather).
    TestClient is sync, so the racers call the endpoint function directly."""
    repo = ApprovalRepo(YieldingStateStore())
    published: list[tuple[str, dict]] = []

    async def fake_publish(topic: str, payload: dict) -> None:
        published.append((topic, payload))

    await seed(repo, make_escalation("inv-race", "2024-01-01T00:00:00+00:00"))

    async def attempt(approver_id: str):
        body = VerdictRequest(verdict="approved", approver_id=approver_id)
        try:
            result = await submit_verdict("inv-race", body, repo=repo, publisher=fake_publish)
            return ("ok", result)
        except HTTPException as e:
            return ("err", e.status_code, e.detail)

    results = await asyncio.gather(attempt("lena"), attempt("marco"))
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]

    assert len(oks) == 1
    assert len(errs) == 1
    assert errs[0][1] == 409
    assert len(published) == 1


class TestPublishFailureRollback:
    async def test_publish_failure_rolls_back_and_id_stays_in_queue(self, env):
        """Publish runs BEFORE remove_from_queue, so on publish failure the
        rollback is just the compensating revert -- the id never left the
        queue and must still be there, with the record back to pending."""
        client, repo, _ = env
        await seed(repo, make_escalation("inv-flaky", "2024-01-01T00:00:00+00:00"))

        async def flaky_publish(topic: str, payload: dict) -> None:
            raise RuntimeError("sidecar down")

        app.dependency_overrides[deps.get_publisher] = lambda: flaky_publish
        resp = client.post("/api/approvals/inv-flaky/verdict",
                           json={"verdict": "approved", "approver_id": "lena"})
        assert resp.status_code == 503

        record = await repo.get("inv-flaky")
        assert record.status == EscalationStatus.pending
        assert record.resolved_by is None
        assert record.resolved_at is None
        assert await repo.list_queue() == ["inv-flaky"]

    async def test_rollback_reraises_id_into_queue_after_self_heal_race(self, env):
        """THE BINDING test for the review's I2 finding: during the publish
        await, the record is already committed-resolved, so a concurrent
        `GET /queue` self-heal can observe it as non-pending and remove the
        id from the index -- all before the publish actually fails and the
        rollback runs. The naive rollback (just the compensating CAS back to
        pending) would leave a record that's pending in storage but ABSENT
        from the queue index: invisible to approvers forever. The rollback
        must re-add the id (idempotent) so the invoice reappears."""
        client, repo, _ = env
        await seed(repo, make_escalation("inv-flaky", "2024-01-01T00:00:00+00:00"))

        async def flaky_publish(topic: str, payload: dict) -> None:
            # Simulate the self-healing GET /queue racing in during the
            # publish await: it observes the (already committed) resolution
            # and reaps the id from the index before we ever get to roll back.
            await repo.remove_from_queue("inv-flaky")
            raise RuntimeError("sidecar down")

        app.dependency_overrides[deps.get_publisher] = lambda: flaky_publish
        resp = client.post("/api/approvals/inv-flaky/verdict",
                           json={"verdict": "approved", "approver_id": "lena"})
        assert resp.status_code == 503

        record = await repo.get("inv-flaky")
        assert record.status == EscalationStatus.pending
        assert record.resolved_by is None
        assert record.resolved_at is None
        assert await repo.list_queue() == ["inv-flaky"]

    async def test_retry_after_rollback_succeeds_with_exactly_one_publish(self, env):
        """Full re-approvability: a DIFFERENT approver with a DIFFERENT
        verdict succeeds after the rollback -- the rolled-back record is not
        sticky to the first attempt in any way."""
        client, repo, published = env
        await seed(repo, make_escalation("inv-flaky", "2024-01-01T00:00:00+00:00"))

        async def flaky_publish(topic: str, payload: dict) -> None:
            raise RuntimeError("sidecar down")

        app.dependency_overrides[deps.get_publisher] = lambda: flaky_publish
        first = client.post("/api/approvals/inv-flaky/verdict",
                            json={"verdict": "approved", "approver_id": "lena"})
        assert first.status_code == 503

        async def good_publish(topic: str, payload: dict) -> None:
            published.append((topic, payload))
        app.dependency_overrides[deps.get_publisher] = lambda: good_publish

        second = client.post("/api/approvals/inv-flaky/verdict",
                             json={"verdict": "rejected", "approver_id": "marco",
                                   "comment": "over budget"})
        assert second.status_code == 200
        assert len(published) == 1
        assert published[0][1]["verdict"] == "rejected"
        assert published[0][1]["approver_id"] == "marco"

        record = await repo.get("inv-flaky")
        assert record.status == EscalationStatus.rejected
        assert record.resolved_by == "marco"
        assert await repo.list_queue() == []

    async def test_rollback_failure_is_loud_500_and_logged_critical(self, env, caplog):
        """If the compensating revert itself fails, the record is
        resolved-but-unpublished -- the one state that must NEVER be silent.
        The endpoint must log critical (invoice_id + manual reconciliation)
        and re-raise, so the caller sees an honest 500, not a retryable 503
        that would 409 on retry."""
        client, repo, _ = env
        await seed(repo, make_escalation("inv-doomed", "2024-01-01T00:00:00+00:00"))

        async def flaky_publish(topic: str, payload: dict) -> None:
            raise RuntimeError("sidecar down")

        original_resolve = repo.resolve
        calls = {"n": 0}

        async def resolve_fails_second_time(invoice_id, transform):
            calls["n"] += 1
            if calls["n"] == 2:  # the compensating revert
                raise RuntimeError("state store down too")
            return await original_resolve(invoice_id, transform)

        repo.resolve = resolve_fails_second_time
        app.dependency_overrides[deps.get_publisher] = lambda: flaky_publish
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/api/approvals/inv-doomed/verdict",
                           json={"verdict": "approved", "approver_id": "lena"})
        assert resp.status_code == 500
        critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert any("inv-doomed" in r.getMessage() for r in critical)
        assert any("manual reconciliation" in r.getMessage() for r in critical)

    async def test_remove_from_queue_failure_after_publish_still_200(self, env, caplog):
        """Publish succeeded -- the resolution and its delivery are the real
        work. A remove_from_queue failure afterwards leaves only a stale
        queue id: the self-healing queue view reaps it, and any second
        verdict gets a 409 read-back. So the endpoint logs a warning and
        still returns 200 rather than failing an already-delivered verdict."""
        client, repo, published = env
        await seed(repo, make_escalation("inv-sticky", "2024-01-01T00:00:00+00:00"))

        async def broken_remove(invoice_id):
            raise RuntimeError("queue write failed")

        repo.remove_from_queue = broken_remove

        resp = client.post("/api/approvals/inv-sticky/verdict",
                           json={"verdict": "approved", "approver_id": "lena"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert len(published) == 1
        assert any(r.levelname == "WARNING" and "inv-sticky" in r.getMessage()
                   for r in caplog.records)

        # The stale queue id is then reaped by the self-healing queue view.
        del repo.remove_from_queue  # restore the real (class) method
        queue_resp = client.get("/api/approvals/queue")
        assert queue_resp.json()["items"] == []
        assert await repo.list_queue() == []

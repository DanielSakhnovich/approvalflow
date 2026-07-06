import asyncio

import pytest
from afcommon.contracts import DecisionMadePayload
from afcommon.events import new_event_meta
from afcommon.state import InMemoryStateStore, YieldingStateStore

from services.intake.src.models import InvoiceRecord, InvoiceStatus, touch
from services.intake.src.repo import IntakeRepo
from services.intake.src.status import IllegalTransition, apply_decision


def rec() -> InvoiceRecord:
    return InvoiceRecord(
        invoice_id="inv_1", client_ref=None, correlation_id="corr_1",
        status=InvoiceStatus.EVALUATING, submission={}, created_at="t", updated_at="t",
    )


async def test_record_roundtrip():
    repo = IntakeRepo(InMemoryStateStore())
    assert await repo.get_record("inv_1") is None
    await repo.save_record(rec())
    got = await repo.get_record("inv_1")
    assert got is not None and got.status == InvoiceStatus.EVALUATING


async def test_save_overwrites_with_latest():
    repo = IntakeRepo(InMemoryStateStore())
    await repo.save_record(rec())
    updated = rec().model_copy(update={"status": InvoiceStatus.PAID})
    await repo.save_record(updated)
    assert (await repo.get_record("inv_1")).status == InvoiceStatus.PAID


async def test_first_time_event_true_exactly_once():
    repo = IntakeRepo(InMemoryStateStore())
    assert await repo.first_time_event("evt-1") is True
    assert await repo.first_time_event("evt-1") is False
    assert await repo.first_time_event("evt-2") is True


async def test_counters_accumulate():
    repo = IntakeRepo(InMemoryStateStore())
    await repo.bump_counters(submitted=1)
    await repo.bump_counters(submitted=1, paid_auto_cents=4200)
    counters = await repo.get_counters()
    assert counters["submitted"] == 2
    assert counters["paid_auto_cents"] == 4200


async def test_update_record_applies_transform_in_place():
    repo = IntakeRepo(InMemoryStateStore())
    await repo.save_record(rec())
    updated = await repo.update_record(
        "inv_1", lambda r: touch(r, status=InvoiceStatus.APPROVED))
    assert updated.status == InvoiceStatus.APPROVED
    assert (await repo.get_record("inv_1")).status == InvoiceStatus.APPROVED


async def test_update_record_unknown_invoice_raises_key_error():
    repo = IntakeRepo(InMemoryStateStore())
    with pytest.raises(KeyError):
        await repo.update_record("inv_ghost", lambda r: r)


def _decision_payload(route: str) -> DecisionMadePayload:
    return DecisionMadePayload(
        meta=new_event_meta("inv_1", "corr_1"), route=route, recommendation="approve",
        confidence=0.95, violations=[], reasoning="In policy.", usd_cents=4200,
        ceiling_cents=25000,
    )


async def test_concurrent_update_record_one_wins_one_sees_illegal_transition():
    # Two concurrent transitions on the SAME invoice, each legal only from
    # EVALUATING. Proves update_record's CAS retry loop re-reads a fresh
    # record on conflict rather than replaying a stale one: whichever task
    # loses the race must observe the *other* task's already-applied status
    # change and raise IllegalTransition, not silently overwrite it.
    store = YieldingStateStore()
    repo = IntakeRepo(store)
    await repo.save_record(rec())  # status == EVALUATING

    async def attempt(route: str):
        payload = _decision_payload(route)
        return await repo.update_record("inv_1", lambda r: apply_decision(r, payload))

    results = await asyncio.gather(
        attempt("auto_approve"), attempt("reject"), return_exceptions=True)

    successes = [r for r in results if isinstance(r, InvoiceRecord)]
    failures = [r for r in results if isinstance(r, IllegalTransition)]
    assert len(successes) == 1
    assert len(failures) == 1

    final = await repo.get_record("inv_1")
    assert final.status == successes[0].status

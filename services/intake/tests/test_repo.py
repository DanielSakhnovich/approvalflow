from afcommon.state import InMemoryStateStore

from services.intake.src.models import InvoiceRecord, InvoiceStatus
from services.intake.src.repo import IntakeRepo


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

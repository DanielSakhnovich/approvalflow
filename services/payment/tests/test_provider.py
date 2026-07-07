"""Task 3: MockPaymentProvider idempotency and fixture-driven failure injection.

Idempotency contract: `provider:{invoice_id}` is a first-write record. The
first successful execute() for an invoice writes it and returns a ref; every
later execute() for the same invoice_id returns that SAME ref without
re-executing (no double payment). Injection is checked BEFORE the record is
written, so a payment that keeps declining (marker never set) keeps raising
PaymentDeclined on every retry while the cause is present -- but the moment
it clears, the very next call succeeds and becomes permanently idempotent.
"""

import pytest
from afcommon.state import InMemoryStateStore

from services.payment.src.provider import MockPaymentProvider, PaymentDeclined


async def test_execute_returns_a_ref():
    provider = MockPaymentProvider(InMemoryStateStore(), injection_enabled=False)
    ref = await provider.execute("inv-1", 10000, scenario="")
    assert isinstance(ref, str)
    assert ref


async def test_execute_is_idempotent_same_invoice_returns_same_ref_one_record():
    store = InMemoryStateStore()
    provider = MockPaymentProvider(store, injection_enabled=False)
    ref1 = await provider.execute("inv-1", 10000, scenario="")
    ref2 = await provider.execute("inv-1", 10000, scenario="")
    assert ref1 == ref2
    value, _ = await store.get("provider:inv-1")
    assert value == {"ref": ref1}


async def test_execute_idempotent_even_with_different_amount_on_retry():
    """Once a record exists, the cached ref is returned regardless of the
    (should-be-identical) amount passed on retry -- no re-execution."""
    store = InMemoryStateStore()
    provider = MockPaymentProvider(store, injection_enabled=False)
    ref1 = await provider.execute("inv-1", 10000, scenario="")
    ref2 = await provider.execute("inv-1", 99999, scenario="")
    assert ref1 == ref2


async def test_injection_enabled_with_matching_scenario_declines_and_writes_no_record():
    store = InMemoryStateStore()
    provider = MockPaymentProvider(store, injection_enabled=True)
    with pytest.raises(PaymentDeclined):
        await provider.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    value, _ = await store.get("provider:inv-1")
    assert value is None


async def test_injection_enabled_retry_still_declines_while_scenario_persists():
    store = InMemoryStateStore()
    provider = MockPaymentProvider(store, injection_enabled=True)
    with pytest.raises(PaymentDeclined):
        await provider.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    with pytest.raises(PaymentDeclined):
        await provider.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    value, _ = await store.get("provider:inv-1")
    assert value is None


async def test_injection_disabled_with_matching_scenario_succeeds_double_gate():
    """The gate is an AND: scenario alone (without injection_enabled) never
    declines."""
    provider = MockPaymentProvider(InMemoryStateStore(), injection_enabled=False)
    ref = await provider.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    assert ref


async def test_injection_enabled_with_non_matching_scenario_succeeds():
    """The gate is an AND: injection_enabled alone (without a matching
    scenario) never declines."""
    provider = MockPaymentProvider(InMemoryStateStore(), injection_enabled=True)
    ref = await provider.execute("inv-1", 10000, scenario="happy-path")
    assert ref


async def test_injection_enabled_no_scenario_succeeds():
    provider = MockPaymentProvider(InMemoryStateStore(), injection_enabled=True)
    ref = await provider.execute("inv-1", 10000, scenario="")
    assert ref


async def test_execute_after_cause_clears_succeeds_and_is_then_stably_idempotent():
    """A declined payment must be retryable once the injection cause clears,
    since no record was ever written while it kept declining."""
    store = InMemoryStateStore()
    declining = MockPaymentProvider(store, injection_enabled=True)
    with pytest.raises(PaymentDeclined):
        await declining.execute("inv-1", 10000, scenario="payment-failure:journey-D")

    cleared = MockPaymentProvider(store, injection_enabled=False)
    ref = await cleared.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    assert ref
    ref_again = await cleared.execute("inv-1", 10000, scenario="payment-failure:journey-D")
    assert ref_again == ref


async def test_execute_is_per_invoice_not_global():
    provider = MockPaymentProvider(InMemoryStateStore(), injection_enabled=False)
    ref_a = await provider.execute("inv-a", 10000, scenario="")
    ref_b = await provider.execute("inv-b", 10000, scenario="")
    assert ref_a != ref_b

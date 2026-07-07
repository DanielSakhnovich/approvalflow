"""MockPaymentProvider (Task 3): idempotent payment execution, with a
fixture-driven failure-injection hook for the harness's payment-decline
scenarios.

Idempotency: `provider:{invoice_id}` is a first-write record keyed by
invoice_id (not event_id -- the saga is the at-least-once boundary, this is
its downstream effect). The FIRST successful execute() for an invoice writes
{"ref": ...} and returns that ref; every later execute() call for the same
invoice_id returns the SAME ref without re-executing (no double payment).

Failure injection is checked and raised BEFORE the record is written, so a
payment that keeps getting declined (the marker is never set) keeps raising
PaymentDeclined on every retry while the injected cause is still present --
but the moment the cause clears (injection disabled, or the scenario no
longer matches), the very next call succeeds and becomes permanently
idempotent, exactly like any other execute().
"""

import uuid

from afcommon.state import StateStore, try_register

_PROVIDER_KEY_PREFIX = "provider:"


class PaymentDeclined(Exception):
    """Raised when the provider declines a payment (fixture-driven injection)."""


class MockPaymentProvider:
    def __init__(self, store: StateStore, injection_enabled: bool):
        self._store = store
        self._injection_enabled = injection_enabled

    async def execute(self, invoice_id: str, amount_cents: int, scenario: str) -> str:
        key = f"{_PROVIDER_KEY_PREFIX}{invoice_id}"
        existing, _ = await self._store.get(key)
        if existing is not None:
            return existing["ref"]

        # Double-gated: injection only bites when BOTH the harness flag is on
        # AND this specific invoice's scenario asks for it. Checked before any
        # write, so a declined payment leaves no record behind and stays
        # retryable once the cause clears.
        if self._injection_enabled and scenario.startswith("payment-failure"):
            raise PaymentDeclined(f"payment declined for invoice_id={invoice_id}")

        ref = f"pay_{uuid.uuid4().hex}"
        if not await try_register(self._store, key, {"ref": ref}):
            # Lost a first-write race to a concurrent execute() for the same
            # invoice_id: defer to whichever ref actually won, never return
            # our own throwaway one.
            existing, _ = await self._store.get(key)
            return existing["ref"]
        return ref

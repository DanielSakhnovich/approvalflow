"""
Vendor trust history (D-012's uplift tier, D-014 point 7): decision-svc owns
trust because trust is decision data, not money -- payment stays pure money
and never learns about vendors/categories (its `PaymentCompletedPayload`
carries only `amount_cents`/`budget_remaining_cents`/`department`, D-015 own-
keys). Trust is earned per vendor+category, keyed
`trust:{vendor.lower()}|{category.lower()}`, credited only once a payment for
that invoice has actually completed (never on submission or on the agent's
say-so).

Bridging the payment-completed event (invoice_id only) back to the vendor +
category it belongs to needs a small invoice_id -> (vendor, category) cache,
since the event itself carries neither. That cache is stored under decision-
svc's own `trust:*` key prefix too (D-015: decision-svc owns exactly
`fp:*`/`trust:*`/`config:*`/`processed:*`, so no new key prefix is
introduced) -- `remember_invoice` populates it at decision time,
`record_paid_for_invoice` resolves + credits it when payment completes.
"""

from afcommon.state import StateStore, cas_update


def _trust_key(vendor: str, category: str) -> str:
    return f"trust:{vendor.lower()}|{category.lower()}"


def _invoice_key(invoice_id: str) -> str:
    return f"trust:invoice:{invoice_id}"


class TrustRepo:
    def __init__(self, store: StateStore):
        self._store = store

    async def is_trusted(self, vendor: str, category: str) -> bool:
        value, _ = await self._store.get(_trust_key(vendor, category))
        return bool(value)

    async def record_paid(self, vendor: str, category: str) -> None:
        await cas_update(self._store, _trust_key(vendor, category), lambda _: {"paid": True})

    async def remember_invoice(self, invoice_id: str, vendor: str, category: str) -> None:
        """Cache `invoice_id`'s vendor/category so a later payment-completed
        event (which carries neither) can still resolve which trust bucket
        to credit."""
        await cas_update(
            self._store,
            _invoice_key(invoice_id),
            lambda _: {"vendor": vendor, "category": category},
        )

    async def record_paid_for_invoice(self, invoice_id: str) -> None:
        """Resolve `invoice_id` -> (vendor, category) via the cache written
        by `remember_invoice` at decision time, then credit that bucket.
        A no-op (not an error) if the invoice was never cached -- e.g. state
        was cleared, or payment-completed arrived for something decision-svc
        never actually decided on -- matching the always-ack posture for
        payment events."""
        cached, _ = await self._store.get(_invoice_key(invoice_id))
        if cached is None:
            return
        await self.record_paid(cached["vendor"], cached["category"])

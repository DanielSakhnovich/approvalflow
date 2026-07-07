"""THE money store (INV-1014 no-overspend guarantee).

BudgetStore seeds per-department budgets from sample-invoices.json and
protects every debit/credit with a compare-and-swap loop against the
StateStore's etag, so concurrent reserve() calls against the same
department can never both succeed against a budget that can't cover both.

reserve_once() closes a second race that plain reserve() cannot: two
truly-concurrent callers for the SAME invoice_id (e.g. two live saga
handlers racing on one invoice, or a resumed/redelivered handler racing a
still-in-flight one) would each pass reserve()'s own CAS guard and each
decrement the budget -- CAS only prevents overspend *within* a single
reserve() call, it says nothing about calling reserve() twice for what is
logically one payment. `reservation:{invoice_id}` is a first-write claim,
mirroring the provider's idempotency record: the FIRST caller to claim it
performs the actual reserve() and caches the outcome; every other caller
for that invoice_id polls until the claim-holder finishes and returns the
SAME cached outcome, never calling reserve() itself.
"""

import asyncio
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from afcommon.state import CasConflict, StateStore, cas_update, try_register

_BUDGET_KEY_PREFIX = "budget:"
_RESERVATION_KEY_PREFIX = "reservation:"
_MAX_RETRIES = 10
_RESERVE_ONCE_MAX_POLLS = 200


def _find_repo_file(name: str) -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / name).exists():
            return parent / name
    raise FileNotFoundError(name)


def to_cents(amount: float) -> int:
    """Decimal-safe dollars -> integer cents, half-up (avoids float drift)."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class BudgetStore:
    def __init__(self, store: StateStore):
        self._store = store
        self._seeded = False

    async def seed_if_absent(self) -> None:
        """Idempotent: register each department's starting budget only if it
        doesn't already exist, so a restart (or a lazy pre-reserve call)
        never resets a budget that has already been spent against."""
        if self._seeded:
            return
        budgets = json.loads(_find_repo_file("sample-invoices.json").read_text())["budgets"]
        for dept, amount in budgets.items():
            await try_register(self._store, _key(dept), {"remaining_cents": to_cents(amount)})
        self._seeded = True

    async def get_remaining(self, dept: str) -> int | None:
        await self.seed_if_absent()
        value, _ = await self._store.get(_key(dept))
        return None if value is None else value["remaining_cents"]

    async def reserve(self, dept: str, amount_cents: int) -> bool:
        """CAS loop: succeeds and decrements iff remaining >= amount_cents.
        Returns False (budget unchanged) if insufficient or dept unknown.
        Raises CasConflict if retries are exhausted under contention."""
        if amount_cents <= 0:
            raise ValueError(f"amount_cents must be positive, got {amount_cents}")
        await self.seed_if_absent()
        key = _key(dept)
        for _ in range(_MAX_RETRIES):
            value, etag = await self._store.get(key)
            if value is None:
                return False
            remaining = value["remaining_cents"]
            if remaining < amount_cents:
                return False
            new_remaining = remaining - amount_cents
            if new_remaining < 0:
                raise ValueError(f"budget {dept} would go negative")
            if await self._store.try_save(key, {"remaining_cents": new_remaining}, etag):
                return True
        raise CasConflict(f"CAS on '{key}' failed after {_MAX_RETRIES} retries")

    async def release(self, dept: str, amount_cents: int) -> None:
        """CAS add-back. Unknown department: no-op, never crashes."""
        if amount_cents <= 0:
            raise ValueError(f"amount_cents must be positive, got {amount_cents}")
        await self.seed_if_absent()
        key = _key(dept)
        for _ in range(_MAX_RETRIES):
            value, etag = await self._store.get(key)
            if value is None:
                return
            new_remaining = value["remaining_cents"] + amount_cents
            if await self._store.try_save(key, {"remaining_cents": new_remaining}, etag):
                return
        raise CasConflict(f"CAS on '{key}' failed after {_MAX_RETRIES} retries")

    async def reserve_once(self, dept: str, amount_cents: int, invoice_id: str) -> bool:
        """Per-invoice idempotent reserve: at most one real decrement ever
        happens for a given invoice_id, no matter how many concurrent or
        resumed callers ask for it.

        The first caller to claim `reservation:{invoice_id}` (first-write
        via try_register) is the ONLY one that ever calls reserve() -- every
        other caller sees the claim already taken and polls the reservation
        record until the claim-holder finishes, then returns that cached
        True/False outcome. This mirrors MockPaymentProvider's idempotency
        record, but for the budget decrement rather than the provider call.
        """
        key = _reservation_key(invoice_id)
        for _ in range(_RESERVE_ONCE_MAX_POLLS):
            existing, _ = await self._store.get(key)
            if existing is not None:
                if existing.get("pending"):
                    await asyncio.sleep(0)
                    continue
                return existing["ok"]
            if await try_register(self._store, key, {"pending": True}):
                ok = await self.reserve(dept, amount_cents)
                await cas_update(self._store, key, lambda _v, ok=ok: {"pending": False, "ok": ok})
                return ok
            # Lost the claim race to a concurrent caller that got there just
            # after our get() saw nothing; loop back and observe its outcome.
        raise CasConflict(f"reserve_once polling exhausted for invoice_id={invoice_id}")

    async def clear_reservation(self, invoice_id: str) -> None:
        """Delete the reservation:{invoice_id} record. Called after a
        compensating release() so a released invoice's reservation slot
        doesn't linger -- harmless no-op if no reservation was ever made."""
        await self._store.delete(_reservation_key(invoice_id))


def _key(dept: str) -> str:
    return f"{_BUDGET_KEY_PREFIX}{dept}"


def _reservation_key(invoice_id: str) -> str:
    return f"{_RESERVATION_KEY_PREFIX}{invoice_id}"

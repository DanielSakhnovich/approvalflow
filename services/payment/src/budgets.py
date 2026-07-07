"""THE money store (INV-1014 no-overspend guarantee).

BudgetStore seeds per-department budgets from sample-invoices.json and
protects every debit/credit with a compare-and-swap loop against the
StateStore's etag, so concurrent reserve() calls against the same
department can never both succeed against a budget that can't cover both.
"""

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from afcommon.state import CasConflict, StateStore, try_register

_BUDGET_KEY_PREFIX = "budget:"
_MAX_RETRIES = 10


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
            assert new_remaining >= 0, "budget invariant violated: negative remaining"
            if await self._store.try_save(key, {"remaining_cents": new_remaining}, etag):
                return True
        raise CasConflict(f"CAS on '{key}' failed after {_MAX_RETRIES} retries")

    async def release(self, dept: str, amount_cents: int) -> None:
        """CAS add-back. Unknown department: no-op, never crashes."""
        await self.seed_if_absent()
        key = _key(dept)
        for _ in range(_MAX_RETRIES):
            value, etag = await self._store.get(key)
            if value is None:
                return
            new_remaining = value["remaining_cents"] + amount_cents
            assert new_remaining >= 0, "budget invariant violated: negative remaining"
            if await self._store.try_save(key, {"remaining_cents": new_remaining}, etag):
                return
        raise CasConflict(f"CAS on '{key}' failed after {_MAX_RETRIES} retries")


def _key(dept: str) -> str:
    return f"{_BUDGET_KEY_PREFIX}{dept}"

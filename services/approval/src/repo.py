from collections.abc import Callable

from afcommon.state import cas_update, try_register

from .models import Escalation


class ApprovalRepo:
    def __init__(self, store):
        self._store = store

    async def save_new(self, esc: Escalation) -> bool:
        """Save a new escalation. Returns False if it already exists (idempotent)."""
        key = f"escalation:{esc.invoice_id}"
        return await try_register(self._store, key, esc.model_dump())

    async def get(self, invoice_id: str) -> Escalation | None:
        """Retrieve an escalation by invoice_id. Returns None if not found."""
        key = f"escalation:{invoice_id}"
        value, _ = await self._store.get(key)
        if value is None:
            return None
        return Escalation(**value)

    async def resolve(
        self, invoice_id: str, transform: Callable[[Escalation], Escalation]
    ) -> Escalation:
        """Apply a CAS transform to resolve an escalation. Raises KeyError if missing."""
        key = f"escalation:{invoice_id}"

        # Check that the escalation exists before starting CAS loop
        existing, _ = await self._store.get(key)
        if existing is None:
            raise KeyError(invoice_id)

        # Use CAS to apply the transform
        def update_fn(value):
            esc = Escalation(**value)
            resolved = transform(esc)
            return resolved.model_dump()

        result_data = await cas_update(self._store, key, update_fn)
        return Escalation(**result_data)

    async def add_to_queue(self, invoice_id: str) -> None:
        """Add an invoice_id to the pending queue (idempotent)."""
        key = "queue:pending"

        def update_fn(value):
            if value is None:
                queue = []
            else:
                queue = list(value)

            # Idempotent: only add if not already present
            if invoice_id not in queue:
                queue.append(invoice_id)

            return queue

        await cas_update(self._store, key, update_fn)

    async def remove_from_queue(self, invoice_id: str) -> None:
        """Remove an invoice_id from the pending queue (idempotent)."""
        key = "queue:pending"

        def update_fn(value):
            if value is None:
                queue = []
            else:
                queue = list(value)

            # Idempotent: only remove if present
            if invoice_id in queue:
                queue.remove(invoice_id)

            return queue

        await cas_update(self._store, key, update_fn)

    async def list_queue(self) -> list[str]:
        """List all invoice_ids in the pending queue."""
        key = "queue:pending"
        value, _ = await self._store.get(key)
        if value is None:
            return []
        return list(value)

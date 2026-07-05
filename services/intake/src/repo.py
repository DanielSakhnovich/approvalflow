from collections.abc import Callable

from afcommon.state import StateStore, cas_update, try_register

from .models import InvoiceRecord

_COUNTERS_KEY = "dashboard:counters"


class IntakeRepo:
    def __init__(self, store: StateStore):
        self._store = store

    async def save_record(self, record: InvoiceRecord) -> None:
        await cas_update(self._store, f"invoice:{record.invoice_id}",
                         lambda _: record.model_dump())

    async def update_record(self, invoice_id: str,
                            transform: Callable[[InvoiceRecord], InvoiceRecord],
                            ) -> InvoiceRecord:
        """Read-modify-write inside the CAS retry loop.

        Unlike get_record + save_record (non-atomic), this re-reads the
        record on every CAS retry, so concurrent transitions on the same
        invoice cannot silently overwrite each other - the loser observes
        the winner's already-applied change and its transform raises
        IllegalTransition instead of stomping it.
        """
        def apply(current: dict | None) -> dict:
            if current is None:
                raise KeyError(f"invoice {invoice_id} not found")
            return transform(InvoiceRecord.model_validate(current)).model_dump()
        result = await cas_update(self._store, f"invoice:{invoice_id}", apply)
        return InvoiceRecord.model_validate(result)

    async def get_record(self, invoice_id: str) -> InvoiceRecord | None:
        value, _ = await self._store.get(f"invoice:{invoice_id}")
        return InvoiceRecord.model_validate(value) if value is not None else None

    async def first_time_event(self, event_id: str) -> bool:
        return await try_register(self._store, f"processed:{event_id}", {"seen": True})

    async def bump_counters(self, **deltas: int) -> dict:
        def bump(current: dict | None) -> dict:
            counters = dict(current or {})
            for key, delta in deltas.items():
                counters[key] = counters.get(key, 0) + delta
            return counters
        return await cas_update(self._store, _COUNTERS_KEY, bump)

    async def get_counters(self) -> dict:
        value, _ = await self._store.get(_COUNTERS_KEY)
        return value or {}

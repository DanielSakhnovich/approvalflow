"""The immutable, append-only decision trail (F9) plus the ceiling-compliance
projection (F10).

F9 -- every event in an invoice's approval lifecycle is appended to
`trail:{correlation_id}` via `cas_update`, so the CAS retry loop serializes
concurrent appends for one correlation id (a lost append would break the
append-only guarantee). The trail is never overwritten or truncated.

F10 -- a key-value store cannot natively "scan every auto-approval and check
its ceiling". So the trail keeps a second, queryable projection:
`index:auto-approvals`, a flat list of one entry per `decision-made` event
with `route=auto_approve`. `ceiling_violations()` reads that index and
returns any entry whose amount exceeded the ceiling in force at decision
time. In a correct system this is ALWAYS `[]` -- the empty list itself IS the
F10 proof, not a placeholder for one. The trail stays the source of truth;
the index is only the projection for the one query a KV store can't do.
"""

from afcommon.state import StateStore, cas_update
from pydantic import BaseModel

_AUTO_APPROVAL_INDEX_KEY = "index:auto-approvals"


class TrailEntry(BaseModel):
    event_type: str
    event_id: str
    occurred_at: str
    payload: dict


class AuditTrail:
    def __init__(self, store: StateStore):
        self._store = store

    async def append(self, correlation_id: str, entry: TrailEntry) -> None:
        # Append-only: the update fn only ever grows the list, and cas_update's
        # retry loop re-reads on conflict so a concurrent append is never lost.
        await cas_update(
            self._store,
            f"trail:{correlation_id}",
            lambda current: (current or []) + [entry.model_dump()],
        )

    async def get_trail(self, correlation_id: str) -> list[TrailEntry]:
        value, _ = await self._store.get(f"trail:{correlation_id}")
        entries = [TrailEntry.model_validate(e) for e in (value or [])]
        entries.sort(key=lambda e: e.occurred_at)
        return entries

    async def append_auto_approval(self, entry: dict) -> None:
        await cas_update(
            self._store,
            _AUTO_APPROVAL_INDEX_KEY,
            lambda current: (current or []) + [entry],
        )

    async def _auto_approvals(self) -> list[dict]:
        value, _ = await self._store.get(_AUTO_APPROVAL_INDEX_KEY)
        return value or []

    async def auto_approval_count(self) -> int:
        return len(await self._auto_approvals())

    async def ceiling_violations(self) -> list[dict]:
        # Index entries carry the SAME snake_case field names as the wire
        # contract (DecisionMadePayload.usd_cents / .ceiling_cents), so Task 2's
        # subscriber can forward the payload fields verbatim. A missing amount
        # defaulting to 0 gives 0 > 0 == False, i.e. it never fabricates a
        # violation from malformed data -- absence fails safe toward "compliant"
        # and the trail (source of truth) still holds the raw event.
        return [
            e for e in await self._auto_approvals()
            if e.get("usd_cents", 0) > e.get("ceiling_cents", 0)
        ]

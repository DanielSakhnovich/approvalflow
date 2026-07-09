from afcommon.dedupe import EventDedupe
from afcommon.state import DaprStateStore

from .trail import AuditTrail

_trail: AuditTrail | None = None
_dedupe: EventDedupe | None = None


def get_trail() -> AuditTrail:
    global _trail
    if _trail is None:
        # D-017: the audit trail lives on its own Postgres-backed Dapr state
        # component. Same afcommon DaprStateStore class, different component
        # name -- the store swap is configuration, not code.
        _trail = AuditTrail(DaprStateStore(store_name="statestore-audit"))
    return _trail


def get_dedupe() -> EventDedupe:
    global _dedupe
    if _dedupe is None:
        # Dedupe marks live on the OPERATIONAL (Redis) store, not the audit
        # Postgres store -- they're transient bookkeeping, not part of the trail.
        _dedupe = EventDedupe(DaprStateStore())
    return _dedupe

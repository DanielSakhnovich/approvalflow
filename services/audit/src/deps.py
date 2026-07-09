from afcommon.state import DaprStateStore

from .trail import AuditTrail

_trail: AuditTrail | None = None


def get_trail() -> AuditTrail:
    global _trail
    if _trail is None:
        # D-017: the audit trail lives on its own Postgres-backed Dapr state
        # component. Same afcommon DaprStateStore class, different component
        # name -- the store swap is configuration, not code.
        _trail = AuditTrail(DaprStateStore(store_name="statestore-audit"))
    return _trail

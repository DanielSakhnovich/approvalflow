from collections.abc import Awaitable, Callable

from afcommon.events import publish
from afcommon.state import DaprStateStore

from .audit_client import AuditInvokeClient
from .repo import IntakeRepo

Publisher = Callable[[str, dict], Awaitable[None]]

_repo: IntakeRepo | None = None
_audit_client: AuditInvokeClient | None = None


def get_repo() -> IntakeRepo:
    global _repo
    if _repo is None:
        _repo = IntakeRepo(DaprStateStore())
    return _repo


def get_publisher() -> Publisher:
    return publish


def get_audit_client() -> AuditInvokeClient:
    global _audit_client
    if _audit_client is None:
        _audit_client = AuditInvokeClient()
    return _audit_client

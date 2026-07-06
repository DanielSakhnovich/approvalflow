from collections.abc import Awaitable, Callable

from afcommon.dedupe import EventDedupe
from afcommon.events import publish
from afcommon.state import DaprStateStore

from .repo import ApprovalRepo

Publisher = Callable[[str, dict], Awaitable[None]]

_repo: ApprovalRepo | None = None
_dedupe: EventDedupe | None = None


def get_repo() -> ApprovalRepo:
    global _repo
    if _repo is None:
        _repo = ApprovalRepo(DaprStateStore())
    return _repo


def get_dedupe() -> EventDedupe:
    global _dedupe
    if _dedupe is None:
        _dedupe = EventDedupe(DaprStateStore())
    return _dedupe


def get_publisher() -> Publisher:
    return publish

from afcommon.dedupe import EventDedupe
from afcommon.state import DaprStateStore

from .repo import ApprovalRepo

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

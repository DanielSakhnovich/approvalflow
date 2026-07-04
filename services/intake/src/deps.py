from collections.abc import Awaitable, Callable

from afcommon.events import publish
from afcommon.state import DaprStateStore

from .repo import IntakeRepo

Publisher = Callable[[str, dict], Awaitable[None]]

_repo: IntakeRepo | None = None


def get_repo() -> IntakeRepo:
    global _repo
    if _repo is None:
        _repo = IntakeRepo(DaprStateStore())
    return _repo


def get_publisher() -> Publisher:
    return publish

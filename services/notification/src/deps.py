from afcommon.dedupe import EventDedupe
from afcommon.state import DaprStateStore

from .processor import NotificationProcessor

_processor: NotificationProcessor | None = None
_dedupe: EventDedupe | None = None


def get_processor() -> NotificationProcessor:
    global _processor
    if _processor is None:
        _processor = NotificationProcessor()
    return _processor


def get_dedupe() -> EventDedupe:
    global _dedupe
    if _dedupe is None:
        _dedupe = EventDedupe(DaprStateStore())
    return _dedupe

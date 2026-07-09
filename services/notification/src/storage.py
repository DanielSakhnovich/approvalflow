from datetime import datetime

from .models import FAILED, RETRY_PENDING, SENT, Notification
from .segmenter import min_sms_segments

notifications = []   # preserves insertion order for get_all()
_by_id = {}          # id -> Notification, for O(1) lookup
next_id = 1


def add_notification(target_channels, message):
    global next_id
    n = Notification(next_id, target_channels, message)
    if any(c.get("type") == "sms" for c in target_channels):
        n.smsSegments = min_sms_segments(message)
    next_id += 1
    notifications.append(n)
    _by_id[n.id] = n
    return n


def get_all():
    return notifications


def find_by_id(nid):
    return _by_id.get(nid)


def seed():
    global next_id
    notifications.clear()
    _by_id.clear()
    next_id = 1

    n1 = add_notification(
        [{"type": "email", "value": "alice@example.com"}], "Welcome to the platform")
    n1.status = SENT
    n1.attempts = 1
    n1.lastAttemptAt = datetime.now().isoformat()
    n1.lastError = "[email] accepted for delivery"

    add_notification([{"type": "sms", "value": "12345"}], "Short number")

    n3 = add_notification([{"type": "push", "value": "device-abc"}], "Your ride is here")
    n3.status = FAILED
    n3.attempts = 1
    n3.lastAttemptAt = datetime.now().isoformat()
    n3.lastError = "[push] device token rejected"

    add_notification(
        [
            {"type": "email", "value": "bob@example.com"},
            {"type": "sms", "value": "+15551234567"},
        ],
        "2FA code 4242",
    )

    n5 = add_notification(
        [
            {"type": "sms", "value": "+15559876543"},
            {"type": "push", "value": "device-xyz"},
            {"type": "email", "value": "carol@example.com"},
        ],
        "Order shipped",
    )
    n5.status = RETRY_PENDING
    n5.attempts = 2
    n5.lastAttemptAt = datetime.now().isoformat()
    n5.lastError = "[sms] temporary outage, retry later"


def clear():
    """Reset the in-memory store to empty (test isolation). Distinct from
    seed(), which populates sample notifications; the running service never
    seeds (D-006 adaptation)."""
    global next_id
    notifications.clear()
    _by_id.clear()
    next_id = 1

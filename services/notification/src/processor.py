import asyncio
import threading
from datetime import datetime

from . import storage
from .models import FAILED, PENDING, PROCESSING, RETRY_PENDING, SENT
from .providers import email_provider, push_provider, sms_provider

# Channel type -> provider module. We call `module.send` at delivery time (not a captured
# reference) so the external vendor stays swappable and testable at its boundary.
_PROVIDERS = {
    "email": email_provider,
    "sms": sms_provider,
    "push": push_provider,
}

MAX_ATTEMPTS = 3

# Worse outcomes win when a notification fans out to several channels.
_STATUS_PRIORITY = {SENT: 0, RETRY_PENDING: 1, FAILED: 2}

# Claiming a notification (flip to PROCESSING) must be atomic. Flask[async] runs each request
# in its own thread, so this is a threading.Lock (not asyncio.Lock) to serialize across threads.
_claim_lock = threading.Lock()


def _result_to_status(result):
    if result == "Success":
        return SENT
    if result == "TemporaryFailure":
        return RETRY_PENDING
    return FAILED  # PermanentFailure, InvalidRequest, or anything unexpected


async def _deliver(channel, message):
    """Deliver to one channel; return (status, message). Never raises for a known channel."""
    module = _PROVIDERS.get(channel.get("type"))
    if module is None:
        return FAILED, f"Unknown channel: {channel.get('type')}"
    payload = {"recipient": channel.get("value"), "message": message}
    response = await asyncio.to_thread(module.send, payload)
    return _result_to_status(response.get("Result")), response.get("Message")


class NotificationProcessor:
    def try_claim(self, n):
        """Atomically claim a notification for sending. Returns False if already processing."""
        with _claim_lock:
            if n.status == PROCESSING:
                return False
            n.status = PROCESSING
            return True

    async def send_one(self, n):
        n.status = PROCESSING
        n.attempts += 1
        n.lastAttemptAt = datetime.now().isoformat()

        if not n.targetChannels:
            n.status = FAILED
            n.lastError = "No target channels"
            return

        try:
            # All channels are delivered concurrently; sync providers run in threads.
            outcomes = await asyncio.gather(
                *[_deliver(ch, n.message) for ch in n.targetChannels]
            )
        except Exception as exc:  # a provider blew up — don't leave it stuck PROCESSING
            n.status = FAILED
            n.lastError = str(exc)
            return

        worst = SENT
        for status, _ in outcomes:
            if _STATUS_PRIORITY[status] > _STATUS_PRIORITY[worst]:
                worst = status
        worst_message = next((m for s, m in outcomes if s == worst), None)

        if worst == RETRY_PENDING and n.attempts >= MAX_ATTEMPTS:
            worst = FAILED
            worst_message = f"Giving up after {n.attempts} attempts"

        n.status = worst
        n.lastError = worst_message

    async def send_all(self):
        # Snapshot and claim sendable items atomically; deliver outside the lock.
        with _claim_lock:
            sendable = [
                n for n in storage.get_all() if n.status in (PENDING, RETRY_PENDING)
            ]
            for n in sendable:
                n.status = PROCESSING
        await asyncio.gather(*[self.send_one(n) for n in sendable])

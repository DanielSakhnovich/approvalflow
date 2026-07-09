from datetime import datetime

PENDING = "pending"
PROCESSING = "processing"
SENT = "sent"
RETRY_PENDING = "retry_pending"
FAILED = "failed"


class Notification:
    def __init__(self, nid, target_channels, message):
        self.id = nid
        self.targetChannels = target_channels
        self.message = message
        self.status = PENDING
        self.createdAt = datetime.now().isoformat()
        self.attempts = 0
        self.lastAttemptAt = None
        self.lastError = None
        self.smsSegments = 0

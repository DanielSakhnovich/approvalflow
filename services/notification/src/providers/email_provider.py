# Email provider integration
import random


def send(req):
    recipient = req.get("recipient", "")
    message = req.get("message", "")
    if not recipient or not message or "@" not in recipient:
        return {
            "Result": "InvalidRequest",
            "ErrorCode": "EMAIL_INVALID_ADDRESS",
            "Message": "[email] invalid recipient address",
        }
    r = random.random()
    if r < 0.6:
        return {
            "Result": "Success",
            "ErrorCode": "EMAIL_OK",
            "Message": "[email] accepted for delivery",
        }
    if r < 0.85:
        code = "EMAIL_TEMP_001" if random.random() < 0.5 else "EMAIL_TEMP_002"
        return {
            "Result": "TemporaryFailure",
            "ErrorCode": code,
            "Message": "[email] temporary outage, retry later",
        }
    code = "EMAIL_PERM_BOUNCED" if random.random() < 0.5 else "EMAIL_PERM_BLOCKED"
    return {
        "Result": "PermanentFailure",
        "ErrorCode": code,
        "Message": "[email] permanent delivery failure",
    }

# Push provider integration
import random


def send(req):
    recipient = req.get("recipient", "")
    message = req.get("message", "")
    if not recipient or not message:
        return {
            "Result": "InvalidRequest",
            "ErrorCode": "PUSH_INVALID_DEVICE",
            "Message": "[push] invalid device token",
        }
    r = random.random()
    if r < 0.45:
        return {
            "Result": "Success",
            "ErrorCode": "PUSH_OK",
            "Message": "[push] notification delivered",
        }
    if r < 0.7:
        code = "PUSH_TEMP_001" if random.random() < 0.5 else "PUSH_TEMP_002"
        return {
            "Result": "TemporaryFailure",
            "ErrorCode": code,
            "Message": "[push] temporary outage, retry later",
        }
    code = "PUSH_PERM_REJECTED" if random.random() < 0.5 else "PUSH_PERM_EXPIRED"
    return {
        "Result": "PermanentFailure",
        "ErrorCode": code,
        "Message": "[push] device token rejected",
    }

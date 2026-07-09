# SMS provider integration
import random


def send(req):
    recipient = req.get("recipient", "")
    message = req.get("message", "")
    if not recipient or not message or len(recipient) < 7:
        return {
            "Result": "InvalidRequest",
            "ErrorCode": "SMS_INVALID_PHONE",
            "Message": "[sms] invalid phone number",
        }
    r = random.random()
    if r < 0.5:
        return {
            "Result": "Success",
            "ErrorCode": "SMS_OK",
            "Message": "[sms] message delivered",
        }
    if r < 0.8:
        code = "SMS_TEMP_001" if random.random() < 0.5 else "SMS_TEMP_002"
        return {
            "Result": "TemporaryFailure",
            "ErrorCode": code,
            "Message": "[sms] temporary outage, retry later",
        }
    code = "SMS_PERM_BLOCKED" if random.random() < 0.5 else "SMS_PERM_UNREACHABLE"
    return {
        "Result": "PermanentFailure",
        "ErrorCode": code,
        "Message": "[sms] permanent delivery failure",
    }

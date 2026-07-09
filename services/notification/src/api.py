from fastapi import APIRouter

from . import storage

router = APIRouter()


@router.get("/notifications")
async def list_notifications() -> list[dict]:
    # Read view for the smoke and any operator/UI: what was delivered, and how
    # it went. The vendored Notification is a plain object; expose its __dict__.
    return [n.__dict__ for n in storage.get_all()]

from fastapi import APIRouter, Depends

from .deps import get_trail
from .trail import AuditTrail

router = APIRouter()


@router.get("/trail/{correlation_id}")
async def get_trail_view(correlation_id: str,
                         trail: AuditTrail = Depends(get_trail)) -> dict:
    # F9: the full decision trail for a correlation id. An unknown id is not
    # an error -- the absence of a trail is a valid answer (empty list, 200).
    entries = await trail.get_trail(correlation_id)
    return {"correlationId": correlation_id,
            "entries": [e.model_dump() for e in entries]}


@router.get("/audit/ceiling-compliance")
async def ceiling_compliance(trail: AuditTrail = Depends(get_trail)) -> dict:
    # F10: prove the system never auto-approved above the configured ceiling.
    # An empty violations list over a non-zero checked count IS the proof.
    checked = await trail.auto_approval_count()
    violations = await trail.ceiling_violations()
    return {"autoApprovalsChecked": checked, "violations": violations}

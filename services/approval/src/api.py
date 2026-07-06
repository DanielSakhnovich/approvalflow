"""
Task 3: the approver API -- the human half of the human-in-the-loop.

Two endpoints:

- `GET /api/approvals/queue`: the full F4 view (recommendation, confidence,
  violations, reasoning, usd_cents, timestamps) for every still-pending
  escalation, oldest first. The queue index (`ApprovalRepo.list_queue`) and
  the escalation records it points at can drift apart -- a crash between
  `save_new`/`resolve` and the matching queue write is exactly the partial
  failure Task 2's subscriber guards against on the write side, but nothing
  stops an id from lingering in the index if a *read*-time bug or an
  operator fixes state by hand. So this endpoint is self-healing: any id
  whose record is missing or whose status is no longer pending is skipped
  from the response AND lazily removed from the index (logged), rather
  than surfacing a broken row or trusting the index blindly forever.

- `POST /api/approvals/{invoice_id}/verdict`: exactly-once resolution (M10
  applied to people). The transform passed into `ApprovalRepo.resolve`
  raises `AlreadyResolved` the instant it sees a non-pending status, which
  `cas_update` propagates immediately (it's not a CAS conflict, so the
  retry loop never swallows it) -- so under a genuine race
  (`YieldingStateStore` + `asyncio.gather`), exactly one caller's transform
  ever observes `pending` and wins; every other caller, including a later
  double-click, gets back a 409 carrying the existing resolution
  (who/when/what) instead of a bare conflict.

  The publish-failure rollback mirrors intake's fail-loud M15: once the
  resolution CAS has committed and the id has been removed from the queue,
  a publish failure would otherwise leave a resolved-but-unpublished
  record -- exactly the invoice-hangs-forever failure M11 exists to
  prevent, since nothing downstream would ever learn the verdict happened.
  So a publish exception triggers a compensating CAS back to pending
  (resolved_* cleared) and a re-add to the queue, and the caller sees 503
  ("verdict accepted but could not be delivered; retry") -- the record is
  fully re-approvable afterward, by anyone, including the same approver.
"""

import logging
from datetime import UTC, datetime

from afcommon.contracts import ApprovalResolvedPayload, Verdict
from afcommon.events import TOPIC_APPROVAL_RESOLVED, new_event_meta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .deps import Publisher, get_publisher, get_repo
from .models import AlreadyResolved, Escalation, EscalationStatus
from .repo import ApprovalRepo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class VerdictRequest(BaseModel):
    verdict: Verdict
    approver_id: str = Field(min_length=1)
    comment: str = ""


def _view(esc: Escalation) -> dict:
    return {
        "invoiceId": esc.invoice_id,
        "status": esc.status,
        "recommendation": esc.recommendation,
        "confidence": esc.confidence,
        "violations": esc.route_violations,
        "reasoning": esc.reasoning,
        "usdCents": esc.usd_cents,
        "vendor": esc.vendor,
        "submitter": esc.submitter,
        "category": esc.category,
        "escalatedAt": esc.escalated_at,
        "resolvedAt": esc.resolved_at,
        "resolvedBy": esc.resolved_by,
        "resolutionComment": esc.resolution_comment,
    }


@router.get("/approvals/queue")
async def get_queue(repo: ApprovalRepo = Depends(get_repo)) -> dict:
    ids = await repo.list_queue()
    escalations = []
    for invoice_id in ids:
        esc = await repo.get(invoice_id)
        if esc is None or esc.status != EscalationStatus.pending:
            log.warning(
                "self-healing: removing invoice_id=%s from queue (missing or non-pending)",
                invoice_id)
            await repo.remove_from_queue(invoice_id)
            continue
        escalations.append(esc)
    escalations.sort(key=lambda e: e.escalated_at)
    return {"items": [_view(e) for e in escalations]}


@router.post("/approvals/{invoice_id}/verdict")
async def submit_verdict(invoice_id: str, body: VerdictRequest,
                         repo: ApprovalRepo = Depends(get_repo),
                         publisher: Publisher = Depends(get_publisher)) -> dict:
    now = datetime.now(UTC).isoformat()

    def transform(esc: Escalation) -> Escalation:
        if esc.status != EscalationStatus.pending:
            raise AlreadyResolved(esc)
        return esc.model_copy(update={
            "status": EscalationStatus(body.verdict),
            "resolved_by": body.approver_id,
            "resolved_at": now,
            "resolution_comment": body.comment,
        })

    try:
        resolved = await repo.resolve(invoice_id, transform)
    except KeyError as e:
        raise HTTPException(
            status_code=404, detail=f"unknown escalation {invoice_id}") from e
    except AlreadyResolved as e:
        raise HTTPException(status_code=409, detail=_view(e.escalation)) from e

    await repo.remove_from_queue(invoice_id)

    payload = ApprovalResolvedPayload(
        meta=new_event_meta(invoice_id, resolved.correlation_id),
        verdict=body.verdict,
        approver_id=body.approver_id,
        comment=body.comment,
    )
    try:
        await publisher(TOPIC_APPROVAL_RESOLVED, payload.model_dump())
    except Exception as e:
        log.exception(
            "failed to publish approval-resolved for invoice_id=%s; rolling back resolution",
            invoice_id)

        def revert(esc: Escalation) -> Escalation:
            return esc.model_copy(update={
                "status": EscalationStatus.pending,
                "resolved_by": None,
                "resolved_at": None,
                "resolution_comment": "",
            })

        await repo.resolve(invoice_id, revert)
        await repo.add_to_queue(invoice_id)
        raise HTTPException(
            status_code=503,
            detail="verdict accepted but could not be delivered; retry") from e

    log.info("verdict resolved")
    return _view(resolved)

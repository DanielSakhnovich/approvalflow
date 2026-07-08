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

  The publish-failure rollback mirrors intake's fail-loud M15. Ordering is
  resolve -> publish -> remove_from_queue (publish-before-dequeue), so a
  publish failure triggers a compensating CAS back to pending (resolved_*
  cleared) and the caller sees 503 ("verdict accepted but could not be
  delivered; retry") -- the record is fully re-approvable afterward, by
  anyone, including a different approver with a different verdict. The
  rollback also re-adds the id to the queue (idempotent): the naive
  assumption that "the id never left the queue, since publish precedes
  remove_from_queue" ignores a third actor -- `GET /queue`'s self-healing
  read can observe the record as resolved (already committed, mid-publish)
  and remove the id from the index *during* the await, before the publish
  fails and the rollback runs. Without the re-add, that leaves a record
  that's `pending` in storage but absent from the queue index: invisible to
  approvers, forever, since nothing else will ever re-add it. A
  resolved-but-unpublished record is exactly the invoice-hangs-forever
  failure M11 exists to prevent, so if the rollback itself fails, that
  state is logged CRITICAL (manual reconciliation required) and surfaces as
  an honest 500 -- never silent. A remove_from_queue failure AFTER a
  successful publish is benign by construction (stale queue id, reaped by
  the self-healing view) and only logs a warning; the approver still gets
  their 200.
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
        if esc is None:
            # A None read is AMBIGUOUS under the state store's eventual
            # consistency: it may be a transient read-after-write miss on a
            # record that was just written (observed live: a GET returning 204
            # immediately followed by 200 for the same key). Evicting here would
            # permanently strand a genuinely-pending escalation on a transient
            # blip, so we only skip it from THIS view and leave the index alone —
            # a later poll re-reads it. Eviction is reserved for records that
            # definitively read back as resolved (below).
            log.info("queue view: invoice_id=%s not readable this pass; skipped, not evicted",
                     invoice_id)
            continue
        if esc.status != EscalationStatus.pending:
            log.warning("self-healing: removing invoice_id=%s from queue (resolved)", invoice_id)
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

    # Ordering is deliberate: resolve -> PUBLISH -> remove_from_queue.
    #
    # Publishing before dequeuing means a queue-write hiccup can never strand
    # a resolved-but-unpublished record (the permanent-hang M11 exists to
    # prevent): once publish succeeds, the worst leftover is a stale queue id,
    # which the self-healing queue view reaps and any second verdict answers
    # with a 409 read-back. The reverse order would convert that same hiccup
    # into an invoice invisible forever (resolved, never published, retries
    # 409, self-heal reaps the queue id).
    #
    # AT-LEAST-ONCE CONTRACT: the rollback-then-retry path below makes
    # `approval-resolved` at-least-once PER INVOICE, and every attempt
    # carries a FRESH event_id (new_event_meta) -- e.g. the broker accepts
    # the publish but our side sees a transport error, we roll back, and a
    # later retry publishes again with a new event_id. Downstream consumers
    # (the Phase 05 payment saga) MUST therefore be idempotent per
    # invoice_id, NOT per event_id.
    payload = ApprovalResolvedPayload(
        meta=new_event_meta(invoice_id, resolved.correlation_id),
        verdict=body.verdict,
        approver_id=body.approver_id,
        comment=body.comment,
        usd_cents=resolved.usd_cents,
        scenario=resolved.scenario,
        department=resolved.department,
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

        # Publish precedes remove_from_queue, but that does NOT guarantee
        # "the id never left the queue": a concurrent GET /queue can observe
        # the already-committed resolution mid-publish-await and self-heal
        # the id out of the index before this rollback runs. So the
        # compensating CAS is re-add_to_queue -- idempotent, so it's a no-op
        # in the common case where the id is still there.
        try:
            await repo.resolve(invoice_id, revert)
            await repo.add_to_queue(invoice_id)
        except Exception:
            log.critical(
                "rollback FAILED for invoice_id=%s: record is "
                "resolved-but-unpublished; manual reconciliation required",
                invoice_id)
            raise
        raise HTTPException(
            status_code=503,
            detail="verdict accepted but could not be delivered; retry") from e

    try:
        await repo.remove_from_queue(invoice_id)
    except Exception:
        # The resolution and its delivery -- the real work -- both succeeded.
        # All this failure leaves behind is a stale queue id, which the
        # self-healing queue view reaps (and any second verdict gets a 409
        # read-back), so log and still hand the approver their 200 rather
        # than failing an already-delivered verdict.
        log.warning(
            "remove_from_queue failed for invoice_id=%s after successful publish; "
            "stale queue id will be reaped by the self-healing queue view",
            invoice_id, exc_info=True)

    log.info("verdict resolved")
    return _view(resolved)

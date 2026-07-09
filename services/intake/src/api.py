import logging
import uuid

from afcommon.auth import require_role
from afcommon.contracts import InvoiceSubmittedPayload
from afcommon.events import TOPIC_INVOICE_SUBMITTED, new_event_meta
from afcommon.logging import correlation_id_var, invoice_id_var
from fastapi import APIRouter, Depends, HTTPException

from .audit_client import AuditInvokeClient
from .deps import Publisher, get_audit_client, get_publisher, get_repo
from .models import InvoiceRecord, InvoiceStatus, InvoiceSubmission, touch
from .repo import IntakeRepo

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _view(record: InvoiceRecord) -> dict:
    return {
        "trackingId": record.invoice_id,
        "clientRef": record.client_ref,
        "status": record.status,
        "route": record.route,
        "reasoning": record.reasoning,
        "decidedBy": record.decided_by,
        "submittedAt": record.created_at,
        "updatedAt": record.updated_at,
    }


async def _publish_submitted(publisher: Publisher, record: InvoiceRecord,
                             submission: InvoiceSubmission) -> None:
    payload = InvoiceSubmittedPayload(
        meta=new_event_meta(record.invoice_id, record.correlation_id),
        invoice=submission.model_dump(by_alias=True),
    )
    await publisher(TOPIC_INVOICE_SUBMITTED, payload.model_dump())


@router.post("/invoices", status_code=202,
            dependencies=[Depends(require_role("submitter", "admin"))])
async def submit(submission: InvoiceSubmission,
                 repo: IntakeRepo = Depends(get_repo),
                 publisher: Publisher = Depends(get_publisher)) -> dict:
    invoice_id = f"inv_{uuid.uuid4().hex}"
    invoice_id_var.set(invoice_id)
    record = InvoiceRecord.new(invoice_id, correlation_id_var.get(), submission)
    await repo.save_record(record)
    try:
        await _publish_submitted(publisher, record, submission)
    except Exception as e:
        log.exception("failed to publish invoice-submitted")
        await repo.save_record(touch(record, status=InvoiceStatus.SUBMIT_FAILED))
        raise HTTPException(
            status_code=503,
            detail="submission stored but could not be queued; please retry") from e
    record = touch(record, status=InvoiceStatus.EVALUATING)
    await repo.save_record(record)
    await repo.bump_counters(submitted=1)
    log.info("invoice accepted")
    return {"trackingId": invoice_id,
            "correlationId": record.correlation_id,
            "status": record.status}


@router.get("/invoices/{invoice_id}",
           dependencies=[Depends(require_role("submitter", "admin"))])
async def get_status(invoice_id: str, trail: bool = False,
                     repo: IntakeRepo = Depends(get_repo),
                     audit: AuditInvokeClient = Depends(get_audit_client)) -> dict:
    record = await repo.get_record(invoice_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown invoice {invoice_id}")
    view = _view(record)
    if trail:
        # M5 sync leg: enrich with the audit trail via Dapr service invocation.
        # Best-effort at BOTH layers -- AuditInvokeClient catches transport
        # errors, and this handler guards against anything else so the status
        # response (the primary answer) never fails on trail enrichment.
        try:
            view["trail"] = await audit.fetch_trail(record.correlation_id)
        except Exception:
            log.warning("trail enrichment failed; returning status without trail")
            view["trail"] = []
    return view


@router.put("/invoices/{invoice_id}", status_code=202)
async def resubmit(invoice_id: str, submission: InvoiceSubmission,
                   repo: IntakeRepo = Depends(get_repo),
                   publisher: Publisher = Depends(get_publisher)) -> dict:
    record = await repo.get_record(invoice_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown invoice {invoice_id}")
    if record.status != InvoiceStatus.NEEDS_INFO:
        raise HTTPException(
            status_code=409,
            detail=f"resubmission only allowed from needs_info (currently {record.status})")
    invoice_id_var.set(invoice_id)
    correlation_id_var.set(record.correlation_id)
    record = touch(record,
                   submission=submission.model_dump(by_alias=True),
                   status=InvoiceStatus.EVALUATING)
    await repo.save_record(record)
    await _publish_submitted(publisher, record, submission)
    log.info("invoice resubmitted after send-back")
    return {"trackingId": invoice_id, "status": record.status}


@router.get("/dashboard")
async def dashboard(repo: IntakeRepo = Depends(get_repo)) -> dict:
    return await repo.get_counters()

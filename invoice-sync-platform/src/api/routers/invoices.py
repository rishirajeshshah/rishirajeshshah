"""Invoice query and retry API routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Invoice
from ...db.session import get_db
from ...worker.tasks.sync_tasks import retry_invoice as retry_invoice_task
from ..deps import verify_api_key

router = APIRouter(prefix="/api/v1/invoices", tags=["Invoices"])


def _invoice_to_dict(inv: Invoice) -> dict[str, Any]:
    return {
        "id": inv.id,
        "tenant_id": inv.tenant_id,
        "erp_source": inv.erp_source,
        "erp_invoice_id": inv.erp_invoice_id,
        "erp_invoice_number": inv.erp_invoice_number,
        "processor_target": inv.processor_target,
        "processor_invoice_id": inv.processor_invoice_id,
        "processor_invoice_url": inv.processor_invoice_url,
        "currency": inv.currency,
        "total_amount": str(inv.total_amount),
        "amount_due": str(inv.amount_due),
        "invoice_status": inv.invoice_status,
        "sync_status": inv.sync_status,
        "sync_attempts": inv.sync_attempts,
        "last_sync_at": inv.last_sync_at.isoformat() if inv.last_sync_at else None,
        "last_sync_error": inv.last_sync_error,
        "invoice_date": str(inv.invoice_date),
        "due_date": str(inv.due_date),
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_invoices(
    tenant_id: str | None = None,
    sync_status: str | None = None,
    erp_source: str | None = None,
    processor_target: str | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    query = select(Invoice)
    if tenant_id:
        query = query.where(Invoice.tenant_id == tenant_id)
    if sync_status:
        query = query.where(Invoice.sync_status == sync_status)
    if erp_source:
        query = query.where(Invoice.erp_source == erp_source)
    if processor_target:
        query = query.where(Invoice.processor_target == processor_target)
    query = query.order_by(Invoice.created_at.desc()).limit(min(limit, 100))

    result = await db.execute(query)
    invoices = result.scalars().all()
    return {
        "data": [_invoice_to_dict(i) for i in invoices],
        "count": len(invoices),
    }


@router.get("/{invoice_id}", dependencies=[Depends(verify_api_key)])
async def get_invoice(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _invoice_to_dict(invoice)


@router.post("/{invoice_id}/retry", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(verify_api_key)])
async def retry_invoice(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Reset a failed invoice to pending and queue it for re-sync."""
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.sync_status not in ("failed", "pending"):
        raise HTTPException(
            status_code=400,
            detail=f"Invoice is in status '{invoice.sync_status}' — only failed/pending can be retried"
        )

    retry_invoice_task.delay(invoice_id)
    return {"status": "queued", "invoice_id": invoice_id}

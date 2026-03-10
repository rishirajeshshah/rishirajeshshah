"""Celery tasks for processing inbound webhook events from payment processors."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...core.adapter_sdk.processor_adapter import ProcessorAdapterConfig
from ...core.adapter_sdk.registry import AdapterRegistry
from ...core.canonical.invoice import InvoiceStatus
from ...db.models import AuditEvent, Invoice, Payment
from ...db.session import AsyncSessionLocal
from ...adapters.processors.paypal.paypal_adapter import PayPalAdapter  # noqa: F401
from ..celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="webhook_tasks.process_paypal_webhook",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_paypal_webhook(
    self,
    event_type: str,
    payload: dict,
    tenant_id: str,
) -> dict:
    """Process a validated PayPal webhook event."""
    return _run_async(_process_paypal_webhook_async(event_type, payload, tenant_id))


async def _process_paypal_webhook_async(
    event_type: str, payload: dict, tenant_id: str
) -> dict:
    adapter = AdapterRegistry.get_processor("paypal-v1")
    config = ProcessorAdapterConfig(tenant_id=tenant_id, credentials={})
    await adapter.initialize(config)

    canonical_payment = await adapter.process_webhook_event(event_type, payload)
    if not canonical_payment:
        logger.info("Webhook event %s does not produce a payment — ignoring", event_type)
        return {"status": "ignored", "event_type": event_type}

    async with AsyncSessionLocal() as db:
        # Resolve processor_invoice_id to internal invoice_id
        proc_invoice_id = payload.get("resource", {}).get("invoice_id", "")
        if proc_invoice_id:
            inv_result = await db.execute(
                select(Invoice).where(
                    Invoice.processor_invoice_id == proc_invoice_id,
                    Invoice.tenant_id == tenant_id,
                )
            )
            db_invoice = inv_result.scalar_one_or_none()
            if db_invoice:
                canonical_payment.invoice_id = db_invoice.id

                # Update invoice status to PAID
                await db.execute(
                    update(Invoice)
                    .where(Invoice.id == db_invoice.id)
                    .values(
                        invoice_status=InvoiceStatus.PAID.value,
                        amount_paid=str(canonical_payment.amount),
                        amount_due="0.00",
                        paid_date=datetime.now(timezone.utc).date(),
                    )
                )

        # Persist payment record (unique constraint prevents duplicates)
        payment = Payment(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            invoice_id=canonical_payment.invoice_id or "",
            processor_source="paypal",
            processor_payment_id=canonical_payment.processor_payment_id,
            amount=canonical_payment.amount,
            currency=canonical_payment.currency,
            method=canonical_payment.method.value,
            status=canonical_payment.status.value,
            paid_at=canonical_payment.paid_at,
            raw_webhook_payload=payload,
        )
        db.add(payment)

        # Audit
        audit = AuditEvent(
            tenant_id=tenant_id,
            entity_type="payment",
            entity_id=payment.id,
            event_type="payment.received",
            actor="webhook:paypal",
            after_state={"amount": canonical_payment.amount,
                         "processor_payment_id": canonical_payment.processor_payment_id},
        )
        db.add(audit)
        await db.commit()

    logger.info("Processed PayPal payment %s for invoice %s",
                canonical_payment.processor_payment_id, canonical_payment.invoice_id)
    return {"status": "processed", "payment_id": payment.id}

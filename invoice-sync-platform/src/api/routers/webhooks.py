"""
Webhook receiver routes.

CRITICAL: These endpoints must ALWAYS return 200 immediately.
Payment processors retry on any non-2xx response, which can cause duplicate events.
Actual processing happens asynchronously via Celery.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.adapter_sdk.processor_adapter import ProcessorAdapterConfig
from ...core.adapter_sdk.registry import AdapterRegistry
from ...db.models import Tenant
from ...db.session import get_db
from ...worker.tasks.webhook_tasks import process_paypal_webhook
# Ensure adapters are registered
from ...adapters.processors.paypal.paypal_adapter import PayPalAdapter  # noqa: F401

from fastapi import Depends

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/{processor_id}", status_code=status.HTTP_200_OK)
async def receive_webhook(
    processor_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Receive a webhook from a payment processor.

    The processor_id path param routes to the correct adapter.
    Tenant is resolved from the webhook payload (PayPal uses invoice_id lookup).

    Always returns 200 immediately — actual processing is async.
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    # Get the right adapter
    adapter_key = f"{processor_id}-v1"
    try:
        adapter = AdapterRegistry.get_processor(adapter_key)
    except KeyError:
        logger.warning("Unknown processor_id in webhook: %s", processor_id)
        # Still return 200 to prevent retries from the processor
        return {"status": "ignored", "reason": "unknown_processor"}

    # Validate signature
    config = ProcessorAdapterConfig(tenant_id="", credentials={})
    await adapter.initialize(config)
    validation = await adapter.validate_webhook(headers, raw_body)

    if not validation.valid:
        logger.warning("Invalid webhook signature from %s: %s",
                       processor_id, validation.error)
        # Return 200 to prevent retry storms; log the failure for investigation
        return {"status": "ignored", "reason": "invalid_signature"}

    # Resolve tenant_id from payload
    tenant_id = await _resolve_tenant_from_payload(
        processor_id, validation.raw_payload, db
    )

    if not tenant_id:
        logger.warning("Could not resolve tenant for %s webhook", processor_id)
        return {"status": "ignored", "reason": "tenant_not_found"}

    # Dispatch to Celery for async processing (return 200 now)
    if processor_id == "paypal":
        process_paypal_webhook.delay(
            event_type=validation.event_type,
            payload=validation.raw_payload,
            tenant_id=tenant_id,
        )

    logger.info("Queued %s webhook event %s for tenant %s",
                processor_id, validation.event_type, tenant_id)
    return {"status": "accepted", "event_type": validation.event_type}


async def _resolve_tenant_from_payload(
    processor_id: str,
    payload: dict[str, Any],
    db: AsyncSession,
) -> str | None:
    """
    Attempt to resolve tenant_id from webhook payload.
    For PayPal, match by processor_target in tenants table.
    In a multi-tenant setup you'd match by PayPal account ID or webhook endpoint.
    """
    result = await db.execute(
        select(Tenant)
        .where(Tenant.processor_target == processor_id, Tenant.active == True)
        .limit(1)
    )
    tenant = result.scalar_one_or_none()
    return tenant.id if tenant else None

"""
SyncEngine — orchestrates the full invoice sync pipeline.

Flow for each invoice:
  1. Check idempotency guard (skip if already synced)
  2. Acquire processing lock
  3. Call processor adapter's push_invoice()
  4. On success: persist processor IDs, mark idempotency as synced, emit audit event
  5. On failure: release lock, increment attempt counter, schedule retry

The engine itself is stateless — all state lives in PostgreSQL + Redis.
It is called by Celery tasks (worker/tasks/sync_invoice.py).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.adapter_sdk.erp_adapter import ERPAdapterConfig, InvoiceFetchOptions
from ..core.adapter_sdk.processor_adapter import ProcessorAdapterConfig
from ..core.adapter_sdk.registry import AdapterRegistry
from ..core.canonical.invoice import CanonicalInvoice, InvoiceSyncStatus
from ..core.canonical.sync_job import SyncJobStatus, SyncTrigger
from ..db.models import AuditEvent, Invoice, SyncJob, Tenant
from .idempotency import IdempotencyGuard

logger = logging.getLogger(__name__)


class SyncEngine:
    """
    Core orchestrator. Instantiated once per Celery task invocation.
    Accepts an async DB session and a Redis client for idempotency.
    """

    def __init__(self, db: AsyncSession, redis: aioredis.Redis):
        self._db = db
        self._idempotency = IdempotencyGuard(redis)

    # ── Job-level Operations ──────────────────────────────────────────────────

    async def create_sync_job(
        self,
        tenant_id: str,
        erp_source: str,
        processor_target: str,
        mode: str,
        triggered_by: str = SyncTrigger.API,
    ) -> SyncJob:
        job = SyncJob(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            erp_source=erp_source,
            processor_target=processor_target,
            mode=mode,
            status=SyncJobStatus.QUEUED,
            triggered_by=triggered_by,
        )
        self._db.add(job)
        await self._db.commit()
        await self._db.refresh(job)
        logger.info("Created sync job %s for tenant %s", job.id, tenant_id)
        return job

    async def run_sync_job(self, job_id: str) -> dict[str, Any]:
        """
        Execute a sync job end-to-end:
        1. Load tenant + credentials
        2. Initialize ERP + processor adapters
        3. Fetch invoices (with cursor pagination)
        4. Push each invoice, tracking success/failure/skipped
        5. Update job status
        """
        # Load job
        result = await self._db.execute(select(SyncJob).where(SyncJob.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError(f"Sync job {job_id} not found")

        # Load tenant
        t_result = await self._db.execute(
            select(Tenant).where(Tenant.id == job.tenant_id)
        )
        tenant = t_result.scalar_one_or_none()
        if not tenant:
            raise ValueError(f"Tenant {job.tenant_id} not found")

        # Mark job as running
        job.status = SyncJobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        await self._db.commit()

        # Build adapter configs (in prod, credentials come from Secrets Manager)
        erp_config = ERPAdapterConfig(
            tenant_id=tenant.id,
            credentials={},  # populated from credential_refs in production
            options={"processor_target": job.processor_target},
        )
        proc_config = ProcessorAdapterConfig(
            tenant_id=tenant.id,
            credentials={},
            options={"tenant_prefix": tenant.name[:8].upper()},
        )

        erp_adapter = AdapterRegistry.get_erp(job.erp_source + "-v1")
        proc_adapter = AdapterRegistry.get_processor(job.processor_target + "-v1")

        await erp_adapter.initialize(erp_config)
        await proc_adapter.initialize(proc_config)

        success, failure, skipped = 0, 0, 0
        cursor: str | None = None

        try:
            while True:
                fetch_opts = InvoiceFetchOptions(
                    cursor=cursor,
                    limit=100,
                )
                fetch_result = await erp_adapter.fetch_invoices(fetch_opts)

                for invoice in fetch_result.invoices:
                    outcome = await self._sync_single_invoice(
                        invoice, proc_adapter, proc_config
                    )
                    if outcome == "synced":
                        success += 1
                    elif outcome == "skipped":
                        skipped += 1
                    else:
                        failure += 1

                if not fetch_result.has_more:
                    break
                cursor = fetch_result.next_cursor

        except Exception as exc:
            logger.exception("Sync job %s failed: %s", job_id, exc)
            job.status = SyncJobStatus.FAILED if (success + skipped) == 0 else SyncJobStatus.PARTIAL
        else:
            job.status = SyncJobStatus.FAILED if (success + skipped) == 0 and failure > 0 \
                else (SyncJobStatus.PARTIAL if failure > 0 else SyncJobStatus.COMPLETED)

        job.success_count = success
        job.failure_count = failure
        job.skipped_count = skipped
        job.completed_at = datetime.now(timezone.utc)
        await self._db.commit()

        logger.info("Sync job %s finished: %d synced, %d failed, %d skipped",
                    job_id, success, failure, skipped)
        return {
            "job_id": job_id,
            "status": job.status,
            "success": success,
            "failure": failure,
            "skipped": skipped,
        }

    # ── Invoice-level Operations ──────────────────────────────────────────────

    async def _sync_single_invoice(
        self,
        invoice: CanonicalInvoice,
        proc_adapter: Any,
        proc_config: ProcessorAdapterConfig,
    ) -> str:
        """
        Sync one invoice. Returns "synced", "skipped", or "failed".
        Idempotency: skip if Redis key exists with status="synced".
        """
        tenant_id = invoice.tenant_id
        idem_key = invoice.idempotency_key

        # 1. Check if already synced
        if await self._idempotency.is_already_synced(tenant_id, idem_key):
            logger.debug("Invoice %s already synced — skipping", invoice.erp_invoice_number)
            return "skipped"

        # 2. Acquire processing lock
        if not await self._idempotency.mark_in_progress(tenant_id, idem_key):
            logger.info("Invoice %s being processed by another worker — skipping",
                        invoice.erp_invoice_number)
            return "skipped"

        # 3. Upsert invoice record in DB
        db_invoice = await self._upsert_invoice(invoice)

        try:
            # 4. Push to processor
            result = await proc_adapter.push_invoice(invoice)

            # 5. Update DB record with processor IDs
            await self._db.execute(
                update(Invoice)
                .where(Invoice.id == db_invoice.id)
                .values(
                    sync_status=InvoiceSyncStatus.SYNCED,
                    processor_invoice_id=result.processor_invoice_id,
                    processor_invoice_url=result.processor_invoice_url,
                    last_sync_at=datetime.now(timezone.utc),
                    last_sync_error=None,
                    sync_attempts=Invoice.sync_attempts + 1,
                )
            )
            await self._db.commit()

            # 6. Mark idempotency as synced
            await self._idempotency.mark_synced(tenant_id, idem_key)

            # 7. Emit audit event
            await self._emit_audit_event(
                tenant_id=tenant_id,
                entity_type="invoice",
                entity_id=db_invoice.id,
                event_type="invoice.sync_succeeded",
                actor="sync_engine",
                after_state={
                    "sync_status": "synced",
                    "processor_invoice_id": result.processor_invoice_id,
                },
            )

            logger.info("Invoice %s → %s (%s)",
                        invoice.erp_invoice_number,
                        result.processor_invoice_id,
                        invoice.processor_target)
            return "synced"

        except Exception as exc:
            error_msg = str(exc)[:1000]
            logger.error("Failed to sync invoice %s: %s",
                         invoice.erp_invoice_number, error_msg)

            await self._db.execute(
                update(Invoice)
                .where(Invoice.id == db_invoice.id)
                .values(
                    sync_status=InvoiceSyncStatus.FAILED,
                    last_sync_error=error_msg,
                    last_sync_at=datetime.now(timezone.utc),
                    sync_attempts=Invoice.sync_attempts + 1,
                )
            )
            await self._db.commit()
            await self._idempotency.mark_failed(tenant_id, idem_key)

            await self._emit_audit_event(
                tenant_id=tenant_id,
                entity_type="invoice",
                entity_id=db_invoice.id,
                event_type="invoice.sync_failed",
                actor="sync_engine",
                after_state={"sync_status": "failed", "error": error_msg},
            )
            return "failed"

    async def _upsert_invoice(self, invoice: CanonicalInvoice) -> Invoice:
        """Insert or update the invoice record in the DB."""
        result = await self._db.execute(
            select(Invoice).where(Invoice.idempotency_key == invoice.idempotency_key)
        )
        db_inv = result.scalar_one_or_none()

        snapshot = invoice.model_dump(mode="json")

        if db_inv is None:
            db_inv = Invoice(
                id=invoice.id,
                tenant_id=invoice.tenant_id,
                idempotency_key=invoice.idempotency_key,
                erp_source=invoice.erp_source,
                erp_invoice_id=invoice.erp_invoice_id,
                erp_invoice_number=invoice.erp_invoice_number,
                processor_target=invoice.processor_target,
                currency=invoice.currency,
                subtotal=invoice.subtotal,
                total_tax=invoice.total_tax,
                total_discount=invoice.total_discount,
                total_amount=invoice.total_amount,
                amount_paid=invoice.amount_paid,
                amount_due=invoice.amount_due,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                invoice_status=invoice.invoice_status.value,
                sync_status=InvoiceSyncStatus.PENDING.value,
                sync_attempts=0,
                canonical_snapshot=snapshot,
            )
            self._db.add(db_inv)
            await self._db.flush()
        else:
            db_inv.canonical_snapshot = snapshot
            db_inv.invoice_status = invoice.invoice_status.value

        await self._db.commit()
        await self._db.refresh(db_inv)
        return db_inv

    async def retry_invoice(self, invoice_id: str) -> None:
        """Manually reset a failed invoice so it will be retried on next sync."""
        await self._db.execute(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .values(sync_status=InvoiceSyncStatus.PENDING.value, last_sync_error=None)
        )
        await self._db.commit()
        logger.info("Invoice %s reset to pending for retry", invoice_id)

    async def _emit_audit_event(
        self,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str,
        before_state: dict | None = None,
        after_state: dict | None = None,
    ) -> None:
        event = AuditEvent(
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor=actor,
            before_state=before_state,
            after_state=after_state,
        )
        self._db.add(event)
        await self._db.commit()

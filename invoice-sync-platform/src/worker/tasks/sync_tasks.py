"""
Celery tasks for invoice sync operations.

These tasks are the bridge between the FastAPI HTTP layer and the SyncEngine.
Each task runs in a Celery worker process with its own DB session and Redis conn.
"""
from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...db.session import AsyncSessionLocal
from ...engine.sync_engine import SyncEngine
# Import adapters so they self-register on worker startup
from ...adapters.erp.netsuite.netsuite_adapter import NetSuiteAdapter  # noqa: F401
from ...adapters.processors.paypal.paypal_adapter import PayPalAdapter  # noqa: F401
from ..celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a synchronous Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(
    name="sync_tasks.run_sync_job",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_sync_job(self, job_id: str) -> dict:
    """
    Execute a sync job. Called when a POST /api/v1/sync-jobs is made,
    or triggered by the scheduler for batch runs.
    """
    logger.info("Starting sync job %s", job_id)
    return _run_async(_run_sync_job_async(job_id))


async def _run_sync_job_async(job_id: str) -> dict:
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    try:
        async with AsyncSessionLocal() as db:
            engine = SyncEngine(db=db, redis=redis_client)
            return await engine.run_sync_job(job_id)
    finally:
        await redis_client.aclose()


@celery_app.task(
    name="sync_tasks.retry_invoice",
    bind=True,
    max_retries=1,
)
def retry_invoice(self, invoice_id: str) -> dict:
    """Reset a failed invoice to pending and re-trigger sync."""
    logger.info("Manually retrying invoice %s", invoice_id)
    return _run_async(_retry_invoice_async(invoice_id))


async def _retry_invoice_async(invoice_id: str) -> dict:
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    try:
        async with AsyncSessionLocal() as db:
            engine = SyncEngine(db=db, redis=redis_client)
            await engine.retry_invoice(invoice_id)
            return {"status": "queued", "invoice_id": invoice_id}
    finally:
        await redis_client.aclose()

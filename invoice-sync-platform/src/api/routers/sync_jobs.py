"""Sync job API routes — trigger, query, and monitor sync jobs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import SyncJob, Tenant
from ...db.session import get_db
from ...engine.sync_engine import SyncEngine
from ...worker.tasks.sync_tasks import run_sync_job
from ..deps import verify_api_key

router = APIRouter(prefix="/api/v1/sync-jobs", tags=["Sync Jobs"])


class SyncJobCreate(BaseModel):
    tenant_id: str
    erp_source: str = "netsuite"
    processor_target: str = "paypal"
    mode: str = "manual"  # "manual" | "batch" | "realtime"
    invoice_ids: list[str] | None = None


class SyncJobResponse(BaseModel):
    id: str
    tenant_id: str
    erp_source: str
    processor_target: str
    mode: str
    status: str
    success_count: int
    failure_count: int
    skipped_count: int
    triggered_by: str
    started_at: str | None
    completed_at: str | None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_safe(cls, obj: SyncJob) -> "SyncJobResponse":
        return cls(
            id=obj.id,
            tenant_id=obj.tenant_id,
            erp_source=obj.erp_source,
            processor_target=obj.processor_target,
            mode=obj.mode,
            status=obj.status,
            success_count=obj.success_count,
            failure_count=obj.failure_count,
            skipped_count=obj.skipped_count,
            triggered_by=obj.triggered_by,
            started_at=obj.started_at.isoformat() if obj.started_at else None,
            completed_at=obj.completed_at.isoformat() if obj.completed_at else None,
            created_at=obj.created_at.isoformat() if obj.created_at else "",
        )


@router.post("", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(verify_api_key)])
async def trigger_sync_job(
    body: SyncJobCreate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Trigger a new sync job. Returns immediately with job_id.
    The actual sync runs asynchronously in a Celery worker.
    """
    # Verify tenant exists
    t_result = await db.execute(select(Tenant).where(Tenant.id == body.tenant_id))
    tenant = t_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Create job record
    import redis.asyncio as aioredis
    from ...config import settings
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    try:
        engine = SyncEngine(db=db, redis=redis_client)
        job = await engine.create_sync_job(
            tenant_id=body.tenant_id,
            erp_source=body.erp_source,
            processor_target=body.processor_target,
            mode=body.mode,
            triggered_by="api",
        )
    finally:
        await redis_client.aclose()

    # Dispatch to Celery worker asynchronously
    run_sync_job.delay(job.id)

    return {
        "job_id": job.id,
        "status": "queued",
        "message": f"Sync job queued. GET /api/v1/sync-jobs/{job.id} to check status.",
    }


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_sync_jobs(
    tenant_id: str | None = None,
    job_status: str | None = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    query = select(SyncJob)
    if tenant_id:
        query = query.where(SyncJob.tenant_id == tenant_id)
    if job_status:
        query = query.where(SyncJob.status == job_status)
    query = query.order_by(SyncJob.created_at.desc()).limit(min(limit, 100))

    result = await db.execute(query)
    jobs = result.scalars().all()
    return {
        "data": [SyncJobResponse.from_orm_safe(j) for j in jobs],
        "count": len(jobs),
    }


@router.get("/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_sync_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> SyncJobResponse:
    result = await db.execute(select(SyncJob).where(SyncJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Sync job not found")
    return SyncJobResponse.from_orm_safe(job)

"""Tenant management API routes."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Tenant
from ...db.session import get_db
from ..deps import verify_api_key

router = APIRouter(prefix="/api/v1/tenants", tags=["Tenants"])


class TenantCreate(BaseModel):
    name: str
    erp_source: str  # "netsuite"
    processor_target: str  # "paypal"
    sync_schedule: str | None = None  # cron expression, e.g. "0 2 * * *"
    retry_policy: dict[str, Any] | None = None


class TenantResponse(BaseModel):
    id: str
    name: str
    erp_source: str
    processor_target: str
    sync_schedule: str | None
    retry_policy: dict[str, Any]
    active: bool

    model_config = {"from_attributes": True}


@router.post("", response_model=TenantResponse, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(verify_api_key)])
async def create_tenant(
    body: TenantCreate,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=body.name,
        erp_source=body.erp_source,
        processor_target=body.processor_target,
        sync_schedule=body.sync_schedule,
        retry_policy=body.retry_policy or {},
        active=True,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("", response_model=list[TenantResponse],
            dependencies=[Depends(verify_api_key)])
async def list_tenants(
    db: AsyncSession = Depends(get_db),
) -> list[Tenant]:
    result = await db.execute(select(Tenant).where(Tenant.active == True))
    return list(result.scalars().all())


@router.get("/{tenant_id}", response_model=TenantResponse,
            dependencies=[Depends(verify_api_key)])
async def get_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant

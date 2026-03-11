from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SyncMode(str, Enum):
    REALTIME = "realtime"
    BATCH = "batch"
    MANUAL = "manual"


class SyncJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class SyncTrigger(str, Enum):
    SCHEDULER = "scheduler"
    WEBHOOK = "webhook"
    API = "api"
    MANUAL = "manual"


class SyncJob(BaseModel):
    id: str
    tenant_id: str
    erp_source: str
    processor_target: str
    mode: SyncMode
    status: SyncJobStatus = SyncJobStatus.QUEUED
    invoice_ids: list[str] = Field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    triggered_by: SyncTrigger = SyncTrigger.API
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str

"""Celery application instance."""
from __future__ import annotations

from celery import Celery

from ..config import settings

celery_app = Celery(
    "invoice_sync",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "src.worker.tasks.sync_tasks",
        "src.worker.tasks.webhook_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,          # Re-queue on worker crash
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, # One task at a time per worker (financial safety)
    task_routes={
        "src.worker.tasks.sync_tasks.*": {"queue": "sync"},
        "src.worker.tasks.webhook_tasks.*": {"queue": "webhooks"},
    },
)

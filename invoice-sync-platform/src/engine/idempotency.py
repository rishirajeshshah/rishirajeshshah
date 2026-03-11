"""
Redis-backed idempotency guard.

Before pushing any invoice to a processor, the sync engine checks Redis for
an existing key. If the key exists and status is "synced", the push is skipped.
This prevents double-billing even if a Celery task is retried.

Key pattern: isp:{tenant_id}:idempotency:{idempotency_key}
TTL: configurable (default 30 days)
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis

from ..config import settings

logger = logging.getLogger(__name__)

_IDEMPOTENCY_PREFIX = "isp:idempotency"
_TTL_SECONDS = settings.idempotency_key_ttl_days * 86400


class IdempotencyGuard:
    """Thread-safe idempotency guard backed by Redis."""

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client

    def _key(self, tenant_id: str, idempotency_key: str) -> str:
        return f"{_IDEMPOTENCY_PREFIX}:{tenant_id}:{idempotency_key}"

    async def is_already_synced(self, tenant_id: str, idempotency_key: str) -> bool:
        """Return True if this invoice has already been successfully synced."""
        key = self._key(tenant_id, idempotency_key)
        value = await self._redis.get(key)
        if value and value.decode() == "synced":
            logger.debug("Idempotency hit for key %s — skipping", idempotency_key[:16])
            return True
        return False

    async def mark_in_progress(self, tenant_id: str, idempotency_key: str) -> bool:
        """
        Attempt to acquire the processing lock using SET NX.
        Returns True if lock acquired (safe to proceed).
        Returns False if another worker is already processing this invoice.
        """
        key = self._key(tenant_id, idempotency_key)
        # NX = set only if not exists; EX = expire in 5 minutes (guard against crashes)
        result = await self._redis.set(key, "in_progress", nx=True, ex=300)
        return result is not None

    async def mark_synced(self, tenant_id: str, idempotency_key: str) -> None:
        """Mark as successfully synced — persists for TTL to prevent re-processing."""
        key = self._key(tenant_id, idempotency_key)
        await self._redis.set(key, "synced", ex=_TTL_SECONDS)

    async def mark_failed(self, tenant_id: str, idempotency_key: str) -> None:
        """Release lock on failure so retries can proceed."""
        key = self._key(tenant_id, idempotency_key)
        # Only delete if still "in_progress" (not yet synced)
        current = await self._redis.get(key)
        if current and current.decode() == "in_progress":
            await self._redis.delete(key)

"""Application configuration loaded from environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "Invoice Sync Platform"
    app_env: str = "development"  # development | staging | production
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://isp:isp@localhost:5432/isp"
    database_url_sync: str = "postgresql+psycopg2://isp:isp@localhost:5432/isp"

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    idempotency_key_ttl_days: int = 30

    # ── Celery ───────────────────────────────────────────────────────────────
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── NetSuite (per-tenant in prod; used for demo/dev here) ────────────────
    netsuite_account_id: str = ""
    netsuite_client_id: str = ""
    netsuite_client_secret: str = ""
    netsuite_demo_mode: bool = True  # Generates mock invoices when True

    # ── PayPal (per-tenant in prod; used for demo/dev here) ──────────────────
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_sandbox: bool = True  # Use sandbox.paypal.com when True
    paypal_demo_mode: bool = True  # Simulates responses when True

    # ── API Auth ─────────────────────────────────────────────────────────────
    api_key_header: str = "X-API-Key"
    admin_api_key: str = "dev-api-key-change-in-prod"

    # ── Sync Engine ──────────────────────────────────────────────────────────
    sync_retry_max_attempts: int = 5
    sync_retry_initial_delay_ms: int = 1000
    sync_retry_backoff_multiplier: float = 2.0
    sync_retry_max_delay_ms: int = 300_000  # 5 min
    sync_concurrency: int = 10
    sync_batch_size: int = 100


settings = Settings()

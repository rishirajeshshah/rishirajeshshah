"""
Invoice Sync Platform — FastAPI application entry point.

Run locally:
    uvicorn src.api.main:app --reload --port 8000

Or via Docker Compose:
    docker compose up
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import settings

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("Invoice Sync Platform starting up (%s)", settings.app_env)
    yield
    logger.info("Invoice Sync Platform shutting down")


app = FastAPI(
    title="Invoice Sync Platform",
    description=(
        "Multi-ERP to multi-payment-processor invoice synchronization platform. "
        "Currently supports NetSuite → PayPal with an extensible adapter architecture."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from .routers import adapters, invoices, sync_jobs, tenants, webhooks  # noqa: E402

app.include_router(tenants.router)
app.include_router(sync_jobs.router)
app.include_router(invoices.router)
app.include_router(webhooks.router)
app.include_router(adapters.router)


@app.get("/health", tags=["System"])
async def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@app.get("/", tags=["System"])
async def root() -> dict:
    return {
        "name": "Invoice Sync Platform",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }

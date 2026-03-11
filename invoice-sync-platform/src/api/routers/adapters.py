"""Adapter registry introspection routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...core.adapter_sdk.registry import AdapterRegistry
# Ensure both adapters are registered
from ...adapters.erp.netsuite.netsuite_adapter import NetSuiteAdapter  # noqa: F401
from ...adapters.processors.paypal.paypal_adapter import PayPalAdapter  # noqa: F401
from ..deps import verify_api_key

router = APIRouter(prefix="/api/v1/adapters", tags=["Adapters"])


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_adapters() -> dict:
    """List all registered ERP and payment processor adapters."""
    return {
        "erp_adapters": AdapterRegistry.list_erp_adapters(),
        "processor_adapters": AdapterRegistry.list_processor_adapters(),
    }

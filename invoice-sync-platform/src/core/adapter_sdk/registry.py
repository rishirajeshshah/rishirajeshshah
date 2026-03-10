"""
Adapter Registry — central registry of all installed ERP and processor adapters.

Adapters register themselves at import time. The sync engine resolves adapters
by their adapter_id at runtime.
"""
from __future__ import annotations

from .erp_adapter import ERPAdapter
from .processor_adapter import ProcessorAdapter


class AdapterRegistry:
    """Singleton registry for ERP and processor adapters."""

    _erp_adapters: dict[str, ERPAdapter] = {}
    _processor_adapters: dict[str, ProcessorAdapter] = {}

    @classmethod
    def register_erp(cls, adapter: ERPAdapter) -> None:
        cls._erp_adapters[adapter.adapter_id] = adapter

    @classmethod
    def register_processor(cls, adapter: ProcessorAdapter) -> None:
        cls._processor_adapters[adapter.adapter_id] = adapter

    @classmethod
    def get_erp(cls, adapter_id: str) -> ERPAdapter:
        if adapter_id not in cls._erp_adapters:
            raise KeyError(f"ERP adapter '{adapter_id}' is not registered. "
                           f"Available: {list(cls._erp_adapters)}")
        return cls._erp_adapters[adapter_id]

    @classmethod
    def get_processor(cls, adapter_id: str) -> ProcessorAdapter:
        if adapter_id not in cls._processor_adapters:
            raise KeyError(f"Processor adapter '{adapter_id}' is not registered. "
                           f"Available: {list(cls._processor_adapters)}")
        return cls._processor_adapters[adapter_id]

    @classmethod
    def list_erp_adapters(cls) -> list[dict]:
        return [
            {"adapter_id": a.adapter_id, "display_name": a.display_name}
            for a in cls._erp_adapters.values()
        ]

    @classmethod
    def list_processor_adapters(cls) -> list[dict]:
        return [
            {"adapter_id": a.adapter_id, "display_name": a.display_name}
            for a in cls._processor_adapters.values()
        ]

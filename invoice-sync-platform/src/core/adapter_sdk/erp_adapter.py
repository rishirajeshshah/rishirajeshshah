"""
Abstract base class for ERP adapters.

To add a new ERP (e.g. SAP, QuickBooks), create a class that:
  1. Inherits from ERPAdapter
  2. Sets adapter_id and display_name
  3. Implements all abstract methods
  4. Calls AdapterRegistry.register_erp(MyAdapter()) at module load time
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..canonical.invoice import CanonicalInvoice


@dataclass
class ERPAdapterConfig:
    tenant_id: str
    credentials: dict[str, str]  # Fetched from Secrets Manager at runtime
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvoiceFetchOptions:
    modified_after: str | None = None  # ISO 8601 datetime
    modified_before: str | None = None
    invoice_ids: list[str] | None = None  # Targeted fetch by ERP IDs
    status_filter: list[str] | None = None
    limit: int = 100
    cursor: str | None = None  # Cursor for pagination


@dataclass
class FetchResult:
    invoices: list[CanonicalInvoice]
    next_cursor: str | None = None
    total_count: int | None = None
    has_more: bool = False


@dataclass
class HealthResult:
    healthy: bool
    message: str = ""


class ERPAdapter(ABC):
    """
    Abstract base class for all ERP integrations.

    Each ERP adapter is responsible for:
    - Authenticating with the ERP's API
    - Fetching invoices (paginated, incremental via watermark)
    - Transforming raw ERP data into the CanonicalInvoice model
    - Optionally writing back the processor's invoice ID to the ERP
    """

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Unique identifier, e.g. 'netsuite-v1'"""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'NetSuite'"""

    @abstractmethod
    async def initialize(self, config: ERPAdapterConfig) -> None:
        """
        Called once before the adapter is used. Set up HTTP clients,
        validate credentials, and perform any auth token exchange here.
        """

    @abstractmethod
    async def health_check(self) -> HealthResult:
        """Verify the ERP connection is alive and credentials are valid."""

    @abstractmethod
    async def fetch_invoices(self, opts: InvoiceFetchOptions) -> FetchResult:
        """
        Fetch invoices from the ERP. Implementations must:
        - Respect opts.modified_after for incremental sync
        - Support cursor-based pagination via opts.cursor
        - Return CanonicalInvoice objects (call transform_to_canonical internally)
        """

    @abstractmethod
    async def fetch_invoice_by_id(self, erp_invoice_id: str) -> CanonicalInvoice | None:
        """Fetch a single invoice by its ERP-native ID."""

    async def acknowledge_sync(
        self, erp_invoice_id: str, processor_invoice_id: str
    ) -> None:
        """
        Optional write-back: after a successful push, notify the ERP of
        the processor's invoice ID. Default is a no-op.
        """

    @abstractmethod
    def transform_to_canonical(self, raw: dict[str, Any]) -> CanonicalInvoice:
        """
        Transform a raw ERP API response dict into a CanonicalInvoice.
        This is the most ERP-specific method — implement carefully.
        """

    async def destroy(self) -> None:
        """Clean up resources (close HTTP clients, etc.). Default no-op."""

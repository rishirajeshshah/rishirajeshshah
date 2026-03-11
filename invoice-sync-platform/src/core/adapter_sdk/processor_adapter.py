"""
Abstract base class for payment processor adapters.

To add a new processor (e.g. Stripe, Adyen), create a class that:
  1. Inherits from ProcessorAdapter
  2. Sets adapter_id and display_name
  3. Implements all abstract methods
  4. Calls AdapterRegistry.register_processor(MyAdapter()) at module load time
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..canonical.invoice import CanonicalInvoice, InvoiceStatus
from ..canonical.payment import CanonicalPayment


@dataclass
class ProcessorAdapterConfig:
    tenant_id: str
    credentials: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PushInvoiceResult:
    processor_invoice_id: str
    processor_invoice_url: str | None = None
    status: str = "created"  # "created" | "updated" | "skipped"
    raw_response: Any = None


@dataclass
class WebhookValidationResult:
    valid: bool
    event_type: str
    raw_payload: dict[str, Any]
    error: str | None = None


class ProcessorAdapter(ABC):
    """
    Abstract base class for all payment processor integrations.

    Each adapter handles:
    - Pushing canonical invoices to the processor's invoice API
    - Receiving and validating inbound webhook events
    - Translating processor-native statuses to canonical InvoiceStatus
    """

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Unique identifier, e.g. 'paypal-v1'"""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'PayPal'"""

    @abstractmethod
    async def initialize(self, config: ProcessorAdapterConfig) -> None:
        """Set up HTTP client, obtain access token, verify sandbox/prod mode."""

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Verify processor API is reachable and credentials are valid."""

    @abstractmethod
    async def push_invoice(self, invoice: CanonicalInvoice) -> PushInvoiceResult:
        """
        Create or update an invoice on the processor.

        Implementations MUST pass the invoice's idempotency_key to the
        processor's native idempotency header to prevent duplicate invoices.
        """

    @abstractmethod
    async def update_invoice(
        self, processor_invoice_id: str, invoice: CanonicalInvoice
    ) -> PushInvoiceResult:
        """Update an existing processor invoice (e.g. amount change)."""

    @abstractmethod
    async def void_invoice(self, processor_invoice_id: str) -> None:
        """Cancel/void an invoice that was previously pushed."""

    @abstractmethod
    async def get_invoice_status(
        self, processor_invoice_id: str
    ) -> InvoiceStatus:
        """Fetch current status from the processor (for reconciliation)."""

    @abstractmethod
    async def validate_webhook(
        self, headers: dict[str, str], raw_body: bytes
    ) -> WebhookValidationResult:
        """
        Validate the webhook signature and parse the event type.
        MUST verify HMAC/signature before returning valid=True.
        """

    @abstractmethod
    async def process_webhook_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> CanonicalPayment | None:
        """
        Parse a validated webhook event into a CanonicalPayment.
        Returns None for events that don't represent payments.
        """

    @abstractmethod
    def transform_from_canonical(self, invoice: CanonicalInvoice) -> dict[str, Any]:
        """
        Transform a CanonicalInvoice into the processor's native API payload.
        Exposed for testability.
        """

    async def destroy(self) -> None:
        """Clean up resources. Default no-op."""

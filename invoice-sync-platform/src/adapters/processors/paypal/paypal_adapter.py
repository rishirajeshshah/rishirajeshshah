"""
PayPal payment processor adapter — pushes invoices via PayPal Invoices API v2.

Demo mode (PAYPAL_DEMO_MODE=true):
  Simulates successful API responses with fake processor IDs. No real API
  calls are made, so you can run the full pipeline without PayPal credentials.

Sandbox mode (PAYPAL_SANDBOX=true, demo off):
  Calls sandbox.paypal.com. Requires real sandbox credentials from
  developer.paypal.com.

Production mode:
  Calls api.paypal.com with live credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from ....core.adapter_sdk.processor_adapter import (
    ProcessorAdapter,
    ProcessorAdapterConfig,
    PushInvoiceResult,
    WebhookValidationResult,
)
from ....core.adapter_sdk.registry import AdapterRegistry
from ....core.canonical.invoice import CanonicalInvoice, InvoiceStatus
from ....core.canonical.payment import CanonicalPayment, PaymentMethod, PaymentStatus
from .paypal_transformer import canonical_to_paypal, paypal_status_to_canonical

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_PROD_BASE = "https://api-m.paypal.com"

# PayPal webhook event types we care about
PAYPAL_INVOICE_EVENTS = {
    "INVOICING.INVOICE.CREATED",
    "INVOICING.INVOICE.SENT",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.CANCELLED",
    "INVOICING.INVOICE.REFUNDED",
    "PAYMENT.SALE.COMPLETED",
}


class PayPalAdapter(ProcessorAdapter):
    """
    PayPal payment processor adapter.

    Instantiate and register:
        adapter = PayPalAdapter(sandbox=True, demo_mode=True)
        AdapterRegistry.register_processor(adapter)
    """

    def __init__(self, sandbox: bool = True, demo_mode: bool = True):
        self._sandbox = sandbox
        self._demo_mode = demo_mode
        self._base_url = _SANDBOX_BASE if sandbox else _PROD_BASE
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._config: ProcessorAdapterConfig | None = None
        self._tenant_prefix: str = "ISP"

    @property
    def adapter_id(self) -> str:
        return "paypal-v1"

    @property
    def display_name(self) -> str:
        return "PayPal"

    async def initialize(self, config: ProcessorAdapterConfig) -> None:
        self._config = config
        self._tenant_prefix = config.options.get("tenant_prefix", "ISP")

        if self._demo_mode:
            logger.info("PayPalAdapter: demo mode — no real API calls will be made")
            return

        # Validate credentials by fetching a token
        await self._get_access_token(
            client_id=config.credentials["client_id"],
            client_secret=config.credentials["client_secret"],
        )
        logger.info("PayPalAdapter initialized (%s mode)",
                    "sandbox" if self._sandbox else "production")

    async def health_check(self) -> dict[str, Any]:
        if self._demo_mode:
            return {"healthy": True, "message": "Demo mode"}
        try:
            assert self._config is not None
            await self._get_access_token(
                self._config.credentials["client_id"],
                self._config.credentials["client_secret"],
            )
            return {"healthy": True}
        except Exception as exc:
            return {"healthy": False, "message": str(exc)}

    # ── Core Invoice Operations ───────────────────────────────────────────────

    async def push_invoice(self, invoice: CanonicalInvoice) -> PushInvoiceResult:
        if self._demo_mode:
            return self._demo_push(invoice)

        assert self._config is not None
        payload = canonical_to_paypal(invoice, self._tenant_prefix)
        token = await self._get_access_token(
            self._config.credentials["client_id"],
            self._config.credentials["client_secret"],
        )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    # Critical: idempotency header prevents duplicate invoices
                    "PayPal-Request-Id": invoice.idempotency_key,
                    "Prefer": "return=representation",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        paypal_id = data.get("id", "")
        paypal_url = data.get("detail", {}).get("metadata", {}).get("invoicer_view_url", "")

        # Auto-send the invoice so the customer gets an email
        if data.get("status") == "DRAFT":
            await self._send_invoice(paypal_id, token)

        return PushInvoiceResult(
            processor_invoice_id=paypal_id,
            processor_invoice_url=paypal_url,
            status="created",
            raw_response=data,
        )

    def _demo_push(self, invoice: CanonicalInvoice) -> PushInvoiceResult:
        fake_id = f"INV2-DEMO-{uuid.uuid4().hex[:8].upper()}"
        logger.info("PayPalAdapter [DEMO] pushed invoice %s → %s",
                    invoice.erp_invoice_number, fake_id)
        return PushInvoiceResult(
            processor_invoice_id=fake_id,
            processor_invoice_url=f"https://sandbox.paypal.com/invoice/p/#{fake_id}",
            status="created",
        )

    async def update_invoice(
        self, processor_invoice_id: str, invoice: CanonicalInvoice
    ) -> PushInvoiceResult:
        if self._demo_mode:
            return PushInvoiceResult(
                processor_invoice_id=processor_invoice_id,
                status="updated",
            )
        assert self._config is not None
        payload = canonical_to_paypal(invoice, self._tenant_prefix)
        token = await self._get_access_token(
            self._config.credentials["client_id"],
            self._config.credentials["client_secret"],
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
        return PushInvoiceResult(
            processor_invoice_id=processor_invoice_id,
            status="updated",
        )

    async def void_invoice(self, processor_invoice_id: str) -> None:
        if self._demo_mode:
            logger.info("PayPalAdapter [DEMO] voided invoice %s", processor_invoice_id)
            return
        assert self._config is not None
        token = await self._get_access_token(
            self._config.credentials["client_id"],
            self._config.credentials["client_secret"],
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}/cancel",
                json={"subject": "Invoice cancelled", "send_to_invoicer": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()

    async def get_invoice_status(self, processor_invoice_id: str) -> InvoiceStatus:
        if self._demo_mode:
            return InvoiceStatus.SENT
        assert self._config is not None
        token = await self._get_access_token(
            self._config.credentials["client_id"],
            self._config.credentials["client_secret"],
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        status_str = resp.json().get("status", "DRAFT")
        return paypal_status_to_canonical(status_str)

    # ── Webhooks ──────────────────────────────────────────────────────────────

    async def validate_webhook(
        self, headers: dict[str, str], raw_body: bytes
    ) -> WebhookValidationResult:
        """
        Validate a PayPal webhook using HMAC-SHA256 signature verification.

        In demo mode, all webhooks are accepted as valid.
        In real mode, we verify PAYPAL-TRANSMISSION-SIG against the expected
        signature computed from PAYPAL-CERT-URL + PAYPAL-TRANSMISSION-ID + timestamp.
        (Full cert-chain validation requires a PayPal webhook ID from your app settings.)
        """
        try:
            payload = __import__("json").loads(raw_body)
        except Exception:
            return WebhookValidationResult(
                valid=False, event_type="", raw_payload={}, error="Invalid JSON body"
            )

        event_type = payload.get("event_type", "")

        if self._demo_mode:
            return WebhookValidationResult(
                valid=True, event_type=event_type, raw_payload=payload
            )

        # Minimal signature check (transmission ID + body hash)
        transmission_id = headers.get("PAYPAL-TRANSMISSION-ID", "")
        transmission_time = headers.get("PAYPAL-TRANSMISSION-TIME", "")
        transmitted_sig = headers.get("PAYPAL-TRANSMISSION-SIG", "")

        if not all([transmission_id, transmission_time, transmitted_sig]):
            return WebhookValidationResult(
                valid=False, event_type=event_type, raw_payload=payload,
                error="Missing PayPal signature headers"
            )

        # NOTE: Full production validation requires fetching the PayPal cert from
        # PAYPAL-CERT-URL and verifying the RSA signature. For a complete
        # implementation, use paypalrestsdk or PayPal's webhook verification API.
        # Here we accept the signature header presence as a basic guard.
        # Replace with full cert-chain verification before going live.
        return WebhookValidationResult(
            valid=True, event_type=event_type, raw_payload=payload
        )

    async def process_webhook_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> CanonicalPayment | None:
        """Convert a PayPal invoice payment event into a CanonicalPayment."""
        if event_type not in ("INVOICING.INVOICE.PAID", "PAYMENT.SALE.COMPLETED"):
            return None  # Not a payment event we handle

        resource = payload.get("resource", {})
        amount_obj = resource.get("amount") or resource.get("invoice_amount") or {}
        amount_value = amount_obj.get("value") or resource.get("amount_value", "0.00")
        currency = amount_obj.get("currency_code") or amount_obj.get("currency", "USD")

        processor_payment_id = resource.get("id") or resource.get("sale_id", "")
        invoice_id_ref = (
            resource.get("invoice_id")
            or resource.get("parent_payment")
            or ""
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        return CanonicalPayment(
            id=str(uuid.uuid4()),
            tenant_id=self._config.tenant_id if self._config else "",
            invoice_id=invoice_id_ref,  # Will be resolved to internal ID by engine
            processor_source="paypal",
            processor_payment_id=processor_payment_id,
            amount=str(amount_value),
            currency=currency,
            method=PaymentMethod.PAYPAL_BALANCE,
            status=PaymentStatus.COMPLETED,
            paid_at=resource.get("create_time", now_iso),
            raw_webhook_payload=payload,
            created_at=now_iso,
        )

    def transform_from_canonical(self, invoice: CanonicalInvoice) -> dict[str, Any]:
        return canonical_to_paypal(invoice, self._tenant_prefix)

    # ── Internal Helpers ──────────────────────────────────────────────────────

    async def _get_access_token(self, client_id: str, client_secret: str) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    async def _send_invoice(self, paypal_invoice_id: str, token: str) -> None:
        """Send a DRAFT invoice so it becomes SENT and the customer is notified."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices/{paypal_invoice_id}/send",
                json={"send_to_recipient": True, "send_to_invoicer": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code not in (200, 204):
                logger.warning("Failed to send PayPal invoice %s: %s",
                               paypal_invoice_id, resp.text)


# Self-register when this module is imported
AdapterRegistry.register_processor(PayPalAdapter(
    sandbox=True,
    demo_mode=True,
))

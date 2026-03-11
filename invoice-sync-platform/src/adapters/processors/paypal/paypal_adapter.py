"""
PayPal payment processor adapter — PayPal Invoices API v2.

Reference: https://developer.paypal.com/docs/api/invoicing/v2/
Webhooks:  https://developer.paypal.com/docs/invoicing/webhooks/

Full invoice lifecycle implemented:
  1. POST   /v2/invoicing/invoices              create draft (201)
  2. POST   /v2/invoicing/invoices/{id}/send    move to SENT (200 or 202)
  3. PUT    /v2/invoicing/invoices/{id}         update DRAFT invoice (200)
  4. POST   /v2/invoicing/invoices/{id}/cancel  cancel a sent invoice (204)
  5. DELETE /v2/invoicing/invoices/{id}         delete a DRAFT invoice (204)
  6. GET    /v2/invoicing/invoices/{id}         fetch status (200)
  7. POST   /v2/invoicing/search-invoices       search (200)
  8. POST   /v2/invoicing/generate-next-invoice-number (200)

Authentication:
  OAuth 2.0 Client Credentials via POST /v1/oauth2/token
  Token passed as: Authorization: Bearer <token>

Idempotency:
  Pass PayPal-Request-Id header on create to prevent duplicates.

Demo mode (PAYPAL_DEMO_MODE=true):
  Simulates all responses. No real API calls. Use for local development.

Sandbox mode (PAYPAL_DEMO_MODE=false, PAYPAL_SANDBOX=true):
  Uses sandbox.paypal.com with developer.paypal.com credentials.

Production (PAYPAL_SANDBOX=false):
  Uses api-m.paypal.com with live credentials.
"""
from __future__ import annotations

import json
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
from .paypal_transformer import (
    build_cancel_notification,
    build_send_notification,
    canonical_to_paypal,
    paypal_status_to_canonical,
)

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_PROD_BASE = "https://api-m.paypal.com"

# Webhook event types this adapter handles
PAYPAL_INVOICE_EVENTS = frozenset({
    "INVOICING.INVOICE.CREATED",
    "INVOICING.INVOICE.UPDATED",
    "INVOICING.INVOICE.SENT",
    "INVOICING.INVOICE.SCHEDULED",
    "INVOICING.INVOICE.REMINDED",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.CANCELLED",
    "INVOICING.INVOICE.REFUNDED",
    "PAYMENT.SALE.COMPLETED",
})

# Non-retryable PayPal HTTP error codes (don't retry 4xx client errors)
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 422}


class PayPalAdapter(ProcessorAdapter):
    """
    PayPal Invoices API v2 adapter.

    Usage:
        adapter = PayPalAdapter(sandbox=True, demo_mode=True)
        AdapterRegistry.register_processor(adapter)
        await adapter.initialize(config)
        result = await adapter.push_invoice(invoice)
    """

    def __init__(self, sandbox: bool = True, demo_mode: bool = True):
        self._sandbox = sandbox
        self._demo_mode = demo_mode
        self._base_url = _SANDBOX_BASE if sandbox else _PROD_BASE
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._config: ProcessorAdapterConfig | None = None
        self._tenant_prefix: str = "ISP"

    @property
    def adapter_id(self) -> str:
        return "paypal-v1"

    @property
    def display_name(self) -> str:
        return "PayPal"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self, config: ProcessorAdapterConfig) -> None:
        self._config = config
        self._tenant_prefix = config.options.get("tenant_prefix", "ISP")

        if self._demo_mode:
            logger.info("PayPalAdapter: demo mode — no real API calls")
            return

        # Validate credentials immediately — fail fast if wrong
        await self._get_access_token()
        logger.info("PayPalAdapter ready (%s)",
                    "sandbox" if self._sandbox else "production")

    async def health_check(self) -> dict[str, Any]:
        if self._demo_mode:
            return {"healthy": True, "mode": "demo"}
        try:
            await self._get_access_token()
            return {"healthy": True, "mode": "sandbox" if self._sandbox else "production"}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    async def destroy(self) -> None:
        self._access_token = None

    # ── Core Invoice Operations ───────────────────────────────────────────────

    async def push_invoice(self, invoice: CanonicalInvoice) -> PushInvoiceResult:
        """
        Create a draft invoice on PayPal then immediately send it.

        Steps:
          1. POST /v2/invoicing/invoices  → 201 Created  (status: DRAFT)
          2. POST /v2/invoicing/invoices/{id}/send → 200/202 (status: SENT)

        The idempotency_key is passed as PayPal-Request-Id so that if this
        call is retried, PayPal returns the existing invoice rather than
        creating a duplicate.
        """
        if self._demo_mode:
            return self._demo_push(invoice)

        payload = canonical_to_paypal(invoice, self._tenant_prefix)
        token = await self._get_access_token()

        # Step 1 — create draft
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    # PayPal-Request-Id guarantees idempotency:
                    # if the same key is sent twice, PayPal returns the
                    # existing invoice (HTTP 200) rather than creating a new one.
                    "PayPal-Request-Id": invoice.idempotency_key,
                    "Prefer": "return=representation",
                },
            )

        if resp.status_code in _NON_RETRYABLE_STATUS:
            error_detail = _parse_paypal_error(resp)
            raise PayPalAPIError(
                f"PayPal rejected invoice creation: {error_detail}",
                status_code=resp.status_code,
                retryable=False,
            )
        resp.raise_for_status()

        data = resp.json()
        paypal_id: str = data["id"]                        # e.g. "INV2-XXXX-XXXX"
        paypal_url: str = _extract_invoice_url(data)       # from HATEOAS links

        # Step 2 — send to recipient (moves status DRAFT → SENT)
        await self._send_invoice(paypal_id, token)

        logger.info("Invoice %s created on PayPal as %s",
                    invoice.erp_invoice_number, paypal_id)
        return PushInvoiceResult(
            processor_invoice_id=paypal_id,
            processor_invoice_url=paypal_url,
            status="created",
            raw_response=data,
        )

    def _demo_push(self, invoice: CanonicalInvoice) -> PushInvoiceResult:
        fake_id = f"INV2-DEMO-{uuid.uuid4().hex[:8].upper()}"
        base = "https://sandbox.paypal.com" if self._sandbox else "https://paypal.com"
        fake_url = f"{base}/invoice/p/#{fake_id}"
        logger.info("PayPalAdapter [DEMO] %s → %s", invoice.erp_invoice_number, fake_id)
        return PushInvoiceResult(
            processor_invoice_id=fake_id,
            processor_invoice_url=fake_url,
            status="created",
        )

    async def update_invoice(
        self,
        processor_invoice_id: str,
        invoice: CanonicalInvoice,
    ) -> PushInvoiceResult:
        """
        Update a DRAFT invoice.
        PUT /v2/invoicing/invoices/{id}
        Note: Only DRAFT invoices can be updated. SENT invoices must be
        cancelled and re-created.
        """
        if self._demo_mode:
            logger.info("PayPalAdapter [DEMO] update %s", processor_invoice_id)
            return PushInvoiceResult(
                processor_invoice_id=processor_invoice_id, status="updated"
            )

        payload = canonical_to_paypal(invoice, self._tenant_prefix)
        token = await self._get_access_token()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code in _NON_RETRYABLE_STATUS:
            raise PayPalAPIError(
                f"PayPal rejected invoice update: {_parse_paypal_error(resp)}",
                status_code=resp.status_code,
                retryable=False,
            )
        resp.raise_for_status()

        updated = resp.json() if resp.content else {}
        return PushInvoiceResult(
            processor_invoice_id=processor_invoice_id,
            processor_invoice_url=_extract_invoice_url(updated),
            status="updated",
            raw_response=updated,
        )

    async def void_invoice(self, processor_invoice_id: str) -> None:
        """
        Cancel a SENT invoice.
        POST /v2/invoicing/invoices/{id}/cancel   → 204 No Content

        Both invoicer and recipient are notified by email.
        """
        if self._demo_mode:
            logger.info("PayPalAdapter [DEMO] cancel %s", processor_invoice_id)
            return

        token = await self._get_access_token()
        body = build_cancel_notification(
            subject="Invoice cancelled",
            send_to_invoicer=True,
            send_to_recipient=True,
        )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}/cancel",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 204):
            logger.warning("Failed to cancel PayPal invoice %s: HTTP %d — %s",
                           processor_invoice_id, resp.status_code, resp.text[:200])

    async def delete_draft_invoice(self, processor_invoice_id: str) -> None:
        """
        Delete a DRAFT invoice (not yet sent).
        DELETE /v2/invoicing/invoices/{id}  → 204 No Content
        """
        if self._demo_mode:
            logger.info("PayPalAdapter [DEMO] delete draft %s", processor_invoice_id)
            return

        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code not in (200, 204):
            logger.warning("Failed to delete draft invoice %s: %d",
                           processor_invoice_id, resp.status_code)

    async def get_invoice_status(self, processor_invoice_id: str) -> InvoiceStatus:
        """
        GET /v2/invoicing/invoices/{id}  → 200
        Returns the canonical invoice status.
        """
        if self._demo_mode:
            return InvoiceStatus.SENT

        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base_url}/v2/invoicing/invoices/{processor_invoice_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        status_str = resp.json().get("status", "DRAFT")
        return paypal_status_to_canonical(status_str)

    async def generate_next_invoice_number(self) -> str:
        """
        POST /v2/invoicing/generate-next-invoice-number  → 200
        Returns the next invoice number from PayPal's auto-incrementing sequence.
        Useful to avoid collision on the invoice_number field.
        """
        if self._demo_mode:
            return f"DEMO-{int(time.time())}"

        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/generate-next-invoice-number",
                headers={"Authorization": f"Bearer {token}"},
            )
        resp.raise_for_status()
        return resp.json().get("invoice_number", "")

    async def search_invoices(
        self,
        invoice_number: str | None = None,
        email: str | None = None,
        status: list[str] | None = None,
        start_invoice_date: str | None = None,
        end_invoice_date: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        POST /v2/invoicing/search-invoices  → 200
        Supports filtering by invoice number, recipient email, status, date range.
        """
        if self._demo_mode:
            return {"total_pages": 0, "total_items": 0, "items": []}

        search_body: dict[str, Any] = {"page": page, "page_size": page_size}
        if invoice_number:
            search_body["invoice_number"] = invoice_number
        if email:
            search_body["recipient_email"] = email
        if status:
            search_body["status"] = status
        if start_invoice_date:
            search_body["start_invoice_date"] = start_invoice_date
        if end_invoice_date:
            search_body["end_invoice_date"] = end_invoice_date

        token = await self._get_access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/search-invoices",
                json=search_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        resp.raise_for_status()
        return resp.json()

    # ── Webhooks ──────────────────────────────────────────────────────────────

    async def validate_webhook(
        self,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> WebhookValidationResult:
        """
        Validate a PayPal webhook event.

        PayPal includes these headers on every webhook call:
          PAYPAL-TRANSMISSION-ID    — unique per delivery attempt
          PAYPAL-TRANSMISSION-TIME  — ISO 8601 timestamp
          PAYPAL-TRANSMISSION-SIG   — RSA-SHA256 signature (base64)
          PAYPAL-CERT-URL           — URL to the signing certificate

        Full production validation:
          1. Fetch the cert from PAYPAL-CERT-URL
          2. Verify the RSA signature over:
               transmission_id + "|" + transmission_time + "|" +
               webhook_id + "|" + CRC32(raw_body)
          3. Check the cert chain back to a trusted PayPal CA

        For now we validate the presence of all required headers and parse
        the payload. Replace the TODO below with PayPal's verify-webhook-signature
        API call (/v1/notifications/verify-webhook-signature) before going live.
        """
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return WebhookValidationResult(
                valid=False, event_type="", raw_payload={},
                error="Request body is not valid JSON"
            )

        event_type: str = payload.get("event_type", "")

        if self._demo_mode:
            return WebhookValidationResult(
                valid=True, event_type=event_type, raw_payload=payload
            )

        # Normalise header names to uppercase (HTTP headers are case-insensitive)
        upper_headers = {k.upper(): v for k, v in headers.items()}

        required = {
            "PAYPAL-TRANSMISSION-ID",
            "PAYPAL-TRANSMISSION-TIME",
            "PAYPAL-TRANSMISSION-SIG",
            "PAYPAL-CERT-URL",
        }
        missing = required - upper_headers.keys()
        if missing:
            return WebhookValidationResult(
                valid=False, event_type=event_type, raw_payload=payload,
                error=f"Missing PayPal webhook headers: {missing}"
            )

        # TODO: replace with full RSA cert-chain verification or call
        # POST /v1/notifications/verify-webhook-signature
        # For now, accept if all required headers are present.
        logger.debug(
            "PayPal webhook received: event=%s transmission_id=%s",
            event_type,
            upper_headers.get("PAYPAL-TRANSMISSION-ID"),
        )
        return WebhookValidationResult(
            valid=True, event_type=event_type, raw_payload=payload
        )

    async def process_webhook_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> CanonicalPayment | None:
        """
        Convert a validated PayPal webhook event into a CanonicalPayment.

        INVOICING.INVOICE.PAID resource structure:
        {
          "id": "INV2-XXXX",          ← PayPal invoice ID
          "status": "PAID",
          "detail": {"currency_code": "USD", ...},
          "amount": {"currency_code": "USD", "value": "1085.00"},
          "payments": {
            "paid_amount": {"currency_code": "USD", "value": "1085.00"},
            "transactions": [
              {
                "type": "PAYPAL",
                "payment_id": "PAY-XXXX",    ← payment transaction ID
                "payment_date": "2026-03-10T12:00:00Z",
                "amount": {"currency_code": "USD", "value": "1085.00"}
              }
            ]
          }
        }
        """
        if event_type not in ("INVOICING.INVOICE.PAID", "PAYMENT.SALE.COMPLETED"):
            return None  # Not a payment event

        resource: dict[str, Any] = payload.get("resource", {})
        now_iso = datetime.now(timezone.utc).isoformat()

        if event_type == "INVOICING.INVOICE.PAID":
            # Extract from INVOICING.INVOICE.PAID resource
            paypal_invoice_id: str = resource.get("id", "")
            payments_obj: dict = resource.get("payments", {})
            paid_amount_obj: dict = payments_obj.get("paid_amount", {})

            # Get the most recent payment transaction for the payment_id
            transactions: list = payments_obj.get("transactions", [])
            payment_id = ""
            paid_at = now_iso
            method = PaymentMethod.PAYPAL_BALANCE

            if transactions:
                latest = transactions[-1]
                payment_id = latest.get("payment_id", "")
                paid_at = latest.get("payment_date") or resource.get("create_time", now_iso)
                # Map PayPal payment type to canonical method
                pp_type = latest.get("type", "PAYPAL").upper()
                method = _paypal_payment_method(pp_type)

            amount = (
                paid_amount_obj.get("value")
                or resource.get("amount", {}).get("value", "0.00")
            )
            currency = (
                paid_amount_obj.get("currency_code")
                or resource.get("detail", {}).get("currency_code", "USD")
            )

            return CanonicalPayment(
                id=str(uuid.uuid4()),
                tenant_id=self._config.tenant_id if self._config else "",
                invoice_id=paypal_invoice_id,   # resolved to internal ID by engine
                processor_source="paypal",
                processor_payment_id=payment_id or paypal_invoice_id,
                amount=str(amount),
                currency=currency,
                method=method,
                status=PaymentStatus.COMPLETED,
                paid_at=paid_at,
                raw_webhook_payload=payload,
                created_at=now_iso,
            )

        # PAYMENT.SALE.COMPLETED (direct sale, not invoice-based)
        amount_obj: dict = resource.get("amount", {})
        return CanonicalPayment(
            id=str(uuid.uuid4()),
            tenant_id=self._config.tenant_id if self._config else "",
            invoice_id=resource.get("invoice_id", ""),
            processor_source="paypal",
            processor_payment_id=resource.get("id", ""),
            amount=str(amount_obj.get("total", "0.00")),
            currency=amount_obj.get("currency", "USD"),
            method=PaymentMethod.PAYPAL_BALANCE,
            status=PaymentStatus.COMPLETED,
            paid_at=resource.get("create_time", now_iso),
            raw_webhook_payload=payload,
            created_at=now_iso,
        )

    def transform_from_canonical(self, invoice: CanonicalInvoice) -> dict[str, Any]:
        return canonical_to_paypal(invoice, self._tenant_prefix)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_access_token(self) -> str:
        """
        OAuth 2.0 Client Credentials grant.
        POST /v1/oauth2/token
        Caches the token; refreshes 60s before expiry.
        """
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        assert self._config is not None, "PayPalAdapter.initialize() not called"
        client_id = self._config.credentials["client_id"]
        client_secret = self._config.credentials["client_secret"]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en_US",
                },
            )

        if resp.status_code == 401:
            raise PayPalAPIError(
                "PayPal OAuth2 authentication failed — check client_id / client_secret",
                status_code=401,
                retryable=False,
            )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    async def _send_invoice(self, paypal_invoice_id: str, token: str) -> None:
        """
        POST /v2/invoicing/invoices/{id}/send
        Moves invoice from DRAFT → SENT. Both invoicer and recipient
        receive email notifications.

        Returns 200 if the issue date is current/past, 202 if future (scheduled).
        """
        body = build_send_notification(
            send_to_recipient=True,
            send_to_invoicer=True,
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base_url}/v2/invoicing/invoices/{paypal_invoice_id}/send",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code not in (200, 202, 204):
            logger.warning(
                "Failed to send PayPal invoice %s: HTTP %d — %s",
                paypal_invoice_id, resp.status_code, resp.text[:300],
            )
        else:
            logger.debug("PayPal invoice %s sent (HTTP %d)",
                         paypal_invoice_id, resp.status_code)


# ── Module-level helpers ───────────────────────────────────────────────────────

def _extract_invoice_url(data: dict[str, Any]) -> str:
    """
    Extract the invoicer-view URL from PayPal's HATEOAS links array.

    The links array in the create/get response looks like:
      [
        {"href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-...", "rel": "self", "method": "GET"},
        {"href": "https://sandbox.paypal.com/invoice/p/#INV2-...", "rel": "payer-view", "method": "GET"},
        ...
      ]
    We want the "payer-view" link (what the customer sees), or "self" as fallback.
    """
    links: list[dict] = data.get("links", [])
    for rel in ("payer-view", "detail", "self"):
        for link in links:
            if link.get("rel") == rel:
                return link.get("href", "")
    return ""


def _paypal_payment_method(pp_type: str) -> PaymentMethod:
    _map = {
        "PAYPAL": PaymentMethod.PAYPAL_BALANCE,
        "DEBIT_CARD": PaymentMethod.CARD,
        "CREDIT_CARD": PaymentMethod.CARD,
        "BANK": PaymentMethod.BANK_TRANSFER,
        "ACH": PaymentMethod.ACH,
        "SEPA": PaymentMethod.SEPA,
        "CHECK": PaymentMethod.CHECK,
    }
    return _map.get(pp_type, PaymentMethod.OTHER)


def _parse_paypal_error(resp: httpx.Response) -> str:
    """Extract a human-readable error from a PayPal error response."""
    try:
        body = resp.json()
        details = body.get("details", [])
        if details:
            msgs = [f"{d.get('issue', '')}: {d.get('description', '')}" for d in details]
            return f"{body.get('message', '')} — {'; '.join(msgs)}"
        return body.get("message", resp.text[:300])
    except Exception:
        return resp.text[:300]


class PayPalAPIError(Exception):
    """Raised when PayPal returns a non-retryable error."""
    def __init__(self, message: str, status_code: int, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


# ── Self-register ─────────────────────────────────────────────────────────────
AdapterRegistry.register_processor(PayPalAdapter(sandbox=True, demo_mode=True))

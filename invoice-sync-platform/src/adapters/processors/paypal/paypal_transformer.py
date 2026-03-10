"""
Transforms CanonicalInvoice → PayPal Invoices API v2 payload, and maps
PayPal status strings back to canonical InvoiceStatus.

Key PayPal-specific rules:
- Invoice numbers MUST be unique per PayPal account.
  Strategy: "{tenant_prefix}-{erp_invoice_number}" prevents collisions when
  multiple tenants share a PayPal Business account.
- Amounts are sent as strings (PayPal v2 uses {"currency_code": "USD", "value": "150.00"})
- Line item unit_amount maps to unit_price
- Tax is applied at the invoice level as a percentage or fixed amount
"""
from __future__ import annotations

from typing import Any

from ....core.canonical.invoice import CanonicalInvoice, InvoiceStatus

# PayPal → canonical status mapping
_PAYPAL_STATUS_MAP = {
    "DRAFT": InvoiceStatus.DRAFT,
    "SENT": InvoiceStatus.SENT,
    "SCHEDULED": InvoiceStatus.PENDING,
    "PAYMENT_PENDING": InvoiceStatus.PENDING,
    "PARTIALLY_PAID": InvoiceStatus.PARTIALLY_PAID,
    "PAID": InvoiceStatus.PAID,
    "MARKED_AS_PAID": InvoiceStatus.PAID,
    "CANCELLED": InvoiceStatus.CANCELLED,
    "REFUNDED": InvoiceStatus.REFUNDED,
}


def canonical_to_paypal(
    invoice: CanonicalInvoice,
    tenant_prefix: str = "ISP",
) -> dict[str, Any]:
    """
    Convert a CanonicalInvoice to a PayPal Invoices API v2 create/update payload.
    """
    # Invoice number must be unique per PayPal account
    paypal_invoice_number = f"{tenant_prefix}-{invoice.erp_invoice_number}"

    # ── Line Items ────────────────────────────────────────────────────────────
    items = []
    for line in invoice.line_items:
        item: dict[str, Any] = {
            "name": line.description[:200],  # PayPal max 200 chars
            "quantity": str(line.quantity),
            "unit_amount": {
                "currency_code": invoice.currency,
                "value": line.unit_price,
            },
        }
        if line.item_code:
            item["sku"] = line.item_code[:100]

        # Per-line tax (PayPal v2 supports line-level tax)
        if line.taxes:
            first_tax = line.taxes[0]
            if first_tax.rate is not None:
                item["tax"] = {
                    "name": first_tax.name[:100],
                    "percent": str(first_tax.rate),
                }

        items.append(item)

    # ── Invoicer (bill_from) ──────────────────────────────────────────────────
    invoicer: dict[str, Any] = {
        "name": {"full_name": invoice.bill_from.name[:300]},
        "email_address": invoice.bill_from.email,
    }
    addr_from = invoice.bill_from.billing_address
    if addr_from.line1:
        invoicer["address"] = {
            "address_line_1": addr_from.line1,
            "admin_area_2": addr_from.city,
            "admin_area_1": addr_from.state or "",
            "postal_code": addr_from.postal_code,
            "country_code": addr_from.country_code,
        }

    # ── Primary Recipient (bill_to) ───────────────────────────────────────────
    recipient: dict[str, Any] = {
        "email_address": invoice.bill_to.email,
        "billing_info": {
            "name": {"full_name": invoice.bill_to.name[:300]},
            "email_address": invoice.bill_to.email,
        },
    }
    addr_to = invoice.bill_to.billing_address
    if addr_to.line1:
        recipient["billing_info"]["address"] = {
            "address_line_1": addr_to.line1,
            "admin_area_2": addr_to.city,
            "admin_area_1": addr_to.state or "",
            "postal_code": addr_to.postal_code,
            "country_code": addr_to.country_code,
        }
    if invoice.bill_to.shipping_address:
        saddr = invoice.bill_to.shipping_address
        recipient["shipping_info"] = {
            "name": {"full_name": invoice.bill_to.name},
            "address": {
                "address_line_1": saddr.line1,
                "admin_area_2": saddr.city,
                "admin_area_1": saddr.state or "",
                "postal_code": saddr.postal_code,
                "country_code": saddr.country_code,
            },
        }

    # ── Payment terms ─────────────────────────────────────────────────────────
    payment_term: dict[str, Any] = {
        "due_date": invoice.due_date,
    }
    if invoice.terms:
        payment_term["term_type"] = "NO_DUE_DATE"  # fallback; real terms need enum

    # ── Build payload ─────────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "detail": {
            "invoice_number": paypal_invoice_number,
            "invoice_date": invoice.invoice_date,
            "currency_code": invoice.currency,
            "payment_term": payment_term,
        },
        "invoicer": invoicer,
        "primary_recipients": [recipient],
        "items": items,
        "amount": {
            "breakdown": {
                "item_total": {
                    "currency_code": invoice.currency,
                    "value": invoice.subtotal,
                },
                "tax_total": {
                    "currency_code": invoice.currency,
                    "value": invoice.total_tax,
                },
                "discount": {
                    "invoice_discount": {
                        "amount": {
                            "currency_code": invoice.currency,
                            "value": invoice.total_discount,
                        }
                    }
                },
            }
        },
    }

    if invoice.memo:
        payload["detail"]["note"] = invoice.memo[:4000]

    return payload


def paypal_status_to_canonical(paypal_status: str) -> InvoiceStatus:
    return _PAYPAL_STATUS_MAP.get(paypal_status.upper(), InvoiceStatus.PENDING)

"""
Transforms CanonicalInvoice ↔ PayPal Invoices API v2 payload.

PayPal API v2 Reference:
  https://developer.paypal.com/docs/api/invoicing/v2/

Key PayPal-specific rules enforced here:
─────────────────────────────────────────
1. Invoice numbers MUST be unique per PayPal business account.
   Strategy: "{tenant_prefix}-{erp_invoice_number}" prevents collisions
   when multiple tenants share a PayPal account.

2. Amounts are money objects: {"currency_code": "USD", "value": "150.00"}
   Values are always decimal strings — never floats.

3. Name objects use given_name / surname (NOT full_name).
   For company names the full name goes in given_name; surname is empty.

4. payment_term sits INSIDE the detail object, not at the invoice root.
   Supported term_type values:
     DUE_ON_DATE     — specific due date (requires due_date field)
     DUE_ON_RECEIPT  — immediate
     NET_10 / NET_15 / NET_30 / NET_45 / NET_60
     NO_DUE_DATE

5. The send endpoint body uses a notification wrapper:
     {"notification": {"send_to_recipient": true, "send_to_invoicer": true}}

6. Cancel endpoint body uses a cancel_notification wrapper:
     {"cancel_notification": {"subject": "...", "send_to_invoicer": true}}
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ....core.canonical.invoice import CanonicalInvoice, CanonicalParty, InvoiceStatus

# ── PayPal status → canonical status ──────────────────────────────────────────
_PAYPAL_STATUS_MAP: dict[str, InvoiceStatus] = {
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

# ── term_type map: canonical terms string → PayPal enum ───────────────────────
_TERM_TYPE_MAP: dict[str, str] = {
    "net10": "NET_10",
    "net15": "NET_15",
    "net30": "NET_30",
    "net45": "NET_45",
    "net60": "NET_60",
    "due_on_receipt": "DUE_ON_RECEIPT",
    "receipt": "DUE_ON_RECEIPT",
    "immediate": "DUE_ON_RECEIPT",
}


def _split_name(full_name: str) -> dict[str, str]:
    """
    PayPal name objects require given_name and surname.
    For company names (e.g. "Acme Corp"), put the full name in given_name
    and leave surname empty — PayPal accepts this for invoicer fields.
    """
    parts = full_name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        return {"given_name": parts[0], "surname": parts[1]}
    return {"given_name": parts[0], "surname": ""}


def _money(currency: str, value: str) -> dict[str, str]:
    """Return a PayPal money object."""
    return {"currency_code": currency, "value": value}


def _build_address(party: CanonicalParty) -> dict[str, str] | None:
    """Return a PayPal address dict if the party has an address, else None."""
    addr = party.billing_address
    if not addr.line1:
        return None
    result: dict[str, str] = {
        "address_line_1": addr.line1[:300],
        "admin_area_2": addr.city,        # city
        "country_code": addr.country_code,
    }
    if addr.line2:
        result["address_line_2"] = addr.line2[:300]
    if addr.state:
        result["admin_area_1"] = addr.state  # state/province
    if addr.postal_code:
        result["postal_code"] = addr.postal_code
    return result


def _payment_term(invoice: CanonicalInvoice) -> dict[str, Any]:
    """
    Build the payment_term object for the detail block.
    Default: DUE_ON_DATE with the invoice's due_date.
    If the invoice has a terms string matching a PayPal enum, use that instead.
    """
    if invoice.terms:
        normalised = invoice.terms.lower().replace(" ", "").replace("-", "")
        pp_term = _TERM_TYPE_MAP.get(normalised)
        if pp_term:
            # NET_* types: PayPal auto-calculates due_date, no due_date needed
            return {"term_type": pp_term}

    # Fall back to DUE_ON_DATE with the explicit due_date from the invoice
    return {
        "term_type": "DUE_ON_DATE",
        "due_date": invoice.due_date,  # YYYY-MM-DD
    }


def canonical_to_paypal(
    invoice: CanonicalInvoice,
    tenant_prefix: str = "ISP",
) -> dict[str, Any]:
    """
    Convert a CanonicalInvoice to a PayPal Invoices API v2 create/update body.

    POST  /v2/invoicing/invoices          (create draft)
    PUT   /v2/invoicing/invoices/{id}     (update — same schema)

    Returns a dict ready to be serialised as JSON.
    """
    currency = invoice.currency
    paypal_invoice_number = f"{tenant_prefix}-{invoice.erp_invoice_number}"

    # ── detail ────────────────────────────────────────────────────────────────
    detail: dict[str, Any] = {
        "invoice_number": paypal_invoice_number,
        "invoice_date": invoice.invoice_date,   # YYYY-MM-DD
        "currency_code": currency,
        "payment_term": _payment_term(invoice),
    }
    # note: customer-visible memo / note field
    if invoice.memo:
        detail["note"] = invoice.memo[:4000]    # PayPal cap

    # ── invoicer (bill_from) ──────────────────────────────────────────────────
    invoicer: dict[str, Any] = {
        "name": _split_name(invoice.bill_from.name),
        "email_address": invoice.bill_from.email,
    }
    from_addr = _build_address(invoice.bill_from)
    if from_addr:
        invoicer["address"] = from_addr
    if invoice.bill_from.phone:
        # PayPal phone: strip non-digits, split country code if possible
        digits = "".join(c for c in invoice.bill_from.phone if c.isdigit())
        if digits:
            invoicer["phones"] = [{
                "country_code": "1",           # default US; override via metadata
                "national_number": digits[-10:],
                "type": "MOBILE",
            }]
    if invoice.bill_from.tax_id:
        invoicer["tax_id"] = invoice.bill_from.tax_id

    # ── primary_recipients (bill_to) ──────────────────────────────────────────
    billing_info: dict[str, Any] = {
        "name": _split_name(invoice.bill_to.name),
        "email_address": invoice.bill_to.email,
    }
    to_addr = _build_address(invoice.bill_to)
    if to_addr:
        billing_info["address"] = to_addr

    recipient: dict[str, Any] = {"billing_info": billing_info}

    # Shipping address (optional)
    if invoice.bill_to.shipping_address:
        s = invoice.bill_to.shipping_address
        shipping_addr: dict[str, str] = {
            "address_line_1": s.line1[:300],
            "admin_area_2": s.city,
            "country_code": s.country_code,
        }
        if s.state:
            shipping_addr["admin_area_1"] = s.state
        if s.postal_code:
            shipping_addr["postal_code"] = s.postal_code
        recipient["shipping_info"] = {
            "name": _split_name(invoice.bill_to.name),
            "address": shipping_addr,
        }

    # ── items ─────────────────────────────────────────────────────────────────
    items: list[dict[str, Any]] = []
    for line in invoice.line_items:
        item: dict[str, Any] = {
            "name": line.description[:200],          # PayPal max 200 chars
            "quantity": str(line.quantity),           # must be a string
            "unit_amount": _money(currency, line.unit_price),
        }
        if line.item_code:
            item["sku"] = line.item_code[:100]
        if line.description and len(line.description) > 200:
            item["description"] = line.description[:1000]

        # Per-line tax — PayPal v2 accepts tax as a percentage on the line item
        if line.taxes:
            first_tax = line.taxes[0]
            if first_tax.rate is not None:
                item["tax"] = {
                    "name": first_tax.name[:100],
                    "percent": str(first_tax.rate),   # "8.5" not 0.085
                }

        # Per-line discount (percentage preferred; fall back to fixed amount)
        if line.discounts:
            d = line.discounts[0]
            if d.rate is not None:
                item["discount"] = {"percent": str(d.rate)}
            else:
                item["discount"] = {
                    "amount": _money(currency, d.amount)
                }

        items.append(item)

    # ── amount breakdown ──────────────────────────────────────────────────────
    breakdown: dict[str, Any] = {
        "item_total": _money(currency, invoice.subtotal),
        "tax_total": _money(currency, invoice.total_tax),
    }
    # Only include discount if non-zero (avoids PayPal validation errors)
    if Decimal(invoice.total_discount) > 0:
        breakdown["discount"] = {
            "invoice_discount": {
                "amount": _money(currency, invoice.total_discount)
            }
        }

    # ── assemble ──────────────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "detail": detail,
        "invoicer": invoicer,
        "primary_recipients": [recipient],
        "items": items,
        "amount": {"breakdown": breakdown},
        "configuration": {
            "tax_calculated_after_discount": True,
            "allow_tip": False,
        },
    }

    return payload


def paypal_status_to_canonical(paypal_status: str) -> InvoiceStatus:
    return _PAYPAL_STATUS_MAP.get(paypal_status.upper(), InvoiceStatus.PENDING)


def build_send_notification(
    send_to_recipient: bool = True,
    send_to_invoicer: bool = True,
    subject: str = "",
    note: str = "",
) -> dict[str, Any]:
    """
    Build the notification body for POST /v2/invoicing/invoices/{id}/send.

    API spec:
      notification.send_to_recipient  bool
      notification.send_to_invoicer   bool
      notification.subject            str  (optional email subject)
      notification.note               str  (optional email note)
    """
    notif: dict[str, Any] = {
        "send_to_recipient": send_to_recipient,
        "send_to_invoicer": send_to_invoicer,
    }
    if subject:
        notif["subject"] = subject[:4000]
    if note:
        notif["note"] = note[:4000]
    return {"notification": notif}


def build_cancel_notification(
    subject: str = "Invoice cancelled",
    note: str = "",
    send_to_invoicer: bool = True,
    send_to_recipient: bool = True,
) -> dict[str, Any]:
    """
    Build the body for POST /v2/invoicing/invoices/{id}/cancel.

    API spec:
      cancel_notification.subject            str
      cancel_notification.note               str
      cancel_notification.send_to_invoicer   bool
      cancel_notification.send_to_recipient  bool
    """
    notif: dict[str, Any] = {
        "subject": subject[:4000],
        "send_to_invoicer": send_to_invoicer,
        "send_to_recipient": send_to_recipient,
    }
    if note:
        notif["note"] = note[:4000]
    return {"cancel_notification": notif}

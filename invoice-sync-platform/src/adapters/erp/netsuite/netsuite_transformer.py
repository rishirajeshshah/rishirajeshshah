"""
Transforms raw NetSuite SuiteQL invoice rows into CanonicalInvoice objects.

Key NetSuite-specific mapping challenges:
- Customer ID is an internal ID — we look it up or use entity metadata
- Currency field is the ISO code if returned via SuiteQL JOIN
- Custom fields (custbody_*) are passed through to metadata
- Tax groups differ per subsidiary — tax amount is read from taxamount field
- Dates come as "1/15/2026" format (MM/DD/YYYY), convert to ISO 8601
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ....core.canonical.invoice import (
    CanonicalAddress,
    CanonicalInvoice,
    CanonicalLineItem,
    CanonicalParty,
    CanonicalTax,
    InvoiceStatus,
    InvoiceSyncStatus,
)


# NetSuite status → canonical status
_NS_STATUS_MAP = {
    "Open": InvoiceStatus.SENT,
    "In Transit": InvoiceStatus.PENDING,
    "Paid In Full": InvoiceStatus.PAID,
    "Voided": InvoiceStatus.CANCELLED,
    "Pending Approval": InvoiceStatus.DRAFT,
    "Cancelled": InvoiceStatus.CANCELLED,
}


def _parse_ns_date(date_str: str | None) -> str:
    """Convert NetSuite date 'M/D/YYYY' → ISO 8601 'YYYY-MM-DD'."""
    if not date_str:
        return datetime.utcnow().strftime("%Y-%m-%d")
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return datetime.utcnow().strftime("%Y-%m-%d")


def _safe_decimal(value: Any, default: str = "0.00") -> str:
    """Convert any numeric value to a decimal string safely."""
    if value is None:
        return default
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return default


def transform_ns_invoice(
    raw: dict[str, Any],
    tenant_id: str,
    processor_target: str,
) -> CanonicalInvoice:
    """
    Transform a raw NetSuite SuiteQL invoice row (with joined fields) into
    a CanonicalInvoice.

    Expected raw keys (from SuiteQL SELECT):
      id, tranid, entity, entity_name, entity_email, custbody_*,
      trandate, duedate, amount, taxamount, amountremaining, status,
      currency, subsidiary, lines (list of line dicts)
    """
    now_iso = datetime.utcnow().isoformat() + "Z"
    erp_invoice_id = str(raw.get("id", ""))
    erp_invoice_number = str(raw.get("tranid", erp_invoice_id))

    # ── Build idempotency key ────────────────────────────────────────────────
    idem_raw = f"{tenant_id}:netsuite:{erp_invoice_id}"
    idempotency_key = hashlib.sha256(idem_raw.encode()).hexdigest()

    # ── Customer (bill_to) ───────────────────────────────────────────────────
    billing_addr_raw = raw.get("billing_address") or {}
    bill_to = CanonicalParty(
        id=str(uuid.uuid4()),
        external_id=str(raw.get("entity", "")),
        external_source="netsuite",
        type="customer",
        name=str(raw.get("entity_name", "Unknown Customer")),
        email=str(raw.get("entity_email", "")),
        phone=raw.get("entity_phone"),
        tax_id=raw.get("entity_vatregnumber"),
        currency=str(raw.get("currency", "USD")),
        billing_address=CanonicalAddress(
            line1=str(billing_addr_raw.get("addr1", "")),
            line2=billing_addr_raw.get("addr2"),
            city=str(billing_addr_raw.get("city", "")),
            state=billing_addr_raw.get("state"),
            postal_code=str(billing_addr_raw.get("zip", "")),
            country_code=str(billing_addr_raw.get("country", "US")),
        ),
    )

    # ── Company (bill_from = the NetSuite subsidiary) ────────────────────────
    subsidiary_addr = raw.get("subsidiary_address") or {}
    bill_from = CanonicalParty(
        id=str(uuid.uuid4()),
        external_id=str(raw.get("subsidiary", "1")),
        external_source="netsuite",
        type="vendor",
        name=str(raw.get("subsidiary_name", "My Company")),
        email=str(raw.get("subsidiary_email", "")),
        currency=str(raw.get("currency", "USD")),
        billing_address=CanonicalAddress(
            line1=str(subsidiary_addr.get("addr1", "")),
            city=str(subsidiary_addr.get("city", "")),
            postal_code=str(subsidiary_addr.get("zip", "")),
            country_code=str(subsidiary_addr.get("country", "US")),
        ),
    )

    # ── Line Items ────────────────────────────────────────────────────────────
    raw_lines = raw.get("lines") or []
    line_items: list[CanonicalLineItem] = []
    for idx, line in enumerate(raw_lines, start=1):
        qty = Decimal(str(line.get("quantity", 1)))
        unit_price_str = _safe_decimal(line.get("rate", "0"))
        unit_price = Decimal(unit_price_str)
        subtotal = (qty * unit_price).quantize(Decimal("0.01"))
        tax_amount = _safe_decimal(line.get("tax1amt", "0"))
        total = (subtotal + Decimal(tax_amount)).quantize(Decimal("0.01"))

        taxes = []
        if Decimal(tax_amount) > 0:
            taxes.append(CanonicalTax(
                name=str(line.get("taxcode", "Tax")),
                amount=tax_amount,
                currency=str(raw.get("currency", "USD")),
            ))

        line_items.append(CanonicalLineItem(
            id=str(uuid.uuid4()),
            line_number=idx,
            description=str(line.get("description", line.get("item_name", f"Line {idx}"))),
            item_code=line.get("item"),
            quantity=qty,
            unit_price=unit_price_str,
            currency=str(raw.get("currency", "USD")),
            subtotal=str(subtotal),
            taxes=taxes,
            total_amount=str(total),
            metadata={k: v for k, v in line.items() if k.startswith("custcol_")},
        ))

    # ── Financials ────────────────────────────────────────────────────────────
    total_amount = _safe_decimal(raw.get("amount", "0"))
    tax_total = _safe_decimal(raw.get("taxamount", "0"))
    amount_paid_str = _safe_decimal(
        str(Decimal(total_amount) - Decimal(_safe_decimal(raw.get("amountremaining", "0"))))
    )
    amount_due = _safe_decimal(raw.get("amountremaining", total_amount))

    # Subtotal = total - tax
    try:
        subtotal = str((Decimal(total_amount) - Decimal(tax_total)).quantize(Decimal("0.01")))
    except InvalidOperation:
        subtotal = total_amount

    ns_status = str(raw.get("status", "Open"))
    invoice_status = _NS_STATUS_MAP.get(ns_status, InvoiceStatus.PENDING)

    # Collect custom body fields into metadata
    metadata = {k: v for k, v in raw.items() if k.startswith("custbody_")}
    metadata["ns_status"] = ns_status
    metadata["ns_subsidiary"] = raw.get("subsidiary")

    return CanonicalInvoice(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        erp_source="netsuite",
        erp_invoice_id=erp_invoice_id,
        erp_invoice_number=erp_invoice_number,
        processor_target=processor_target,
        bill_from=bill_from,
        bill_to=bill_to,
        currency=str(raw.get("currency", "USD")),
        subtotal=subtotal,
        total_tax=tax_total,
        total_discount="0.00",
        total_amount=total_amount,
        amount_paid=amount_paid_str,
        amount_due=amount_due,
        line_items=line_items,
        invoice_date=_parse_ns_date(raw.get("trandate")),
        due_date=_parse_ns_date(raw.get("duedate")),
        invoice_status=invoice_status,
        sync_status=InvoiceSyncStatus.PENDING,
        memo=raw.get("memo"),
        metadata=metadata,
        created_at=now_iso,
        updated_at=now_iso,
    )

"""
Canonical invoice model — the single shared contract between all ERP adapters
and all payment processor adapters. All monetary values are stored as strings
(decimal representation) to avoid floating-point precision errors.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending"
    SENT = "sent"
    VIEWED = "viewed"
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class InvoiceSyncStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SYNCED = "synced"
    FAILED = "failed"
    RETRYING = "retrying"
    SKIPPED = "skipped"


class CanonicalAddress(BaseModel):
    line1: str
    line2: str | None = None
    city: str
    state: str | None = None  # ISO 3166-2 subdivision code
    postal_code: str
    country_code: str  # ISO 3166-1 alpha-2 (e.g. "US")


class CanonicalTax(BaseModel):
    name: str
    rate: Decimal | None = None  # e.g. Decimal("8.5") for 8.5%
    amount: str  # Decimal string, e.g. "12.50"
    currency: str  # ISO 4217

    @field_validator("amount")
    @classmethod
    def validate_decimal_string(cls, v: str) -> str:
        Decimal(v)  # will raise if invalid
        return v


class CanonicalDiscount(BaseModel):
    name: str | None = None
    rate: Decimal | None = None  # percentage rate if applicable
    amount: str  # Decimal string
    currency: str

    @field_validator("amount")
    @classmethod
    def validate_decimal_string(cls, v: str) -> str:
        Decimal(v)
        return v


class CanonicalParty(BaseModel):
    """Represents a customer, vendor, or company involved in an invoice."""
    id: str  # Platform-internal UUID (set by platform after first seen)
    external_id: str  # ID in source ERP system
    external_source: str  # e.g. "netsuite", "sap", "quickbooks"
    type: str  # "customer" | "vendor" | "both"
    name: str
    email: str
    phone: str | None = None
    tax_id: str | None = None  # EIN, VAT number, etc.
    currency: str  # ISO 4217 — default currency for this party
    billing_address: CanonicalAddress
    shipping_address: CanonicalAddress | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalLineItem(BaseModel):
    id: str
    line_number: int
    description: str
    item_code: str | None = None  # SKU or product code
    quantity: Decimal
    unit_price: str  # Decimal string
    currency: str
    subtotal: str  # Decimal string (quantity × unit_price before tax/discount)
    taxes: list[CanonicalTax] = Field(default_factory=list)
    discounts: list[CanonicalDiscount] = Field(default_factory=list)
    total_amount: str  # Decimal string (final after tax + discount)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("unit_price", "subtotal", "total_amount")
    @classmethod
    def validate_decimal_string(cls, v: str) -> str:
        Decimal(v)
        return v


class CanonicalInvoice(BaseModel):
    """
    The universal invoice representation used by all adapters.

    idempotency_key is SHA-256(tenant_id + erp_source + erp_invoice_id) and
    is used to prevent duplicate pushes to payment processors.
    """
    # ── Identity ────────────────────────────────────────────────────────────
    id: str  # Platform UUID
    tenant_id: str
    idempotency_key: str  # SHA-256 hash, computed automatically

    # ── ERP Origin ──────────────────────────────────────────────────────────
    erp_source: str  # "netsuite" | "sap" | "quickbooks"
    erp_invoice_id: str  # Native ID in the ERP
    erp_invoice_number: str  # Human-readable invoice number

    # ── Processor Target ────────────────────────────────────────────────────
    processor_target: str  # "paypal" | "stripe" | "adyen"
    processor_invoice_id: str | None = None  # Set after successful push
    processor_invoice_url: str | None = None  # Hosted payment page

    # ── Parties ─────────────────────────────────────────────────────────────
    bill_from: CanonicalParty
    bill_to: CanonicalParty

    # ── Financial Data ───────────────────────────────────────────────────────
    currency: str  # ISO 4217
    subtotal: str  # Decimal string
    total_tax: str  # Decimal string
    total_discount: str  # Decimal string
    total_amount: str  # Decimal string
    amount_paid: str  # Decimal string
    amount_due: str  # Decimal string
    line_items: list[CanonicalLineItem] = Field(default_factory=list)

    # ── Dates ────────────────────────────────────────────────────────────────
    invoice_date: str  # ISO 8601 date "YYYY-MM-DD"
    due_date: str  # ISO 8601 date
    paid_date: str | None = None

    # ── Status ───────────────────────────────────────────────────────────────
    invoice_status: InvoiceStatus = InvoiceStatus.PENDING
    sync_status: InvoiceSyncStatus = InvoiceSyncStatus.PENDING
    sync_attempts: int = 0
    last_sync_at: str | None = None
    last_sync_error: str | None = None

    # ── Notes ────────────────────────────────────────────────────────────────
    memo: str | None = None
    terms: str | None = None

    # ── Passthrough ──────────────────────────────────────────────────────────
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str  # ISO 8601 datetime
    updated_at: str  # ISO 8601 datetime

    @field_validator("subtotal", "total_tax", "total_discount", "total_amount",
                     "amount_paid", "amount_due")
    @classmethod
    def validate_decimal_string(cls, v: str) -> str:
        Decimal(v)
        return v

    @model_validator(mode="before")
    @classmethod
    def compute_idempotency_key(cls, values: dict) -> dict:
        """Auto-compute idempotency_key if not provided."""
        if not values.get("idempotency_key"):
            tenant_id = values.get("tenant_id", "")
            erp_source = values.get("erp_source", "")
            erp_invoice_id = values.get("erp_invoice_id", "")
            raw = f"{tenant_id}:{erp_source}:{erp_invoice_id}"
            values["idempotency_key"] = hashlib.sha256(raw.encode()).hexdigest()
        return values

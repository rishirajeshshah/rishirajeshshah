from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PaymentMethod(str, Enum):
    PAYPAL_BALANCE = "paypal_balance"
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    ACH = "ach"
    SEPA = "sepa"
    CHECK = "check"
    OTHER = "other"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERSED = "reversed"


class CanonicalPayment(BaseModel):
    """Represents a payment received from a processor webhook."""
    id: str  # Platform UUID
    tenant_id: str
    invoice_id: str  # References CanonicalInvoice.id
    processor_source: str  # "paypal" | "stripe"
    processor_payment_id: str  # Native payment ID from processor
    amount: str  # Decimal string
    currency: str  # ISO 4217
    method: PaymentMethod
    status: PaymentStatus
    paid_at: str  # ISO 8601 datetime
    raw_webhook_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str  # ISO 8601 datetime

    @field_validator("amount")
    @classmethod
    def validate_decimal_string(cls, v: str) -> str:
        Decimal(v)
        return v

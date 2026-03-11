"""
SQLAlchemy ORM models. All monetary columns use NUMERIC(19,4) — never FLOAT.
The audit_events table is append-only (never UPDATE or DELETE rows from it).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Tenants ──────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    erp_source: Mapped[str] = mapped_column(String(50), nullable=False)
    processor_target: Mapped[str] = mapped_column(String(50), nullable=False)
    sync_schedule: Mapped[str | None] = mapped_column(String(100))  # cron expression
    retry_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                   onupdate=_now)

    credential_refs: Mapped[list[TenantCredentialRef]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    invoices: Mapped[list[Invoice]] = relationship(back_populates="tenant")
    sync_jobs: Mapped[list[SyncJob]] = relationship(back_populates="tenant")


class TenantCredentialRef(Base):
    """Stores the path to secrets in Secrets Manager / Vault. Never raw credentials."""
    __tablename__ = "tenant_credential_refs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(50), nullable=False)
    # In prod: path in AWS Secrets Manager, e.g. "isp/tenants/{tenant_id}/netsuite"
    # In dev: can store JSON credentials directly (use .env for local)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tenant: Mapped[Tenant] = relationship(back_populates="credential_refs")

    __table_args__ = (
        UniqueConstraint("tenant_id", "adapter_id", name="uq_tenant_adapter"),
    )


# ── Parties ──────────────────────────────────────────────────────────────────

class Party(Base):
    __tablename__ = "parties"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_source: Mapped[str] = mapped_column(String(50), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    tax_id: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    billing_address: Mapped[dict] = mapped_column(JSONB, nullable=False)
    shipping_address: Mapped[dict | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                  onupdate=_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_source", "external_id",
                         name="uq_party_source_id"),
    )


# ── Invoices ─────────────────────────────────────────────────────────────────

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # ERP origin
    erp_source: Mapped[str] = mapped_column(String(50), nullable=False)
    erp_invoice_id: Mapped[str] = mapped_column(Text, nullable=False)
    erp_invoice_number: Mapped[str] = mapped_column(Text, nullable=False)

    # Processor target
    processor_target: Mapped[str] = mapped_column(String(50), nullable=False)
    processor_invoice_id: Mapped[str | None] = mapped_column(Text)
    processor_invoice_url: Mapped[str | None] = mapped_column(Text)

    # Parties (FK to parties table)
    bill_to_id: Mapped[str | None] = mapped_column(ForeignKey("parties.id"))
    bill_from_id: Mapped[str | None] = mapped_column(ForeignKey("parties.id"))

    # Financial — NUMERIC(19,4) avoids floating-point errors
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    total_tax: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False, default=0)
    total_discount: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False, default=0)
    total_amount: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False, default=0)
    amount_due: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)

    # Dates
    invoice_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    due_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    paid_date: Mapped[datetime | None] = mapped_column(Date)

    # Status
    invoice_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    sync_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)

    # Full canonical snapshot for reproducibility
    canonical_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                  onupdate=_now)

    tenant: Mapped[Tenant] = relationship(back_populates="invoices")
    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(back_populates="invoice")

    __table_args__ = (
        UniqueConstraint("tenant_id", "erp_source", "erp_invoice_id",
                         name="uq_invoice_erp_id"),
        Index("idx_invoices_tenant_sync", "tenant_id", "sync_status"),
        Index("idx_invoices_idempotency", "idempotency_key"),
        Index("idx_invoices_processor", "processor_target", "processor_invoice_id"),
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"),
                                             nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    item_code: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    taxes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    discounts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_amount: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number", name="uq_line_item_number"),
    )


# ── Sync Jobs ─────────────────────────────────────────────────────────────────

class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    erp_source: Mapped[str] = mapped_column(String(50), nullable=False)
    processor_target: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tenant: Mapped[Tenant] = relationship(back_populates="sync_jobs")

    __table_args__ = (
        Index("idx_sync_jobs_tenant_status", "tenant_id", "status"),
    )


# ── Payments ──────────────────────────────────────────────────────────────────

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), nullable=False)
    processor_source: Mapped[str] = mapped_column(String(50), nullable=False)
    processor_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(19, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_webhook_payload: Mapped[dict | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")

    __table_args__ = (
        UniqueConstraint("processor_source", "processor_payment_id",
                         name="uq_payment_processor_id"),
    )


# ── Audit Events (append-only) ────────────────────────────────────────────────

class AuditEvent(Base):
    """
    Immutable audit trail for all state changes.
    NEVER UPDATE or DELETE rows from this table.
    """
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                   nullable=False)

    __table_args__ = (
        Index("idx_audit_tenant_entity", "tenant_id", "entity_type", "entity_id"),
        Index("idx_audit_occurred_at", "occurred_at"),
    )

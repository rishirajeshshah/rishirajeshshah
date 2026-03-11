"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-10

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("erp_source", sa.String(50), nullable=False),
        sa.Column("processor_target", sa.String(50), nullable=False),
        sa.Column("sync_schedule", sa.String(100)),
        sa.Column("retry_policy", JSONB, nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    op.create_table(
        "tenant_credential_refs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("adapter_id", sa.String(50), nullable=False),
        sa.Column("secret_ref", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("tenant_id", "adapter_id", name="uq_tenant_adapter"),
    )

    op.create_table(
        "parties",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("external_source", sa.String(50), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("phone", sa.Text),
        sa.Column("tax_id", sa.Text),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("billing_address", JSONB, nullable=False),
        sa.Column("shipping_address", JSONB),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("tenant_id", "external_source", "external_id",
                            name="uq_party_source_id"),
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("erp_source", sa.String(50), nullable=False),
        sa.Column("erp_invoice_id", sa.Text, nullable=False),
        sa.Column("erp_invoice_number", sa.Text, nullable=False),
        sa.Column("processor_target", sa.String(50), nullable=False),
        sa.Column("processor_invoice_id", sa.Text),
        sa.Column("processor_invoice_url", sa.Text),
        sa.Column("bill_to_id", sa.String(36), sa.ForeignKey("parties.id")),
        sa.Column("bill_from_id", sa.String(36), sa.ForeignKey("parties.id")),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("subtotal", sa.Numeric(19, 4), nullable=False),
        sa.Column("total_tax", sa.Numeric(19, 4), nullable=False, server_default="0"),
        sa.Column("total_discount", sa.Numeric(19, 4), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(19, 4), nullable=False),
        sa.Column("amount_paid", sa.Numeric(19, 4), nullable=False, server_default="0"),
        sa.Column("amount_due", sa.Numeric(19, 4), nullable=False),
        sa.Column("invoice_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("paid_date", sa.Date),
        sa.Column("invoice_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sync_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sync_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_error", sa.Text),
        sa.Column("canonical_snapshot", JSONB, nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("tenant_id", "erp_source", "erp_invoice_id",
                            name="uq_invoice_erp_id"),
    )
    op.create_index("idx_invoices_tenant_sync", "invoices", ["tenant_id", "sync_status"])
    op.create_index("idx_invoices_idempotency", "invoices", ["idempotency_key"])
    op.create_index("idx_invoices_processor", "invoices", ["processor_target",
                                                             "processor_invoice_id"])

    op.create_table(
        "line_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("invoice_id", sa.String(36),
                  sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("line_number", sa.Integer, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("item_code", sa.Text),
        sa.Column("quantity", sa.Numeric(19, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(19, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("subtotal", sa.Numeric(19, 4), nullable=False),
        sa.Column("taxes", JSONB, nullable=False, server_default="[]"),
        sa.Column("discounts", JSONB, nullable=False, server_default="[]"),
        sa.Column("total_amount", sa.Numeric(19, 4), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("invoice_id", "line_number", name="uq_line_item_number"),
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("erp_source", sa.String(50), nullable=False),
        sa.Column("processor_target", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("success_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("triggered_by", sa.String(20), nullable=False, server_default="api"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_sync_jobs_tenant_status", "sync_jobs", ["tenant_id", "status"])

    op.create_table(
        "payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("invoice_id", sa.String(36), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("processor_source", sa.String(50), nullable=False),
        sa.Column("processor_payment_id", sa.Text, nullable=False),
        sa.Column("amount", sa.Numeric(19, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("method", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_webhook_payload", JSONB),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("processor_source", "processor_payment_id",
                            name="uq_payment_processor_id"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("before_state", JSONB),
        sa.Column("after_state", JSONB),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_audit_tenant_entity", "audit_events",
                    ["tenant_id", "entity_type", "entity_id"])
    op.create_index("idx_audit_occurred_at", "audit_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("payments")
    op.drop_table("sync_jobs")
    op.drop_table("line_items")
    op.drop_table("invoices")
    op.drop_table("parties")
    op.drop_table("tenant_credential_refs")
    op.drop_table("tenants")

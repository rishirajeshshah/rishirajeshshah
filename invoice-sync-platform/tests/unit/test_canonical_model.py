"""Unit tests for the CanonicalInvoice model."""
import hashlib
import uuid
from decimal import Decimal

import pytest

from src.core.canonical.invoice import (
    CanonicalAddress,
    CanonicalInvoice,
    CanonicalLineItem,
    CanonicalParty,
    InvoiceStatus,
    InvoiceSyncStatus,
)


def make_party(name: str = "Test Customer", external_id: str = "CUST-001") -> CanonicalParty:
    return CanonicalParty(
        id=str(uuid.uuid4()),
        external_id=external_id,
        external_source="netsuite",
        type="customer",
        name=name,
        email="customer@example.com",
        currency="USD",
        billing_address=CanonicalAddress(
            line1="123 Main St",
            city="San Jose",
            postal_code="95101",
            country_code="US",
        ),
    )


def make_invoice(**kwargs) -> CanonicalInvoice:
    defaults = dict(
        id=str(uuid.uuid4()),
        tenant_id="tenant-001",
        erp_source="netsuite",
        erp_invoice_id="INV-001",
        erp_invoice_number="INV-2026-0001",
        processor_target="paypal",
        bill_from=make_party("Acme Corp", "ACME"),
        bill_to=make_party("Customer A", "CUST-001"),
        currency="USD",
        subtotal="1000.00",
        total_tax="85.00",
        total_discount="0.00",
        total_amount="1085.00",
        amount_paid="0.00",
        amount_due="1085.00",
        invoice_date="2026-03-10",
        due_date="2026-04-10",
        created_at="2026-03-10T00:00:00Z",
        updated_at="2026-03-10T00:00:00Z",
    )
    defaults.update(kwargs)
    return CanonicalInvoice(**defaults)


class TestCanonicalInvoice:
    def test_idempotency_key_auto_computed(self):
        inv = make_invoice(tenant_id="tenant-001", erp_source="netsuite",
                           erp_invoice_id="INV-001")
        expected = hashlib.sha256("tenant-001:netsuite:INV-001".encode()).hexdigest()
        assert inv.idempotency_key == expected

    def test_idempotency_key_preserved_if_provided(self):
        custom_key = "custom-key-abc123"
        inv = make_invoice(idempotency_key=custom_key)
        assert inv.idempotency_key == custom_key

    def test_idempotency_key_is_deterministic(self):
        inv1 = make_invoice(tenant_id="t1", erp_invoice_id="INV-100")
        inv2 = make_invoice(tenant_id="t1", erp_invoice_id="INV-100")
        assert inv1.idempotency_key == inv2.idempotency_key

    def test_different_invoices_have_different_idempotency_keys(self):
        inv1 = make_invoice(tenant_id="t1", erp_invoice_id="INV-100")
        inv2 = make_invoice(tenant_id="t1", erp_invoice_id="INV-101")
        assert inv1.idempotency_key != inv2.idempotency_key

    def test_different_tenants_have_different_idempotency_keys(self):
        inv1 = make_invoice(tenant_id="tenant-A", erp_invoice_id="INV-100")
        inv2 = make_invoice(tenant_id="tenant-B", erp_invoice_id="INV-100")
        assert inv1.idempotency_key != inv2.idempotency_key

    def test_invalid_monetary_amount_raises(self):
        with pytest.raises(Exception):
            make_invoice(total_amount="not-a-number")

    def test_default_sync_status_is_pending(self):
        inv = make_invoice()
        assert inv.sync_status == InvoiceSyncStatus.PENDING

    def test_model_json_roundtrip(self):
        inv = make_invoice()
        data = inv.model_dump(mode="json")
        inv2 = CanonicalInvoice(**data)
        assert inv.idempotency_key == inv2.idempotency_key
        assert inv.total_amount == inv2.total_amount


class TestCanonicalLineItem:
    def test_valid_line_item(self):
        item = CanonicalLineItem(
            id=str(uuid.uuid4()),
            line_number=1,
            description="Professional Services",
            quantity=Decimal("2"),
            unit_price="500.00",
            currency="USD",
            subtotal="1000.00",
            total_amount="1000.00",
        )
        assert item.line_number == 1

    def test_invalid_unit_price_raises(self):
        with pytest.raises(Exception):
            CanonicalLineItem(
                id=str(uuid.uuid4()),
                line_number=1,
                description="Bad item",
                quantity=Decimal("1"),
                unit_price="abc",  # invalid
                currency="USD",
                subtotal="0.00",
                total_amount="0.00",
            )

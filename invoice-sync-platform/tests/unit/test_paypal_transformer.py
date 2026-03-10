"""Unit tests for the PayPal transformer."""
import uuid
from decimal import Decimal

from src.adapters.processors.paypal.paypal_transformer import canonical_to_paypal
from src.core.canonical.invoice import (
    CanonicalAddress,
    CanonicalInvoice,
    CanonicalLineItem,
    CanonicalParty,
)


def _make_invoice() -> CanonicalInvoice:
    addr = CanonicalAddress(
        line1="100 Main St", city="San Jose", postal_code="95101", country_code="US"
    )
    bill_to = CanonicalParty(
        id=str(uuid.uuid4()), external_id="CUST-1", external_source="netsuite",
        type="customer", name="Acme Customer", email="customer@acme.com",
        currency="USD", billing_address=addr,
    )
    bill_from = CanonicalParty(
        id=str(uuid.uuid4()), external_id="CORP-1", external_source="netsuite",
        type="vendor", name="My Company", email="billing@myco.com",
        currency="USD", billing_address=addr,
    )
    return CanonicalInvoice(
        id=str(uuid.uuid4()), tenant_id="tenant-1",
        erp_source="netsuite", erp_invoice_id="INV-001",
        erp_invoice_number="INV-2026-0001",
        processor_target="paypal",
        bill_from=bill_from, bill_to=bill_to,
        currency="USD",
        subtotal="1000.00", total_tax="85.00",
        total_discount="0.00", total_amount="1085.00",
        amount_paid="0.00", amount_due="1085.00",
        invoice_date="2026-03-10", due_date="2026-04-10",
        line_items=[
            CanonicalLineItem(
                id=str(uuid.uuid4()), line_number=1,
                description="Professional Services", item_code="SVC-001",
                quantity=Decimal("2"), unit_price="500.00",
                currency="USD", subtotal="1000.00", total_amount="1000.00",
            )
        ],
        created_at="2026-03-10T00:00:00Z", updated_at="2026-03-10T00:00:00Z",
    )


class TestCanonicalToPayPal:
    def test_invoice_number_prefixed_with_tenant(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv, tenant_prefix="DEMO")
        assert payload["detail"]["invoice_number"] == "DEMO-INV-2026-0001"

    def test_currency_code_preserved(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        assert payload["detail"]["currency_code"] == "USD"

    def test_line_items_mapped(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        assert len(payload["items"]) == 1
        assert payload["items"][0]["name"] == "Professional Services"
        assert payload["items"][0]["sku"] == "SVC-001"
        assert payload["items"][0]["unit_amount"]["value"] == "500.00"

    def test_amount_breakdown(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        breakdown = payload["amount"]["breakdown"]
        assert breakdown["item_total"]["value"] == "1000.00"
        assert breakdown["tax_total"]["value"] == "85.00"

    def test_invoicer_email(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        assert payload["invoicer"]["email_address"] == "billing@myco.com"

    def test_recipient_email(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        assert payload["primary_recipients"][0]["email_address"] == "customer@acme.com"

    def test_due_date_set(self):
        inv = _make_invoice()
        payload = canonical_to_paypal(inv)
        assert payload["detail"]["payment_term"]["due_date"] == "2026-04-10"

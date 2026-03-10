"""Unit tests for the PayPal transformer."""
import uuid
from decimal import Decimal

from src.adapters.processors.paypal.paypal_transformer import (
    build_cancel_notification,
    build_send_notification,
    canonical_to_paypal,
    paypal_status_to_canonical,
)
from src.core.canonical.invoice import (
    CanonicalAddress,
    CanonicalDiscount,
    CanonicalInvoice,
    CanonicalLineItem,
    CanonicalParty,
    CanonicalTax,
    InvoiceStatus,
)


def _make_invoice(
    *,
    terms: str | None = None,
    total_discount: str = "0.00",
    memo: str | None = None,
) -> CanonicalInvoice:
    addr = CanonicalAddress(
        line1="100 Main St", city="San Jose", postal_code="95101", country_code="US"
    )
    bill_to = CanonicalParty(
        id=str(uuid.uuid4()),
        external_id="CUST-1",
        external_source="netsuite",
        type="customer",
        name="Acme Customer",
        email="customer@acme.com",
        currency="USD",
        billing_address=addr,
    )
    bill_from = CanonicalParty(
        id=str(uuid.uuid4()),
        external_id="CORP-1",
        external_source="netsuite",
        type="vendor",
        name="My Company",
        email="billing@myco.com",
        currency="USD",
        billing_address=addr,
    )
    return CanonicalInvoice(
        id=str(uuid.uuid4()),
        tenant_id="tenant-1",
        erp_source="netsuite",
        erp_invoice_id="INV-001",
        erp_invoice_number="INV-2026-0001",
        processor_target="paypal",
        bill_from=bill_from,
        bill_to=bill_to,
        currency="USD",
        subtotal="1000.00",
        total_tax="85.00",
        total_discount=total_discount,
        total_amount="1085.00",
        amount_paid="0.00",
        amount_due="1085.00",
        invoice_date="2026-03-10",
        due_date="2026-04-10",
        terms=terms,
        memo=memo,
        line_items=[
            CanonicalLineItem(
                id=str(uuid.uuid4()),
                line_number=1,
                description="Professional Services",
                item_code="SVC-001",
                quantity=Decimal("2"),
                unit_price="500.00",
                currency="USD",
                subtotal="1000.00",
                total_amount="1000.00",
            )
        ],
        created_at="2026-03-10T00:00:00Z",
        updated_at="2026-03-10T00:00:00Z",
    )


class TestCanonicalToPayPal:
    def test_invoice_number_prefixed_with_tenant(self):
        payload = canonical_to_paypal(_make_invoice(), tenant_prefix="DEMO")
        assert payload["detail"]["invoice_number"] == "DEMO-INV-2026-0001"

    def test_default_tenant_prefix(self):
        payload = canonical_to_paypal(_make_invoice())
        assert payload["detail"]["invoice_number"] == "ISP-INV-2026-0001"

    def test_currency_code_preserved(self):
        payload = canonical_to_paypal(_make_invoice())
        assert payload["detail"]["currency_code"] == "USD"

    def test_invoice_date_preserved(self):
        payload = canonical_to_paypal(_make_invoice())
        assert payload["detail"]["invoice_date"] == "2026-03-10"

    # ── name objects (given_name / surname, NOT full_name) ─────────────────────

    def test_invoicer_name_split(self):
        payload = canonical_to_paypal(_make_invoice())
        name = payload["invoicer"]["name"]
        assert name == {"given_name": "My", "surname": "Company"}
        assert "full_name" not in name

    def test_recipient_name_split(self):
        payload = canonical_to_paypal(_make_invoice())
        name = payload["primary_recipients"][0]["billing_info"]["name"]
        assert name == {"given_name": "Acme", "surname": "Customer"}
        assert "full_name" not in name

    def test_single_word_name_uses_given_name_only(self):
        inv = _make_invoice()
        inv.bill_from.name = "Acme"
        payload = canonical_to_paypal(inv)
        name = payload["invoicer"]["name"]
        assert name["given_name"] == "Acme"
        assert name["surname"] == ""

    # ── email addresses ────────────────────────────────────────────────────────

    def test_invoicer_email(self):
        payload = canonical_to_paypal(_make_invoice())
        assert payload["invoicer"]["email_address"] == "billing@myco.com"

    def test_recipient_email(self):
        payload = canonical_to_paypal(_make_invoice())
        assert (
            payload["primary_recipients"][0]["billing_info"]["email_address"]
            == "customer@acme.com"
        )

    # ── payment_term lives INSIDE detail ──────────────────────────────────────

    def test_payment_term_inside_detail(self):
        payload = canonical_to_paypal(_make_invoice())
        assert "payment_term" in payload["detail"]
        assert "payment_term" not in payload   # must NOT be at root

    def test_due_date_default_term_type(self):
        payload = canonical_to_paypal(_make_invoice())
        pt = payload["detail"]["payment_term"]
        assert pt["term_type"] == "DUE_ON_DATE"
        assert pt["due_date"] == "2026-04-10"

    def test_net30_term_mapped(self):
        payload = canonical_to_paypal(_make_invoice(terms="net30"))
        pt = payload["detail"]["payment_term"]
        assert pt["term_type"] == "NET_30"
        assert "due_date" not in pt

    def test_net60_term_mapped(self):
        payload = canonical_to_paypal(_make_invoice(terms="net60"))
        assert payload["detail"]["payment_term"]["term_type"] == "NET_60"

    def test_due_on_receipt_term_mapped(self):
        payload = canonical_to_paypal(_make_invoice(terms="receipt"))
        assert payload["detail"]["payment_term"]["term_type"] == "DUE_ON_RECEIPT"

    def test_unknown_term_falls_back_to_due_on_date(self):
        payload = canonical_to_paypal(_make_invoice(terms="custom_term"))
        pt = payload["detail"]["payment_term"]
        assert pt["term_type"] == "DUE_ON_DATE"
        assert pt["due_date"] == "2026-04-10"

    # ── line items ─────────────────────────────────────────────────────────────

    def test_line_items_mapped(self):
        payload = canonical_to_paypal(_make_invoice())
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["name"] == "Professional Services"
        assert item["sku"] == "SVC-001"
        assert item["unit_amount"]["value"] == "500.00"
        assert item["quantity"] == "2"

    def test_line_item_quantity_is_string(self):
        payload = canonical_to_paypal(_make_invoice())
        assert isinstance(payload["items"][0]["quantity"], str)

    # ── amount breakdown ───────────────────────────────────────────────────────

    def test_amount_breakdown(self):
        payload = canonical_to_paypal(_make_invoice())
        breakdown = payload["amount"]["breakdown"]
        assert breakdown["item_total"]["value"] == "1000.00"
        assert breakdown["tax_total"]["value"] == "85.00"

    def test_zero_discount_excluded_from_breakdown(self):
        payload = canonical_to_paypal(_make_invoice(total_discount="0.00"))
        assert "discount" not in payload["amount"]["breakdown"]

    def test_nonzero_discount_included_in_breakdown(self):
        payload = canonical_to_paypal(_make_invoice(total_discount="50.00"))
        discount = payload["amount"]["breakdown"]["discount"]
        assert discount["invoice_discount"]["amount"]["value"] == "50.00"

    # ── memo / note ────────────────────────────────────────────────────────────

    def test_memo_added_to_detail(self):
        payload = canonical_to_paypal(_make_invoice(memo="Net 30 payment terms"))
        assert payload["detail"]["note"] == "Net 30 payment terms"

    def test_no_memo_key_absent(self):
        payload = canonical_to_paypal(_make_invoice(memo=None))
        assert "note" not in payload["detail"]

    # ── configuration block ────────────────────────────────────────────────────

    def test_configuration_block_present(self):
        payload = canonical_to_paypal(_make_invoice())
        cfg = payload["configuration"]
        assert cfg["tax_calculated_after_discount"] is True
        assert cfg["allow_tip"] is False


class TestPayPalStatusMapping:
    def test_paid_maps_to_paid(self):
        assert paypal_status_to_canonical("PAID") == InvoiceStatus.PAID

    def test_marked_as_paid_maps_to_paid(self):
        assert paypal_status_to_canonical("MARKED_AS_PAID") == InvoiceStatus.PAID

    def test_draft_maps_to_draft(self):
        assert paypal_status_to_canonical("DRAFT") == InvoiceStatus.DRAFT

    def test_cancelled_maps_to_cancelled(self):
        assert paypal_status_to_canonical("CANCELLED") == InvoiceStatus.CANCELLED

    def test_unknown_status_maps_to_pending(self):
        assert paypal_status_to_canonical("UNKNOWN_XYZ") == InvoiceStatus.PENDING

    def test_case_insensitive(self):
        assert paypal_status_to_canonical("paid") == InvoiceStatus.PAID


class TestNotificationBuilders:
    def test_send_notification_wrapper(self):
        body = build_send_notification()
        assert "notification" in body
        assert body["notification"]["send_to_recipient"] is True
        assert body["notification"]["send_to_invoicer"] is True

    def test_send_notification_custom_flags(self):
        body = build_send_notification(send_to_recipient=False, send_to_invoicer=False)
        assert body["notification"]["send_to_recipient"] is False
        assert body["notification"]["send_to_invoicer"] is False

    def test_send_notification_with_subject_and_note(self):
        body = build_send_notification(subject="Your invoice", note="Please pay")
        assert body["notification"]["subject"] == "Your invoice"
        assert body["notification"]["note"] == "Please pay"

    def test_send_notification_empty_subject_omitted(self):
        body = build_send_notification()
        assert "subject" not in body["notification"]
        assert "note" not in body["notification"]

    def test_cancel_notification_wrapper(self):
        body = build_cancel_notification()
        assert "cancel_notification" in body
        assert body["cancel_notification"]["subject"] == "Invoice cancelled"
        assert body["cancel_notification"]["send_to_invoicer"] is True
        assert body["cancel_notification"]["send_to_recipient"] is True

    def test_cancel_notification_custom_subject(self):
        body = build_cancel_notification(subject="Order void")
        assert body["cancel_notification"]["subject"] == "Order void"

    def test_cancel_notification_with_note(self):
        body = build_cancel_notification(note="Billing error")
        assert body["cancel_notification"]["note"] == "Billing error"

    def test_cancel_notification_empty_note_omitted(self):
        body = build_cancel_notification()
        assert "note" not in body["cancel_notification"]

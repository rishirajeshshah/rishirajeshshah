"""Unit tests for the NetSuite transformer."""
from src.adapters.erp.netsuite.netsuite_transformer import transform_ns_invoice
from src.core.canonical.invoice import InvoiceStatus, InvoiceSyncStatus


def _raw_ns_invoice():
    return {
        "id": "12345",
        "tranid": "INV-2026-0001",
        "trandate": "3/10/2026",
        "duedate": "4/10/2026",
        "memo": "Payment due net 30",
        "amount": "1085.00",
        "taxamount": "85.00",
        "amountremaining": "1085.00",
        "status": "Open",
        "entity": "CUST-001",
        "entity_name": "Acme Customer Inc",
        "entity_email": "billing@acme.com",
        "currency": "USD",
        "subsidiary": "1",
        "subsidiary_name": "My Corp HQ",
        "subsidiary_email": "ar@mycorp.com",
        "billing_address": {
            "addr1": "100 Enterprise Way",
            "city": "San Jose",
            "state": "CA",
            "zip": "95110",
            "country": "US",
        },
        "lines": [
            {
                "description": "Professional Services - Q1",
                "item": "SVC-001",
                "quantity": "2",
                "rate": "500.00",
                "tax1amt": "85.00",
                "taxcode": "CA-SALES-TAX",
            }
        ],
    }


class TestNetSuiteTransformer:
    def test_erp_source_is_netsuite(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.erp_source == "netsuite"

    def test_erp_invoice_id(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.erp_invoice_id == "12345"

    def test_erp_invoice_number(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.erp_invoice_number == "INV-2026-0001"

    def test_date_conversion(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.invoice_date == "2026-03-10"
        assert inv.due_date == "2026-04-10"

    def test_open_status_maps_to_sent(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.invoice_status == InvoiceStatus.SENT

    def test_sync_status_defaults_to_pending(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.sync_status == InvoiceSyncStatus.PENDING

    def test_total_amount(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.total_amount == "1085.00"

    def test_total_tax(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.total_tax == "85.00"

    def test_bill_to_name(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.bill_to.name == "Acme Customer Inc"
        assert inv.bill_to.email == "billing@acme.com"

    def test_line_item_description(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert len(inv.line_items) == 1
        assert "Professional Services" in inv.line_items[0].description

    def test_line_item_tax(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        line = inv.line_items[0]
        assert len(line.taxes) == 1
        assert line.taxes[0].amount == "85.00"

    def test_currency(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.currency == "USD"

    def test_idempotency_key_is_sha256(self):
        import hashlib
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        expected = hashlib.sha256("tenant-1:netsuite:12345".encode()).hexdigest()
        assert inv.idempotency_key == expected

    def test_memo_preserved(self):
        inv = transform_ns_invoice(_raw_ns_invoice(), "tenant-1", "paypal")
        assert inv.memo == "Payment due net 30"

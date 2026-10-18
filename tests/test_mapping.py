"""D6: taxes by name and type (then amount); a missing tax fails the sync."""

from odoo import Command
from odoo.tests import tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestMapping(DualBookkeepingCommon):
    def _both(self):
        """Admin in both companies (records of the official company)."""
        return self.env(
            context=dict(
                self.env.context,
                allowed_company_ids=(self.company + self.official).ids,
            )
        )

    def _tax(self, name, company, amount=7.5, type_tax_use="sale"):
        return self._both()["account.tax"].create(
            {
                "name": name,
                "amount": amount,
                "amount_type": "percent",
                "type_tax_use": type_tax_use,
                "company_id": company.id,
            }
        )

    def _taxed_invoice(self, tax, **extra):
        return self._invoice(
            invoice_line_ids=[
                Command.create(
                    {
                        "name": "line",
                        "quantity": 1.0,
                        "price_unit": 1000.0,
                        "tax_ids": [Command.set(tax.ids)],
                    }
                )
            ],
            post=True,
            **extra,
        )

    def _last_error(self, move):
        return self._logs(move).filtered(lambda log: log.sync_state == "error")[:1]

    def test_tax_by_name_and_type(self):
        """Same name in the official company, but a purchase tax: not used."""
        self._tax("NV VAT 7.5%", self.official, type_tax_use="purchase")
        official_tax = self._tax("NV VAT 7.5%", self.official)
        invoice = self._taxed_invoice(self._tax("NV VAT 7.5%", self.company))
        mirror = self._mirror(invoice)
        self.assertEqual(mirror.invoice_line_ids.tax_ids, official_tax)
        self.assertAlmostEqual(mirror.amount_tax, invoice.amount_tax)

    def test_tax_by_amount_when_unique(self):
        official_tax = self._tax("Official sales 7.5%", self.official)
        invoice = self._taxed_invoice(self._tax("NV VAT 7.5%", self.company))
        self.assertEqual(self._mirror(invoice).invoice_line_ids.tax_ids, official_tax)

    def test_missing_tax_fails_and_retry(self):
        """D6: the sync fails with the tax's name, the invoice stays posted
        with an error log; created in the official company, a retry syncs
        it."""
        invoice = self._taxed_invoice(self._tax("NV VAT 7.5%", self.company))
        self.assertEqual(invoice.state, "posted")
        self.assertFalse(self._mirror(invoice))
        self.assertEqual(invoice.sync_state, "error")
        error = self._last_error(invoice).error_message
        self.assertIn("NV VAT 7.5%", error)
        self.assertIn("no match", error)
        self.assertIn(self.official.name, error)

        self._tax("NV VAT 7.5%", self.official)
        self._last_error(invoice).with_user(self.sync_manager).action_retry()
        mirror = self._mirror(invoice)
        self.assertTrue(mirror)
        self.assertAlmostEqual(mirror.amount_tax, 75.0)
        self.assertEqual(invoice.sync_state, "synced")

    def test_ambiguous_tax_fails(self):
        self._tax("Official A 7.5%", self.official)
        self._tax("Official B 7.5%", self.official)
        invoice = self._taxed_invoice(self._tax("NV VAT 7.5%", self.company))
        self.assertFalse(self._mirror(invoice))
        self.assertIn("several matches", self._last_error(invoice).error_message)

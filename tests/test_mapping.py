"""D6: taxes by name and type (then amount); a missing tax fails the sync."""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import DualBookkeepingCommon

ENGINE_LOGGER = "odoo.addons.nv_dual_bookkeeping.services.sync_engine"


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
        with self.assertLogs(ENGINE_LOGGER, "ERROR"):
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
        with self.assertLogs(ENGINE_LOGGER, "ERROR"):
            invoice = self._taxed_invoice(self._tax("NV VAT 7.5%", self.company))
        self.assertFalse(self._mirror(invoice))
        self.assertIn("several matches", self._last_error(invoice).error_message)

    # ------------------------------------------------------------------
    # Partner: the mirror keeps the same contact
    # ------------------------------------------------------------------

    def test_mirror_keeps_same_partner_when_vat_shared(self):
        """Branches of one group share a VAT: each mirror keeps its own branch
        (the VAT search used to return the first branch for all of them)."""
        Partner = self.env["res.partner"]
        first = Partner.create({"name": "Group - First branch", "vat": "NVSHAREDVAT1", "is_official": True})
        other = Partner.create({"name": "Group - Other branch", "vat": "NVSHAREDVAT1", "is_official": True})
        for partner in (first, other):
            invoice = self._invoice(partner=partner, post=True)
            self.assertEqual(self._mirror(invoice).partner_id, partner)
        payment = self._payment(partner=other)
        self.assertEqual(self._mirror(payment.move_id).partner_id, other)

    def test_mirror_partner_restricted_to_main_refused(self):
        """A contact restricted to the operating company cannot be used in the
        official one: no contact found by VAT / name instead (not even a twin
        with the same VAT and name), no stub - the sync stops until the
        contact is made available there."""
        engine = self.env["nv.sync.engine"]
        main_only = self.env["res.partner"].create(
            {"name": "Main only", "vat": "NVMAINONLY1", "company_id": self.company.id})
        self._both()["res.partner"].create(
            {"name": "Main only", "vat": "NVMAINONLY1", "company_id": self.official.id})
        with self.assertRaisesRegex(UserError, "is not available in"):
            engine._map_partner(main_only, self.official)
        self.assertFalse(engine._map_partner(main_only, self.official, create=False))

    def test_mirror_partner_companies_without_official_refused(self):
        """companies filled without the Official Books company: not synced,
        and the sync does not add it by itself."""
        if "company_ids" not in self.env["res.partner"]._fields:
            self.skipTest("company_ids needs nv_pr_prunelle")
        engine = self.env["nv.sync.engine"]
        partner = self.env["res.partner"].create(
            {"name": "Main companies only", "company_ids": [(6, 0, self.company.ids)]})
        with self.assertRaisesRegex(UserError, "is not available in"):
            engine._map_partner(partner, self.official)
        self.assertNotIn(self.official, partner.company_ids)
        partner.company_ids = [(4, self.official.id)]
        self.assertEqual(engine._map_partner(partner, self.official), partner)

    def test_mirror_partner_empty_companies_allowed(self):
        engine = self.env["nv.sync.engine"]
        partner = self.env["res.partner"].create({"name": "All companies"})
        self.assertEqual(engine._map_partner(partner, self.official), partner)

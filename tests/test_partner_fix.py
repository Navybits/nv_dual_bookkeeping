"""Partner of the mirrors (2026-11-12): the mirror has exactly the source's
contact when it is shared; the Fix Mirror Partners wizard repairs existing
mirrors without resetting anything to draft."""

from odoo import Command
from odoo.tests import tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestPartnerFix(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._set_param(cls, "auto_post_mirror", True)
        # a contact with the same VAT, created first (it is what a search
        # on the VAT alone returned): a branch, a child contact, a duplicate
        cls.same_vat = cls.env["res.partner"].create({"name": "A branch with the same VAT", "vat": "LB-4455"})
        cls.customer = cls.env["res.partner"].create({"name": "Real customer", "vat": "LB-4455"})

    def _corrupt(self, mirror, partner):
        """Give a mirror the wrong contact, as the old VAT search did."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET partner_id = %(p)s, commercial_partner_id = %(p)s WHERE id = %(m)s;"
            "UPDATE account_move_line SET partner_id = %(p)s WHERE move_id = %(m)s AND partner_id IS NOT NULL;"
            "UPDATE account_payment SET partner_id = %(p)s WHERE move_id = %(m)s;",
            {"p": partner.id, "m": mirror.id},
        )
        self.env.invalidate_all()

    def _wizard(self):
        return self.env["nv.mirror.partner.fix.wizard"].sudo().with_company(self.company).create({})

    def test_mirror_uses_the_shared_contact(self):
        invoice = self._invoice(partner=self.customer, post=True)
        mirror = self._mirror(invoice)
        self.assertEqual(mirror.partner_id, self.customer)
        self.assertEqual(mirror.line_ids.partner_id, self.customer)

    def test_fix_paid_invoice(self):
        invoice = self._invoice(partner=self.customer, post=True)
        self._pay(invoice)
        mirror = self._mirror(invoice)
        state_before = mirror.payment_state
        term_before = mirror.invoice_payment_term_id
        self._corrupt(mirror, self.same_vat)
        self._corrupt(self._mirror(invoice.line_ids.matched_credit_ids.credit_move_id.move_id), self.same_vat)

        wizard = self._wizard()
        wizard.action_find()
        line = wizard.line_ids.filtered(lambda l: l.source_move_id == invoice)
        self.assertEqual(line.state, "to_fix")
        self.assertEqual(line.current_partner_id, self.same_vat)
        self.assertEqual(line.correct_partner_id, self.customer)

        wizard.action_fix()
        self.assertFalse(wizard.line_ids.filtered(lambda l: l.state != "fixed"), wizard.line_ids.mapped("message"))
        self.assertEqual(mirror.state, "posted", "nothing is reset to draft")
        self.assertEqual(mirror.partner_id, self.customer)
        self.assertEqual(mirror.commercial_partner_id, self.customer)
        self.assertEqual(mirror.line_ids.partner_id, self.customer)
        self.assertEqual(mirror.payment_state, state_before, "the reconciliation is kept")
        self.assertEqual(mirror.invoice_payment_term_id, term_before)

        wizard.action_find()
        self.assertFalse(wizard.line_ids, "nothing left to fix")

    def test_fix_payment(self):
        payment = self._payment(partner=self.customer)
        mirror = self._mirror(payment.move_id)
        self._corrupt(mirror, self.same_vat)
        self.assertEqual(mirror.origin_payment_id.partner_id, self.same_vat)
        destination = mirror.origin_payment_id.destination_account_id

        wizard = self._wizard()
        wizard.action_find()
        wizard.action_fix()
        self.assertEqual(mirror.state, "posted")
        self.assertEqual(mirror.origin_payment_id.partner_id, self.customer)
        self.assertEqual(mirror.origin_payment_id.destination_account_id, destination)
        self.assertEqual(mirror.line_ids.partner_id, self.customer)

    def test_fix_entry_lines_with_their_own_partners(self):
        entry = self._entry()
        debit = entry.line_ids.filtered(lambda l: l.debit)
        credit = entry.line_ids - debit
        entry.write({"line_ids": [Command.update(debit.id, {"partner_id": self.customer.id}),
                                  Command.update(credit.id, {"partner_id": self.partner_b.id})]})
        entry.action_post()
        mirror = self._mirror(entry)
        self._corrupt(mirror, self.same_vat)

        wizard = self._wizard()
        wizard.action_find()
        wizard.action_fix()
        self.assertEqual(mirror.line_ids.filtered(lambda l: l.debit).partner_id, self.customer)
        self.assertEqual(mirror.line_ids.filtered(lambda l: l.credit).partner_id, self.partner_b)

    def test_locked_period_reported(self):
        invoice = self._invoice(partner=self.customer, post=True)
        mirror = self._mirror(invoice)
        self._corrupt(mirror, self.same_vat)
        self.official.sudo().fiscalyear_lock_date = "2026-12-31"

        wizard = self._wizard()
        wizard.action_find()
        wizard.action_fix()
        line = wizard.line_ids.filtered(lambda l: l.source_move_id == invoice)
        self.assertEqual(line.state, "error")
        self.assertIn("Lock Date", line.message)
        self.assertEqual(mirror.partner_id, self.same_vat, "left as it was")

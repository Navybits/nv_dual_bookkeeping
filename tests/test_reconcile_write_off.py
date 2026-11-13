"""The reconcile screen's write-off takes the Official flag of the lines it
closes: an Official customer's difference is written off in the Official
stream and copied (and matched) in the Official Books."""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestReconcileWriteOff(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        icp = cls.env["ir.config_parameter"].sudo()
        icp.set_param("nv_dual_bookkeeping.auto_post_mirror", "True")
        icp.set_param("nv_dual_bookkeeping.default_is_official", "False")
        cls.receivable = cls.company_data["default_account_receivable"]
        cls.expense = cls.company_data["default_account_expense"]
        cls.revenue = cls.company_data["default_account_revenue"]

    def _customer_line(self, partner, amount, official):
        move = self.env["account.move"].create({
            "move_type": "entry", "journal_id": self.misc_journal.id, "date": "2026-01-10",
            "rate_main_second": RATE, "is_official": official,
            "line_ids": [
                Command.create({"account_id": self.receivable.id, "partner_id": partner.id,
                                "debit": max(amount, 0.0), "credit": max(-amount, 0.0)}),
                Command.create({"account_id": self.revenue.id,
                                "debit": max(-amount, 0.0), "credit": max(amount, 0.0)}),
            ],
        })
        move.action_post()
        return move.line_ids.filtered(lambda l: l.account_id == self.receivable)

    def _write_off(self, lines):
        wizard = self.env["account.reconcile.wizard"].with_context(
            active_model="account.move.line", active_ids=lines.ids,
        ).new({"account_id": self.expense.id, "journal_id": self.misc_journal.id})
        wizard.reconcile()
        return (lines.matched_debit_ids | lines.matched_credit_ids).mapped(
            lambda p: p.debit_move_id | p.credit_move_id).move_id - lines.move_id

    def test_official_write_off_is_official_and_copied(self):
        opening = self._customer_line(self.partner_a, 56.12, True)
        payment = self._customer_line(self.partner_a, -56.00, True)
        write_off = self._write_off(opening | payment)
        self.assertEqual(len(write_off), 1)
        self.assertTrue(write_off.is_official)
        self.assertIn("/1/", write_off.name)
        mirror = self._mirror(write_off)
        self.assertTrue(mirror, "the write-off is copied to the Official Books")
        self.assertTrue(self._mirror(opening.move_id).line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable").reconciled,
            "the customer is closed in the Official Books too")

    def test_non_official_write_off_stays_non_official(self):
        opening = self._customer_line(self.partner_no, 56.12, False)
        payment = self._customer_line(self.partner_no, -56.00, False)
        write_off = self._write_off(opening | payment)
        self.assertFalse(write_off.is_official)
        self.assertIn("/2/", write_off.name)
        self.assertFalse(self._mirror(write_off))

    def test_mixed_streams_refused(self):
        opening = self._customer_line(self.partner_a, 56.12, True)
        payment = self._customer_line(self.partner_a, -56.00, False)
        with self.assertRaisesRegex(UserError, "cannot be matched together"):
            self._write_off(opening | payment)

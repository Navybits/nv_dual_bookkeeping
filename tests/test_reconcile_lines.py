"""Reconciliations copied to the Official Books line to line: an entry with
several customers (an opening entry) has one receivable line per customer,
and a payment must be matched with the copy of ITS customer's line, not with
the first open receivable line of the copy."""

from odoo import Command
from odoo.tests import tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestReconcileLines(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "nv_dual_bookkeeping.auto_post_mirror", "True"
        )
        cls.partner_c = cls.env["res.partner"].create({"name": "Customer C"})
        # an account both books have (same code)
        cls.receivable = cls.company_data["default_account_receivable"]
        for partner in (cls.partner_a, cls.partner_b, cls.partner_c):
            partner.with_company(cls.company).property_account_receivable_id = cls.receivable

    def _opening(self):
        """Opening entry: A 100, B 50, C 30 on the receivable account."""
        lines = [
            Command.create({"name": p.name, "partner_id": p.id,
                            "account_id": self.receivable.id, "debit": amount})
            for p, amount in ((self.partner_a, 100.0), (self.partner_b, 50.0), (self.partner_c, 30.0))
        ]
        lines.append(Command.create({
            "name": "opening", "credit": 180.0,
            "account_id": self.company_data["default_account_revenue"].id}))
        move = self._env_for(self.accountant)["account.move"].create({
            "move_type": "entry", "date": "2026-01-10", "journal_id": self.misc_journal.id,
            "rate_main_second": RATE, "line_ids": lines,
        })
        move.action_post()
        return move

    def _line(self, move, partner):
        return move.line_ids.filtered(
            lambda l: l.partner_id == partner and l.account_id.account_type == "asset_receivable")

    def _match(self, opening, partner, amount):
        payment = self._payment(amount=amount, partner=partner)
        (self._line(opening, partner) | self._line(payment.move_id, partner)).reconcile()
        return payment

    def _mirror_pairs(self, mirror):
        partials = mirror.line_ids.matched_debit_ids | mirror.line_ids.matched_credit_ids
        return {(p.debit_move_id.partner_id.name, p.credit_move_id.partner_id.name) for p in partials}

    def test_payment_matched_with_its_customer_line(self):
        opening = self._opening()
        mirror = self._mirror(opening)
        self.assertTrue(mirror)
        self._match(opening, self.partner_c, 30.0)
        self._match(opening, self.partner_b, 50.0)

        self.assertEqual(self._mirror_pairs(mirror), {
            (self.partner_c.name, self.partner_c.name),
            (self.partner_b.name, self.partner_b.name),
        })
        self.assertEqual(self._line(mirror, self.partner_a).amount_residual, 100.0,
                         "the first customer's line stays open")
        self.assertTrue(self._line(mirror, self.partner_b).reconciled)
        self.assertTrue(self._line(mirror, self.partner_c).reconciled)

    def test_unreconcile_removes_the_right_copy(self):
        opening = self._opening()
        mirror = self._mirror(opening)
        payment_b = self._match(opening, self.partner_b, 50.0)
        self._match(opening, self.partner_c, 30.0)

        payment_b.move_id.line_ids.remove_move_reconcile()
        self.assertEqual(self._mirror_pairs(mirror), {(self.partner_c.name, self.partner_c.name)})
        self.assertEqual(self._line(mirror, self.partner_b).amount_residual, 50.0)

    def test_relink_repairs_wrong_copies(self):
        """A copy made by the old code (C's payment on A's line) is removed
        and C's payment is matched with C's line again."""
        opening = self._opening()
        mirror = self._mirror(opening)
        payment_c = self._match(opening, self.partner_c, 30.0)
        mirror_payment = self._mirror(payment_c.move_id)
        official_env = self.env(context=dict(self.env.context, allowed_company_ids=self.official.ids))
        # as the old code did it: undo the right copy, match with A's line
        (mirror.line_ids.matched_debit_ids | mirror.line_ids.matched_credit_ids).sudo().unlink()
        (self._line(mirror, self.partner_a) | self._line(mirror_payment, self.partner_c)).with_env(
            official_env).sudo().reconcile()
        self.assertEqual(self._mirror_pairs(mirror), {(self.partner_a.name, self.partner_c.name)})

        result = self.env["nv.sync.engine"].relink_mirror_reconciliations(opening)
        self.assertEqual(result[opening.name], (1, 1))
        self.assertEqual(self._mirror_pairs(mirror), {(self.partner_c.name, self.partner_c.name)})
        self.assertEqual(self._line(mirror, self.partner_a).amount_residual, 100.0)

        # nothing left to repair
        self.assertEqual(
            self.env["nv.sync.engine"].relink_mirror_reconciliations(opening)[opening.name], (0, 0))

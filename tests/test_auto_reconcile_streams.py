"""Auto-Reconcile matches Official lines with Official lines and Non-Official
lines with Non-Official lines, never one with the other."""

from odoo import Command
from odoo.tests import tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestAutoReconcileStreams(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.receivable = cls.company_data["default_account_receivable"]
        cls.revenue = cls.company_data["default_account_revenue"]

    def _line(self, partner, amount, official):
        """A posted entry with one receivable line (created in code, as old
        data could be: a P1 customer on a Non-Official entry)."""
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

    def _run(self, mode):
        wizard = self.env["account.auto.reconcile.wizard"].create({
            "account_ids": [Command.set(self.receivable.ids)],
            "search_mode": mode,
            "to_date": "2026-12-31",
        })
        wizard.auto_reconcile()

    def test_perfect_match_per_stream(self):
        off_a, off_b = self._line(self.partner_a, 100.0, True), self._line(self.partner_a, -100.0, True)
        no_a, no_b = self._line(self.partner_no, 50.0, False), self._line(self.partner_no, -50.0, False)
        mix_o, mix_no = self._line(self.partner_b, 30.0, True), self._line(self.partner_b, -30.0, False)
        self._run("one_to_one")
        self.assertTrue(off_a.reconciled and off_b.reconciled, "Official with Official")
        self.assertTrue(no_a.reconciled and no_b.reconciled, "Non-Official with Non-Official")
        self.assertFalse(mix_o.reconciled or mix_no.reconciled, "never Official with Non-Official")

    def test_clear_account_per_stream(self):
        mix_o, mix_no = self._line(self.partner_b, 30.0, True), self._line(self.partner_b, -30.0, False)
        off_a, off_b = self._line(self.partner_a, 40.0, True), self._line(self.partner_a, -40.0, True)
        self._run("zero_balance")
        self.assertTrue(off_a.reconciled and off_b.reconciled)
        self.assertFalse(mix_o.reconciled or mix_no.reconciled,
                         "a balance that is zero only across the streams is not cleared")

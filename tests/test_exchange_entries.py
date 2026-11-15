"""Exchange difference entries are never mirrored: in the operating company
they are always Non-Official (whatever the default Official setting) and the
official company makes its own when it reconciles the mirrors. A copy
counted the difference twice there, and copied the differences of
Non-Official invoices."""

from odoo import Command
from odoo.tests import tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestExchangeEntries(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        icp = cls.env["ir.config_parameter"].sudo()
        icp.set_param("nv_dual_bookkeeping.auto_post_mirror", "True")
        # the setting that made them Official (and mirrored) before
        icp.set_param("nv_dual_bookkeeping.default_is_official", "True")
        cls.exchange_journal = cls.company.currency_exchange_journal_id
        # linked as in production (Main EXCH -> S.A.L. EXCH)
        cls.exchange_journal.official_journal_id = cls.official.currency_exchange_journal_id

    def _exchange_entry(self, **vals):
        move = self.env["account.move"].create({
            "move_type": "entry", "journal_id": self.exchange_journal.id, "date": "2026-01-10",
            "line_ids": [
                Command.create({"account_id": self.company_data["default_account_receivable"].id,
                                "partner_id": self.partner_a.id, "debit": 1.0}),
                Command.create({"account_id": self.company.expense_currency_exchange_account_id.id,
                                "credit": 1.0}),
            ],
            **vals,
        })
        move.action_post()
        return move

    def test_exchange_entry_non_official_whatever_the_setting(self):
        move = self._exchange_entry()
        self.assertFalse(move.is_official)
        self.assertFalse(self._mirror(move))

    def test_exchange_entry_forced_non_official(self):
        move = self._exchange_entry(is_official=True)
        self.assertFalse(move.is_official)
        self.assertFalse(self._mirror(move))

    def test_official_exchange_entry_never_mirrored(self):
        """An Official exchange entry (made before this change) is not
        mirrored either, e.g. by a manual sync."""
        move = self._exchange_entry()
        self.env.cr.execute("UPDATE account_move SET is_official = TRUE WHERE id = %s", [move.id])
        move.invalidate_recordset(["is_official"])
        self.assertTrue(move.is_official)
        self.env["nv.sync.engine"].sync_move(move, triggered_by="manual")
        self.assertFalse(self._mirror(move))

    def test_reversal_of_mirrored_exchange_entry_reverses_the_mirror(self):
        """An old Official exchange entry that was mirrored (before this
        rule): when Main reverses it (e.g. the invoice is unmatched), the
        reversal is mirrored too, so the Official Books stay equal."""
        move = self._exchange_entry()
        self.env.cr.execute("UPDATE account_move SET is_official = TRUE WHERE id = %s", [move.id])
        move.invalidate_recordset(["is_official"])
        mirror = self.env["nv.sync.engine"]._do_sync(move)  # as the old sync did
        self.assertTrue(mirror)
        # as nv_double_currencies reverses it when the invoice is unmatched
        reversal = move._reverse_moves([{"date": move.date, "ref": "Reversal"}], cancel=True)
        self.assertEqual(reversal.state, "posted")
        self.assertTrue(reversal.is_official, "the reversal keeps the flag of the reversed entry")
        self.assertTrue(self._mirror(reversal), "and is mirrored: the copy is reversed in the Official Books")
        both = self._mirror(move).line_ids | self._mirror(reversal).line_ids
        receivable = both.filtered(lambda l: l.account_id.account_type == "asset_receivable")
        self.assertEqual(len(receivable), 2)
        self.assertAlmostEqual(sum(receivable.mapped("balance")), 0.0,
                               msg="the copy and its reversal cancel out in the Official Books")

    def test_old_mirrored_exchange_entry_not_in_fix_mirror_partners(self):
        """An old exchange entry copied before this rule (e.g. of a P2
        invoice, its copy zeroed) is left out of Fix Mirror Partners, as of
        Compare with Mirror: it showed as "Cannot Fix"."""
        move = self._exchange_entry()
        self.env.cr.execute("UPDATE account_move SET is_official = TRUE WHERE id = %s", [move.id])
        move.invalidate_recordset(["is_official"])
        mirror = self.env["nv.sync.engine"]._do_sync(move)  # as the old sync did
        self.env.cr.execute("UPDATE account_move_line SET partner_id = %s WHERE move_id = %s AND partner_id IS NOT NULL",
                            [self.partner_b.id, mirror.id])
        self.env.invalidate_all()
        wizard = self.env["nv.mirror.partner.fix.wizard"].sudo().with_company(self.company).create({})
        wizard.action_find()
        self.assertNotIn(move, wizard.line_ids.source_move_id)

    def test_paid_invoice_one_exchange_entry_per_book(self):
        invoice = self._invoice(post=True)
        self._pay(invoice, rate_main_second=RATE + 500.0)
        main_exchange = self.env["account.move"].search([
            ("company_id", "=", self.company.id), ("journal_id", "=", self.exchange_journal.id)])
        self.assertTrue(main_exchange, "the LBP difference is booked in the operating company")
        self.assertFalse(any(main_exchange.mapped("is_official")))
        self.assertFalse(main_exchange.sudo().nv_mirror_move_id, "never copied")
        official_exchange = self.env["account.move"].sudo().search([
            ("company_id", "=", self.official.id),
            ("journal_id", "=", self.official.currency_exchange_journal_id.id)])
        self.assertFalse(official_exchange.filtered("is_mirror"), "no copy in the Official Books")

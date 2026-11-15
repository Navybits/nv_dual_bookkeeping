"""Deferral entries of an Official invoice / bill (account_accountant): the
Official Books generate their own from the mirror invoice, linked to it; the
deferral entries of Main are never mirrored (a copy doubled them, unlinked)."""

from odoo.tests import tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestDeferralEntries(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "deferred_start_date" not in cls.env["account.move.line"]._fields:
            cls.skipTest(cls, "no deferral management (account_accountant)")
        # as configured: the mirror invoices are posted (a draft generates
        # no deferral entries)
        cls.env["ir.config_parameter"].sudo().set_param("nv_dual_bookkeeping.auto_post_mirror", "True")
        Account = cls.env["account.account"].sudo()
        for company, journal in ((cls.company, cls.misc_journal), (cls.official, cls.off_misc_journal)):
            company.sudo().write({
                "generate_deferred_revenue_entries_method": "on_validation",
                "generate_deferred_expense_entries_method": "on_validation",
                "deferred_revenue_account_id": Account.search([
                    ("company_ids", "in", company.id), ("account_type", "=", "liability_current")], limit=1).id,
                "deferred_expense_account_id": Account.search([
                    ("company_ids", "in", company.id), ("account_type", "=", "asset_current")], limit=1).id,
                "deferred_revenue_journal_id": journal.id,
                "deferred_expense_journal_id": journal.id,
            })

    def _deferred(self, move_type="out_invoice", end="2026-03-31"):
        move = self._invoice(move_type=move_type)
        move.invoice_line_ids.write({"deferred_start_date": "2026-01-01", "deferred_end_date": end})
        move.action_post()
        return move

    def _summary(self, moves):
        return sorted((str(m.date), m.state, round(m.amount_total, 2)) for m in moves)

    def _assert_not_duplicated(self, move):
        source_deferrals = move.deferred_move_ids
        self.assertTrue(source_deferrals, "Main generates the deferral entries")
        self.assertFalse(source_deferrals.sudo().nv_mirror_move_id,
                         "Main's deferral entries are not copied to the Official Books")
        mirror = self._mirror(move)
        self.assertEqual(self._summary(mirror.deferred_move_ids), self._summary(source_deferrals),
                         "the Official Books have the same deferral entries, linked to the mirror")
        official_deferrals = self.env["account.move"].sudo().search([
            ("company_id", "=", self.official.id), ("journal_id", "=", self.off_misc_journal.id)])
        self.assertEqual(official_deferrals, mirror.deferred_move_ids,
                         "no other (unlinked) deferral entry in the Official Books")

    def test_invoice_deferrals_not_duplicated(self):
        self._assert_not_duplicated(self._deferred())

    def test_bill_deferrals_not_duplicated(self):
        self._assert_not_duplicated(self._deferred("in_invoice"))

    def test_reset_change_and_post_again(self):
        invoice = self._deferred()
        # Odoo deletes the deferral entries: only an Accounting Administrator
        # may delete entries that are not the last of their sequence
        self.accountant.sudo().group_ids |= self.env.ref("account.group_account_manager")
        invoice.with_env(self._env_for(self.accountant)).button_draft()
        self.assertEqual(self._mirror(invoice).state, "draft")
        self.assertEqual(self._summary(self._mirror(invoice).deferred_move_ids),
                         self._summary(invoice.deferred_move_ids),
                         "reset: the mirror's deferral entries follow")
        invoice.invoice_line_ids.deferred_end_date = "2026-06-30"
        invoice.action_post()
        self._assert_not_duplicated(invoice)


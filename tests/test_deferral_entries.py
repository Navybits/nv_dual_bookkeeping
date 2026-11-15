"""Deferral entries of an Official invoice / bill (account_accountant): Main
is the only source. Its deferral entries are mirrored like any entry and
linked to the mirror invoice; the mirror invoice generates none of its own
(they doubled), so a change to one deferral entry follows too."""

from odoo.tests import tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestDeferralEntries(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if "deferred_start_date" not in cls.env["account.move.line"]._fields:
            cls.skipTest(cls, "no deferral management (account_accountant)")
        # as configured: the mirrors are posted
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

    def _deferred(self, move_type="out_invoice", end="2026-03-31", **extra):
        move = self._invoice(move_type=move_type, **extra)
        move.invoice_line_ids.write({"deferred_start_date": "2026-01-01", "deferred_end_date": end})
        move.action_post()
        return move

    def _summary(self, moves):
        return sorted((m.name, str(m.date), m.state, round(m.amount_total, 2)) for m in moves)

    def _official_deferrals(self):
        return self.env["account.move"].sudo().search([
            ("company_id", "=", self.official.id), ("journal_id", "=", self.off_misc_journal.id)])

    def _assert_mirrored(self, move):
        source_deferrals = move.deferred_move_ids.filtered(lambda m: m.state == "posted")
        self.assertTrue(source_deferrals, "Main generates the deferral entries")
        mirrors = source_deferrals.sudo().nv_mirror_move_id
        self.assertEqual(len(mirrors), len(source_deferrals), "each deferral entry is mirrored")
        mirror = self._mirror(move)
        self.assertEqual(mirror.deferred_move_ids.filtered(lambda m: m.state == "posted"), mirrors,
                         "the mirrors are the mirror invoice's deferral entries")
        self.assertEqual(self._summary(mirrors), self._summary(source_deferrals))
        for deferral in mirrors:
            self.assertEqual(deferral.deferred_original_move_ids, mirror)
        self.assertEqual(self._official_deferrals().filtered(lambda m: m.state == "posted"), mirrors,
                         "no deferral entry of its own in the Official Books")

    def test_invoice_deferrals_mirrored_and_linked(self):
        self._assert_mirrored(self._deferred())

    def test_bill_deferrals_mirrored_and_linked(self):
        self._assert_mirrored(self._deferred("in_invoice"))

    def test_one_deferral_entry_reset_follows(self):
        invoice = self._deferred()
        deferral = invoice.deferred_move_ids.sorted("date")[1]
        deferral.button_draft()
        self.assertEqual(self._mirror(deferral).state, "draft", "its mirror is reset")
        deferral.action_post()
        self.assertEqual(self._mirror(deferral).state, "posted")
        self._assert_mirrored(invoice)

    def test_reset_change_and_post_again(self):
        invoice = self._deferred()
        # Odoo deletes the deferral entries: only an Accounting Administrator
        # may delete entries that are not the last of their sequence
        self.accountant.sudo().group_ids |= self.env.ref("account.group_account_manager")
        invoice.with_env(self._env_for(self.accountant)).button_draft()
        self.assertEqual(self._mirror(invoice).state, "draft")
        self.assertFalse(self._official_deferrals(), "the mirrors of the deleted deferral entries go")
        invoice.invoice_line_ids.deferred_end_date = "2026-06-30"
        invoice.action_post()
        self._assert_mirrored(invoice)
        self.assertEqual(len(self._mirror(invoice).deferred_move_ids), 7)

    def test_non_official_invoice_deferrals_not_mirrored(self):
        invoice = self._deferred(partner=self.partner_no, is_official=False)
        self.assertTrue(invoice.deferred_move_ids)
        self.assertFalse(any(invoice.deferred_move_ids.mapped("is_official")),
                         "a deferral entry has the type of its invoice")
        self.assertFalse(invoice.deferred_move_ids.sudo().nv_mirror_move_id)
        self.assertFalse(self._official_deferrals())

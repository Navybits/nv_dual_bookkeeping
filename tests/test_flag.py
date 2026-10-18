"""D7: "Require explicit Official/Non-Official selection" and the stream of
the payments of the Pay wizard."""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestFlag(DualBookkeepingCommon):
    def _move_form(self, move_type="out_invoice", user=None, **context):
        env = self._env_for(user or self.no_accountant)
        return Form(
            env["account.move"].with_context(default_move_type=move_type, **context)
        )

    def _register(self, moves, user=None, **vals):
        env = self._env_for(user or self.accountant)
        return (
            env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=moves.ids)
            .create({"payment_date": "2026-01-20", "rate_main_second": RATE, **vals})
        )

    # ------------------------------------------------------------------
    # Pay wizard
    # ------------------------------------------------------------------

    def test_pay_wizard_inherits_stream(self):
        bill = self._invoice(move_type="in_invoice", partner=self.partner_no, post=True)
        wizard = self._register(bill)
        self.assertFalse(wizard.is_official)
        self.assertTrue(wizard.is_official_set)
        payment = wizard._create_payments()
        self.assertFalse(payment.is_official)
        self.assertFalse(payment.move_id.is_official)
        self.assertIn("/2/", payment.move_id.name)

    def test_pay_wizard_mixed_defaults_to_official(self):
        official = self._invoice(post=True)
        non_official = self._invoice(partner=self.partner_no, post=True)
        wizard = self._register(official + non_official)
        self.assertTrue(wizard.is_official)
        self.assertFalse(wizard.is_official_set)
        # one payment per partner: each keeps its documents' stream.
        # nv_double_currencies 19.0 creates these batch payments without the
        # wizard's rate (no LBP amounts) and settles the whole LBP amount with
        # a second-currency exchange entry (F12, F13 accounts of common.py);
        # only the streams are checked here.
        payments = wizard._create_payments()
        self.assertEqual(len(payments), 2)
        by_partner = {p.partner_id: p for p in payments}
        self.assertTrue(by_partner[self.partner_a].is_official)
        self.assertFalse(by_partner[self.partner_no].is_official)

    # ------------------------------------------------------------------
    # D7: Require explicit O/NO selection
    # ------------------------------------------------------------------

    def _entry_form(self, user=None):
        form = self._move_form(move_type="entry", user=user)
        form.journal_id = self.misc_journal
        form.date = "2026-01-10"
        for name, account, debit, credit in (
            ("debit", self.company_data["default_account_expense"], 10.0, 0.0),
            ("credit", self.company_data["default_account_revenue"], 0.0, 10.0),
        ):
            with form.line_ids.new() as line:
                line.name = name
                line.account_id = account
                line.debit = debit
                line.credit = credit
        return form

    def test_require_flag_blocks_untouched_form(self):
        self._set_param("require_official_flag", True)
        move = self._entry_form().save()
        self.assertFalse(move.is_official_set)
        with self.assertRaisesRegex(UserError, "explicitly set Official"):
            move.action_post()

    def test_require_flag_toggle_click_counts(self):
        self._set_param("require_official_flag", True)
        form = self._entry_form()
        form.is_official = False
        form.is_official = True
        move = form.save()
        self.assertTrue(move.is_official_set)
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_require_flag_write_counts(self):
        self._set_param("require_official_flag", True)
        move = self._entry_form().save()
        move.is_official = True
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_require_flag_partner_counts(self):
        self._set_param("require_official_flag", True)
        with self._move_form() as form:
            form.partner_id = self.partner_a
            form.invoice_date = "2026-01-10"
            with form.invoice_line_ids.new() as line:
                line.name = "line"
                line.price_unit = 10.0
        form.record.action_post()
        self.assertEqual(form.record.state, "posted")

    def test_require_flag_never_blocks_code_entries(self):
        """Entries created by Odoo or other modules (no is_official_set in
        the creation values) are never blocked."""
        self._set_param("require_official_flag", True)
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "journal_id": self.misc_journal.id,
                "date": "2026-01-10",
                "line_ids": [
                    Command.create(
                        {
                            "account_id": self.company_data[
                                "default_account_expense"
                            ].id,
                            "debit": 5.0,
                        }
                    ),
                    Command.create(
                        {
                            "account_id": self.company_data[
                                "default_account_revenue"
                            ].id,
                            "credit": 5.0,
                        }
                    ),
                ],
            }
        )
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_require_flag_copied_documents_post(self):
        self._set_param("require_official_flag", True)
        bill = self._invoice(move_type="in_invoice", partner=self.partner_no, post=True)
        payment = self._register(bill, user=self.no_accountant)._create_payments()
        # 19.0: "paid" only once the bank side is reconciled (accountant)
        self.assertIn(payment.state, ("in_process", "paid"))
        self.assertEqual(payment.move_id.state, "posted")

    def test_require_flag_off_by_default(self):
        move = self._entry_form().save()
        self.assertFalse(move.is_official_set)
        move.action_post()
        self.assertEqual(move.state, "posted")

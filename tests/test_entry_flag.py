"""Manual journal entries: an Official entry posts without clicking the
toggle; the customers / vendors of the lines must be of the entry's type (P1
Official, P2 Non-Official), checked when the form is saved."""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import RATE, DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestEntryFlag(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "nv_dual_bookkeeping.require_official_flag", "True")
        cls.env["ir.config_parameter"].sudo().set_param(
            "nv_dual_bookkeeping.default_is_official", "False")
        cls.receivable = cls.company_data["default_account_receivable"]
        cls.partner_p1 = cls.partner_a  # Official by default
        cls.partner_p2 = cls.partner_no

    def _save(self, is_official, partners=(), move=None):
        """The form's Save (web_save), the toggle never clicked
        (is_official_set False as the form sends it)."""
        lines = [Command.create({
            "name": "customer", "account_id": self.receivable.id,
            "partner_id": partner.id, "debit": 10.0}) for partner in partners]
        if not partners:
            lines.append(Command.create({
                "name": "cash", "account_id": self.company_data["default_account_expense"].id,
                "debit": 10.0}))
        lines.append(Command.create({
            "name": "counterpart", "account_id": self.company_data["default_account_revenue"].id,
            "credit": 10.0 * max(len(partners), 1)}))
        vals = {
            "move_type": "entry", "journal_id": self.misc_journal.id, "date": "2026-01-10",
            "rate_main_second": RATE, "is_official": is_official, "is_official_set": False,
            "line_ids": lines,
        }
        Move = self._env_for(self.no_accountant)["account.move"]
        result = Move.web_save(vals, {"id": {}})
        return Move.browse(result[0]["id"])

    def test_official_entry_without_customer_posts(self):
        move = self._save(True)
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_non_official_entry_without_customer_posts(self):
        """The toggle as shown is used: Non-Official without a customer posts
        directly (Main only), no click needed."""
        move = self._save(False)
        move.action_post()
        self.assertEqual(move.state, "posted")
        self.assertFalse(move.is_official)
        self.assertFalse(self._mirror(move), "Non-Official: not copied")

    def test_payment_without_customer_non_official_posts(self):
        payment = self._env_for(self.no_accountant)["account.payment"].create({
            "amount": 20.0, "payment_type": "inbound", "partner_type": "customer",
            "journal_id": self.bank_journal.id, "rate_main_second": RATE,
            "is_official": False, "is_official_set": False,
            "partner_amount": 20.0,
            "receivable_payable_account_id": self.company_data["default_account_expense"].id,
        })
        payment.action_post()
        self.assertEqual(payment.move_id.state, "posted")
        self.assertFalse(payment.move_id.is_official)

    def test_payment_toggle_as_shown_with_customer(self):
        for partner, official in ((self.partner_p1, False), (self.partner_p1, True)):
            payment = self._env_for(self.no_accountant)["account.payment"].create({
                "amount": 20.0, "payment_type": "inbound", "partner_type": "customer",
                "partner_id": partner.id, "journal_id": self.bank_journal.id,
                "rate_main_second": RATE, "is_official": official, "is_official_set": False,
                "partner_amount": 20.0,
            })
            payment.action_post()
            self.assertEqual(payment.move_id.state, "posted")
            self.assertEqual(payment.move_id.is_official, official)

    def test_p1_customer_official_entry_posts(self):
        move = self._save(True, [self.partner_p1])
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_p2_customer_non_official_entry_posts(self):
        move = self._save(False, [self.partner_p2])
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_p1_customer_on_non_official_entry_refused_on_save(self):
        with self.assertRaisesRegex(UserError, "this journal entry is Non-Official"):
            self._save(False, [self.partner_p1])

    def test_p2_customer_on_official_entry_refused_on_save(self):
        with self.assertRaisesRegex(UserError, "this journal entry is Official"):
            self._save(True, [self.partner_p2])

    def test_mixed_customers_refused_on_save(self):
        with self.assertRaisesRegex(UserError, "cannot be in the same journal entry"):
            self._save(True, [self.partner_p1, self.partner_p2])

    def test_toggle_switched_on_saved_entry_checked(self):
        move = self._save(True, [self.partner_p1])
        Move = self._env_for(self.no_accountant)["account.move"]
        with self.assertRaisesRegex(UserError, "this journal entry is Non-Official"):
            move.with_env(Move.env).web_save({"is_official": False}, {"id": {}})

    def test_code_entries_not_checked(self):
        """Entries created in code (drivers, write-offs, sync) are not checked
        on save."""
        move = self.env["account.move"].create({
            "move_type": "entry", "journal_id": self.misc_journal.id, "date": "2026-01-10",
            "is_official": False,
            "line_ids": [
                Command.create({"account_id": self.receivable.id, "partner_id": self.partner_p1.id, "debit": 5.0}),
                Command.create({"account_id": self.company_data["default_account_revenue"].id, "credit": 5.0}),
            ],
        })
        move.action_post()
        self.assertEqual(move.state, "posted")

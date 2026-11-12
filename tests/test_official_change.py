"""Change to Non-Official (nv.official.change.wizard) and the O/NO switch
rule on entries reset to draft that already hold a number."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestOfficialChange(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "nv_dual_bookkeeping.auto_post_mirror", "True"
        )
        cls.sale_journal.write({"seq_prefix_o": "INV-O", "seq_prefix_no": "INV-NO"})
        cls.bank_journal.write({"seq_prefix_o": "BNK-O", "seq_prefix_no": "BNK-NO"})

    def _wizard_form(self, record):
        action = record.with_env(self.env).action_change_to_non_official()
        return Form(self.env[action["res_model"]].with_context(action["context"]))

    # ------------------------------------------------------------------
    # O/NO switch after a reset to draft
    # ------------------------------------------------------------------

    def test_cannot_switch_stream_after_reset(self):
        invoice = self._invoice(post=True).with_env(self.env)
        invoice.button_draft()
        with self.assertRaisesRegex(UserError, "Change to Non-Official"):
            invoice.is_official = False

    def test_cannot_switch_payment_stream_after_reset(self):
        payment = self._payment().with_env(self.env)
        payment.action_draft()
        with self.assertRaisesRegex(UserError, "Change to Non-Official"):
            payment.is_official = False

    # ------------------------------------------------------------------
    # Access and preconditions
    # ------------------------------------------------------------------

    def test_accountant_refused(self):
        invoice = self._invoice(post=True)
        with self.assertRaises(AccessError):
            invoice.action_change_to_non_official()

    def test_reconciled_entry_refused(self):
        invoice = self._invoice(post=True)
        self._pay(invoice)
        with self.assertRaisesRegex(UserError, "reconciled"):
            invoice.with_env(self.env).action_change_to_non_official()

    # ------------------------------------------------------------------
    # Renumber
    # ------------------------------------------------------------------

    def test_renumber_last_official_invoice(self):
        invoice = self._invoice(post=True).with_env(self.env)
        official_name = invoice.name
        mirror = self._mirror(invoice)
        self.assertEqual(mirror.state, "posted")

        wizard_form = self._wizard_form(invoice)
        self.assertEqual(wizard_form.method, "renumber")
        wizard_form.reason = "Wrong flag"
        wizard = wizard_form.save()
        with self.assertRaises(UserError):
            wizard.action_confirm()
        self.assertTrue(invoice.is_official, "nothing changed")
        wizard.not_sent_confirmed = True
        wizard.action_confirm()

        self.assertFalse(mirror.exists())
        self.assertFalse(self._mirror(invoice))
        self.assertFalse(invoice.is_official)
        self.assertEqual(invoice.state, "posted")
        self.assertTrue(invoice.name.startswith("INV-NO/"), invoice.name)
        self.assertIn(("delete", "synced"), self._logs(invoice).mapped(
            lambda log: (log.operation, log.sync_state)))

        # The freed Official number goes to the next Official invoice: no gap.
        next_invoice = self._invoice(post=True)
        self.assertEqual(next_invoice.name, official_name)
        self.assertEqual(self._mirror(next_invoice).name, official_name)

    def test_renumber_payment(self):
        payment = self._payment().with_env(self.env)
        mirror = self._mirror(payment.move_id)
        self.assertTrue(mirror.origin_payment_id)

        wizard_form = self._wizard_form(payment)
        self.assertEqual(wizard_form.method, "renumber")
        wizard_form.reason = "Wrong flag"
        wizard_form.not_sent_confirmed = True
        wizard_form.save().action_confirm()

        self.assertFalse(mirror.exists())
        self.assertFalse(payment.is_official)
        self.assertFalse(payment.move_id.is_official)
        self.assertEqual(payment.move_id.state, "posted")
        self.assertTrue(payment.move_id.name.startswith("BNK-NO/"), payment.move_id.name)

    # ------------------------------------------------------------------
    # Reverse
    # ------------------------------------------------------------------

    def test_reverse_when_later_official_numbers_exist(self):
        invoice = self._invoice(amount=100.0, post=True).with_env(self.env)
        self._invoice(amount=200.0, post=True)

        wizard_form = self._wizard_form(invoice)
        self.assertEqual(wizard_form.method, "reverse")
        self.assertTrue(wizard_form.renumber_block_reason)
        wizard_form.reason = "Wrong flag"
        action = wizard_form.save().action_confirm()

        self.assertTrue(invoice.is_official)
        self.assertEqual(invoice.payment_state, "reversed")
        reversal = invoice.reversal_move_ids
        self.assertEqual(reversal.state, "posted")
        self.assertTrue(reversal.is_official)
        self.assertEqual(self._mirror(reversal).state, "posted")
        self.assertTrue(
            self._mirror(invoice).line_ids.filtered("reconciled"),
            "the mirror invoice is reconciled with the mirror credit note",
        )

        copy = self.env["account.move"].browse(action["res_id"])
        self.assertFalse(copy.is_official)
        self.assertEqual(copy.state, "draft")
        self.assertAlmostEqual(copy.amount_untaxed, 100.0)

    def test_reverse_payment(self):
        payment = self._payment().with_env(self.env)
        self._payment()
        mirror = self._mirror(payment.move_id)

        wizard_form = self._wizard_form(payment)
        self.assertEqual(wizard_form.method, "reverse")
        wizard_form.reason = "Wrong flag"
        action = wizard_form.save().action_confirm()

        self.assertEqual(payment.state, "canceled")
        self.assertEqual(mirror.state, "cancel")
        copy = self.env["account.payment"].browse(action["res_id"])
        self.assertFalse(copy.is_official)
        self.assertEqual(copy.state, "draft")

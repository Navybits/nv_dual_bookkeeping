"""Sequence-gap protection: the Official flag follows the partner, and once a
document has a number it cannot change stream (P1 <-> P2 partner, O/NO switch)
nor be renamed; numbering only continues numbers of its own stream prefix."""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, new_test_user, tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestSequenceLock(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sale_journal.write({"seq_prefix_o": "INV-O", "seq_prefix_no": "INV-NO"})
        cls.bank_journal.write({"seq_prefix_o": "BNK-O", "seq_prefix_no": "BNK-NO"})
        cls.misc_journal.write({"seq_prefix_o": "MISC-O", "seq_prefix_no": "MISC-NO"})
        cls.clerk = new_test_user(
            cls.env,
            login="nv_db_seq_clerk",
            groups="base.group_user,account.group_account_user,"
                   "nv_dual_bookkeeping.group_can_set_non_official",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.p1 = cls.env["res.partner"].create({"name": "P1 customer", "is_official": True})
        cls.p1b = cls.env["res.partner"].create({"name": "P1 customer B", "is_official": True})
        cls.p2 = cls.env["res.partner"].create({"name": "P2 customer", "is_official": False})
        cls.p2b = cls.env["res.partner"].create({"name": "P2 customer B", "is_official": False})

    def _pay(self, partner, post=True, **extra):
        return self._payment(partner=partner, user=self.clerk, post=post, **extra)

    def _payment_form(self):
        form = Form(self._env_for(self.clerk)["account.payment"].with_context(
            default_payment_type="inbound", default_partner_type="customer"))
        form.journal_id = self.bank_journal
        form.amount = 10.0
        return form

    # ------------------------------------------------------------------
    # Numbering
    # ------------------------------------------------------------------

    def test_payment_numbering_follows_customer(self):
        self.assertTrue(self._pay(self.p2).move_id.name.startswith("BNK-NO/"))
        self.assertTrue(self._pay(self.p1).move_id.name.startswith("BNK-O/"))

    def test_wrong_stream_entry_does_not_break_numbering(self):
        """One Non-Official entry holding an Official number (the production
        Cash - A/R 2 case) no longer drags the next ones into that stream."""
        wrong = self._pay(self.p2).move_id
        self.env.cr.execute(
            "UPDATE account_move SET name=%s, sequence_prefix=%s, sequence_number=%s WHERE id=%s",
            ["BNK-O/26/000900", "BNK-O/26/", 900, wrong.id])
        self.env.invalidate_all()
        nxt = self._pay(self.p2).move_id
        self.assertTrue(nxt.name.startswith("BNK-NO/"), nxt.name)
        official = self._pay(self.p1).move_id
        self.assertTrue(official.name.startswith("BNK-O/"), official.name)
        self.assertLess(official.sequence_number, 900)

    # ------------------------------------------------------------------
    # Payments already numbered (reset to draft)
    # ------------------------------------------------------------------

    def test_numbered_payment_locked(self):
        payment = self._pay(self.p1)
        name = payment.move_id.name
        payment.action_draft()
        with self.assertRaisesRegex(UserError, "Change to Non-Official"):
            payment.write({"partner_id": self.p2.id, "is_official": False})
        with self.assertRaisesRegex(UserError, "P1 <-> P2"):
            payment.write({"partner_id": self.p2.id})
        with self.assertRaisesRegex(UserError, "Change to Non-Official"):
            payment.write({"is_official": False})
        # same type (P1 -> P1) and other edits are allowed
        payment._nv_check_locked_after_number({"partner_id": self.p1b.id})
        payment.write({"memo": "other edit"})
        payment.action_post()
        self.assertEqual(payment.move_id.name, name)

    def test_numbered_payment_form_partner_refused(self):
        payment = self._pay(self.p1)
        payment.action_draft()
        with self.assertRaisesRegex(UserError, "P1 <-> P2"):
            with Form(payment) as form:
                form.partner_id = self.p2

    # ------------------------------------------------------------------
    # The flag must match the customer (user edits only)
    # ------------------------------------------------------------------

    def test_draft_payment_flag_must_match_customer(self):
        payment = self._pay(self.p2, post=False)
        with self.assertRaisesRegex(UserError, "cannot be Official"):
            payment.write({"is_official": True})
        # never posted: changing customer, the flag follows
        payment.write({"partner_id": self.p1.id, "is_official": True})

    def test_form_toggle_must_match_customer(self):
        form = self._payment_form()
        form.partner_id = self.p2
        with self.assertRaisesRegex(UserError, "cannot be Official"):
            form.is_official = True
        form = self._payment_form()
        form.partner_id = self.p1
        with self.assertRaisesRegex(UserError, "must be Official"):
            form.is_official = False

    def test_no_customer_toggle_free(self):
        form = self._payment_form()
        form.is_official = not form.is_official
        form.save()

    def test_web_save_new_record_must_match(self):
        Payment = self._env_for(self.clerk)["account.payment"]
        vals = {
            "payment_type": "inbound", "partner_type": "customer", "amount": 10.0,
            "journal_id": self.bank_journal.id, "partner_id": self.p2.id, "is_official": True,
        }
        with self.assertRaisesRegex(UserError, "cannot be Official"):
            Payment.web_save(vals, {})
        Payment.web_save(dict(vals, is_official=False), {})
        Payment.web_save(dict(vals, partner_id=False), {})
        Move = self._env_for(self.clerk)["account.move"]
        inv_vals = {
            "move_type": "out_invoice", "partner_id": self.p2.id, "is_official": True,
            "invoice_line_ids": [Command.create({"name": "l", "quantity": 1, "price_unit": 5})],
        }
        with self.assertRaisesRegex(UserError, "cannot be Official"):
            Move.web_save(inv_vals, {})
        Move.web_save(dict(inv_vals, partner_id=self.p1.id), {})

    def test_code_create_not_blocked(self):
        """Driver payments / register-payment wizard create with the invoice's
        flag: create() is not checked."""
        payment = self._pay(self.p2, is_official=True)
        self.assertTrue(payment.is_official)

    # ------------------------------------------------------------------
    # Invoices and journal entries
    # ------------------------------------------------------------------

    def test_numbered_invoice_partner_type_locked(self):
        invoice = self._invoice(partner=self.p2, user=self.clerk, post=True)
        self.assertTrue(invoice.name.startswith("INV-NO/"), invoice.name)
        invoice.button_draft()
        with self.assertRaisesRegex(UserError, "same Official/Non-Official type"):
            invoice.write({"partner_id": self.p1.id})
        invoice.write({"partner_id": self.p2b.id})

    def test_numbered_entry_rename_refused(self):
        entry = self._entry(user=self.clerk, post=True)
        name = entry.name
        entry.button_draft()
        with self.assertRaisesRegex(UserError, "cannot be renamed"):
            entry.write({"name": "MISC-O/26/000016"})
        entry.write({"ref": "other edit"})
        entry.action_post()
        self.assertEqual(entry.name, name)

    def test_official_books_not_restricted(self):
        entry = self._entry(user=self.clerk, post=True)
        entry.button_draft()
        self.company.is_official_company = True
        try:
            entry.with_user(self.clerk).write({"name": "MISC-O/26/000777"})
        finally:
            self.company.is_official_company = False

    # ------------------------------------------------------------------
    # Change to Non-Official wizard still works
    # ------------------------------------------------------------------

    def test_change_wizard_still_renumbers_payment(self):
        """Run by a real accounting manager (not superuser): the wizard makes an
        Official customer's payment Non-Official on purpose."""
        manager = new_test_user(
            self.env,
            login="nv_db_seq_manager",
            groups="base.group_user,account.group_account_manager",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        payment = self._pay(self.p1)
        self.assertTrue(payment.move_id.name.startswith("BNK-O/"))
        menv = self._env_for(manager)
        action = payment.with_env(menv).action_change_to_non_official()
        form = Form(menv[action["res_model"]].with_context(action["context"]))
        form.reason = "test"
        form.not_sent_confirmed = True
        form.save().action_confirm()
        self.assertFalse(payment.is_official)
        self.assertTrue(payment.move_id.name.startswith("BNK-NO/"), payment.move_id.name)

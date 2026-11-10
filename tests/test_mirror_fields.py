"""Fields the mirror carries over (testers' round of 2026-11-10): payment
method line, advance payment, manual VAT rate, partner bank account, memo
edited on a posted payment, analytic distributions, deferral periods; the
O/NO toggle of a payment without contact."""

from odoo import Command
from odoo.tests import Form, tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestMirrorFields(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._set_param(cls, "auto_post_mirror", True)

    def _account_pair(self, code, account_type="asset_current", name="Account"):
        """An account with the same code in both books (main one returned)."""
        Account = self.env["account.account"].sudo()
        main = Account.create({"name": name, "code": code, "account_type": account_type,
                               "reconcile": True, "company_ids": self.company.ids})
        Account.create({"name": name, "code": code, "account_type": account_type,
                        "reconcile": True, "company_ids": self.official.ids})
        return main

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    def test_payment_toggle_without_contact_counts_as_set(self):
        """The toggle click reaches the server (an onchange on is_official):
        it used to be lost on payments, so one without contact could not be
        posted with "Require explicit Official/Non-Official selection"."""
        self._set_param("require_official_flag", True)
        form = Form(self.env["account.payment"].with_context(
            default_payment_type="inbound", default_partner_type="customer"))
        form.journal_id = self.bank_journal
        form.amount = 100.0
        form.is_official = not form.is_official
        self.assertTrue(form.is_official_set)

    def test_payment_method_line_matched(self):
        manual = self.bank_journal.inbound_payment_method_line_ids[:1].payment_method_id
        lbp_account = self._account_pair("101599", name="Outstanding Receipts LBP")
        off_lbp_account = self.env["account.account"].sudo().with_company(self.official).search(
            [("code", "=", "101599")], limit=1)
        for journal, account in ((self.bank_journal, lbp_account), (self.off_bank_journal, off_lbp_account)):
            self.env["account.payment.method.line"].sudo().create({
                "name": "Manual Payment LBP",
                "payment_method_id": manual.id,
                "journal_id": journal.id,
                "payment_account_id": account.id,
            })
        line = self.bank_journal.inbound_payment_method_line_ids.filtered(
            lambda l: l.name == "Manual Payment LBP")
        payment = self._payment(payment_method_line_id=line.id)
        mirror_payment = self._mirror(payment.move_id).origin_payment_id

        self.assertEqual(mirror_payment.payment_method_line_id.name, "Manual Payment LBP")
        self.assertEqual(mirror_payment.payment_method_line_id.journal_id, self.off_bank_journal)
        liquidity_codes = mirror_payment.move_id.line_ids.account_id.with_company(self.official).mapped("code")
        self.assertIn("101599", liquidity_codes)

    def test_payment_double_currency_fields(self):
        Payment = self.env["account.payment"]
        names = [n for n in ("is_advance_payment", "set_manual_vat_rate", "manual_rate_vat")
                 if n in Payment._fields]
        if not names:
            self.skipTest("nv_double_currencies without these fields")
        extra = {"is_advance_payment": True, "set_manual_vat_rate": True, "manual_rate_vat": 90000.0}
        payment = self._payment(**{n: extra[n] for n in names})
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        for name in names:
            self.assertEqual(mirror_payment[name], payment[name], name)

    def test_payment_partner_bank(self):
        bank = self.env["res.partner.bank"].create(
            {"acc_number": "LB12 3456 7890", "partner_id": self.partner_a.id})
        payment = self._payment(partner_bank_id=bank.id)
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        self.assertEqual(mirror_payment.partner_bank_id.sanitized_acc_number, bank.sanitized_acc_number)

    def test_payment_memo_edited_after_posting(self):
        payment = self._payment().with_env(self.env)
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        payment.memo = "Changed after posting"
        self.assertEqual(mirror_payment.memo, "Changed after posting")

    def test_payment_analytic_distribution(self):
        if "analytic_distribution" not in self.env["account.payment"]._fields:
            self.skipTest("no analytic distribution on payments (e.g. nv_pr_prunelle)")
        analytic = self._analytic_account()
        payment = self._payment(analytic_distribution={str(analytic.id): 100})
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        self.assertEqual(mirror_payment.analytic_distribution, {str(analytic.id): 100})

    # ------------------------------------------------------------------
    # Invoices / bills: analytic distribution, deferral period
    # ------------------------------------------------------------------

    def _analytic_account(self, company=None, code=False, name="Project X"):
        plan = self.env["account.analytic.plan"].sudo().search([], limit=1) or \
            self.env["account.analytic.plan"].sudo().create({"name": "Projects"})
        return self.env["account.analytic.account"].sudo().create(
            {"name": name, "code": code, "plan_id": plan.id,
             "company_id": company.id if company else False})

    def test_bill_analytic_distribution_shared_account(self):
        analytic = self._analytic_account()
        bill = self._invoice(move_type="in_invoice")
        bill.invoice_line_ids.analytic_distribution = {str(analytic.id): 100}
        bill.action_post()
        mirror_line = self._mirror(bill).invoice_line_ids
        self.assertEqual(mirror_line.analytic_distribution, {str(analytic.id): 100})

    def test_bill_analytic_distribution_mapped_by_code(self):
        main_analytic = self._analytic_account(self.company, code="PX1")
        off_analytic = self._analytic_account(self.official, code="PX1")
        bill = self._invoice(move_type="in_invoice")
        bill.invoice_line_ids.analytic_distribution = {str(main_analytic.id): 100}
        bill.action_post()
        mirror_line = self._mirror(bill).invoice_line_ids
        self.assertEqual(mirror_line.analytic_distribution, {str(off_analytic.id): 100})

    def test_bill_analytic_missing_fails_the_sync(self):
        main_analytic = self._analytic_account(self.company, code="PX2", name="Only in main")
        bill = self._invoice(move_type="in_invoice")
        bill.invoice_line_ids.analytic_distribution = {str(main_analytic.id): 100}
        bill.action_post()
        self.assertFalse(self._mirror(bill))
        self.assertEqual(bill.sync_state, "error")
        self.assertIn("Only in main", self._logs(bill).sorted("id")[-1].error_message)

    def test_invoice_deferral_period(self):
        if "deferred_start_date" not in self.env["account.move.line"]._fields:
            self.skipTest("no deferral management (account_accountant)")
        deferred_account = self.env["account.account"].sudo().with_company(self.official).search(
            [("account_type", "=", "liability_current")], limit=1)
        self.official.sudo().write({
            "generate_deferred_revenue_entries_method": "on_validation",
            "deferred_revenue_account_id": deferred_account.id,
            "deferred_revenue_journal_id": self.off_misc_journal.id,
        })
        invoice = self._invoice()
        invoice.invoice_line_ids.write(
            {"deferred_start_date": "2026-01-01", "deferred_end_date": "2026-12-31"})
        invoice.action_post()

        mirror_line = self._mirror(invoice).invoice_line_ids
        self.assertEqual(str(mirror_line.deferred_start_date), "2026-01-01")
        self.assertEqual(str(mirror_line.deferred_end_date), "2026-12-31")
        self.assertTrue(self._mirror(invoice).deferred_move_ids,
                        "the official company generates the mirror's deferral entries")

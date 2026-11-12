"""Compare with Mirror (2026-11-12): no difference for documents synced
correctly (no false alarms), every damaged field reported."""

from odoo import Command
from odoo.tests import tagged

from .common import DualBookkeepingCommon


@tagged("post_install", "-at_install")
class TestMirrorCheck(DualBookkeepingCommon):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._set_param(cls, "auto_post_mirror", True)
        plan = cls.env["account.analytic.plan"].sudo().search([], limit=1) or \
            cls.env["account.analytic.plan"].sudo().create({"name": "Projects"})
        cls.analytic = cls.env["account.analytic.account"].sudo().create(
            {"name": "Shared project", "plan_id": plan.id, "company_id": False})
        cls.term = cls.env["account.payment.term"].sudo().create({
            "name": "45 days",
            "line_ids": [Command.create({"value": "percent", "value_amount": 100.0, "nb_days": 45})],
        })

    def _compare(self, move):
        return self.env["nv.mirror.checker"].compare(move)

    def _labels(self, move):
        return [(d["group"], d["label"]) for d in self._compare(move)]

    def _sql(self, query, params):
        self.env.flush_all()
        self.env.cr.execute(query, params)
        self.env.invalidate_all()

    # ------------------------------------------------------------------
    # No false alarms
    # ------------------------------------------------------------------

    def test_clean_documents(self):
        invoice = self._invoice(invoice_payment_term_id=self.term.id)
        invoice.invoice_line_ids.analytic_distribution = {str(self.analytic.id): 100}
        invoice.action_post()
        bill = self._invoice(move_type="in_invoice", post=True)
        entry = self._entry(post=True)
        payment = self._payment()
        for move in (invoice, bill, entry, payment.move_id):
            self.assertEqual(self._compare(move), [], move.name)

    def test_clean_paid_invoice(self):
        invoice = self._invoice(post=True)
        payment = self._pay(invoice)
        self.assertEqual(self._compare(invoice), [])
        self.assertEqual(self._compare(payment.move_id), [])

    # ------------------------------------------------------------------
    # Differences reported
    # ------------------------------------------------------------------

    def test_header_differences(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        other = self.env["res.partner"].create({"name": "Someone else"})
        self._sql(
            "UPDATE account_move SET partner_id = %s, invoice_date_due = '2030-01-01', ref = 'X' WHERE id = %s",
            (other.id, mirror.id))
        labels = [label for group, label in self._labels(invoice) if group == "header"]
        self.assertIn("Partner", labels)
        self.assertIn("Reference", labels)
        self.assertTrue(any("Due" in label for label in labels), labels)

    def test_line_differences(self):
        invoice = self._invoice()
        invoice.invoice_line_ids.analytic_distribution = {str(self.analytic.id): 100}
        invoice.action_post()
        mirror_line = self._mirror(invoice).invoice_line_ids
        self._sql("UPDATE account_move_line SET name = 'changed', analytic_distribution = NULL WHERE id = %s",
                  (mirror_line.id,))
        labels = [label for group, label in self._labels(invoice) if group == "lines"]
        self.assertTrue(any(label.endswith("Label") for label in labels), labels)
        self.assertTrue(any(label.endswith("Analytic") for label in labels), labels)

    def test_amount_difference_per_account(self):
        entry = self._entry(post=True)
        mirror = self._mirror(entry)
        self._sql("UPDATE account_move_line SET balance = balance * 2, debit = debit * 2, "
                  "credit = credit * 2 WHERE move_id = %s", (mirror.id,))
        groups = {group for group, _label in self._labels(entry)}
        self.assertIn("lines", groups)

    def test_payment_difference(self):
        payment = self._payment()
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        self._sql("UPDATE account_payment SET memo = 'other memo' WHERE id = %s", (mirror_payment.id,))
        self.assertIn(("payment", "Memo"), self._labels(payment.move_id))

    def test_reconciliation_difference(self):
        invoice = self._invoice(post=True)
        self._pay(invoice)
        mirror = self._mirror(invoice)
        mirror_partials = mirror.line_ids.matched_credit_ids | mirror.line_ids.matched_debit_ids
        self.assertTrue(mirror_partials)
        mirror_partials.sudo().unlink()
        groups = {group for group, _label in self._labels(invoice)}
        self.assertIn("reconciliation", groups)

    def test_missing_mirror(self):
        main_only = self.env["account.analytic.account"].sudo().create({
            "name": "Main only", "code": "MO1", "plan_id": self.analytic.plan_id.id,
            "company_id": self.company.id})
        bill = self._invoice(move_type="in_invoice")
        bill.invoice_line_ids.analytic_distribution = {str(main_only.id): 100}
        bill.action_post()
        self.assertFalse(self._mirror(bill))
        self.assertEqual(self._labels(bill), [("link", "Mirror")])

    def test_outdated_mirror(self):
        invoice = self._invoice(post=True)
        invoice.button_draft()
        self.assertIn(("link", "Mirror up to date"), self._labels(invoice))

    # ------------------------------------------------------------------
    # Wizard
    # ------------------------------------------------------------------

    def test_wizard_period_and_selection(self):
        january = self._invoice(post=True, date="2026-01-10")
        march = self._invoice(post=True, date="2026-03-10")
        for move in (january, march):
            self._sql("UPDATE account_move SET ref = 'X' WHERE id = %s", (self._mirror(move).id,))

        Wizard = self.env["nv.mirror.check.wizard"].sudo().with_company(self.company)
        wizard = Wizard.create({"date_from": "2026-01-01", "date_to": "2026-01-31"})
        wizard.action_check()
        self.assertEqual(wizard.line_ids.source_move_id, january)
        self.assertEqual(wizard.document_count, 1)

        selected = Wizard.with_context(active_model="account.move", active_ids=march.ids).create({})
        selected.action_check()
        self.assertEqual(selected.line_ids.source_move_id, march)

    def test_form_button(self):
        invoice = self._invoice(post=True)
        action = invoice.with_env(self.env).sudo().action_compare_with_mirror()
        wizard = self.env["nv.mirror.check.wizard"].sudo().browse(action["res_id"])
        self.assertTrue(wizard.checked)
        self.assertEqual(wizard.checked_count, 1)
        self.assertFalse(wizard.line_ids)

    # ------------------------------------------------------------------
    # The sync itself: after each scenario the mirror matches
    # ------------------------------------------------------------------

    def test_sync_scenarios_leave_no_difference(self):
        from odoo.tests import Form

        # repost with another payment term and price
        bill = self._invoice(move_type="in_invoice", invoice_payment_term_id=self.term.id, post=True)
        bill.button_draft()
        bill.write({
            "invoice_payment_term_id": self.env.ref("account.account_payment_term_30days").id,
            "invoice_line_ids": [Command.update(bill.invoice_line_ids.id, {"price_unit": 250.0})],
        })
        bill.action_post()
        # due date edited after posting (no payment term)
        bill2 = self._invoice(move_type="in_invoice", invoice_payment_term_id=False,
                              invoice_date_due="2026-02-15", post=True)
        with Form(bill2.with_env(self.env)) as form:
            form.invoice_date_due = "2026-03-20"
        # analytic edited after posting
        entry = self._entry(post=True)
        entry.line_ids.filtered("debit").analytic_distribution = {str(self.analytic.id): 100}
        # memo edited after posting
        payment = self._payment().with_env(self.env)
        payment.memo = "edited after posting"
        moves = [bill, bill2, entry, payment.move_id]
        # deferral dates (the official company generates the deferral entries)
        if "deferred_start_date" in self.env["account.move.line"]._fields:
            self.official.sudo().write({
                "generate_deferred_revenue_entries_method": "on_validation",
                "deferred_revenue_account_id": self.env["account.account"].sudo().with_company(
                    self.official).search([("account_type", "=", "liability_current")], limit=1).id,
                "deferred_revenue_journal_id": self.off_misc_journal.id,
            })
            invoice = self._invoice()
            invoice.invoice_line_ids.write(
                {"deferred_start_date": "2026-01-01", "deferred_end_date": "2026-12-31"})
            invoice.action_post()
            moves.append(invoice)
        for move in moves:
            self.assertEqual(self._compare(move), [], move.name)

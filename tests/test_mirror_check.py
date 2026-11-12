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
        cls.env.user.sudo().write({"group_ids": [Command.link(
            cls.env.ref("nv_dual_bookkeeping.group_sync_manager").id)]})
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
        self.assertEqual(self._labels(invoice), [], "reset with its document: expected")
        # posted again, but the rewrite fails (analytic account missing in
        # the official company): the mirror stays out of date
        main_only = self.env["account.analytic.account"].sudo().create({
            "name": "Main only", "code": "MO3", "plan_id": self.analytic.plan_id.id,
            "company_id": self.company.id})
        invoice.invoice_line_ids.analytic_distribution = {str(main_only.id): 100}
        invoice.action_post()
        self.assertIn(("link", "Mirror up to date"), self._labels(invoice))
        self.assertEqual(self._classify(invoice), "rewrite")

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
        self.assertEqual(wizard.doc_ids.source_move_id, january)
        self.assertEqual(wizard.document_count, 1)

        selected = Wizard.with_context(active_model="account.move", active_ids=march.ids).create({})
        selected.action_check()
        self.assertEqual(selected.doc_ids.source_move_id, march)

    def test_form_button(self):
        invoice = self._invoice(post=True)
        action = invoice.with_env(self.env).sudo().action_compare_with_mirror()
        wizard = self.env["nv.mirror.check.wizard"].sudo().browse(action["res_id"])
        self.assertTrue(wizard.checked)
        self.assertEqual(wizard.checked_count, 1)
        self.assertFalse(wizard.doc_ids)

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

    # ------------------------------------------------------------------
    # Fixing (per document)
    # ------------------------------------------------------------------

    def _check(self, *moves):
        wizard = self.env["nv.mirror.check.wizard"].sudo().with_context(
            active_model="account.move", active_ids=[m.id for m in moves]).create({})
        wizard.action_check()
        return wizard

    def _classify(self, move):
        checker = self.env["nv.mirror.checker"]
        return checker.classify(move, checker.compare(move))[0]

    def test_light_fix_in_place(self):
        invoice = self._invoice()
        invoice.invoice_line_ids.analytic_distribution = {str(self.analytic.id): 100}
        invoice.action_post()
        self._pay(invoice)
        mirror = self._mirror(invoice)
        other = self.env["res.partner"].create({"name": "Wrong contact"})
        self._sql("UPDATE account_move SET partner_id = %(p)s, commercial_partner_id = %(p)s WHERE id = %(m)s;"
                  "UPDATE account_move_line SET partner_id = %(p)s WHERE move_id = %(m)s;"
                  "UPDATE account_move_line SET analytic_distribution = NULL WHERE move_id = %(m)s",
                  {"p": other.id, "m": mirror.id})
        mirror_partials = mirror.line_ids.matched_credit_ids | mirror.line_ids.matched_debit_ids
        mirror_partials.sudo().unlink()
        self.assertEqual(self._classify(invoice), "light")

        wizard = self._check(invoice)
        doc = wizard.doc_ids
        self.assertEqual(doc.fix, "light")
        self.assertTrue(doc.selected, "light fixes are ticked")
        self.assertGreaterEqual(doc.difference_count, 3)
        wizard.action_fix()
        self.assertEqual(doc.state, "fixed", doc.message)
        self.assertEqual(self._mirror(invoice), mirror, "the same mirror")
        self.assertEqual(mirror.state, "posted", "not reset")
        self.assertEqual(self._compare(invoice), [])

    def test_rewrite(self):
        entry = self._entry(post=True)
        mirror = self._mirror(entry)
        name = mirror.name
        self._sql("UPDATE account_move_line SET balance = balance * 2, debit = debit * 2, credit = credit * 2, "
                  "amount_currency = amount_currency * 2 WHERE move_id = %s", (mirror.id,))
        wizard = self._check(entry)
        doc = wizard.doc_ids
        self.assertEqual(doc.fix, "rewrite")
        self.assertFalse(doc.selected, "a rewrite is an explicit choice")
        self.assertFalse(wizard.selected_count)
        doc.selected = True
        wizard.action_fix()
        self.assertEqual(doc.state, "fixed", doc.message)
        self.assertEqual(self._mirror(entry), mirror, "rewritten in place")
        self.assertEqual(mirror.name, name)
        self.assertEqual(mirror.state, "posted")
        self.assertEqual(self._compare(entry), [])

    def test_mixed_differences_rewrite(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        other = self.env["res.partner"].create({"name": "Wrong contact"})
        self._sql("UPDATE account_move SET partner_id = %s, invoice_date = '2026-02-01' WHERE id = %s",
                  (other.id, mirror.id))
        self.assertEqual(self._classify(invoice), "rewrite")
        wizard = self._check(invoice)
        wizard.doc_ids.selected = True
        wizard.action_fix()
        self.assertEqual(wizard.doc_ids.state, "fixed", wizard.doc_ids.message)
        self.assertEqual(self._compare(invoice), [])

    def test_missing_mirror_created(self):
        main_only = self.env["account.analytic.account"].sudo().create({
            "name": "Main only", "code": "MO2", "plan_id": self.analytic.plan_id.id,
            "company_id": self.company.id})
        bill = self._invoice(move_type="in_invoice")
        bill.invoice_line_ids.analytic_distribution = {str(main_only.id): 100}
        bill.action_post()
        wizard = self._check(bill)
        doc = wizard.doc_ids
        self.assertEqual(doc.fix, "rewrite")
        doc.selected = True
        wizard.action_fix()
        self.assertEqual(doc.state, "error", "the cause is still there")
        self.assertIn("Main only", doc.message)

        self.env["account.analytic.account"].sudo().create({
            "name": "Main only", "code": "MO2", "plan_id": self.analytic.plan_id.id,
            "company_id": self.official.id})
        doc.selected = True
        wizard.action_fix()
        self.assertEqual(doc.state, "fixed", doc.message)
        self.assertTrue(self._mirror(bill))
        self.assertEqual(self._compare(bill), [])

    def test_status_light(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        mirror.with_company(self.official).sudo().button_draft()
        self.assertEqual(self._classify(invoice), "light")
        wizard = self._check(invoice)
        wizard.action_fix()
        self.assertEqual(mirror.state, "posted")
        self.assertEqual(self._compare(invoice), [])

    def test_locked_cannot_fix(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        self._sql("UPDATE account_move SET ref = 'X' WHERE id = %s", (mirror.id,))
        self.official.sudo().fiscalyear_lock_date = "2026-12-31"
        wizard = self._check(invoice)
        doc = wizard.doc_ids
        self.assertEqual(doc.fix, "cannot")
        self.assertIn("locked", doc.reason)
        self.assertFalse(doc.selected)
        self.assertFalse(wizard.selected_count)

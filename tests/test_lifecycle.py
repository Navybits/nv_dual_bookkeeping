"""D2: the mirror follows its source's life-cycle; the 19.0.1.0.2 migration
links the existing mirrors to their source."""

import importlib.util

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools.misc import file_path

from .common import DualBookkeepingCommon


def load_migration(name, version="19.0.1.0.2"):
    path = file_path(f"nv_dual_bookkeeping/migrations/{version}/{name}.py")
    spec = importlib.util.spec_from_file_location(
        f"nv_dual_bookkeeping_{name}".replace("-", "_"), path
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


@tagged("post_install", "-at_install")
class TestLifecycle(DualBookkeepingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the lock check and the reconciliations concern posted mirrors
        cls.env["ir.config_parameter"].sudo().set_param(
            "nv_dual_bookkeeping.auto_post_mirror", "True"
        )

    def _operations(self, move):
        return (
            self._logs(move)
            .sorted("id")
            .mapped(lambda log: (log.operation, log.sync_state))
        )

    def _second_amounts(self, move):
        """Second-currency amount per account code."""
        amounts = {}
        for line in move.line_ids:
            code = line.account_id.with_company(move.company_id).code
            amounts[code] = amounts.get(code, 0.0) + line.second_currency_amount
        return amounts

    def _assert_same(self, source, mirror):
        self.assertEqual(mirror.name, source.name)
        self.assertEqual(mirror.state, source.state)
        self.assertAlmostEqual(mirror.amount_total, source.amount_total)
        self.assertAlmostEqual(mirror.amount_tax, source.amount_tax)
        self.assertEqual(mirror.rate_main_second, source.rate_main_second)
        self.assertEqual(self._second_amounts(mirror), self._second_amounts(source))

    def test_reset_and_repost_rewrites_the_mirror(self):
        invoice = self._invoice(
            user=self.no_accountant,
            post=True,
            invoice_line_ids=[
                Command.create(
                    {
                        "name": "line",
                        "quantity": 1.0,
                        "price_unit": 100.0,
                        "tax_ids": [Command.set(self.tax_sale_a.ids)],
                    }
                )
            ],
        )
        mirror = self._mirror(invoice)
        self._assert_same(invoice, mirror)

        invoice.button_draft()
        self.assertEqual(mirror.state, "draft")
        self.assertTrue(invoice.nv_mirror_outdated)

        invoice.write(
            {
                "rate_main_second": 90000.0,
                "invoice_line_ids": [
                    Command.update(invoice.invoice_line_ids.id, {"price_unit": 250.0}),
                    Command.create(
                        {"name": "second line", "quantity": 2.0, "price_unit": 10.0}
                    ),
                ],
            }
        )
        invoice.action_post()
        self.assertEqual(self._mirror(invoice), mirror, "the same mirror is rewritten")
        self._assert_same(invoice, mirror)
        self.assertEqual(len(mirror.invoice_line_ids), 2)
        self.assertEqual(mirror.amount_untaxed, 270.0)
        self.assertFalse(invoice.nv_mirror_outdated)
        self.assertEqual(
            self._operations(invoice),
            [("create", "synced"), ("reset", "synced"), ("update", "synced")],
        )

    def test_reset_keeps_draft_mirror(self):
        self._set_param("auto_post_mirror", False)
        entry = self._entry(post=True)
        mirror = self._mirror(entry)
        self.assertEqual(mirror.state, "draft")
        entry.button_draft()
        self.assertEqual(mirror.state, "draft")
        debit, credit = entry.line_ids.sorted("balance", reverse=True)
        entry.write(
            {
                "line_ids": [
                    Command.update(debit.id, {"debit": 70.0}),
                    Command.update(credit.id, {"credit": 70.0}),
                ]
            }
        )
        entry.action_post()
        self.assertEqual(mirror.state, "draft")
        self.assertEqual(sorted(mirror.line_ids.mapped("balance")), [-70.0, 70.0])

    def test_cancel(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        invoice.button_cancel()
        self.assertEqual(invoice.state, "cancel")
        self.assertEqual(mirror.state, "cancel")
        self.assertEqual(
            self._operations(invoice)[-2:], [("reset", "synced"), ("cancel", "synced")]
        )
        # back to draft and posted again: the cancelled mirror follows
        invoice.button_draft()
        self.assertEqual(mirror.state, "draft")
        invoice.action_post()
        self.assertEqual(mirror.state, "posted")
        self.assertEqual(mirror.name, invoice.name)

    def test_delete_draft(self):
        entry = self._entry(post=True)
        mirror = self._mirror(entry)
        name = entry.name
        entry.button_draft()
        entry.unlink()
        self.assertFalse(mirror.exists())
        log = self.env["nv.sync.log"].sudo().search([("source_ref", "=", name)])
        self.assertIn(
            ("delete", "synced"), log.mapped(lambda l: (l.operation, l.sync_state))
        )

    def test_delete_refused_while_mirror_posted(self):
        entry = self._entry(post=True)
        mirror = self._mirror(entry)
        entry.button_draft()
        mirror.sudo().action_post()
        with self.assertRaisesRegex(UserError, "mirror .* is posted"):
            entry.unlink()
        self.assertTrue(entry.exists())

    def test_payment_lifecycle(self):
        payment = self._payment(user=self.no_accountant)
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        # 19.0: "in_process" until the bank side is reconciled
        self.assertEqual(mirror_payment.state, payment.state)
        self.assertIn(mirror_payment.state, ("in_process", "paid"))
        self.assertEqual(
            self._second_amounts(mirror_payment.move_id),
            self._second_amounts(payment.move_id),
        )

        payment.action_draft()
        self.assertEqual(mirror_payment.state, "draft")
        payment.write({"amount": 150.0, "rate_main_second": 90000.0})
        payment.action_post()
        self.assertIn(mirror_payment.state, ("in_process", "paid"))
        self.assertEqual(mirror_payment.move_id.state, "posted")
        self.assertEqual(mirror_payment.amount, 150.0)
        self.assertEqual(mirror_payment.rate_main_second, 90000.0)
        self.assertEqual(mirror_payment.move_id.name, payment.move_id.name)
        self.assertEqual(
            self._second_amounts(mirror_payment.move_id),
            self._second_amounts(payment.move_id),
        )

        payment.action_cancel()
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(mirror_payment.state, "canceled")
        self.assertEqual(mirror_payment.move_id.state, "cancel")

    def test_payment_delete(self):
        payment = self._payment(user=self.no_accountant)
        mirror_payment = self._mirror(payment.move_id).origin_payment_id
        payment.action_draft()
        payment.unlink()
        self.assertFalse(mirror_payment.exists())

    def test_locked_period_refused(self):
        invoice = self._invoice(post=True)
        mirror = self._mirror(invoice)
        self.official.fiscalyear_lock_date = "2026-01-31"
        for action in ("button_draft", "button_cancel"):
            with self.assertRaises(UserError) as error:
                getattr(invoice, action)()
            message = str(error.exception)
            self.assertIn(self.official.name, message)
            self.assertIn("01/31/2026", message)
            self.assertIn(invoice.name, message)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(mirror.state, "posted")

    def test_reconciliation_redone_around_rewrite(self):
        invoice = self._invoice(post=True)
        payment = self._pay(invoice)
        mirror = self._mirror(invoice)
        mirror_payment = self._mirror(payment.move_id)
        self.assertEqual(mirror.payment_state, invoice.payment_state)
        self.assertIn(mirror.payment_state, ("paid", "in_payment"))

        invoice.button_draft()
        self.assertFalse(
            mirror_payment.line_ids.matched_debit_ids
            | mirror_payment.line_ids.matched_credit_ids,
            "the mirror's reconciliations are removed",
        )
        invoice.action_post()
        self.assertEqual(mirror.payment_state, invoice.payment_state)
        self.assertIn(mirror.payment_state, ("paid", "in_payment"))

    # ------------------------------------------------------------------
    # migration 19.0.1.0.2
    # ------------------------------------------------------------------

    def test_migration_links_19_mirrors(self):
        """Mirrors made before 19.0.1.0.2 (no link) are linked by their log,
        else by number, so a re-post rewrites them instead of mirroring the
        transaction twice."""
        by_log = self._entry(post=True)
        by_name = self._entry(post=True)
        mirrors = self._mirror(by_log) + self._mirror(by_name)
        self.assertEqual(len(mirrors), 2)
        self._logs(by_name).unlink()
        (by_log + by_name).flush_recordset()
        self.env["nv.sync.log"].flush_model()
        self.env.cr.execute(
            "UPDATE account_move SET nv_mirror_move_id = NULL WHERE id IN %s",
            [tuple((by_log + by_name).ids)],
        )
        self.env.cr.execute(
            "UPDATE nv_sync_log SET source_ref = NULL WHERE source_move_id = %s",
            [by_log.id],
        )
        self.env.invalidate_all()
        self.assertFalse(self._mirror(by_log) | self._mirror(by_name))

        script = load_migration("post-mirror-links")
        script.migrate(self.env.cr, "19.0.1.0.1")
        script.migrate(self.env.cr, "19.0.1.0.1")
        self.env.invalidate_all()
        self.assertEqual(self._mirror(by_log) + self._mirror(by_name), mirrors)
        self.assertEqual(set(self._logs(by_log).mapped("source_ref")), {by_log.name})

        # re-posted after the upgrade: the linked mirror is rewritten
        by_name.button_draft()
        by_name.action_post()
        self.assertEqual(self._mirror(by_name), mirrors[1])
        self.assertEqual(
            self.env["account.move"]
            .sudo()
            .search_count([("is_mirror", "=", True), ("source_move_ref", "=", by_name.name)]),
            1,
        )

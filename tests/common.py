"""Shared set-up for the nv_dual_bookkeeping tests (from the 20.0 rebuild,
adapted to 19.0).

Two companies, as the module is used: an operating company in USD with LBP
as second currency (nv_double_currencies) whose Official Books company is a
second company built from the same chart template (same account codes, tax
names and journal codes), journals linked by code. Users without admin
rights: a plain accountant, an accountant allowed to mark Non-Official, a
sync manager (operating company) and a metadata member (official company
only).
"""

from odoo import Command
from odoo.tests import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

RATE = 89500.0


class DualBookkeepingCommon(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.usd = cls.company.currency_id
        cls.lbp = cls.setup_other_currency("LBP", rates=[("2020-01-01", RATE)])
        cls.company.write(
            {
                "enable_second_currency_for_company": True,
                "second_currency_id": cls.lbp.id,
            }
        )

        cls.official_data = cls.setup_other_company(name="Official Books")
        cls.official = cls.official_data["company"]
        cls.official.write(
            {
                "enable_second_currency_for_company": True,
                "second_currency_id": cls.lbp.id,
            }
        )
        cls.company.official_company_id = cls.official

        cls.sale_journal = cls.company_data["default_journal_sale"]
        cls.purchase_journal = cls.company_data["default_journal_purchase"]
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.misc_journal = cls.company_data["default_journal_misc"]
        cls.off_sale_journal = cls.official_data["default_journal_sale"]
        cls.off_purchase_journal = cls.official_data["default_journal_purchase"]
        cls.off_bank_journal = cls.official_data["default_journal_bank"]
        cls.off_misc_journal = cls.official_data["default_journal_misc"]
        for journal, off_journal in (
            (cls.sale_journal, cls.off_sale_journal),
            (cls.purchase_journal, cls.off_purchase_journal),
            (cls.bank_journal, cls.off_bank_journal),
            (cls.misc_journal, cls.off_misc_journal),
        ):
            journal.official_journal_id = off_journal
        cls._setup_outstanding_accounts(cls.official, cls.off_bank_journal)

        # partner_a / partner_b come from the parent set-up (Official by
        # default); a Non-Official contact:
        cls.partner_no = cls.env["res.partner"].create(
            {"name": "Non-Official customer", "is_official": False}
        )

        both = [Command.set((cls.company + cls.official).ids)]
        cls.accountant = new_test_user(
            cls.env,
            login="nv_db_accountant",
            groups="base.group_user,account.group_account_user",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.no_accountant = new_test_user(
            cls.env,
            login="nv_db_no_accountant",
            groups="base.group_user,nv_dual_bookkeeping.group_can_set_non_official",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.sync_manager = new_test_user(
            cls.env,
            login="nv_db_sync_manager",
            groups="base.group_user,nv_dual_bookkeeping.group_sync_manager",
            company_id=cls.company.id,
            company_ids=both,
        )
        cls.meta_user = new_test_user(
            cls.env,
            login="nv_db_meta_user",
            groups="base.group_user,nv_dual_bookkeeping.group_view_dual_meta",
            company_id=cls.official.id,
            company_ids=[Command.set(cls.official.ids)],
        )

        # The web client always sends allowed_company_ids.
        cls.env = cls.env(
            context=dict(cls.env.context, allowed_company_ids=cls.company.ids)
        )

    @classmethod
    def _setup_outstanding_accounts(cls, company, bank_journal):
        """Payments of a new company need outstanding accounts on its bank
        journal's payment methods to get a journal entry."""
        env = cls.env(context=dict(cls.env.context, allowed_company_ids=company.ids))
        for method_lines, name, code in (
            (
                bank_journal.inbound_payment_method_line_ids,
                "Outstanding Receipts",
                "OUTREC",
            ),
            (
                bank_journal.outbound_payment_method_line_ids,
                "Outstanding Payments",
                "OUTPAY",
            ),
        ):
            account = env["account.account"].search(
                [("code", "=", code), ("company_ids", "in", company.ids)], limit=1
            ) or env["account.account"].create(
                {
                    "name": name,
                    "code": code,
                    "account_type": "asset_current",
                    "reconcile": True,
                    "company_ids": company.ids,
                }
            )
            method_lines.payment_account_id = account

    # ------------------------------------------------------------------
    # Environments and documents
    # ------------------------------------------------------------------

    def _env_for(self, user, company=None):
        company = company or user.company_id
        return self.env(
            user=user,
            context=dict(self.env.context, allowed_company_ids=company.ids),
        )

    def _invoice(
        self,
        move_type="out_invoice",
        partner=None,
        amount=100.0,
        date="2026-01-10",
        user=None,
        post=False,
        journal=None,
        **extra,
    ):
        user = user or self.accountant
        vals = {
            "move_type": move_type,
            "partner_id": (partner or self.partner_a).id,
            "invoice_date": date,
            "date": date,
            "rate_main_second": RATE,
            "invoice_line_ids": [
                Command.create({"name": "line", "quantity": 1.0, "price_unit": amount})
            ],
            **extra,
        }
        if journal:
            vals["journal_id"] = journal.id
        move = self._env_for(user)["account.move"].create(vals)
        if post:
            move.action_post()
        return move

    def _entry(self, amount=100.0, date="2026-01-10", user=None, post=False, **extra):
        user = user or self.accountant
        move = self._env_for(user)["account.move"].create(
            {
                "move_type": "entry",
                "date": date,
                "journal_id": self.misc_journal.id,
                "rate_main_second": RATE,
                "line_ids": [
                    Command.create(
                        {
                            "name": "debit",
                            "account_id": self.company_data[
                                "default_account_expense"
                            ].id,
                            "debit": amount,
                        }
                    ),
                    Command.create(
                        {
                            "name": "credit",
                            "account_id": self.company_data[
                                "default_account_revenue"
                            ].id,
                            "credit": amount,
                        }
                    ),
                ],
                **extra,
            }
        )
        if post:
            move.action_post()
        return move

    def _payment(
        self,
        amount=100.0,
        partner=None,
        date="2026-01-10",
        user=None,
        post=True,
        journal=None,
        **extra,
    ):
        user = user or self.accountant
        payment = self._env_for(user)["account.payment"].create(
            {
                "amount": amount,
                "date": date,
                "payment_type": "inbound",
                "partner_type": "customer",
                "partner_id": (partner or self.partner_a).id,
                "journal_id": (journal or self.bank_journal).id,
                "rate_main_second": RATE,
                **extra,
            }
        )
        if post:
            payment.action_post()
        return payment

    def _mirror(self, move):
        """The mirror of a source transaction, from its latest synced log
        (sudo: the test users have no access to the official company)."""
        logs = self._logs(move).filtered(lambda log: log.sync_state == "synced")
        return logs[:1].target_move_id

    def _logs(self, move):
        return self.env["nv.sync.log"].sudo().search([("source_move_id", "=", move.id)])

    def _pay(self, documents, user=None, **wizard_vals):
        """Register Payment as the web client does it, as a user."""
        user = user or self.accountant
        env = self._env_for(user)
        documents = documents.with_env(env)
        # set by an onchange in the web client (nv_double_currencies)
        wizard_vals.setdefault("rate_main_second", RATE)
        wizard = (
            env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=documents.ids)
            .create(wizard_vals)
        )
        return wizard._create_payments()

    def _set_param(self, key, value):
        # 19.0: no get_bool / set_bool; the module reads "True" / "False"
        self.env["ir.config_parameter"].sudo().set_param(
            f"nv_dual_bookkeeping.{key}", "True" if value else "False"
        )

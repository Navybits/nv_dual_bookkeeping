from odoo import Command, api, models

SYNC_FIELDS = {'name', 'code', 'account_type', 'reconcile', 'currency_id',
               'non_trade', 'tag_ids', 'note', 'active'}


class AccountAccount(models.Model):
    """An account created or edited in a Main company is created / updated in
    its Official (S.A.L.) company too (same code, name, type, reconciliation,
    currency, non-trade, tags, notes, active): the mirror finds its accounts
    by code. Default taxes are not copied (each company has its own)."""
    _inherit = 'account.account'

    def _nv_official_vals(self):
        self.ensure_one()
        return {
            'name': self.name,
            'account_type': self.account_type,
            'reconcile': self.reconcile,
            'currency_id': self.currency_id.id,
            'non_trade': self.non_trade,
            'tag_ids': [Command.set(self.tag_ids.ids)],
            'note': self.note,
            'active': self.active,
        }

    @api.model_create_multi
    def create(self, vals_list):
        # The form's per-company codes (code_mapping_ids) write the code of
        # each company during the create: those writes must not copy to
        # S.A.L. too (it made a second, code-less account). Copied once, below.
        accounts = super(AccountAccount, self.with_context(nv_account_creating=True)).create(vals_list)
        accounts = accounts.with_env(self.env)
        if not self.env.context.get('nv_account_sync'):
            accounts._nv_copy_to_official()
        return accounts

    def write(self, vals):
        if (self.env.context.get('nv_account_sync') or self.env.context.get('nv_account_creating')
                or not SYNC_FIELDS & set(vals)):
            return super().write(vals)
        # code of each account in its Main company before the change (to find
        # the S.A.L. account when the code itself is changed)
        old_codes = {
            (account.id, company.id): account.with_company(company).code
            for account in self
            for company in account.company_ids.filtered('official_company_id')
        }
        result = super().write(vals)
        Account = self.env['account.account'].sudo().with_context(
            nv_account_sync=True, active_test=False)
        for account in self:
            for company in account.company_ids.filtered('official_company_id'):
                official = company.official_company_id
                if official in account.company_ids:
                    continue  # shared with S.A.L.: nothing to copy
                old_code = old_codes.get((account.id, company.id))
                target = Account.with_company(official).search([('code', '=', old_code)], limit=1) \
                    if old_code else Account
                if not target:
                    account._nv_copy_to_official()  # missing in S.A.L.: create it
                    continue
                target.with_company(official).write(dict(
                    account._nv_official_vals(), code=account.with_company(company).code))
        return result

    def _nv_copy_to_official(self):
        Account = self.env['account.account'].sudo().with_context(
            nv_account_sync=True, active_test=False)
        for account in self:
            for company in account.company_ids.filtered('official_company_id'):
                official = company.official_company_id
                if official in account.company_ids:
                    continue  # already shared with S.A.L.
                code = account.with_company(company).code
                if not code or Account.with_company(official).search_count(
                        [('code', '=', code)], limit=1):
                    continue
                new = Account.with_company(official).create(dict(
                    account._nv_official_vals(),
                    code=code,
                    company_ids=[Command.set(official.ids)],
                ))
                if new.with_company(official).code != code:
                    new.with_company(official).write({'code': code})

from odoo import Command, api, models


class AccountAccount(models.Model):
    """An account created in a Main company is created in its Official
    (S.A.L.) company too (same code, name, type, reconciliation, currency,
    tags), when S.A.L. has no account with that code yet: the mirror finds
    its accounts by code. Default taxes are not copied (each company has its
    own). Edits are not copied."""
    _inherit = 'account.account'

    @api.model_create_multi
    def create(self, vals_list):
        accounts = super().create(vals_list)
        if not self.env.context.get('nv_account_sync'):
            accounts._nv_copy_to_official()
        return accounts

    def _nv_copy_to_official(self):
        Account = self.env['account.account'].sudo().with_context(nv_account_sync=True)
        for account in self:
            for company in account.company_ids.filtered('official_company_id'):
                official = company.official_company_id
                if official in account.company_ids:
                    continue  # already shared with S.A.L.
                code = account.with_company(company).code
                if not code or Account.with_company(official).search_count(
                        [('code', '=', code)], limit=1):
                    continue
                Account.with_company(official).create({
                    'name': account.name,
                    'code': code,
                    'account_type': account.account_type,
                    'reconcile': account.reconcile,
                    'currency_id': account.currency_id.id,
                    'non_trade': account.non_trade,
                    'tag_ids': [Command.set(account.tag_ids.ids)],
                    'note': account.note,
                    'company_ids': [Command.set(official.ids)],
                })

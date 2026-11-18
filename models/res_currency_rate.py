from odoo import api, models

RATE_FIELDS = {'name', 'rate', 'company_rate', 'inverse_company_rate', 'currency_id'}


class ResCurrencyRate(models.Model):
    """A rate added, edited or deleted in a Main company is done the same way
    in its Official (S.A.L.) company, so the mirror converts the foreign
    amounts with the same rate as its source. Only Main -> S.A.L.; a rate
    entered in S.A.L. itself is left alone."""
    _inherit = 'res.currency.rate'

    def _nv_official_company(self):
        """The S.A.L. company of this rate's company, when both keep their
        books in the same currency (else the rate would not mean the same)."""
        company = self.company_id
        official = company.official_company_id
        if official and official.currency_id == company.currency_id:
            return official
        return self.env['res.company']

    def _nv_official_rate(self, official, currency, date):
        return self.sudo().search([
            ('company_id', '=', official.id),
            ('currency_id', '=', currency.id),
            ('name', '=', date),
        ], limit=1)

    def _nv_push_to_official(self, old_keys=None):
        Rate = self.sudo().with_context(nv_rate_sync=True)
        for rate in self:
            official = rate._nv_official_company()
            if not official:
                continue
            # S.A.L.'s rate of the same day; else the one of the old day (the
            # date or currency was changed in Main), moved to the new day
            target = self._nv_official_rate(official, rate.currency_id, rate.name)
            if not target and old_keys and rate.id in old_keys:
                target = self._nv_official_rate(official, *old_keys[rate.id])
            vals = {
                'name': rate.name,
                'currency_id': rate.currency_id.id,
                'company_rate': rate.company_rate,
            }
            if target:
                target.with_context(nv_rate_sync=True).write(vals)
            else:
                Rate.with_company(official).create(dict(vals, company_id=official.id))

    @api.model_create_multi
    def create(self, vals_list):
        rates = super().create(vals_list)
        if not self.env.context.get('nv_rate_sync'):
            rates._nv_push_to_official()
        return rates

    def write(self, vals):
        if self.env.context.get('nv_rate_sync') or not RATE_FIELDS & set(vals):
            return super().write(vals)
        old_keys = {rate.id: (rate.currency_id, rate.name) for rate in self}
        result = super().write(vals)
        self._nv_push_to_official(old_keys)
        return result

    def unlink(self):
        to_remove = self.env['res.currency.rate']
        if not self.env.context.get('nv_rate_sync'):
            for rate in self:
                official = rate._nv_official_company()
                if official:
                    to_remove |= self._nv_official_rate(official, rate.currency_id, rate.name)
        result = super().unlink()
        to_remove.with_context(nv_rate_sync=True).unlink()
        return result

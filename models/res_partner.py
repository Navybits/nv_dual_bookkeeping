from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_official = fields.Boolean(
        string='Official',
        default=lambda self: self._default_is_official_partner(),
        help=(
            "When checked, invoices, bills, credit notes, and payments for this "
            "contact default to the Official (O) bookkeeping stream. "
            "Uncheck for contacts whose transactions should always be recorded "
            "as Non-Official (NO)."
        ),
    )

    # Used in the view to hide the Dual Bookkeeping group when the user's
    # active company is the Official Books company (no O/NO choice exists there).
    env_company_is_official = fields.Boolean(
        compute='_compute_env_company_is_official',
        store=False,
    )

    @api.model
    def _default_is_official_partner(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'nv_dual_bookkeeping.default_is_official', 'False'
        )
        return param.strip().lower() not in ('false', '0', '')

    @api.depends_context('allowed_company_ids')
    def _compute_env_company_is_official(self):
        is_official = self.env.company.is_official_company
        for partner in self:
            partner.env_company_is_official = is_official

from odoo import models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def write(self, vals):
        """EXTENDS account: the analytic distribution and the due date of a
        line can be edited on a posted entry (e.g. from Journal Items); the
        mirror is updated, as nothing is reposted."""
        result = super().write(vals)
        if {'analytic_distribution', 'date_maturity'} & set(vals):
            self.env['nv.sync.engine'].sync_posted_edits(
                self.move_id.filtered(lambda m: m.state == 'posted' and not m.is_mirror))
        return result

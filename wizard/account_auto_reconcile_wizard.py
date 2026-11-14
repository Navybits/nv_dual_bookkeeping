from odoo import models


class AccountAutoReconcileWizard(models.TransientModel):
    """Auto-Reconcile never matches an Official line with a Non-Official one:
    the Official lines are matched together, then the Non-Official lines
    together (each run is Odoo's own, on one stream). A mixed match would
    exist in the operating company only - the Official Books would differ.
    Lines that only match across the streams are left open."""

    _inherit = 'account.auto.reconcile.wizard'

    def _get_amls_domain(self):
        domain = super()._get_amls_domain()
        stream = self.env.context.get('nv_official_stream')
        if stream is not None:
            domain = domain + [('move_id.is_official', '=', stream)]
        return domain

    def _nv_by_stream(self, method_name):
        lines = self.env['account.move.line']
        for stream in (True, False):
            wizard = self.with_context(nv_official_stream=stream)
            lines |= getattr(super(AccountAutoReconcileWizard, wizard), method_name)()
        return lines

    def _auto_reconcile_one_to_one(self):
        if self.env.context.get('nv_official_stream') is not None:
            return super()._auto_reconcile_one_to_one()
        return self._nv_by_stream('_auto_reconcile_one_to_one')

    def _auto_reconcile_zero_balance(self):
        if self.env.context.get('nv_official_stream') is not None:
            return super()._auto_reconcile_zero_balance()
        return self._nv_by_stream('_auto_reconcile_zero_balance')

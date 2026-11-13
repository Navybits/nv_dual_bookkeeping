from odoo import _, models
from odoo.exceptions import UserError


class AccountReconcileWizard(models.TransientModel):
    """The write-off and transfer entries the reconcile screen creates by
    itself take the Official flag of the lines they close. Without it they
    took the default flag (Non-Official in the operating company): the
    write-off of an Official customer's difference stayed out of the Official
    Books, where the customer kept the difference open."""

    _inherit = 'account.reconcile.wizard'

    def _nv_lines_official(self):
        """True / False: the Official flag of the lines matched; None in the
        Official Books company (always Official)."""
        moves = self.move_line_ids.move_id
        if self.company_id.is_official_company:
            return None
        flags = set(moves.mapped('is_official'))
        if len(flags) > 1:
            raise UserError(_(
                "Official and Non-Official entries cannot be matched together: "
                "match the Official ones and the Non-Official ones separately."))
        return flags.pop() if flags else None

    def create_write_off(self):
        official = self._nv_lines_official()
        if official is None:
            return super().create_write_off()
        return super(AccountReconcileWizard, self.with_context(
            nv_reconcile_is_official=official)).create_write_off()

    def create_transfer(self):
        official = self._nv_lines_official()
        if official is None:
            return super().create_transfer()
        return super(AccountReconcileWizard, self.with_context(
            nv_reconcile_is_official=official)).create_transfer()

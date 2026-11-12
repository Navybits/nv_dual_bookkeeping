import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class NvOfficialChangeWizard(models.TransientModel):
    """
    Change a posted Official entry, invoice or payment to Non-Official.

    Two methods, so the Official numbering never gets a gap:

    renumber — only when the entry holds the LAST Official number of its
        sequence (checked in its own journal and in the official company's
        journal, where its mirror holds the same number). The entry is reset
        to draft (its mirror too, D2), the mirror is deleted, the entry's
        number is cleared and it is posted again with the next Non-Official
        number. The next Official entry reuses the freed number.

    reverse  — always available. Invoices / entries: an Official reversal is
        posted (and mirrored), then a Non-Official draft copy is created.
        Payments: the payment is cancelled (its mirror too), then a
        Non-Official draft copy is created.
    """

    _name = 'nv.official.change.wizard'
    _description = 'Change Posted Entry to Non-Official'

    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Entry',
        required=True,
        readonly=True,
    )
    payment_id = fields.Many2one(
        comodel_name='account.payment',
        related='move_id.origin_payment_id',
    )
    renumber_block_reason = fields.Char(
        string='Why Renumbering Is Not Possible',
        compute='_compute_renumber_block_reason',
    )
    method = fields.Selection(
        selection=[
            ('renumber', 'Renumber as Non-Official'),
            ('reverse', 'Reverse and recreate as Non-Official'),
        ],
        string='Method',
        compute='_compute_method',
        store=True,
        precompute=True,
        readonly=False,
        required=True,
    )
    not_sent_confirmed = fields.Boolean(
        string='Not sent to the customer or declared',
        help="Renumbering removes the Official number from this document. Only do it "
             "if the document was not sent to the customer/vendor and not included "
             "in a VAT declaration.",
    )
    reason = fields.Text(
        string='Reason',
        required=True,
    )

    @api.depends('move_id')
    def _compute_renumber_block_reason(self):
        for wizard in self:
            wizard.renumber_block_reason = wizard._get_renumber_block_reason()

    @api.depends('renumber_block_reason')
    def _compute_method(self):
        for wizard in self:
            wizard.method = 'reverse' if wizard.renumber_block_reason else 'renumber'

    # -------------------------------------------------------------------------
    # Checks
    # -------------------------------------------------------------------------

    @api.model
    def _check_move(self, move):
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_("Only accounting managers can change a posted entry to Non-Official."))
        if move.state != 'posted' or not move.is_official:
            raise UserError(_("Only posted Official entries can be changed to Non-Official."))
        if move.is_mirror or move.company_id.is_official_company:
            raise UserError(_(
                "Entries of the Official Books company cannot be changed to Non-Official. "
                "Change the source entry in the operating company instead."
            ))
        if move.line_ids.matched_debit_ids or move.line_ids.matched_credit_ids:
            raise UserError(_(
                "'%s' is reconciled. Unreconcile it first (remove its payments, "
                "or undo the bank reconciliation), then try again."
            ) % move.name)

    def _get_renumber_block_reason(self):
        self.ensure_one()
        move = self.move_id
        if not move:
            return False
        mirror = move.sudo().nv_mirror_move_id
        if move.inalterable_hash or mirror.inalterable_hash:
            return _("The entry or its mirror is locked by the inalterability hash.")
        if not move.sequence_prefix or not move.sequence_number:
            return _("The entry has no sequence number.")
        later_move = self.env['account.move'].sudo().search([
            ('journal_id', 'in', (move.journal_id | mirror.journal_id).ids),
            ('sequence_prefix', '=', move.sequence_prefix),
            ('sequence_number', '>', move.sequence_number),
        ], order='sequence_number desc', limit=1)
        if later_move:
            return _(
                "Later Official numbers exist (up to %s); renumbering would leave a "
                "gap in the Official sequence."
            ) % later_move.name
        return False

    # -------------------------------------------------------------------------
    # Confirm
    # -------------------------------------------------------------------------

    def action_confirm(self):
        self.ensure_one()
        move = self.move_id
        self._check_move(move)
        if self.method == 'renumber':
            block_reason = self._get_renumber_block_reason()
            if block_reason:
                raise UserError(block_reason)
            if not self.not_sent_confirmed:
                raise UserError(_(
                    "Confirm that the document was not sent to the customer/vendor "
                    "or declared, or use the reverse method instead."
                ))
            return self._renumber()
        return self._reverse()

    def _renumber(self):
        move = self.move_id
        payment = self.payment_id
        old_name = move.name
        # Reset to draft: the mirror is reset with its source (D2); the lock
        # check of the Official Books runs here.
        if payment:
            payment.action_draft()
        else:
            move.button_draft()
        self._delete_mirror(move)
        # Clearing the name allows the stream switch; posting assigns the next
        # NO number. nv_official_change: the user-edit checks (flag must match
        # the partner, locked once numbered) let this wizard through.
        move.with_context(nv_official_change=True).write({'name': False, 'is_official': False})
        if payment:
            payment.with_context(nv_official_change=True).write({'is_official': False})
            payment.action_post()
        else:
            move.action_post()
        move.message_post(body=_(
            "Changed from Official %(old)s to Non-Official %(new)s. Reason: %(reason)s",
            old=old_name, new=move.name, reason=self.reason,
        ))
        _logger.info(
            "nv_dual_bookkeeping: %s renumbered as Non-Official %s by uid=%s.",
            old_name, move.name, self.env.uid,
        )
        return {'type': 'ir.actions.act_window_close'}

    def _delete_mirror(self, move):
        """Delete the (reset) mirror of move — blocking: the source must not
        become Non-Official while its Official mirror stays in the books."""
        source = move.sudo()
        mirror = source.nv_mirror_move_id
        if not mirror:
            return
        if mirror.state == 'posted':
            raise UserError(_(
                "The mirror %(mirror)s in %(company)s could not be reset to draft "
                "(see the sync log of %(document)s), so %(document)s was not changed.",
                mirror=mirror.name, company=mirror.company_id.name, document=move.name,
            ))
        engine = self.env['nv.sync.engine']
        mirror_name = mirror.name
        engine._delete(mirror)
        source.write({'nv_mirror_move_id': False, 'nv_mirror_outdated': False})
        engine._log(move, 'delete', 'synced', 'manual', error=False)
        _logger.info(
            "nv_dual_bookkeeping: mirror %s of %s deleted (Change to Non-Official).",
            mirror_name, move.name,
        )

    def _reverse(self):
        move = self.move_id
        payment = self.payment_id
        if payment:
            # action_cancel resets and cancels the entry: its mirror follows (D2)
            payment.action_cancel()
            new_record = payment.copy({'is_official': False})
            move.message_post(body=_(
                "Cancelled and recreated as Non-Official draft payment. Reason: %(reason)s",
                reason=self.reason,
            ))
        else:
            today = fields.Date.context_today(self)
            # cancel=True posts the reversal through _post(): it is mirrored and
            # reconciled with the source, and so are their mirrors.
            reversal = move._reverse_moves([{
                'date': today,
                'invoice_date': today if move.is_invoice(include_receipts=True) else False,
                'ref': _("Reversal of: %(name)s (changed to Non-Official)", name=move.name),
            }], cancel=True)
            new_record = move.copy({
                'is_official': False,
                'date': move.date,
                'invoice_date': move.invoice_date,
            })
            move.message_post(body=_(
                "Reversed by %(reversal)s and recreated as Non-Official draft. Reason: %(reason)s",
                reversal=reversal.name, reason=self.reason,
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': new_record._name,
            'res_id': new_record.id,
            'view_mode': 'form',
            'target': 'current',
        }

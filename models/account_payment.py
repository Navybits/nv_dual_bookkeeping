import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    is_official = fields.Boolean(
        string='Official',
        default=lambda self: self._default_is_official(),
        tracking=True,
        copy=True,
        help="When True the resulting journal entry will use the Official (O) sequence. "
             "When False it will use the Non-Official (NO) sequence. Locked after posting. "
             "Copied on duplicate so the stream is always preserved.",
    )

    is_official_set = fields.Boolean(
        string='Official Flag Explicitly Set',
        default=lambda self: bool(self.env.context.get('default_partner_id')),
        copy=False,
        help="Set when the flag was chosen: the user clicked the toggle, it came "
             "from the contact or from the paid documents, or the payment was "
             "created in code.",
    )

    is_mirror = fields.Boolean(
        string='Mirror Entry',
        default=False,
        copy=False,
        help="Set to True by the sync engine when this payment is a mirror created "
             "in the official company. Forwarded to the underlying move before posting.",
    )

    source_move_ref = fields.Char(
        string='Source Reference',
        copy=False,
        help="Sequence number of the original payment in the main company. "
             "Forwarded to the underlying move before posting so _set_next_sequence() "
             "reuses that name instead of consuming a new slot.",
    )

    # Related fields — surface move-level sync state on the payment form
    sync_state = fields.Char(
        string='Sync State',
        related='move_id.sync_state',
        store=False,
    )

    show_dual_bookkeeping_ui = fields.Boolean(
        string='Show Dual Bookkeeping UI',
        related='move_id.show_dual_bookkeeping_ui',
        store=False,
    )

    company_is_official_company = fields.Boolean(
        string='Company Is Official',
        related='company_id.is_official_company',
        store=False,
    )

    company_official_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Official Company',
        related='company_id.official_company_id',
        store=False,
    )

    # -------------------------------------------------------------------------
    # Defaults / onchange
    # -------------------------------------------------------------------------

    @api.model
    def _default_is_official(self):
        """
        Priority order (mirrors account.move._default_is_official):
        1. Official Books company → always True.
        2. Partner flag from context.
        3. Global config parameter fallback.
        """
        if self.env.company.is_official_company:
            return True
        partner_id = self.env.context.get('default_partner_id')
        if partner_id:
            partner = self.env['res.partner'].browse(partner_id)
            return partner.is_official
        param = self.env['ir.config_parameter'].sudo().get_param(
            'nv_dual_bookkeeping.default_is_official', 'False'
        )
        return param.strip().lower() not in ('false', '0', '')

    @api.onchange('partner_id')
    def _onchange_partner_id_dual_bookkeeping(self):
        """Sync is_official from the partner whenever partner changes on a
        draft payment in a non-official-books company; a flag from an O/NO
        partner counts as set (D7)."""
        if (
            self.partner_id
            and self.state == 'draft'
            and not self.company_id.is_official_company
        ):
            self.is_official = self.partner_id.is_official
            self.is_official_set = True

    def onchange(self, values, field_names, fields_spec):
        """EXTENDS web: a user click on the toggle marks the flag as set (D7);
        see account.move.onchange."""
        result = super().onchange(values, field_names, fields_spec)
        if field_names and 'is_official' in field_names and 'is_official_set' in fields_spec:
            result.setdefault('value', {})['is_official_set'] = True
        return result

    # -------------------------------------------------------------------------
    # Create override — set is_official from partner for payments created
    # directly in code (mirrors account.move's create() override). Without
    # this, programmatic payment creation (e.g. nv_delivery_routes' driver
    # cash reconciliation / cash-on-delivery payments) never fires the
    # onchange above, silently defaults is_official to False, and the
    # sync-to-official-company hook then never runs for them.
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        # D7: only the web form sends is_official_set; payments created in
        # code count as set.
        for vals in vals_list:
            if 'is_official' not in vals and not self.env.company.is_official_company:
                partner_id = vals.get('partner_id')
                if partner_id:
                    partner = self.env['res.partner'].browse(partner_id)
                    vals['is_official'] = partner.is_official
            vals.setdefault('is_official_set', True)
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Write override — mirror account.move locking logic
    # -------------------------------------------------------------------------

    def write(self, vals):
        """
        Prevent changing is_official after the payment is posted, and restrict
        setting is_official=False to privileged users only.
        """
        if 'is_official' in vals:
            for payment in self:
                if payment.state == 'posted':
                    raise UserError(_(
                        "Cannot change the Official/Non-Official status of payment '%s' "
                        "after it has been posted."
                    ) % payment.name)

            # Mark as explicitly set so require_official_flag validation passes
            vals['is_official_set'] = True

            if vals['is_official'] is False:
                can_set_no = (
                    self.env.user.has_group('nv_dual_bookkeeping.group_can_set_non_official')
                    or self.env.user.has_group('account.group_account_manager')
                )
                if not can_set_no:
                    raise AccessError(_(
                        "You do not have the rights to mark payments as Non-Official. "
                        "Please contact your accounting manager."
                    ))

        return super().write(vals)

    # -------------------------------------------------------------------------
    # action_post override — push flag to the underlying journal entry
    # -------------------------------------------------------------------------

    def _generate_journal_entry(self, write_off_line_vals=None, force_balance=None, line_ids=None):
        """
        In Odoo 19, the underlying journal entry is created here and immediately
        set to 'in_process' state (not 'draft') by the native code.  We propagate
        our dual-bookkeeping flags immediately after super() so that:
        - _set_next_sequence() picks the right stream (O vs NO) or reuses the
          source name for mirror payments.
        - The sync guard in account.move.action_post() sees is_official=False
          for non-official payments and does not trigger a mirror sync.

        State guard: 'not in posted/cancel' (not == draft) because Odoo 19 sets
        the move to 'in_process' right after creation.

        sudo(): the group check in account.move.write() is intended for UI-initiated
        changes by the user.  Here we are propagating a value the user already
        validated (e.g. by selecting Non-Official in the payment wizard), so no
        additional group check is needed.
        """
        super()._generate_journal_entry(
            write_off_line_vals=write_off_line_vals,
            force_balance=force_balance,
            line_ids=line_ids,
        )
        for pay in self:
            if pay.move_id and pay.move_id.state not in ('posted', 'cancel'):
                pay._nv_push_flags_to_move()

    def _generate_move_vals(self, write_off_line_vals=None, force_balance=None, line_ids=None):
        """EXTENDS account: the journal entry is created with the payment's
        O/NO flag and its "explicitly set" mark (D7), so "Require explicit
        O/NO selection" applies to the entry as it does to the payment."""
        vals = super()._generate_move_vals(
            write_off_line_vals=write_off_line_vals,
            force_balance=force_balance,
            line_ids=line_ids,
        )
        vals['is_official'] = self.is_official
        vals['is_official_set'] = self.is_official_set
        return vals

    def _nv_push_flags_to_move(self):
        """Write the dual-bookkeeping flags of the payment on its unposted
        entry. Only the values that differ are written: writing is_official
        on a move always marks the flag as set (account.move.write), which
        would hide a flag never chosen (D7)."""
        self.ensure_one()
        move = self.move_id
        move_vals = {}
        if move.is_official != self.is_official:
            move_vals['is_official'] = self.is_official
        if move.is_official_set != self.is_official_set:
            move_vals['is_official_set'] = self.is_official_set
        if self.is_mirror:
            move_vals['is_mirror'] = True
        if self.source_move_ref:
            move_vals['source_move_ref'] = self.source_move_ref
        if move_vals:
            move.sudo().write(move_vals)

    def action_post(self):
        """
        Fallback: for payments that already have a move_id in a non-posted state
        (e.g. some journal types create the move on payment creation), stamp the
        flags now so _set_next_sequence() has them before super() triggers posting.

        For payments without a move_id at this point, _generate_journal_entry()
        above handles propagation when the move is created.

        sudo() — see note in _generate_journal_entry.
        """
        for payment in self:
            if payment.move_id and payment.move_id.state not in ('posted', 'cancel'):
                payment._nv_push_flags_to_move()

        return super().action_post()

    # -------------------------------------------------------------------------
    # Sync actions — delegate to the underlying journal entry
    # -------------------------------------------------------------------------

    def action_view_sync_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Logs'),
            'res_model': 'nv.sync.log',
            'view_mode': 'list,form',
            'domain': [('source_move_id', '=', self.move_id.id)],
            'context': {'default_source_move_id': self.move_id.id},
        }

    def action_manual_sync(self):
        """Delegate to the underlying move's manual sync action."""
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No journal entry is linked to this payment."))
        return self.move_id.action_manual_sync()

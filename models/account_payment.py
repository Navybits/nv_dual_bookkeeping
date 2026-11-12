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
        partner counts as set (D7).

        Once the payment has a number (reset to draft), a customer of the other
        type (P1 <-> P2) is refused: it would keep a number of the wrong stream."""
        if (
            self.partner_id
            and self.state == 'draft'
            and not self.company_id.is_official_company
        ):
            move = self._origin.move_id
            if (
                move and move._nv_has_number()
                and self.partner_id.is_official != self._origin.is_official
            ):
                raise UserError(_(
                    "Payment '%s' already has a number: the customer can only be changed "
                    "to one of the same Official/Non-Official type (P1 <-> P2 not allowed)."
                ) % move.name)
            self.is_official = self.partner_id.is_official
            self.is_official_set = True

    @api.onchange('is_official')
    def _onchange_is_official_dual_bookkeeping(self):
        """Declaring it makes the web client send a click on the O/NO toggle to
        the server (it only does so for fields with an onchange; on
        account.move a computed field depends on is_official, on
        account.payment nothing did), so that onchange() below marks the flag
        as set. Without it a payment without contact could never be posted
        with "Require explicit Official/Non-Official selection".

        Form only (driver payments / register-payment wizard create in code and
        are not affected): the Official toggle must match the customer."""
        if (
            self.partner_id
            and not self.company_id.is_official_company
            and self.is_official != self.partner_id.is_official
        ):
            raise UserError(
                self.env['account.move']._nv_partner_mismatch_message(self.partner_id))

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
    # Locked once numbered (avoid sequence gaps / mixed O-NO numbers)
    # -------------------------------------------------------------------------

    def web_save(self, vals, specification, next_id=None):
        """Form save only (driver payments, register-payment wizard and the
        sync engine call create()/write() directly, never web_save): a NEW
        payment's Official flag must match its customer, whatever the order
        the form was filled in. Existing payments are checked in write()."""
        if not self and vals.get('partner_id') and 'is_official' in vals:
            company = (self.env['res.company'].browse(vals['company_id'])
                       if vals.get('company_id') else self.env.company)
            partner = self.env['res.partner'].browse(vals['partner_id'])
            if (
                not self.env['account.move']._nv_skip_dual_checks()
                and not company.is_official_company
                and bool(vals['is_official']) != partner.is_official
            ):
                raise UserError(
                    self.env['account.move']._nv_partner_mismatch_message(partner))
        return super().web_save(vals, specification, next_id=next_id)

    def _nv_check_locked_after_number(self, vals):
        """User edits of existing payments in an operating company: same rules
        as account.move (flag must match the customer when it changes; once
        numbered no customer of the other type - the O/NO switch itself is
        Rule 1b in write(), for everyone). Not checked: Official Books
        companies, superuser (sync engine), the Change to Non-Official wizard,
        create() (drivers, wizards)."""
        if (not ({'is_official', 'partner_id'} & set(vals))
                or self.env['account.move']._nv_skip_dual_checks()):
            return
        new_partner = None
        if vals.get('partner_id'):
            new_partner = self.env['res.partner'].browse(vals['partner_id'])
        for payment in self:
            if payment.company_id.is_official_company:
                continue
            move = payment.move_id
            numbered = bool(move) and move._nv_has_number()
            if numbered and 'is_official' in vals and bool(vals['is_official']) != payment.is_official:
                continue  # Rule 1b in write() gives the error
            if (
                numbered and new_partner and new_partner != payment.partner_id
                and new_partner.is_official != payment.is_official
            ):
                raise UserError(_(
                    "Payment '%s' already has a number: the customer can only be changed "
                    "to one of the same Official/Non-Official type (P1 <-> P2 not allowed)."
                ) % move.name)
            if 'is_official' in vals:
                partner = new_partner if 'partner_id' in vals else payment.partner_id
                changed = (bool(vals['is_official']) != payment.is_official
                           or partner != payment.partner_id)
                if changed and partner and bool(vals['is_official']) != partner.is_official:
                    raise UserError(
                        self.env['account.move']._nv_partner_mismatch_message(partner))

    # -------------------------------------------------------------------------
    # Write override — mirror account.move locking logic
    # -------------------------------------------------------------------------

    def write(self, vals):
        """
        Prevent changing is_official after the payment is posted, and restrict
        setting is_official=False to privileged users only.
        """
        self._nv_check_locked_after_number(vals)
        if 'is_official' in vals:
            for payment in self:
                # Same as account.move Rule 1b: a payment holding a number
                # (posted, or reset to draft: it keeps its number on repost)
                # cannot switch stream. Covers posted payments too: a
                # state == 'posted' check never matched (Odoo 19 payments are
                # in_process / paid).
                move = payment.move_id
                if (
                    bool(vals['is_official']) != payment.is_official
                    and move.posted_before
                    and move.name and move.name != '/'
                ):
                    raise UserError(_(
                        "Payment '%s' already holds a number from its sequence, so its "
                        "Official/Non-Official status cannot be switched. Use the "
                        "'Change to Non-Official' button on the posted payment instead."
                    ) % move.name)

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

        if 'journal_id' in vals:
            self._check_journal_change(vals['journal_id'])

        result = super().write(vals)
        if 'memo' in vals:
            # The memo can be edited on a posted payment; nothing is reposted,
            # so it is copied to the mirror payment here.
            self.env['nv.sync.engine'].sync_payment_memo(
                self.filtered(lambda p: p.state != 'draft' and not p.is_mirror))
        return result

    def _check_journal_change(self, journal_id):
        """A payment posted once keeps its journal: moving it would free its
        number in the old journal, a gap in the (Official) numbering. Cancel
        it and create a new payment instead. Mirrors are exempt: the sync
        engine moves them when their source's journal link changes."""
        for payment in self:
            move = payment.move_id
            if (
                payment.journal_id.id != journal_id
                and not payment.is_mirror
                and move.posted_before
                and move.name and move.name != '/'
            ):
                raise UserError(_(
                    "Payment %(name)s already holds a number from journal %(journal)s, "
                    "so its journal cannot be changed. Cancel it and create a new "
                    "payment in the right journal.",
                    name=move.name, journal=payment.journal_id.display_name,
                ))

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

    def action_change_to_non_official(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No journal entry is linked to this payment."))
        return self.move_id.action_change_to_non_official()

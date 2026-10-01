import logging
from psycopg2 import errors as pgerrors
from odoo import api, fields, models, _
from odoo.exceptions import UserError, AccessError
from odoo.tools import SQL

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------

    is_official = fields.Boolean(
        string='Official',
        default=lambda self: self._default_is_official(),
        tracking=True,
        copy=True,
        help="When True this transaction uses the Official (O) sequence. "
             "When False it uses the Non-Official (NO) sequence. Locked after posting. "
             "Copied on duplicate/reversal so the stream is always preserved.",
    )

    is_official_set = fields.Boolean(
        string='Official Flag Explicitly Set',
        default=True,
        copy=False,
        help="Tracks whether the user explicitly chose Official or Non-Official "
             "before posting. Used when require_official_flag setting is enabled.",
    )

    dual_sequence_label = fields.Char(
        string='Sequence Type',
        compute='_compute_dual_sequence_label',
        store=False,
    )

    is_mirror = fields.Boolean(
        string='Mirror Entry',
        default=False,
        readonly=True,
        copy=False,
        help="Set to True by the sync engine when this record is a mirror created "
             "in the official company. Mirror entries should not be re-synced.",
    )

    source_move_ref = fields.Char(
        string='Source Reference',
        readonly=True,
        copy=False,
        help="Stores the sequence number of the original transaction in the main company "
             "(e.g. 'BLOM-O/2026/0001'). Set on mirror entries by the sync engine.",
    )

    sync_log_ids = fields.One2many(
        comodel_name='nv.sync.log',
        inverse_name='source_move_id',
        string='Sync Logs',
        copy=False,
    )

    sync_state = fields.Char(
        string='Sync State',
        compute='_compute_sync_state',
        store=False,
        help="Latest sync state derived from sync_log_ids.",
    )

    company_is_official_company = fields.Boolean(
        string='Company Is Official',
        related='company_id.is_official_company',
        store=False,
        help="Passed to views to show the official-company warning banner.",
    )

    company_official_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Official Company',
        related='company_id.official_company_id',
        store=False,
        help="Used in views to show/hide the manual sync button.",
    )

    show_dual_bookkeeping_ui = fields.Boolean(
        string='Show Dual Bookkeeping UI',
        compute='_compute_show_dual_bookkeeping_ui',
        store=False,
        help="True when the current user should see O/NO badges, mirror labels, and sync "
             "status. Always True in operating companies. In the Official Books company, "
             "True only for members of group_view_dual_meta.",
    )

    # -------------------------------------------------------------------------
    # Defaults
    # -------------------------------------------------------------------------

    @api.model
    def _default_is_official(self):
        """
        Priority order:
        1. Official Books company → always True.
        2. Partner flag (from default_partner_id context, e.g. when opening a new
           invoice pre-filled for a customer/vendor).
        3. System parameter fallback.
        """
        if self.env.company.is_official_company:
            return True
        partner_id = self.env.context.get('default_partner_id')
        if partner_id:
            partner = self.env['res.partner'].sudo().browse(partner_id)
            return partner.is_official
        param = self.env['ir.config_parameter'].sudo().get_param(
            'nv_dual_bookkeeping.default_is_official', 'False'
        )
        return param.strip().lower() not in ('false', '0', '')

    # -------------------------------------------------------------------------
    # Computed fields
    # -------------------------------------------------------------------------

    @api.depends('is_official')
    def _compute_dual_sequence_label(self):
        for move in self:
            move.dual_sequence_label = 'O' if move.is_official else 'NO'

    @api.depends('sync_log_ids', 'sync_log_ids.sync_state', 'sync_log_ids.sync_date')
    def _compute_sync_state(self):
        for move in self:
            logs = move.sync_log_ids.sorted('sync_date', reverse=True)
            move.sync_state = logs[0].sync_state if logs else False

    @api.depends('company_id', 'company_id.is_official_company')
    @api.depends_context('uid')
    def _compute_show_dual_bookkeeping_ui(self):
        """
        Controls whether dual-bookkeeping UI elements (O/NO badge, mirror label,
        sync buttons, source reference) are visible to the current user.

        - Operating company: always visible to all users.
        - Official Books company: visible only to members of group_view_dual_meta.
          This keeps the view clean for external auditors who should see standard
          accounting records without internal classification metadata.
        """
        in_meta_group = self.env.user.has_group('nv_dual_bookkeeping.group_view_dual_meta')
        for move in self:
            if move.company_id.is_official_company:
                move.show_dual_bookkeeping_ui = in_meta_group
            else:
                move.show_dual_bookkeeping_ui = True

    # -------------------------------------------------------------------------
    # Partner → is_official onchange
    # -------------------------------------------------------------------------

    @api.onchange('partner_id')
    def _onchange_partner_id_dual_bookkeeping(self):
        """Sync is_official from the partner whenever partner changes on a draft
        document in a non-official-books company."""
        if (
            self.partner_id
            and self.state == 'draft'
            and not self.company_id.is_official_company
        ):
            self.is_official = self.partner_id.is_official

    # -------------------------------------------------------------------------
    # Sequence overrides (Odoo 19 native sequence.mixin)
    # -------------------------------------------------------------------------

    def _get_last_sequence_domain(self, relaxed=False):
        """
        EXTENDS account sequence.mixin.

        Appends an is_official filter so that Odoo's pattern-matching always
        searches within the same stream (O or NO). This gives Official and
        Non-Official entries fully independent counters within the same journal.
        """
        where_string, param = super()._get_last_sequence_domain(relaxed)
        if where_string and where_string != "WHERE FALSE":
            where_string += " AND is_official = %(nv_is_official)s"
            param['nv_is_official'] = self.is_official
        return where_string, param

    def _get_last_sequence(self, relaxed=False, with_prefix=None):
        """
        EXTENDS sequence.mixin.

        For Official entries that have an official_journal_id mapping, also
        checks the official company's journal to find the true last sequence.
        This prevents collisions when entries are posted directly in the
        official company (bypassing the sync engine).

        Example: Operating Co has INV-O/2026/0003. Someone posts INV-O/2026/0004
        directly in the Official Co. Without this override the next Operating Co
        entry would also get 0004. With this override it correctly gets 0005.
        """
        local_last = super()._get_last_sequence(relaxed=relaxed, with_prefix=with_prefix)

        # Only applies to Official entries with a cross-company mapping
        if not self.is_official:
            return local_last
        official_journal = self.journal_id.sudo().official_journal_id
        if not official_journal:
            return local_last

        # Determine the sequence_prefix to search in the official journal
        if with_prefix:
            seq_prefix = with_prefix
        else:
            prefix = self.journal_id.seq_prefix_o
            if not prefix:
                return local_last
            move_date = self.date or self.invoice_date or fields.Date.context_today(self)
            seq_prefix = '%s/%04d/' % (prefix, move_date.year)

        # Find the highest-numbered posted entry in the official company's journal
        official_last_move = self.env['account.move'].sudo().search([
            ('journal_id', '=', official_journal.id),
            ('sequence_prefix', '=', seq_prefix),
            ('state', '=', 'posted'),
        ], order='sequence_number desc', limit=1)

        if not official_last_move:
            return local_last
        if not local_last:
            return official_last_move.name

        # Parse the local sequence number and compare
        try:
            _, local_fmt = self._get_sequence_format_param(local_last)
            local_seq_num = int(local_fmt.get('seq') or 0)
        except Exception:
            return local_last

        if official_last_move.sequence_number > local_seq_num:
            return official_last_move.name
        return local_last

    def _get_starting_sequence(self):
        """
        EXTENDS account sequence.mixin.

        Seeds the very first name for each stream in a given journal/period.
        Returns PREFIX/YYYY/0000 — Odoo increments to /0001 on the first post.

        Falls back to super() if the journal has no prefix configured, so that
        journals not set up for dual bookkeeping are completely unaffected.
        """
        self.ensure_one()
        journal = self.journal_id
        if self.move_type in ('out_refund', 'in_refund'):
            # Credit Notes drop the -O/-NO Official/Non-Official suffix
            # entirely, just R + the journal's own short code (e.g. RINV) -
            # Official vs Non-Official still count independently behind the
            # scenes via the is_official filter in _get_last_sequence_domain,
            # even though the visible prefix text is now identical for both.
            prefix = 'R%s' % (journal.code or 'INV')
        else:
            prefix = journal.seq_prefix_o if self.is_official else journal.seq_prefix_no
            if not prefix:
                return super()._get_starting_sequence()
        move_date = self.date or self.invoice_date or fields.Date.context_today(self)
        return '%s/%04d/0000' % (prefix, move_date.year)

    def _set_next_sequence(self):
        """
        EXTENDS sequence.mixin.

        Mirror entries are the only special case: they represent the SAME
        transaction as their source, so they reuse the source name without
        consuming a new slot. Numeric-only journals (use_numeric_sequence)
        get their own fully custom CODE/FYY/NNNNNN computation - Odoo's
        native regex-based sequence parsing can't reliably split a
        delimiter-less number into prefix/counter, so we bypass it
        entirely for those - but ONLY for Customer Invoices
        (move_type == 'out_invoice'). Credit Notes and everything else
        keep Odoo's normal behaviour untouched (the existing slash-based/
        native sequence logic below), even on a journal with
        use_numeric_sequence enabled.
        """
        self.ensure_one()
        if self.is_mirror and self.source_move_ref:
            self.name = self.source_move_ref
            return
        if self.journal_id.use_numeric_sequence and self.move_type == 'out_invoice':
            self.name = self._get_next_numeric_sequence_number()
            return
        return super()._set_next_sequence()

    @api.depends('journal_id.use_numeric_sequence', 'is_official')
    def _compute_name_placeholder(self):
        """
        EXTENDS account.move.

        Odoo computes the greyed-out "what the number will look like"
        placeholder shown on a draft move via _get_starting_sequence(),
        a completely separate code path from _set_next_sequence() above -
        so without this override, a draft on a numeric-sequence journal
        would misleadingly preview the OLD PREFIX/YYYY/NNNN format while
        actually posting with CODE/FYY/NNNNNN. This recomputes the
        placeholder the same way the real number will be computed,
        read-only (no DB write, unlike _get_next_numeric_sequence_number).
        """
        super()._compute_name_placeholder()
        for move in self:
            if not (move.journal_id.use_numeric_sequence and move.move_type == 'out_invoice'):
                continue
            if move.name and move.name != '/':
                continue
            move_date = move.date or move.invoice_date or fields.Date.context_today(move)
            flag_year = move._get_numeric_sequence_flag_year(move.is_official, move_date)
            name_prefix = move._get_numeric_sequence_name_prefix(move.journal_id, move.is_official, move_date)
            highest = move._find_highest_numeric_sequence(move.journal_id, move.is_official, flag_year)
            if not highest and move_date.year == move.journal_id.numeric_seq_continue_from_year:
                highest = (
                    move.journal_id.numeric_seq_continue_from_o if move.is_official
                    else move.journal_id.numeric_seq_continue_from_no
                ) or 0
            move.name_placeholder = '%s%06d' % (name_prefix, highest + 1)

    # -------------------------------------------------------------------------
    # Numeric sequence (CODE/FYY/NNNNNN) - see account_journal.py use_numeric_sequence
    #
    # e.g. INV/126/000001 = journal code "INV" / Official(1)+year(26) / counter.
    # Each journal uses ITS OWN code (journal.code), so this works unchanged
    # for any journal that turns the toggle on, not just Sales/Invoices.
    # -------------------------------------------------------------------------

    def _get_numeric_sequence_flag_year(self, is_official, move_date=None):
        move_date = move_date or self.date or self.invoice_date or fields.Date.context_today(self)
        return '%s%02d' % ('1' if is_official else '2', move_date.year % 100)

    def _get_numeric_sequence_name_prefix(self, journal, is_official, move_date=None):
        flag_year = self._get_numeric_sequence_flag_year(is_official, move_date)
        return '%s/%s/' % (journal.code or 'INV', flag_year)

    def _find_highest_numeric_sequence(self, journal, is_official, flag_year):
        """
        Highest existing counter for this year+flag stream, checked both in
        this journal and - for Official entries - the official company's
        mapped journal too, mirroring the cross-company collision check
        that _get_last_sequence() already does for the slash-based format.

        Matches on '/{flag_year}/' + 6 digits ANYWHERE in the name (not
        anchored to a specific journal code prefix), since a mirrored entry
        in the Official company keeps the Operating company's own journal
        code, which may differ from the Official journal's own code.
        """
        like_pattern = '%%/%s/______' % flag_year
        domain = [
            ('company_id', '=', journal.company_id.id),
            ('journal_id', '=', journal.id),
            ('is_official', '=', is_official),
            ('name', '=like', like_pattern),
            ('state', '=', 'posted'),
        ]
        move = self.env['account.move'].sudo().search(domain, order='name desc', limit=1)
        highest = int(move.name[-6:]) if move else 0

        if is_official:
            official_journal = journal.sudo().official_journal_id
            if official_journal:
                official_move = self.env['account.move'].sudo().search([
                    ('company_id', '=', official_journal.company_id.id),
                    ('journal_id', '=', official_journal.id),
                    ('is_official', '=', True),
                    ('name', '=like', like_pattern),
                    ('state', '=', 'posted'),
                ], order='name desc', limit=1)
                if official_move:
                    highest = max(highest, int(official_move.name[-6:]))
        return highest

    def _get_next_numeric_sequence_number(self):
        self.ensure_one()
        journal = self.journal_id
        move_date = self.date or self.invoice_date or fields.Date.context_today(self)
        flag_year = self._get_numeric_sequence_flag_year(self.is_official, move_date)
        name_prefix = self._get_numeric_sequence_name_prefix(journal, self.is_official, move_date)
        self.flush_recordset()
        highest = self._find_highest_numeric_sequence(journal, self.is_official, flag_year)
        if not highest and move_date.year == journal.numeric_seq_continue_from_year:
            highest = (
                journal.numeric_seq_continue_from_o if self.is_official
                else journal.numeric_seq_continue_from_no
            ) or 0
        return self._locked_increment_numeric(name_prefix, highest)

    def _locked_increment_numeric(self, name_prefix, highest):
        """
        Mirrors sequence.mixin._locked_increment(): a plain search-then-assign
        is vulnerable to two invoices computing the same "next" number when
        posted close together (e.g. in the same batch/transaction, before
        either is visible as 'posted' to the other's search) - this instead
        tries to claim the candidate number directly against the database and
        retries with the next one on a collision, exactly like Odoo's own
        sequence engine does for the standard PREFIX/YYYY/NNNN format.
        """
        self.ensure_one()
        seq = highest
        with self.env.cr.savepoint(flush=False) as sp:
            while True:
                seq += 1
                candidate = '%s%06d' % (name_prefix, seq)
                try:
                    self.env.cr.execute(SQL(
                        "UPDATE %(table)s SET name = %(name)s WHERE id = %(id)s",
                        table=SQL.identifier(self._table),
                        name=candidate,
                        id=self.id,
                    ), log_exceptions=False)
                    return candidate
                except (pgerrors.UniqueViolation, pgerrors.ExclusionViolation):
                    sp.rollback()

    @api.model
    def _preview_next_numeric_sequence(self, journal, is_official):
        """Used by account_journal.py's preview fields (read-only, no move needed)."""
        move_date = fields.Date.context_today(self)
        flag_year = '%s%02d' % ('1' if is_official else '2', move_date.year % 100)
        name_prefix = '%s/%s/' % (journal.code or 'INV', flag_year)
        highest = self._find_highest_numeric_sequence(journal, is_official, flag_year)
        if not highest and move_date.year == journal.numeric_seq_continue_from_year:
            highest = (journal.numeric_seq_continue_from_o if is_official else journal.numeric_seq_continue_from_no) or 0
        return '%s%06d' % (name_prefix, highest + 1)

    # -------------------------------------------------------------------------
    # Create override — set is_official from partner when creating from SO/PO
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Only auto-set when not already supplied and not in Official Books company.
            # This covers programmatic creation (SO/PO invoice, batch billing, etc.)
            # where partner_id is in vals but is_official is not.
            if 'is_official' not in vals and not self.env.company.is_official_company:
                partner_id = vals.get('partner_id')
                if partner_id:
                    partner = self.env['res.partner'].browse(partner_id)
                    vals['is_official'] = partner.is_official
        return super().create(vals_list)

    # -------------------------------------------------------------------------
    # Write override — field locking
    # -------------------------------------------------------------------------

    def write(self, vals):
        """
        Enforce rules around is_official:
        1. Cannot be changed once a move is posted.
        2. Cannot be set to False in the Official Books company.
        3. Can only be set to False by users with the appropriate group.
        """
        if 'is_official' in vals:
            for move in self:
                # Rule 1: locked after posting
                if move.state == 'posted':
                    raise UserError(_(
                        "Cannot change the Official/Non-Official status of '%s' "
                        "after it has been posted."
                    ) % move.name)

                # Rule 2: Official Books company — all transactions must stay Official
                if vals['is_official'] is False and move.company_id.is_official_company:
                    raise UserError(_(
                        "All transactions in the Official Books company must be Official. "
                        "The Non-Official flag cannot be used here."
                    ))

            # Mark the flag as explicitly set so require_official_flag validation passes.
            # We do this regardless of the value (True or False) — both are valid explicit choices.
            vals['is_official_set'] = True

            # Rule 3: only privileged users may mark as Non-Official
            if vals['is_official'] is False:
                can_set_no = (
                    self.env.user.has_group('nv_dual_bookkeeping.group_can_set_non_official')
                    or self.env.user.has_group('account.group_account_manager')
                )
                if not can_set_no:
                    raise AccessError(_(
                        "You do not have the rights to mark transactions as Non-Official. "
                        "Please contact your accounting manager."
                    ))

        return super().write(vals)

    # -------------------------------------------------------------------------
    # action_post override
    # -------------------------------------------------------------------------

    def action_post(self):
        """
        Pre-posting validations and post-posting sync trigger.

        1. Require explicit O/NO selection when setting is enabled.
        2. Block direct posting in official company when setting is enabled.
        3. Call super() to perform the actual posting.
        4. Trigger sync engine (non-blocking — never prevents posting).
        """
        ICP = self.env['ir.config_parameter'].sudo()

        for move in self:
            # Validation 1: explicit flag required before posting
            # Skipped for Official Books company — the toggle does not exist there and
            # all transactions are always Official, so no conscious choice is needed.
            require_flag = ICP.get_param(
                'nv_dual_bookkeeping.require_official_flag', 'False'
            ).strip().lower() in ('true', '1')
            if require_flag and not move.is_official_set and not move.company_id.is_official_company:
                raise UserError(_(
                    "Please explicitly set Official or Non-Official on '%s' before posting. "
                    "You can do this using the O/NO toggle on the transaction form."
                ) % (move.name or move.ref or _('this transaction')))

            # Validation 2: block direct posting in official company
            block_direct = ICP.get_param(
                'nv_dual_bookkeeping.block_direct_posting_in_official', 'False'
            ).strip().lower() in ('true', '1')
            if block_direct and move.company_id.is_official_company and not move.is_mirror:
                raise UserError(_(
                    "Direct posting is disabled in the Official Books company. "
                    "Post transactions from the main operating company — "
                    "they will sync to '%s' automatically."
                ) % move.company_id.name)

        # Core posting
        result = super().action_post()

        # Post-posting: trigger sync engine for each eligible move
        for move in self:
            if move.is_official and not move.is_mirror and move.state == 'posted':
                if move.company_id.official_company_id:
                    try:
                        self.env['nv.sync.engine'].sync_move(move)
                    except Exception as e:
                        # Non-blocking: log error but never raise to the user here.
                        # The sync engine itself writes an error log and sends a
                        # bus notification, so the user is informed without losing the post.
                        _logger.error(
                            "nv_dual_bookkeeping: Sync failed for move %s: %s",
                            move.name, str(e),
                        )

        return result

    # -------------------------------------------------------------------------
    # Manual sync action
    # -------------------------------------------------------------------------

    def action_manual_sync(self):
        """
        Manually trigger sync to the official company for a single posted move.
        Restricted to Sync Manager group.
        """
        self.ensure_one()
        if not self.env.user.has_group('nv_dual_bookkeeping.group_sync_manager'):
            raise AccessError(_("Only Dual Bookkeeping Sync Managers can trigger manual sync."))

        if self.state != 'posted':
            raise UserError(_("Only posted transactions can be synced."))
        if not self.is_official:
            raise UserError(_("Only Official transactions are synced to the official company."))
        if self.is_mirror:
            raise UserError(_("Mirror entries cannot be re-synced."))
        if not self.company_id.official_company_id:
            raise UserError(_(
                "No Official Company is configured for '%s'. "
                "Set it in Settings > Accounting > Dual Bookkeeping."
            ) % self.company_id.name)

        self.env['nv.sync.engine'].sync_move(self, triggered_by='manual')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sync Triggered'),
                'message': _('Synchronisation to the official company has been triggered.'),
                'type': 'success',
                'sticky': False,
            },
        }

    # -------------------------------------------------------------------------
    # Smart button action — open sync logs
    # -------------------------------------------------------------------------

    def action_view_sync_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Logs'),
            'res_model': 'nv.sync.log',
            'view_mode': 'list,form',
            'domain': [('source_move_id', '=', self.id)],
            'context': {'default_source_move_id': self.id},
        }

import logging
from datetime import date as _date
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------

    official_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Official Company Journal',
        copy=False,
        help=(
            "Mirror journal in the official company. Leave empty if this journal "
            "is not used for Official (O) transactions that sync to the official company."
        ),
    )

    # Exposed as a flat field so view domains can reference it directly without
    # dotted traversal (company_id.official_company_id fails in Odoo 19 domains).
    journal_official_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Official Company (resolved)',
        related='company_id.official_company_id',
        store=False,
    )

    seq_prefix_o = fields.Char(
        string='Official Sequence Prefix',
        help=(
            'Prefix code for Official entries (e.g. BANK-O). '
            'Entries will be numbered BANK-O/2026/0001.'
        ),
    )

    seq_prefix_no = fields.Char(
        string='Non-Official Sequence Prefix',
        help=(
            'Prefix code for Non-Official entries (e.g. BANK-NO). '
            'Entries will be numbered BANK-NO/2026/0001.'
        ),
    )

    seq_preview_o = fields.Char(
        string='Next Official Entry',
        compute='_compute_seq_previews',
        store=False,
    )

    seq_preview_no = fields.Char(
        string='Next Non-Official Entry',
        compute='_compute_seq_previews',
        store=False,
    )

    use_numeric_sequence = fields.Boolean(
        string='Use CODE/FYY/NNNNNN Sequence',
        help=(
            'When enabled, entries are numbered as CODE/FYY/NNNNNN using this '
            "journal's own short code (e.g. INV/126/000001 = code INV, Official "
            'flag 1 + year 26, counter 000001), instead of the PREFIX/YYYY/NNNN '
            'format above. Resets to 000001 every new year. Independent of '
            'seq_prefix_o/seq_prefix_no, which are ignored while this is on. '
            'Applies to EVERY move type on this journal - Invoices, Bills, '
            'Journal Entries, and Payments. Refunds/Credit Notes get an R '
            'prepended to the code (e.g. RINV). Each move type counts '
            'independently - Invoices and Refunds never share a counter.'
        ),
    )
    numeric_seq_continue_from_year = fields.Integer(
        string='Continue From - Year',
        help=(
            'The year the two "Continue From" numbers below apply to (e.g. 2026, '
            'the current migration/transition year). Only invoices dated in THIS '
            'year use those numbers - any other year (2027, 2028, ...) always '
            'starts fresh at 000001, even if nothing has been posted for it yet.'
        ),
    )
    numeric_seq_continue_from_o = fields.Integer(
        string='Continue From (Official)',
        help=(
            'One-time migration value: the LAST number already used in the old '
            'external system for Official entries in the year above (e.g. 99724). '
            'The next entry created here will continue as 99725. Only matters '
            'until the first entry is posted for that year - after that, Odoo '
            'continues from its own posted entries.'
        ),
    )
    numeric_seq_continue_from_no = fields.Integer(
        string='Continue From (Non-Official)',
        help='Same as above, for Non-Official entries.',
    )
    numeric_seq_preview_o = fields.Char(
        string='Next Official Entry (Numeric)',
        compute='_compute_numeric_seq_previews',
        store=False,
    )
    numeric_seq_preview_no = fields.Char(
        string='Next Non-Official Entry (Numeric)',
        compute='_compute_numeric_seq_previews',
        store=False,
    )

    # -------------------------------------------------------------------------
    # Computed fields
    # -------------------------------------------------------------------------

    @api.depends('seq_prefix_o', 'seq_prefix_no')
    def _compute_seq_previews(self):
        year = _date.today().year
        for journal in self:
            for result_field, prefix_val, is_official in [
                ('seq_preview_o', journal.seq_prefix_o, True),
                ('seq_preview_no', journal.seq_prefix_no, False),
            ]:
                if not prefix_val:
                    journal[result_field] = ''
                    continue
                seq_prefix = '%s/%04d/' % (prefix_val, year)
                last_move = self.env['account.move'].sudo().search([
                    ('journal_id', '=', journal.id),
                    ('is_official', '=', is_official),
                    ('sequence_prefix', '=', seq_prefix),
                    ('state', '=', 'posted'),
                ], order='sequence_number desc', limit=1)
                next_num = (last_move.sequence_number + 1) if last_move else 1
                journal[result_field] = '%s%04d' % (seq_prefix, next_num)

    @api.depends('use_numeric_sequence', 'numeric_seq_continue_from_year', 'numeric_seq_continue_from_o', 'numeric_seq_continue_from_no')
    def _compute_numeric_seq_previews(self):
        # DISABLED (not removed) - the numeric sequence engine this called
        # into (account.move._preview_next_numeric_sequence) is commented
        # out, since that whole approach was superseded by the
        # PREFIX/YY/NNNNNN style now built into _get_starting_sequence().
        # Always blank while use_numeric_sequence stays inactive.
        for journal in self:
            journal.numeric_seq_preview_o = ''
            journal.numeric_seq_preview_no = ''
            # if not journal.use_numeric_sequence:
            #     journal.numeric_seq_preview_o = ''
            #     journal.numeric_seq_preview_no = ''
            #     continue
            # journal.numeric_seq_preview_o = self.env['account.move']._preview_next_numeric_sequence(journal, is_official=True)
            # journal.numeric_seq_preview_no = self.env['account.move']._preview_next_numeric_sequence(journal, is_official=False)

    # -------------------------------------------------------------------------
    # Cross-company read helpers
    # -------------------------------------------------------------------------

    def _official_journal_company_ctx(self):
        """Return context dict that adds the official companies to
        allowed_company_ids so cross-company official_journal_id Many2one
        values can be resolved without triggering record-rule access errors."""
        if not self.ids:
            return None
        self.env.cr.execute("""
            SELECT DISTINCT j2.company_id
              FROM account_journal j1
              JOIN account_journal j2 ON j2.id = j1.official_journal_id
             WHERE j1.id IN %s
        """, [tuple(self.ids)])
        official_cos = {r[0] for r in self.env.cr.fetchall()}
        if not official_cos:
            return None
        current = set(self.env.context.get('allowed_company_ids') or [self.env.company.id])
        extra = official_cos - current
        if not extra:
            return None
        return {'allowed_company_ids': list(current | extra)}

    def read(self, fields=None, load='_classic_read'):
        if not self.env.su and (fields is None or 'official_journal_id' in (fields or [])):
            ctx = self._official_journal_company_ctx()
            if ctx:
                return self.with_context(**ctx).read(fields=fields, load=load)
        return super().read(fields=fields, load=load)

    def web_read(self, specification):
        if not self.env.su and 'official_journal_id' in specification:
            ctx = self._official_journal_company_ctx()
            if ctx:
                return self.with_context(**ctx).web_read(specification)
        return super().web_read(specification)

    # -------------------------------------------------------------------------
    # ORM overrides
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        journals = super().create(vals_list)
        for journal in journals:
            # Auto-populate prefixes when not explicitly provided
            updates = {}
            if not journal.seq_prefix_o:
                code = (journal.code or 'JNL').upper()
                updates['seq_prefix_o'] = '%s/1' % code
            if not journal.seq_prefix_no:
                code = (journal.code or 'JNL').upper()
                updates['seq_prefix_no'] = '%s/2' % code
            if updates:
                journal.sudo().write(updates)
        return journals

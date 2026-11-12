from odoo import api, fields, models, _
from odoo.exceptions import UserError

FIX_SELECTION = [
    ('light', 'Light (in place)'),
    ('rewrite', 'Rewrite'),
    ('cannot', 'Cannot fix'),
]


class NvMirrorCheckWizard(models.TransientModel):
    """Compare documents with their mirror in the Official Books company
    (nv.mirror.checker) and repair the ticked ones. Opened from the menu it
    checks the Official documents of a period; from the Action menu / the
    form button, the selected documents.

    One row per document with differences, its suggested fix (the strongest
    any of its differences needs) and its differences. Fixing is per
    document: light fixes in place, rewrite rebuilds the mirror from the
    document; the document itself is never changed."""

    _name = 'nv.mirror.check.wizard'
    _description = 'Compare with Mirror'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True, readonly=True,
        default=lambda self: self.env.company,
    )
    source_move_ids = fields.Many2many(
        'account.move', string='Selected Documents', readonly=True,
        help="Set when opened from selected documents / payments: only these "
             "are compared. Empty: the Official documents of the period.",
    )
    date_from = fields.Date(string='From', default=lambda self: fields.Date.today().replace(month=1, day=1))
    date_to = fields.Date(string='To', default=fields.Date.today)
    checked = fields.Boolean()
    checked_count = fields.Integer(string='Documents Compared', readonly=True)
    document_count = fields.Integer(string='Documents with Differences', compute='_compute_counts')
    selected_count = fields.Integer(compute='_compute_counts')
    doc_ids = fields.One2many('nv.mirror.check.wizard.doc', 'wizard_id', string='Documents')

    @api.depends('doc_ids.state', 'doc_ids.selected')
    def _compute_counts(self):
        for wizard in self:
            open_docs = wizard.doc_ids.filtered(lambda d: d.state != 'fixed')
            wizard.document_count = len(open_docs)
            wizard.selected_count = len(open_docs.filtered(
                lambda d: d.selected and d.fix in ('light', 'rewrite')))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        model = self.env.context.get('active_model')
        ids = self.env.context.get('active_ids') or []
        if 'source_move_ids' in fields_list and ids and model in ('account.move', 'account.payment'):
            records = self.env[model].browse(ids)
            moves = records if model == 'account.move' else records.move_id
            res['source_move_ids'] = [fields.Command.set(moves.ids)]
            if len(moves.company_id) == 1:
                res['company_id'] = moves.company_id.id
        return res

    def _sources(self):
        if self.source_move_ids:
            return self.source_move_ids.sudo().filtered(lambda m: not m.is_mirror)
        if not self.company_id.official_company_id:
            raise UserError(_("No Official Company is configured for '%s'.", self.company_id.name))
        domain = [
            ('company_id', '=', self.company_id.id),
            ('is_mirror', '=', False),
            '|', ('nv_mirror_move_id', '!=', False),
                 '&', ('is_official', '=', True), ('state', '=', 'posted'),
        ]
        if self.date_from:
            domain.append(('date', '>=', self.date_from))
        if self.date_to:
            domain.append(('date', '<=', self.date_to))
        return self.env['account.move'].sudo().search(domain, order='date, id')

    @api.model
    def _doc_vals(self, source):
        """Values of the row of `source`, None when its mirror matches."""
        checker = self.env['nv.mirror.checker']
        diffs = checker.compare(source)
        fix, reason = checker.classify(source, diffs)
        if fix == 'ok':
            return None
        return {
            'source_move_id': source.id,
            'mirror_move_id': source.nv_mirror_move_id.id,
            'fix': fix,
            'reason': reason,
            # a rewrite is always an explicit choice
            'selected': fix == 'light',
            'state': 'to_fix',
            'line_ids': [fields.Command.create({
                'group': diff['group'],
                'label': diff['label'],
                'source_value': diff['source'],
                'mirror_value': diff['mirror'],
            }) for diff in diffs],
        }

    def action_check(self):
        self.ensure_one()
        sources = self._sources()
        doc_vals = [vals for vals in map(self._doc_vals, sources) if vals]
        self.doc_ids.unlink()
        self.write({
            'doc_ids': [fields.Command.create(vals) for vals in doc_vals],
            'checked': True,
            'checked_count': len(sources),
        })
        return self._reopen()

    def action_fix(self):
        """Fix the ticked documents (each in its own savepoint), then compare
        them again: a document still different is reported. Sync managers
        only (the mirrors are changed, and rewritten ones reset)."""
        self.ensure_one()
        if not self.env.user.has_group('nv_dual_bookkeeping.group_sync_manager'):
            raise UserError(_("Only Dual Bookkeeping Sync Managers can fix mirrors."))
        engine = self.env['nv.sync.engine']
        docs = self.doc_ids.filtered(
            lambda d: d.selected and d.state != 'fixed' and d.fix in ('light', 'rewrite'))
        for doc in docs:
            source = doc.source_move_id.sudo()
            try:
                with self.env.cr.savepoint():
                    engine.repair_mirror(source, doc.fix)
            except Exception as e:  # noqa: BLE001 - reported on the row
                doc.write({'state': 'error', 'message': str(e)})
                continue
            vals = self._doc_vals(source)
            if vals is None:
                doc.write({'state': 'fixed', 'message': False, 'selected': False,
                           'mirror_move_id': source.nv_mirror_move_id.id})
                continue
            doc.line_ids.unlink()
            doc.write({
                **vals,
                'state': 'error',
                'message': _("Still %s difference(s) after the fix.", len(vals['line_ids'])),
            })
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _('Compare with Mirror'),
        }


class NvMirrorCheckWizardDoc(models.TransientModel):
    _name = 'nv.mirror.check.wizard.doc'
    _description = 'Compare with Mirror: document'
    _order = 'id'

    wizard_id = fields.Many2one('nv.mirror.check.wizard', required=True, ondelete='cascade')
    selected = fields.Boolean(string='Fix')
    source_move_id = fields.Many2one('account.move', string='Document', readonly=True)
    mirror_move_id = fields.Many2one('account.move', string='Mirror', readonly=True)
    difference_count = fields.Integer(string='Differences', compute='_compute_difference_count')
    fix = fields.Selection(FIX_SELECTION, string='Suggested Fix', readonly=True)
    reason = fields.Char(string='Why Not', readonly=True)
    state = fields.Selection(
        [('to_fix', 'To Fix'), ('fixed', 'Fixed'), ('error', 'Not Fixed')],
        string='Result', readonly=True,
    )
    message = fields.Char(readonly=True)
    line_ids = fields.One2many('nv.mirror.check.wizard.line', 'doc_id', string='Differences', readonly=True)

    @api.depends('line_ids')
    def _compute_difference_count(self):
        for doc in self:
            doc.difference_count = len(doc.line_ids)


class NvMirrorCheckWizardLine(models.TransientModel):
    _name = 'nv.mirror.check.wizard.line'
    _description = 'Compare with Mirror: difference'
    _order = 'id'

    doc_id = fields.Many2one('nv.mirror.check.wizard.doc', required=True, ondelete='cascade')
    group = fields.Selection([
        ('link', 'Mirror'),
        ('header', 'Header'),
        ('lines', 'Lines'),
        ('totals', 'Totals'),
        ('payment', 'Payment'),
        ('reconciliation', 'Reconciliation'),
    ], readonly=True)
    label = fields.Char(string='Field', readonly=True)
    source_value = fields.Char(string='Document Value', readonly=True)
    mirror_value = fields.Char(string='Mirror Value', readonly=True)

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class NvMirrorCheckWizard(models.TransientModel):
    """Compare documents with their mirror in the Official Books company
    (nv.mirror.checker). Read-only: lists the differences, changes nothing.
    Opened from the menu it checks the Official documents of a period;
    from the Action menu / the form button, the selected documents."""

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
    document_count = fields.Integer(string='Documents with Differences', readonly=True)
    line_ids = fields.One2many(
        'nv.mirror.check.wizard.line', 'wizard_id', string='Differences', readonly=True)

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

    def action_check(self):
        self.ensure_one()
        checker = self.env['nv.mirror.checker']
        sources = self._sources()
        line_vals = []
        documents = 0
        for source in sources:
            diffs = checker.compare(source)
            if diffs:
                documents += 1
            line_vals += [{
                'source_move_id': source.id,
                'mirror_move_id': source.nv_mirror_move_id.id,
                'group': diff['group'],
                'label': diff['label'],
                'source_value': diff['source'],
                'mirror_value': diff['mirror'],
            } for diff in diffs]
        self.line_ids.unlink()
        self.write({
            'line_ids': [fields.Command.create(vals) for vals in line_vals],
            'checked': True,
            'checked_count': len(sources),
            'document_count': documents,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _('Compare with Mirror'),
        }


class NvMirrorCheckWizardLine(models.TransientModel):
    _name = 'nv.mirror.check.wizard.line'
    _description = 'Compare with Mirror: difference'
    _order = 'id'

    wizard_id = fields.Many2one('nv.mirror.check.wizard', required=True, ondelete='cascade')
    source_move_id = fields.Many2one('account.move', string='Document', readonly=True)
    mirror_move_id = fields.Many2one('account.move', string='Mirror', readonly=True)
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

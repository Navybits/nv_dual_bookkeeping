from odoo import api, fields, models, _
from odoo.exceptions import UserError


class NvMirrorPartnerFixWizard(models.TransientModel):
    """Repair of the partners of existing mirrors: the mirror of a document
    gets exactly its source's contacts (document, lines, payment), without
    resetting anything to draft. "Find" lists the mirrors to fix, "Fix"
    applies the list."""

    _name = 'nv.mirror.partner.fix.wizard'
    _description = 'Fix Mirror Partners'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True, readonly=True,
        default=lambda self: self.env.company,
    )
    source_move_ids = fields.Many2many(
        'account.move', string='Selected Documents', readonly=True,
        help="Set when the wizard is opened from selected documents or payments "
             "(Action menu): only these are checked. Empty: all documents.",
    )
    line_ids = fields.One2many(
        'nv.mirror.partner.fix.wizard.line', 'wizard_id', string='Mirrors', readonly=True)
    searched = fields.Boolean()
    to_fix_count = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')

    @api.depends('line_ids.state')
    def _compute_counts(self):
        for wizard in self:
            wizard.to_fix_count = len(wizard.line_ids.filtered(lambda l: l.state == 'to_fix'))
            wizard.error_count = len(wizard.line_ids.filtered(lambda l: l.state == 'error'))

    @api.model
    def default_get(self, fields_list):
        """Opened from the Action menu of documents / payments: only the
        selected ones are checked."""
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
        domain = [
            ('company_id', '=', self.company_id.id),
            ('nv_mirror_move_id', '!=', False),
            ('nv_mirror_move_id.state', '!=', 'cancel'),
        ]
        if self.source_move_ids:
            domain.append(('id', 'in', self.source_move_ids.ids))
        return self.env['account.move'].sudo().search(domain)

    def action_find(self):
        """List the mirrors whose partners differ from their source's."""
        self.ensure_one()
        if not self.company_id.official_company_id:
            raise UserError(_("No Official Company is configured for '%s'.", self.company_id.name))
        engine = self.env['nv.sync.engine']
        line_vals = []
        for source in self._sources():
            vals = {'source_move_id': source.id, 'mirror_move_id': source.nv_mirror_move_id.id}
            try:
                mirror, move_vals, lines, payment_vals = engine.mirror_partner_changes(source)
            except UserError as e:
                line_vals.append({**vals, 'state': 'error', 'message': str(e)})
                continue
            if not (move_vals or lines or payment_vals):
                continue
            line_vals.append({
                **vals,
                'state': 'to_fix',
                'current_partner_id': mirror.partner_id.id,
                'correct_partner_id': move_vals.get('partner_id', mirror.partner_id.id),
                'line_count': len(lines),
                'payment_to_fix': bool(payment_vals),
            })
        self.line_ids.unlink()
        self.write({'line_ids': [fields.Command.create(v) for v in line_vals], 'searched': True})
        return self._reopen()

    def action_fix(self):
        """Fix the listed mirrors (each in its own savepoint)."""
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.state == 'to_fix')
        results = self.env['nv.sync.engine'].fix_mirror_partners(lines.source_move_id)
        for line in lines:
            state, message = results.get(line.source_move_id.id, ('error', _("Not processed")))
            line.write({'state': 'error' if state == 'error' else 'fixed', 'message': message})
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'name': _('Fix Mirror Partners'),
        }


class NvMirrorPartnerFixWizardLine(models.TransientModel):
    _name = 'nv.mirror.partner.fix.wizard.line'
    _description = 'Fix Mirror Partners: mirror'

    wizard_id = fields.Many2one('nv.mirror.partner.fix.wizard', required=True, ondelete='cascade')
    source_move_id = fields.Many2one('account.move', string='Document', readonly=True)
    mirror_move_id = fields.Many2one('account.move', string='Mirror', readonly=True)
    current_partner_id = fields.Many2one('res.partner', string='Mirror Partner', readonly=True)
    correct_partner_id = fields.Many2one('res.partner', string='Correct Partner', readonly=True)
    line_count = fields.Integer(string='Lines to Fix', readonly=True)
    payment_to_fix = fields.Boolean(string='Payment', readonly=True)
    state = fields.Selection(
        [('to_fix', 'To Fix'), ('fixed', 'Fixed'), ('error', 'Cannot Fix')],
        readonly=True,
    )
    message = fields.Char(readonly=True)

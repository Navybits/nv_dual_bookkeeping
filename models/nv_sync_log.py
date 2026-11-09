import logging
from odoo import api, fields, models, _
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class NvSyncLog(models.Model):
    _name = 'nv.sync.log'
    _description = 'Dual Bookkeeping Sync Log'
    _order = 'sync_date desc, id desc'
    _rec_name = 'source_ref'

    source_move_id = fields.Many2one(
        comodel_name='account.move',
        string='Source Transaction',
        readonly=True,
        ondelete='set null',
        help="The original transaction in the main operating company.",
    )
    source_ref = fields.Char(
        string='Source Number',
        readonly=True,
        help="Number of the source transaction when the log was written; kept "
             "when the source is deleted.",
    )
    operation = fields.Selection(
        selection=[
            ('create', 'Mirror'),
            ('update', 'Update'),
            ('reset', 'Reset to Draft'),
            ('cancel', 'Cancel'),
            ('delete', 'Delete'),
            ('reconcile', 'Reconcile'),
            ('unreconcile', 'Unreconcile'),
        ],
        readonly=True,
        default='create',
        help="Step of the mirror's life-cycle this log records (D2).",
    )
    target_move_id = fields.Many2one(
        comodel_name='account.move',
        string='Mirror Transaction',
        readonly=True,
        ondelete='set null',
        help="The mirrored transaction created in the official company.",
    )
    source_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Source Company',
        readonly=True,
    )
    target_company_id = fields.Many2one(
        comodel_name='res.company',
        string='Target Company',
        readonly=True,
    )
    sync_date = fields.Datetime(
        string='Sync Date',
        readonly=True,
        default=fields.Datetime.now,
    )
    sync_state = fields.Selection(
        selection=[
            ('synced', 'Synced'),
            ('error', 'Error'),
            ('pending', 'Pending'),
        ],
        string='State',
        required=True,
        default='pending',
        readonly=True,
    )
    error_message = fields.Text(
        string='Error Message',
        readonly=True,
    )
    triggered_by = fields.Selection(
        selection=[
            ('auto', 'Automatic'),
            ('manual', 'Manual'),
        ],
        string='Triggered By',
        readonly=True,
        default='auto',
    )

    def action_retry(self):
        """
        Retry synchronisation for this log entry's source move.
        Restricted to users with the group_sync_manager group.
        """
        self.ensure_one()
        if not self.env.user.has_group('nv_dual_bookkeeping.group_sync_manager'):
            raise AccessError(_("Only Dual Bookkeeping Sync Managers can retry sync."))

        if not self.source_move_id:
            _logger.warning("nv.sync.log %d: retry called but source_move_id is unset.", self.id)
            return

        # The engine writes a new log with the result; this one keeps its error
        # (it used to be set to "pending" and stay so for ever).
        self.env['nv.sync.engine'].sync_move(self.source_move_id, triggered_by='manual')

    @api.model
    def action_retry_all_failed(self):
        """
        Re-trigger synchronisation for every log record currently in 'error' state.
        Restricted to users with the group_sync_manager group.
        """
        if not self.env.user.has_group('nv_dual_bookkeeping.group_sync_manager'):
            raise AccessError(_("Only Dual Bookkeeping Sync Managers can retry sync."))

        failed = self.search([('sync_state', '=', 'error'), ('source_move_id', '!=', False)])
        _logger.info("nv_dual_bookkeeping: Retrying %d failed sync records.", len(failed))
        for log in failed:
            log.sudo().write({'sync_state': 'pending'})
            self.env['nv.sync.engine'].sync_move(log.source_move_id, triggered_by='manual')

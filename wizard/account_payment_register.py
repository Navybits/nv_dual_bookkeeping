import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountPaymentRegister(models.TransientModel):
    """
    Extend the standard Pay wizard so that:
    - is_official defaults from the source invoice / vendor bill.
    - The user can override it before confirming (subject to group checks).
    - The flag is injected into every payment created by the wizard.
    - In the Official Books company the flag is always True and the toggle
      is hidden (mirroring the account.move / account.payment behaviour).
    """
    _inherit = 'account.payment.register'

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------

    is_official = fields.Boolean(
        string='Official',
        default=True,
        help="When True the resulting journal entry will use the Official (O) sequence. "
             "Defaults from the source invoice / vendor bill.",
    )

    is_official_set = fields.Boolean(
        string='Official Flag Explicitly Set',
        default=False,
    )

    company_is_official_company = fields.Boolean(
        string='Company Is Official',
        related='company_id.is_official_company',
        store=False,
    )

    # -------------------------------------------------------------------------
    # Default — inherit flag from source invoices
    # -------------------------------------------------------------------------

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)

        # Force True in the Official Books company — no choice needed there.
        if self.env.company.is_official_company:
            defaults['is_official'] = True
            defaults['is_official_set'] = True
            return defaults

        # Resolve source invoices from line_ids already populated by the parent
        # default_get (handles both active_model=account.move and account.move.line).
        # This is more reliable than reading active_ids directly because Odoo 19
        # sets active_ids to move LINE ids when the wizard is opened from
        # Journal Items, making a direct account.move browse return empty.
        source_moves = self.env['account.move']
        line_ids_cmd = defaults.get('line_ids')
        if line_ids_cmd and isinstance(line_ids_cmd, list) and line_ids_cmd:
            cmd = line_ids_cmd[0]
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[0] == 6 and cmd[2]:
                source_moves = self.env['account.move.line'].browse(cmd[2]).move_id

        # Fallback: active_ids when active_model is explicitly account.move
        if not source_moves and self._context.get('active_model') == 'account.move':
            source_moves = self.env['account.move'].browse(
                self._context.get('active_ids', [])
            ).exists()

        if source_moves:
            official_vals = set(source_moves.mapped('is_official'))
            # If all source documents agree, inherit that value.
            # If mixed (edge case: paying official + non-official together),
            # default to True (safer) and leave is_official_set=False so
            # the require_official_flag check will ask the user to confirm.
            if len(official_vals) == 1:
                # copied from the documents: counts as set (D7)
                defaults['is_official'] = next(iter(official_vals))
                defaults['is_official_set'] = True
            else:
                # mixed streams: Official by default, the user should confirm
                defaults['is_official'] = True
                defaults['is_official_set'] = False

        return defaults

    def onchange(self, values, field_names, fields_spec):
        """EXTENDS web: a user click on the toggle marks the flag as set (D7);
        see account.move.onchange."""
        result = super().onchange(values, field_names, fields_spec)
        if field_names and 'is_official' in field_names and 'is_official_set' in fields_spec:
            result.setdefault('value', {})['is_official_set'] = True
        return result

    # -------------------------------------------------------------------------
    # Push flag into payment vals
    # -------------------------------------------------------------------------

    def _create_payment_vals_from_wizard(self, batch_result):
        """
        Inject is_official and is_official_set into the payment created from
        the wizard's values.  account.payment then forwards both flags to the
        underlying account.move, which routes the move to the correct
        sequence stream (O or NO).
        """
        vals = super()._create_payment_vals_from_wizard(batch_result)
        vals['is_official'] = self.is_official
        vals['is_official_set'] = self.is_official_set
        return vals

    def _create_payment_vals_from_batch(self, batch_result):
        """EXTENDS account: one payment per batch. When the paid documents mix
        both streams, each batch keeps the stream of its own documents (when
        they share one); otherwise the wizard's stream applies."""
        vals = super()._create_payment_vals_from_batch(batch_result)
        vals['is_official'] = self.is_official
        vals['is_official_set'] = self.is_official_set
        mixed = len(set(self.line_ids.move_id.mapped('is_official'))) > 1
        streams = set(batch_result['lines'].move_id.mapped('is_official'))
        if mixed and len(streams) == 1 and not self.company_id.is_official_company:
            vals['is_official'] = streams.pop()
            vals['is_official_set'] = True
        return vals

    def _init_payments(self, to_process, edit_mode=False):
        """
        Safety net: nv_double_currencies overrides
        _create_payment_vals_from_wizard and, when
        enable_second_currency_for_company=True, does NOT call super(), so
        the flags of _create_payment_vals_from_wizard above are missing from
        the creation values. They are added here, before the payments are
        created, unless a batch already set them: the payment and its
        journal entry are created in the right stream, with the wizard's
        "explicitly set" mark (D7; it used to be forced to True).
        """
        if not self.env.company.is_official_company:
            for values in to_process:
                create_vals = values['create_vals']
                create_vals.setdefault('is_official', self.is_official)
                create_vals.setdefault('is_official_set', self.is_official_set)
        return super()._init_payments(to_process, edit_mode=edit_mode)

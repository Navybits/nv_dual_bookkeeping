import logging
from odoo import Command, api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

RECONCILABLE_TYPES = ('asset_receivable', 'liability_payable')


class NvSyncEngine(models.AbstractModel):
    """
    Dual Bookkeeping Sync Engine.

    Responsibilities:
    1. Mirror posted Official journal entries / invoices / payments from the
       main operating company into the Official Books company, and keep the
       mirror in step with its source's life-cycle: reset, re-post, cancel,
       delete (D2). The source points to its mirror (nv_mirror_move_id).
    2. Mirror payment–invoice reconciliations so that the official books stay
       fully reconciled automatically.

    All public entry points are NON-BLOCKING: each step runs in a savepoint,
    exceptions are caught, logged to nv.sync.log, and reported via bus
    notification — they never propagate to the user during normal posting or
    reconciliation. Only a change refused because the mirror's period is
    locked in the Official Books company raises, before anything happens.
    """

    _name = 'nv.sync.engine'
    _description = 'NV Dual Bookkeeping Sync Engine'

    # =========================================================================
    # Public entry points — move sync
    # =========================================================================

    @api.model
    def sync_move(self, source_move, triggered_by='auto'):
        """
        Create or update the mirror of a posted Official transaction.

        Uses a database SAVEPOINT (_run_step) so that any DB-level error inside
        the sync (constraint violation, duplicate key, etc.) only rolls back the
        sync work — never the caller's transaction (e.g. the payment posting that
        triggered us).  Without a savepoint a DB error would mark the whole
        PostgreSQL transaction as aborted, causing every subsequent SQL command
        in the same request to fail with InFailedSqlTransaction.
        """
        if not (
            source_move.is_official
            and not source_move.is_mirror
            and source_move.state == 'posted'
            and source_move.company_id.official_company_id
        ):
            return None
        operation = 'update' if source_move.sudo().nv_mirror_move_id else 'create'
        return self._run_step(
            source_move,
            operation,
            lambda: self._do_sync(source_move),
            triggered_by=triggered_by,
        )

    @api.model
    def check_mirror_locks(self, source_moves, action):
        """D2: refuse a change of these sources before it happens when the
        period of their posted mirror is locked in the Official Books company
        (the lock dates the current user is subject to)."""
        for source in source_moves:
            mirror = source.sudo().nv_mirror_move_id
            if mirror.state != 'posted':
                continue
            company = mirror.company_id
            violated = company._get_violated_lock_dates(
                mirror.date,
                bool(mirror.line_ids.tax_ids or mirror.line_ids.tax_line_id),
                mirror.journal_id,
            )
            if violated:
                raise UserError(_(
                    "%(document)s cannot be %(action)s: its mirror in the Official "
                    "Books company %(company)s is in a locked period (%(locks)s)."
                ) % {
                    'document': source.name,
                    'action': action,
                    'company': company.name,
                    'locks': company._format_lock_dates(violated),
                })

    @api.model
    def check_mirrors_deletable(self, source_moves):
        """D2: a draft source can be deleted with its mirror only when the
        mirror is not posted (it was reset with the source)."""
        self.check_mirror_locks(source_moves, _("deleted"))
        for source in source_moves:
            mirror = source.sudo().nv_mirror_move_id
            if mirror.state == 'posted':
                raise UserError(_(
                    "%(document)s cannot be deleted: its mirror %(mirror)s in "
                    "%(company)s is posted. Sync %(document)s again or reset the "
                    "mirror to draft first."
                ) % {
                    'document': source.name,
                    'mirror': mirror.name,
                    'company': mirror.company_id.name,
                })

    @api.model
    def reset_mirrors(self, source_moves):
        """D2: a source reset to draft resets its posted (or cancelled) mirror
        to draft, without its reconciliations; a draft mirror is kept. The
        mirror is rewritten when the source is posted again."""
        for source in source_moves:
            self._run_step(source, 'reset', lambda source=source: self._reset(source))

    @api.model
    def cancel_mirrors(self, source_moves):
        """D2: a cancelled source cancels its mirror."""
        for source in source_moves:
            self._run_step(source, 'cancel', lambda source=source: self._cancel(source))

    @api.model
    def delete_mirrors(self, deleted):
        """D2: the mirrors of deleted draft sources are deleted.

        :param deleted: list of (source name, source company, mirror), read
            before the sources were deleted.
        """
        for source_ref, source_company, mirror in deleted:
            self._run_step(
                self.env['account.move'],
                'delete',
                lambda mirror=mirror: self._delete(mirror),
                source_ref=source_ref,
                source_company=source_company,
                target=mirror,
            )

    # =========================================================================
    # Public entry points — reconciliation sync
    # =========================================================================

    @api.model
    def sync_reconcile(self, partial_reconcile):
        """
        Mirror a reconciliation from the main company to the official company.
        Called from account.partial.reconcile.create().  Non-blocking.
        """
        try:
            self._do_reconcile_sync(partial_reconcile)
        except Exception as e:
            _logger.error(
                "nv_dual_bookkeeping: Reconcile sync failed for partial id=%s: %s",
                partial_reconcile.id, str(e),
            )

    @api.model
    def sync_unreconcile(self, partial_reconcile):
        """
        Remove the mirror reconciliation when the source is unreconciled.
        Called from account.partial.reconcile.unlink().  Non-blocking.
        """
        try:
            self._do_reconcile_unsync(partial_reconcile)
        except Exception as e:
            _logger.error(
                "nv_dual_bookkeeping: Reconcile unsync failed for partial id=%s: %s",
                partial_reconcile.id, str(e),
            )

    # =========================================================================
    # Steps and logs
    # =========================================================================

    def _run_step(self, source, operation, step, triggered_by='auto',
                  source_ref=None, source_company=None, target=None):
        """Run one step in a savepoint and log it (nv.sync.log, with its
        operation). Returns the step's result (the mirror) or None when it
        failed."""
        source_ref = source_ref or source.name
        source_company = source_company or source.company_id
        try:
            with self.env.cr.savepoint():
                result = step()
        except Exception as e:
            _logger.error(
                "nv_dual_bookkeeping: %s of %s failed: %s",
                operation, source_ref, str(e),
            )
            self._log(
                source, operation, 'error', triggered_by,
                target=target, error=str(e),
                source_ref=source_ref, source_company=source_company,
            )
            self._notify_sync_failure(source_ref, str(e))
            return None
        self._log(
            source, operation, 'synced', triggered_by,
            target=result if isinstance(result, models.BaseModel) else target,
            source_ref=source_ref, source_company=source_company,
        )
        return result

    def _log(self, source, operation, state, triggered_by, target=None,
             error=None, source_ref=None, source_company=None):
        """Write the log in a fresh savepoint so the log write itself cannot be
        affected by the aborted state of a previous savepoint."""
        source_company = source_company or source.company_id
        try:
            with self.env.cr.savepoint():
                self.env['nv.sync.log'].sudo().create({
                    'source_move_id': source.id,
                    'source_ref': source_ref or source.name,
                    'target_move_id': target.exists().id if target else False,
                    'source_company_id': source_company.id,
                    'target_company_id': source_company.official_company_id.id or False,
                    'sync_date': fields.Datetime.now(),
                    'operation': operation,
                    'sync_state': state,
                    'error_message': error or False,
                    'triggered_by': triggered_by,
                })
        except Exception:
            _logger.exception("nv_dual_bookkeeping: could not write the sync log")

    # =========================================================================
    # Core move-sync logic
    # =========================================================================

    def _do_sync(self, source_move):
        """Core sync — separated from the savepoint wrapper for clean tracebacks.

        D2: the source's mirror link replaces the "synced" log search: an
        up-to-date mirror is left as it is (a manual retry), a mirror marked
        outdated (the source was reset to draft) is rewritten in place,
        otherwise a mirror is created."""
        official_co = source_move.company_id.official_company_id

        # ------------------------------------------------------------------
        # Validate journal mapping
        # ------------------------------------------------------------------
        # Use sudo() so the multi-company record rule does not block reading
        # the official company's journal when the current user only has the
        # main company in their allowed_company_ids context.
        official_journal = source_move.journal_id.sudo().official_journal_id
        if not official_journal:
            raise UserError(_(
                "Journal '%(journal)s' has no Official Company Journal mapped.\n\n"
                "Please use Accounting > Dual Bookkeeping > Link Official Journals "
                "to link each journal in '%(company)s' to its counterpart in '%(official)s'."
            ) % {
                'journal': source_move.journal_id.name,
                'company': source_move.company_id.name,
                'official': official_co.name,
            })

        source_sudo = source_move.sudo()
        mirror_move = source_sudo.nv_mirror_move_id
        if mirror_move and not source_sudo.nv_mirror_outdated:
            # up to date (a manual retry): only its reconciliations follow
            self._sync_existing_reconciliations(source_move, mirror_move, official_co)
            return mirror_move

        # ------------------------------------------------------------------
        # Create or rewrite the mirror — payment or plain journal entry
        # ------------------------------------------------------------------
        if mirror_move and mirror_move.origin_payment_id:
            mirror_move = self._update_mirror_payment(
                source_move, mirror_move, official_co, official_journal)
        elif mirror_move:
            mirror_move = self._update_mirror_entry(
                source_move, mirror_move, official_co, official_journal)
        elif source_move.origin_payment_id:
            mirror_move = self._create_mirror_payment(source_move, official_co, official_journal)
        else:
            mirror_move = self._create_mirror_entry(source_move, official_co, official_journal)
        source_sudo.write({'nv_mirror_move_id': mirror_move.id, 'nv_mirror_outdated': False})
        self._post_mirror(mirror_move)

        # ------------------------------------------------------------------
        # After syncing, check for existing reconciliations to mirror
        # ------------------------------------------------------------------
        self._sync_existing_reconciliations(source_move, mirror_move, official_co)

        return mirror_move

    def _mirror_env(self, official_co):
        """The environment the mirrors are written in: sudo (the user may have
        no access to the official company), the official company."""
        return self.env(
            su=True,
            context=dict(self.env.context, allowed_company_ids=official_co.ids),
        )

    def _post_mirror(self, mirror_move):
        """Payment mirrors are always posted; other mirrors when "Auto-post
        mirrored transactions" is on (off by default: drafts for review)."""
        if mirror_move.state != 'draft':
            return
        if mirror_move.origin_payment_id:
            mirror_move.origin_payment_id.action_post()
            return
        auto_post = self.env['ir.config_parameter'].sudo().get_param(
            'nv_dual_bookkeeping.auto_post_mirror', 'False'
        ).strip().lower() in ('true', '1')
        if auto_post:
            mirror_move.action_post()
            _logger.info("nv_dual_bookkeeping: Mirror entry %s auto-posted.", mirror_move.name)

    # =========================================================================
    # Mirror creation / rewrite — plain entry / invoice
    # =========================================================================

    def _mirror_move_vals(self, source_move, official_co):
        """
        Header and lines of the mirror of an invoice or an entry, shared by its
        creation and its rewrite (D2).

        Strategy differs by move_type because Odoo 19's invoice machinery
        hard-sets balance=0 on invoice lines via _compute_balance (precompute=True).
        Any attempt to write raw debit/credit on invoice-type lines is overridden
        by that precompute, causing "The entry is not balanced."

        INVOICE / BILL / CREDIT NOTE / REFUND (move_type != 'entry'):
          invoice_line_ids containing price+quantity+remapped taxes.
          Odoo computes tax lines and the receivable/payable line from scratch —
          identical to what happens when a user creates a draft invoice. Amounts
          match the source as long as tax rates are the same in both companies
          (which they must be for legal mirroring, D6).

        PLAIN JOURNAL ENTRY (move_type == 'entry'):
          all lines with explicit balance + skip_invoice_sync=True.
          _compute_balance does NOT reset balance to 0 for non-invoice moves, so
          the raw values persist and the entry is balanced.
        """
        mirror_partner = self._map_partner(source_move.partner_id, official_co)
        vals = {
            'date': source_move.date,
            'partner_id': mirror_partner.id if mirror_partner else False,
            'currency_id': source_move.currency_id.id,
            'ref': source_move.ref or False,
            'narration': source_move.narration or False,
            'is_mirror': True,
            'is_official': True,
            'is_official_set': True,
            'source_move_ref': source_move.name,
            # ── nv_double_currencies move fields ─────────────────────
            # rate_main_second: drives second_currency_amount on all
            # invoice lines via _sync_invoice; must match source so the
            # totals are identical and the second_currency_total==0 check
            # in nv_double_currencies._post() passes.
            'rate_main_second': source_move.rate_main_second,
            'set_manual_vat_rate': source_move.set_manual_vat_rate,
            'manual_rate_vat': source_move.manual_rate_vat,
        }
        if source_move.move_type != 'entry':
            # ── Invoice / bill / credit note / refund ────────────────────────
            invoice_line_vals = []
            for line in source_move.invoice_line_ids:
                if line.display_type in ('line_section', 'line_note'):
                    invoice_line_vals.append(Command.create({
                        'display_type': line.display_type,
                        'name': line.name or '',
                        'sequence': line.sequence,
                    }))
                    continue
                mirror_account = self._get_mirror_account(line.account_id, official_co)
                mirror_taxes = self._get_mirror_taxes(line.tax_ids, official_co)
                mp = self._map_partner(line.partner_id, official_co) if line.partner_id else None
                invoice_line_vals.append(Command.create({
                    'account_id': mirror_account.id,
                    'name': line.name or '',
                    'quantity': line.quantity,
                    'product_uom_id': line.product_uom_id.id,
                    'price_unit': line.price_unit,
                    'discount': line.discount,
                    'tax_ids': [Command.set(mirror_taxes.ids)],
                    'partner_id': mp.id if mp else False,
                    'display_type': line.display_type,
                    'sequence': line.sequence,
                }))
            vals.update({
                'invoice_date': source_move.invoice_date or source_move.date,
                'invoice_date_due': source_move.invoice_date_due,
                'fiscal_position_id': False,  # avoid tax remapping by fiscal position
                'payment_reference': source_move.payment_reference,
                'invoice_line_ids': invoice_line_vals,
            })
        else:
            # ── Plain journal entry ──────────────────────────────────────────
            line_vals = []
            for line in source_move.line_ids:
                mirror_account = self._get_mirror_account(line.account_id, official_co)
                mp = self._map_partner(line.partner_id, official_co) if line.partner_id else None
                line_vals.append(Command.create({
                    'account_id': mirror_account.id,
                    'name': line.name or '',
                    'balance': line.balance,
                    'amount_currency': line.amount_currency,
                    'currency_id': line.currency_id.id,
                    'partner_id': mp.id if mp else False,
                    'display_type': line.display_type,
                    # ── nv_double_currencies line fields ─────────────────────
                    # second_currency_amount / rate_main_second_line: copied
                    # directly from source so second_currency_total sums to 0
                    # (passing nv_double_currencies._post() balance check) and
                    # amount_currency == second_currency_amount for second-
                    # currency lines (passing the discrepancy check).
                    'second_currency_amount': line.second_currency_amount,
                    'rate_main_second_line': line.rate_main_second_line,
                    'receivable_payable_line': line.receivable_payable_line,
                }))
            vals['line_ids'] = line_vals
        return vals

    def _create_mirror_entry(self, source_move, official_co, official_journal):
        """Create a mirror account.move in the official company (draft; posted
        by _post_mirror)."""
        vals = self._mirror_move_vals(source_move, official_co)
        vals.update({
            'move_type': source_move.move_type,
            'journal_id': official_journal.id,
            'company_id': official_co.id,
        })
        mirror_move = (
            self._mirror_env(official_co)['account.move']
            .with_context(skip_invoice_sync=source_move.move_type == 'entry')
            .create(vals)
        )
        _logger.info(
            "nv_dual_bookkeeping: Mirror %s created in '%s' for source %s.",
            source_move.move_type, official_co.name, source_move.name,
        )
        return mirror_move

    def _update_mirror_entry(self, source_move, mirror_move, official_co, official_journal):
        """D2: the mirror of a re-posted source is rewritten in place: its lines
        are replaced, its header and rates updated, its number kept (or set to
        the source's new number)."""
        mirror_move = mirror_move.with_env(self._mirror_env(official_co))
        self._reset_mirror_move(mirror_move)
        vals = self._mirror_move_vals(source_move, official_co)
        line_field = 'invoice_line_ids' if 'invoice_line_ids' in vals else 'line_ids'
        vals[line_field] = [
            Command.delete(line.id) for line in mirror_move[line_field]
        ] + vals[line_field]
        if mirror_move.name not in (source_move.name, '/', False):
            vals['name'] = source_move.name
        is_invoice = source_move.move_type != 'entry'
        mirror_move.with_context(
            skip_invoice_sync=not is_invoice, check_move_validity=False
        ).write(vals)
        _logger.info(
            "nv_dual_bookkeeping: Mirror of %s rewritten in '%s'.",
            source_move.name, official_co.name,
        )
        return mirror_move

    # =========================================================================
    # Mirror creation / rewrite — payment
    # =========================================================================

    def _mirror_payment_vals(self, source_move, official_co, official_journal):
        """
        Values of a mirror account.payment in the official company, shared by
        its creation and its rewrite (D2).

        create() is used instead of copy() to avoid cross-company account leakage:
        copy() carries over stored computed account fields (destination_account_id,
        outstanding_account_id, etc.) from the source payment before Odoo's computes
        can rerun for the new company, triggering Odoo's check_company constraint.
        With create() all account fields are computed fresh in the official company
        context from the journal and partner — no cross-company contamination.

        is_mirror and source_move_ref are set on the payment itself; our
        account.payment.action_post() override forwards them to move_id in a
        single write before calling super(), so _set_next_sequence() sees
        is_mirror=True and reuses the source name without consuming a new slot.
        """
        source_payment = source_move.origin_payment_id
        mirror_partner = self._map_partner(source_payment.partner_id, official_co)

        # payment_method_line_id must be supplied explicitly — Odoo does not
        # auto-select it on create() when journal_id is set programmatically.
        official_journal_sudo = official_journal.sudo().with_company(official_co)
        if source_payment.payment_type == 'inbound':
            method_line = official_journal_sudo.inbound_payment_method_line_ids[:1]
        else:
            method_line = official_journal_sudo.outbound_payment_method_line_ids[:1]

        vals = {
            'payment_type': source_payment.payment_type,
            'partner_type': source_payment.partner_type,
            'partner_id': mirror_partner.id if mirror_partner else False,
            'amount': source_payment.amount,
            'currency_id': source_payment.currency_id.id,
            'date': source_payment.date,
            'memo': source_payment.memo or False,
            'is_official': True,
            'is_official_set': True,
            'is_mirror': True,
            'source_move_ref': source_move.name,
            # ── nv_double_currencies fields ───────────────────────────────────
            # rate_main_second / source_currency_id: control currency conversion
            # and which _prepare_move_line_default_vals branch runs (direct vs
            # standard). Must match the source so amounts are identical.
            'rate_main_second': source_payment.rate_main_second,
            'source_currency_id': source_payment.source_currency_id.id if source_payment.source_currency_id else False,
            # partner_amount / partner_currency_id: used by
            # _prepare_move_line_default_vals_direct() to build the counterpart
            # line amount.  Without these, the line balance would be 0.
            'partner_amount': source_payment.partner_amount,
            'partner_currency_id': source_payment.partner_currency_id.id if source_payment.partner_currency_id else False,
            # rate_main_transaction: fallback rate for non-standard currencies
            'rate_main_transaction': source_payment.rate_main_transaction,
            # is_vat_payment / invoice_rate: affect rate selection in get_vat_rate()
            'is_vat_payment': source_payment.is_vat_payment,
            'invoice_rate': source_payment.invoice_rate,
            # bank_fees_amount intentionally NOT synced: bank charges are
            # incurred at the source company's bank — not in the official books.
        }
        if method_line:
            vals['payment_method_line_id'] = method_line.id

        # destination_account_id: always resolve explicitly from the source so
        # that manual payments (no partner) get a valid account. Odoo can only
        # auto-compute this from partner.property_account_receivable/payable_id,
        # which is null when there is no partner — leaving account_id=null on the
        # counterpart line and triggering the check_accountable_required_fields
        # constraint. With create() (unlike copy()) the account belongs to the
        # official company, so no cross-company constraint is raised.
        #
        # Also set receivable_payable_account_id (nv_double_currencies field):
        # when enable_second_currency_for_company=True and source_currency_id is
        # empty, _prepare_move_line_default_vals_direct() uses
        # receivable_payable_account_id (not destination_account_id) for the
        # counterpart line. Both must be set regardless of which path runs.
        if source_payment.destination_account_id:
            official_dest = self._get_mirror_account(
                source_payment.destination_account_id, official_co
            )
            vals['destination_account_id'] = official_dest.id
            vals['receivable_payable_account_id'] = official_dest.id

        # difference_account_id (nv_double_currencies): exchange diff account.
        # Map it if set on the source; if not, the code falls back to the
        # company's loss/gain exchange difference accounts automatically.
        if source_payment.difference_account_id:
            try:
                official_diff = self._get_mirror_account(
                    source_payment.difference_account_id, official_co
                )
                vals['difference_account_id'] = official_diff.id
            except Exception:
                pass  # fallback to company defaults is safe
        return vals

    def _create_mirror_payment(self, source_move, official_co, official_journal):
        """Create and post a mirror account.payment in the official company
        (its journal entry is created when it is posted)."""
        vals = self._mirror_payment_vals(source_move, official_co, official_journal)
        vals.update({'journal_id': official_journal.id, 'company_id': official_co.id})
        mirror_payment = self._mirror_env(official_co)['account.payment'].create(vals)
        mirror_payment.action_post()

        _logger.info(
            "nv_dual_bookkeeping: Mirror payment %s created and posted in '%s' for source %s.",
            mirror_payment.move_id.name, official_co.name, source_move.name,
        )
        return mirror_payment.move_id

    def _update_mirror_payment(self, source_move, mirror_move, official_co, official_journal):
        """D2: the mirror payment of a re-posted payment is reset (without its
        reconciliations), rewritten (amount, date, partner, rates, accounts)
        and posted again by _post_mirror."""
        mirror_payment = mirror_move.origin_payment_id.with_env(self._mirror_env(official_co))
        if mirror_payment.state != 'draft':
            self._remove_mirror_reconciliations(mirror_payment.move_id)
            mirror_payment.action_draft()
        mirror_payment.write(self._mirror_payment_vals(source_move, official_co, official_journal))
        return mirror_payment.move_id

    # =========================================================================
    # Life-cycle (D2)
    # =========================================================================

    def _reset(self, source_move):
        source_move.sudo().nv_mirror_outdated = True
        mirror_move = source_move.sudo().nv_mirror_move_id
        mirror_move = mirror_move.with_env(self._mirror_env(mirror_move.company_id))
        if mirror_move.origin_payment_id and mirror_move.state != 'draft':
            self._remove_mirror_reconciliations(mirror_move)
            mirror_move.origin_payment_id.action_draft()
        else:
            self._reset_mirror_move(mirror_move)
        return mirror_move

    def _reset_mirror_move(self, mirror_move):
        if mirror_move.state == 'posted':
            self._remove_mirror_reconciliations(mirror_move)
        if mirror_move.state in ('posted', 'cancel'):
            mirror_move.button_draft()

    def _cancel(self, source_move):
        mirror_move = source_move.sudo().nv_mirror_move_id
        mirror_move = mirror_move.with_env(self._mirror_env(mirror_move.company_id))
        if mirror_move.state != 'cancel':
            mirror_move.button_cancel()
        return mirror_move

    def _delete(self, mirror_move):
        mirror_move = mirror_move.with_env(self._mirror_env(mirror_move.company_id))
        if not mirror_move.exists():
            return
        (mirror_move.origin_payment_id or mirror_move).unlink()

    def _remove_mirror_reconciliations(self, mirror_move):
        """Before a mirror is reset or rewritten: its reconciliations go (they
        are redone from the source's after it is posted again). Odoo 19's
        button_draft keeps the reconciliations of an entry, so they are
        removed here."""
        mirror_move.line_ids.remove_move_reconcile()

    # =========================================================================
    # Reconciliation sync
    # =========================================================================

    def _do_reconcile_sync(self, partial_reconcile):
        """
        When a reconciliation is created in the main company between two Official
        non-mirror lines, find the corresponding mirror lines in the official
        company and reconcile them there too.
        """
        debit_line = partial_reconcile.debit_move_id
        credit_line = partial_reconcile.credit_move_id
        debit_move = debit_line.move_id
        credit_move = credit_line.move_id

        # Only sync Official, non-mirror reconciliations
        if not (debit_move.is_official and credit_move.is_official):
            return
        # Guard against infinite loops — mirrors reconciling mirrors
        if debit_move.is_mirror or credit_move.is_mirror:
            return

        # Both moves must target the same official company
        official_co = debit_move.company_id.official_company_id
        if not official_co:
            return
        if credit_move.company_id.official_company_id != official_co:
            return

        mirror_debit_move = self._find_mirror_move(debit_move, official_co)
        mirror_credit_move = self._find_mirror_move(credit_move, official_co)

        if not mirror_debit_move or not mirror_credit_move:
            _logger.debug(
                "nv_dual_bookkeeping: Mirror move(s) not found for reconcile sync "
                "(debit_move=%s, credit_move=%s). Skipping.",
                debit_move.name, credit_move.name,
            )
            return

        self._reconcile_mirror_lines(mirror_debit_move, mirror_credit_move, official_co)

    def _do_reconcile_unsync(self, partial_reconcile):
        """
        When a reconciliation is removed in the main company, find and remove
        the corresponding mirror reconciliation in the official company.
        """
        debit_line = partial_reconcile.debit_move_id
        credit_line = partial_reconcile.credit_move_id
        debit_move = debit_line.move_id
        credit_move = credit_line.move_id

        if not (debit_move.is_official and credit_move.is_official):
            return
        if debit_move.is_mirror or credit_move.is_mirror:
            return

        official_co = debit_move.company_id.official_company_id
        if not official_co:
            return

        mirror_debit_move = self._find_mirror_move(debit_move, official_co)
        mirror_credit_move = self._find_mirror_move(credit_move, official_co)

        if not mirror_debit_move or not mirror_credit_move:
            return

        # Find the partial reconcile that links the two mirror lines
        mirror_partial = self.env['account.partial.reconcile'].sudo().search([
            ('debit_move_id.move_id', '=', mirror_debit_move.id),
            ('credit_move_id.move_id', '=', mirror_credit_move.id),
        ], limit=1)

        if mirror_partial:
            mirror_partial.sudo().unlink()
            _logger.info(
                "nv_dual_bookkeeping: Mirror reconcile removed in '%s' "
                "(debit_move=%s, credit_move=%s).",
                official_co.name, mirror_debit_move.name, mirror_credit_move.name,
            )

    def _sync_existing_reconciliations(self, source_move, mirror_move, official_co):
        """
        After a move is synced, check whether it is already reconciled with
        other Official moves that also have mirrors.  If so, reconcile the
        mirrors in the official company so the reconciliation state stays in sync.

        This handles the case where the invoice was reconciled before the
        payment was synced (or vice-versa), and the reconciliations removed from
        a mirror that was reset and rewritten (D2).
        """
        if mirror_move.state != 'posted':
            return
        for line in source_move.line_ids:
            for partial in (line.matched_debit_ids | line.matched_credit_ids):
                other_line = (
                    partial.debit_move_id
                    if partial.credit_move_id == line
                    else partial.credit_move_id
                )
                other_move = other_line.move_id

                if not other_move.is_official or other_move.is_mirror:
                    continue

                mirror_other = self._find_mirror_move(other_move, official_co)
                if not mirror_other:
                    continue

                # Determine which mirror is debit and which is credit
                if line == partial.debit_move_id:
                    self._reconcile_mirror_lines(mirror_move, mirror_other, official_co)
                else:
                    self._reconcile_mirror_lines(mirror_other, mirror_move, official_co)

    def _reconcile_mirror_lines(self, mirror_debit_move, mirror_credit_move, official_co):
        """
        Find the outstanding receivable/payable lines in both mirror moves and
        reconcile them in the official company context.

        Non-blocking: logs a warning and returns if lines cannot be found or
        if they are already fully reconciled.
        """
        debit_line = self._get_reconcilable_line(mirror_debit_move, want_debit=True)
        credit_line = self._get_reconcilable_line(mirror_credit_move, want_debit=False)

        if not debit_line or not credit_line:
            _logger.warning(
                "nv_dual_bookkeeping: Could not find reconcilable lines for mirrors "
                "%s / %s. Skipping reconciliation sync.",
                mirror_debit_move.name, mirror_credit_move.name,
            )
            return

        if debit_line.reconciled or credit_line.reconciled:
            _logger.debug(
                "nv_dual_bookkeeping: Mirror lines already reconciled (%s / %s). Skipping.",
                mirror_debit_move.name, mirror_credit_move.name,
            )
            return

        try:
            (debit_line | credit_line).sudo().with_company(official_co).reconcile()
            _logger.info(
                "nv_dual_bookkeeping: Mirror reconciliation created in '%s' "
                "between %s and %s.",
                official_co.name, mirror_debit_move.name, mirror_credit_move.name,
            )
        except Exception as e:
            _logger.warning(
                "nv_dual_bookkeeping: Could not reconcile mirror lines (%s / %s): %s",
                mirror_debit_move.name, mirror_credit_move.name, str(e),
            )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _find_mirror_move(self, source_move, official_co):
        """Return the posted mirror of source_move (its mirror link), or empty."""
        mirror_move = source_move.sudo().nv_mirror_move_id
        if mirror_move.state == 'posted' and mirror_move.company_id == official_co:
            return mirror_move
        return self.env['account.move']

    def _get_reconcilable_line(self, move, want_debit=True):
        """
        Return the first unreconciled receivable/payable line in move.
        Prefer the debit side when want_debit=True, credit side otherwise.
        Falls back to whichever side is available.
        """
        candidates = move.line_ids.filtered(
            lambda l: l.account_id.account_type in RECONCILABLE_TYPES and not l.reconciled
        )
        if not candidates:
            return self.env['account.move.line']
        preferred = candidates.filtered(lambda l: (l.debit > 0) == want_debit)
        return (preferred or candidates)[:1]

    # =========================================================================
    # Account and tax mapping
    # =========================================================================

    def _get_mirror_taxes(self, taxes, target_company):
        """
        D6: the taxes of target_company matching these, each by name and type
        (sale / purchase), else by amount, amount type and type when exactly
        one tax fits. A tax without a single match fails the sync with a
        UserError naming it (it used to be dropped silently: the official
        books then showed another VAT); sync_move logs the error and Retry
        syncs again once the tax exists.
        """
        Tax = self.env['account.tax'].sudo()
        mirror_taxes = Tax
        for tax in taxes.sudo():
            same_type = [
                ('company_id', '=', target_company.id),
                ('type_tax_use', '=', tax.type_tax_use),
            ]
            same_amount = same_type + [
                ('amount_type', '=', tax.amount_type),
                ('amount', '=', tax.amount),
            ]
            matches = Tax.search(same_type + [('name', '=', tax.name)])
            if len(matches) > 1:
                matches = matches.filtered_domain(same_amount)
            if not matches:
                matches = Tax.search(same_amount)
            if len(matches) != 1:
                type_labels = dict(tax._fields['type_tax_use']._description_selection(self.env))
                raise UserError(_(
                    "Tax '%(tax)s' (%(type)s, %(amount)s) has %(problem)s in the "
                    "Official Books company '%(company)s'.\n\n"
                    "Create (or rename) the tax there with the same name and type, "
                    "then sync again."
                ) % {
                    'tax': tax.name,
                    'type': type_labels.get(tax.type_tax_use, tax.type_tax_use),
                    'amount': tax.amount,
                    'problem': _("several matches") if matches else _("no match"),
                    'company': target_company.name,
                })
            mirror_taxes |= matches
        return mirror_taxes

    def _get_mirror_account(self, account, target_company):
        """
        Find the corresponding account in target_company by matching account code.
        with_company() is required in Odoo 18 for correct company scoping.
        """
        if not account:
            raise UserError(_("A move line has no account set. Cannot create mirror entry."))

        mirror_account = (
            self.env['account.account']
            .sudo()
            .with_company(target_company)
            .search([('code', '=', account.code)], limit=1)
        )

        if not mirror_account:
            raise UserError(_(
                "Account '%(code)s - %(name)s' was not found in the official company "
                "'%(company)s'.\n\nPlease ensure the Chart of Accounts is synchronised "
                "between '%(source)s' and '%(company)s' before syncing transactions."
            ) % {
                'code': account.code,
                'name': account.name,
                'company': target_company.name,
                'source': account.company_ids[:1].name if account.company_ids else '?',
            })

        return mirror_account

    # =========================================================================
    # Partner mapping
    # =========================================================================

    def _map_partner(self, partner, target_company):
        """
        Find or create a partner in target_company context.
        Search order: VAT → name → create stub.
        """
        if not partner:
            return self.env['res.partner']

        if partner.vat:
            match = self.env['res.partner'].sudo().search([
                ('vat', '=', partner.vat),
                ('active', 'in', [True, False]),
            ], limit=1)
            if match:
                return match

        match = self.env['res.partner'].sudo().search([
            ('name', '=ilike', partner.name),
            ('active', 'in', [True, False]),
        ], limit=1)
        if match:
            return match

        _logger.warning(
            "nv_dual_bookkeeping: Partner '%s' (id=%s) not found in company '%s'. "
            "Creating a mirror stub partner.",
            partner.name, partner.id, target_company.name,
        )
        return self.env['res.partner'].sudo().with_company(target_company).create({
            'name': partner.name,
            'vat': partner.vat,
            'email': partner.email,
            'phone': partner.phone,
            'street': partner.street,
            'city': partner.city,
            'country_id': partner.country_id.id if partner.country_id else False,
            'comment': _(
                "Auto-created by NV Dual Bookkeeping sync engine as a mirror of "
                "partner id=%d from company '%s'."
            ) % (partner.id, partner.company_id.name if partner.company_id else '?'),
        })

    # =========================================================================
    # Bus notification
    # =========================================================================

    def _notify_sync_failure(self, source_ref, error_message):
        """Non-blocking bus notification so the user sees a warning without a modal."""
        try:
            message = _(
                "Transaction '%s' posted successfully, but synchronisation to the "
                "official company failed. Please check the Sync Dashboard."
            ) % (source_ref or '?')

            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'title': _('Dual Bookkeeping Sync Warning'),
                    'message': message,
                    'warning': True,
                    'sticky': True,
                },
            )
        except Exception as notify_exc:
            _logger.debug(
                "nv_dual_bookkeeping: Could not send bus notification: %s", str(notify_exc),
            )

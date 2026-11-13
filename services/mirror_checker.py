"""Mirror checker: the differences between a document and its mirror in the
Official Books company. Read-only.

What is expected is decided by simple rules of its own, NOT by the sync
engine's mapping (which would repeat its mistakes, e.g. the former VAT
search returning another contact):
- a record shared between the companies (no company) or of the official
  company must be the SAME record (partner, analytic account, payment
  term, bank account);
- a company-specific one must have the same name (analytic: same plan and
  code, else name; partner: name and VAT);
- an account must have the same code; a tax the same name and amount;
- amounts are equal to the half-cent.
Intended differences are ignored: company, ids, the fiscal position (none
on purpose), the mirror flag.
"""

import datetime

from odoo import _, api, models
from odoo.tools import float_compare

AMOUNT_PRECISION = 2  # half a cent
RATE_PRECISION = 6


class NvMirrorChecker(models.AbstractModel):
    _name = 'nv.mirror.checker'
    _description = 'Dual Bookkeeping: compare documents with their mirror'

    # =========================================================================
    # Entry point
    # =========================================================================

    @api.model
    def compare(self, source):
        """[{'group', 'label', 'source', 'mirror'}] for `source` (values as
        text); empty when the mirror matches."""
        source = source.sudo()
        diffs = []
        mirror = source.nv_mirror_move_id
        if not mirror:
            if source.state == 'posted' and source.is_official and not source.is_mirror:
                diffs.append(self._diff('link', _("Mirror"), source.name, _("missing"), key='missing'))
            return diffs
        official_co = mirror.company_id
        self._compare_link(source, mirror, diffs)
        if source.state != 'posted':
            # a draft source is rewritten into its mirror when posted again,
            # a cancelled one only needs a cancelled mirror
            return diffs
        self._compare_header(source, mirror, official_co, diffs)
        if source.origin_payment_id or mirror.origin_payment_id:
            self._compare_payment(source.origin_payment_id, mirror.origin_payment_id, official_co, diffs)
        if source.move_type == 'entry' and not source.origin_payment_id:
            self._compare_lines(source.line_ids, mirror.line_ids, official_co, diffs, entry=True)
        else:
            if source.is_invoice(include_receipts=True):
                self._compare_lines(source.invoice_line_ids, mirror.invoice_line_ids, official_co, diffs)
                self._compare_totals(source, mirror, diffs)
            self._compare_balances(source, mirror, official_co, diffs)
        if mirror.state == 'posted':
            self._compare_reconciliation(source, mirror, diffs)
        return diffs

    def _diff(self, group, label, source_value, mirror_value, key=None):
        return {
            'group': group,
            'key': key,
            'label': label,
            'source': self._text(source_value),
            'mirror': self._text(mirror_value),
        }

    # =========================================================================
    # Groups
    # =========================================================================

    def _compare_link(self, source, mirror, diffs):
        if source.nv_mirror_outdated and source.state == 'posted':
            diffs.append(self._diff(
                'link', _("Mirror up to date"), _("changed since the sync"), _("not rewritten yet"),
                key='outdated'))
        expected = self._expected_mirror_states(source)
        if mirror.state not in expected:
            diffs.append(self._diff(
                'link', _("Status"), self._selection(source, 'state'), self._selection(mirror, 'state'),
                key='status'))

    def _expected_mirror_states(self, source):
        if source.state == 'cancel':
            return ('cancel',)
        if source.state == 'draft':
            return ('draft',)
        auto_post = self.env['ir.config_parameter'].sudo().get_param(
            'nv_dual_bookkeeping.auto_post_mirror', 'False').strip().lower() in ('true', '1')
        if source.origin_payment_id or auto_post:
            return ('posted',)
        return ('draft', 'posted')  # drafts for review, posted by hand

    def _compare_header(self, source, mirror, official_co, diffs):
        add = lambda label, a, b, key=None: diffs.append(self._diff('header', label, a, b, key))  # noqa: E731
        if mirror.name != source.name and mirror.state == 'posted':
            add(_("Number"), source.name, mirror.name)
        if mirror.move_type != source.move_type:
            add(_("Type"), self._selection(source, 'move_type'), self._selection(mirror, 'move_type'))
        expected_journal = source.journal_id.official_journal_id
        if mirror.journal_id != expected_journal:
            add(_("Journal"), expected_journal or source.journal_id, mirror.journal_id)
        for name in ('date', 'ref', 'currency_id'):
            if not self._same_value(source[name], mirror[name]):
                add(source._fields[name].get_description(self.env)['string'], source[name], mirror[name], name)
        if not self._same_partner(source.partner_id, mirror.partner_id, official_co):
            add(_("Partner"), source.partner_id, mirror.partner_id, 'partner')
        for name in ('rate_main_second', 'set_manual_vat_rate', 'manual_rate_vat'):
            if name in source._fields and not self._same_value(source[name], mirror[name], RATE_PRECISION):
                add(source._fields[name].get_description(self.env)['string'], source[name], mirror[name])
        if source.is_invoice(include_receipts=True):
            for name in ('invoice_date', 'invoice_date_due', 'payment_reference'):
                if not self._same_value(source[name], mirror[name]):
                    add(source._fields[name].get_description(self.env)['string'], source[name], mirror[name], name)
            if not self._same_by_name(source.invoice_payment_term_id, mirror.invoice_payment_term_id, official_co):
                add(_("Payment Terms"), source.invoice_payment_term_id, mirror.invoice_payment_term_id,
                    'payment_term')
            source_due, mirror_due = self._due_lines(source), self._due_lines(mirror)
            if source_due != mirror_due:
                add(_("Due Lines"), self._due_text(source_due), self._due_text(mirror_due), 'due_lines')
            # a draft mirror (auto-post off) cannot be reconciled yet
            if mirror.state == 'posted' and not self._same_value(source.payment_state, mirror.payment_state):
                add(_("Payment Status"), self._selection(source, 'payment_state'),
                    self._selection(mirror, 'payment_state'), 'payment_state')

    def _compare_payment(self, payment, mirror_payment, official_co, diffs):
        add = lambda label, a, b, key=None: diffs.append(self._diff('payment', label, a, b, key))  # noqa: E731
        if not payment or not mirror_payment:
            add(_("Payment"), payment.name if payment else _("none"),
                mirror_payment.name if mirror_payment else _("none"))
            return
        for name in ('amount', 'payment_type', 'partner_type', 'date', 'memo', 'currency_id',
                     'is_advance_payment', 'set_manual_vat_rate', 'manual_rate_vat', 'partner_amount'):
            if name not in payment._fields:
                continue
            if not self._same_value(payment[name], mirror_payment[name], RATE_PRECISION if 'rate' in name else AMOUNT_PRECISION):
                add(payment._fields[name].get_description(self.env)['string'], payment[name], mirror_payment[name], name)
        if not self._same_partner(payment.partner_id, mirror_payment.partner_id, official_co):
            add(_("Partner"), payment.partner_id, mirror_payment.partner_id, 'partner')
        if mirror_payment.journal_id != payment.journal_id.official_journal_id:
            add(_("Journal"), payment.journal_id.official_journal_id or payment.journal_id, mirror_payment.journal_id)
        if payment.payment_method_line_id.name != mirror_payment.payment_method_line_id.name:
            add(_("Payment Method"), payment.payment_method_line_id.name, mirror_payment.payment_method_line_id.name)
        for name in ('receivable_payable_account_id', 'destination_account_id'):
            if name not in payment._fields:
                continue
            if name == 'receivable_payable_account_id' and not payment[name]:
                # empty on payments from Register Payment (nv_double_currencies
                # fills it from a form onchange only); the account actually
                # used is destination_account_id, compared below
                continue
            if not self._same_account(payment[name], mirror_payment[name]):
                add(payment._fields[name].get_description(self.env)['string'],
                    self._account_label(payment[name]), self._account_label(mirror_payment[name]))
        if (payment.partner_bank_id.sanitized_acc_number or False) != (mirror_payment.partner_bank_id.sanitized_acc_number or False):
            add(_("Bank Account"), payment.partner_bank_id, mirror_payment.partner_bank_id)
        if 'analytic_distribution' in payment._fields and not self._same_analytic(
                payment.analytic_distribution, mirror_payment.analytic_distribution):
            add(_("Analytic"), self._analytic_text(payment.analytic_distribution),
                self._analytic_text(mirror_payment.analytic_distribution))

    def _compare_lines(self, lines, mirror_lines, official_co, diffs, entry=False):
        lines, mirror_lines = lines.sorted('id'), mirror_lines.sorted('id')
        if len(lines) != len(mirror_lines):
            diffs.append(self._diff('lines', _("Number of lines"), len(lines), len(mirror_lines)))
            return
        for index, (line, mirror_line) in enumerate(zip(lines, mirror_lines), start=1):
            add = lambda label, a, b, key=None, i=index, l=line: diffs.append(  # noqa: E731
                self._diff('lines', _("Line %(n)s (%(label)s): %(field)s",
                                      n=i, label=(l.name or '')[:40], field=label), a, b, key))
            if line.display_type != mirror_line.display_type:
                add(_("Type"), line.display_type, mirror_line.display_type)
                continue
            if line.display_type in ('line_section', 'line_subsection', 'line_note'):
                if (line.name or '') != (mirror_line.name or ''):
                    add(_("Label"), line.name, mirror_line.name)
                continue
            if not self._same_account(line.account_id, mirror_line.account_id):
                add(_("Account"), self._account_label(line.account_id), self._account_label(mirror_line.account_id))
            if (line.name or '') != (mirror_line.name or ''):
                add(_("Label"), line.name, mirror_line.name)
            names = ['balance', 'amount_currency', 'currency_id', 'second_currency_amount']
            if entry:
                names += ['date_maturity']
            else:
                names += ['quantity', 'price_unit', 'discount', 'product_uom_id']
            names += ['deferred_start_date', 'deferred_end_date']
            for name in names:
                if name in line._fields and not self._same_value(line[name], mirror_line[name]):
                    add(line._fields[name].get_description(self.env)['string'], line[name], mirror_line[name], name)
            if not entry and not self._same_taxes(line.tax_ids, mirror_line.tax_ids):
                add(_("Taxes"), line.tax_ids, mirror_line.tax_ids)
            if not self._same_partner(line.partner_id, mirror_line.partner_id, official_co):
                add(_("Partner"), line.partner_id, mirror_line.partner_id, 'partner')
            if not self._same_analytic(line.analytic_distribution, mirror_line.analytic_distribution):
                add(_("Analytic"), self._analytic_text(line.analytic_distribution),
                    self._analytic_text(mirror_line.analytic_distribution), 'analytic')

    def _compare_totals(self, source, mirror, diffs):
        for name in ('amount_untaxed', 'amount_tax', 'amount_total'):
            if not self._same_value(source[name], mirror[name]):
                diffs.append(self._diff(
                    'totals', source._fields[name].get_description(self.env)['string'],
                    source[name], mirror[name]))

    def _compare_balances(self, source, mirror, official_co, diffs):
        """Invoices and payments: the mirror's tax, receivable/payable and
        liquidity lines are rebuilt by Odoo, so the amounts are compared per
        account (code) instead of line by line."""
        def per_account(move, company):
            result = {}
            for line in move.line_ids:
                code = line.account_id.with_company(company).code or line.account_id.display_name
                values = result.setdefault(code, [0.0, 0.0])
                values[0] += line.balance
                if 'second_currency_amount' in line._fields:
                    values[1] += line.second_currency_amount
            return result

        source_amounts = per_account(source, source.company_id)
        mirror_amounts = per_account(mirror, official_co)
        for code in sorted(set(source_amounts) | set(mirror_amounts)):
            src = source_amounts.get(code, [0.0, 0.0])
            mir = mirror_amounts.get(code, [0.0, 0.0])
            if not self._same_value(src[0], mir[0]):
                diffs.append(self._diff('totals', _("Balance of account %s", code), src[0], mir[0]))
            if not self._same_value(src[1], mir[1]):
                diffs.append(self._diff('totals', _("Second currency amount of account %s", code), src[1], mir[1]))

    def _compare_reconciliation(self, source, mirror, diffs):
        """Each document reconciled with the source must have its mirror
        reconciled with the source's mirror (and the other way round).
        Exchange difference entries are left out: they have no mirror."""
        def counterparts(move):
            partials = move.line_ids.matched_debit_ids | move.line_ids.matched_credit_ids
            others = (partials.debit_move_id | partials.credit_move_id).move_id - move
            return others.filtered(lambda m: not m.exchange_diff_partial_ids)

        expected = counterparts(source).nv_mirror_move_id
        actual = counterparts(mirror).filtered('is_mirror')
        if expected != actual:
            # only missing ones can be added in place; an extra one in the
            # mirror needs the rewrite (its reconciliations are redone)
            diffs.append(self._diff(
                'reconciliation', _("Reconciled with"),
                ', '.join(sorted(counterparts(source).mapped('name'))) or _("nothing"),
                ', '.join(sorted(actual.mapped('name'))) or _("nothing"),
                key='missing' if not (actual - expected) else 'extra'))

    # =========================================================================
    # Suggested fix
    # =========================================================================

    # (group, key) of the differences fixed in place on a posted mirror
    # (fields Odoo lets change on a posted entry, reconciliations to add, a
    # status to set); any other difference needs the rewrite.
    LIGHT = {
        ('link', 'status'),
        ('header', 'partner'), ('header', 'ref'), ('header', 'payment_reference'),
        ('header', 'payment_state'),
        ('lines', 'partner'), ('lines', 'analytic'), ('lines', 'date_maturity'),
        ('payment', 'partner'), ('payment', 'memo'),
        ('reconciliation', 'missing'),
    }

    def _due_lines(self, move):
        """[(due date, amount)] of the receivable / payable (due) lines,
        by date."""
        lines = move.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        return sorted((l.date_maturity, round(l.balance, AMOUNT_PRECISION)) for l in lines)

    def _due_text(self, due_lines):
        return '; '.join(f"{date}: {amount:,.2f}" for date, amount in due_lines) or False

    def _same_installments(self, source, mirror):
        """Same number of due lines with the same amounts (by date): only
        their dates differ, so the term and the dates can be set in place."""
        source_due, mirror_due = self._due_lines(source), self._due_lines(mirror)
        return len(source_due) == len(mirror_due) and all(
            float_compare(a[1], b[1], precision_digits=AMOUNT_PRECISION) == 0
            for a, b in zip(source_due, mirror_due))

    def _date_keeps_number(self, mirror, date):
        """The mirror's number still fits `date`: the year (and month) its
        number carries, parsed by Odoo's sequence format, are those of the
        date. The accounting date can then be set in place; otherwise the
        mirror needs a new number (rewrite)."""
        if not mirror.name or mirror.name == '/':
            return True
        try:
            format_values = mirror._get_sequence_format_param(mirror.name)[1]
            reset = mirror._deduce_sequence_number_reset(mirror.name)
        except Exception:  # noqa: BLE001 - unknown format: be safe
            return False
        if reset == 'year_range':
            fiscal = mirror.company_id.compute_fiscalyear_dates(date)
            start_year, end_year = fiscal['date_from'].year, fiscal['date_to'].year
        else:
            start_year = end_year = date.year
        if format_values.get('year') and not mirror._year_match(format_values['year'], start_year):
            return False
        if format_values.get('year_end') and not mirror._year_match(format_values['year_end'], end_year):
            return False
        if format_values.get('month') and format_values['month'] != date.month:
            return False
        return True

    def _is_light(self, source, mirror, diff):
        key = (diff['group'], diff['key'])
        if key in self.LIGHT:
            return True
        if key == ('header', 'invoice_date'):
            return True
        if key in (('header', 'invoice_date_due'), ('header', 'payment_term'), ('header', 'due_lines')):
            # without term: the due date is set in place; with one, the dates
            # come from the due lines, set in place when only their dates differ
            return not source.invoice_payment_term_id and key == ('header', 'invoice_date_due') or (
                bool(mirror) and self._same_installments(source, mirror))
        if key == ('header', 'date'):
            return bool(mirror) and self._date_keeps_number(mirror, source.date)
        return False

    @api.model
    def classify(self, source, diffs):
        """(fix, reason) for a document and its differences, fix being
        'ok' (nothing to do), 'light' (fixed in place), 'rewrite' (the mirror
        is rebuilt from the document) or 'cannot'. The strongest one any of
        its differences needs applies to the whole document."""
        if not diffs:
            return 'ok', False
        source = source.sudo()
        mirror = source.nv_mirror_move_id
        light = all(self._is_light(source, mirror, d) for d in diffs)
        fix = 'light' if light else 'rewrite'
        if fix == 'rewrite' and source.state != 'posted':
            return 'cannot', _("The document is not posted: its mirror is rewritten when it is posted.")
        if mirror and mirror.state == 'posted':
            if mirror.inalterable_hash:
                return 'cannot', _("The mirror %s is secured (hashed): it cannot be changed.", mirror.name)
            violated = mirror.company_id._get_violated_lock_dates(
                mirror.date, bool(mirror.line_ids.tax_ids or mirror.line_ids.tax_line_id), mirror.journal_id)
            if violated:
                return 'cannot', _("The mirror %(mirror)s is in a locked period (%(locks)s).",
                                   mirror=mirror.name,
                                   locks=mirror.company_id._format_lock_dates(violated))
            if source.date != mirror.date:
                # the mirror moves to the source's date: that period too
                violated = mirror.company_id._get_violated_lock_dates(
                    source.date, bool(mirror.line_ids.tax_ids or mirror.line_ids.tax_line_id),
                    mirror.journal_id)
                if violated:
                    return 'cannot', _("The date %(date)s is in a locked period of %(company)s (%(locks)s).",
                                       date=source.date, company=mirror.company_id.name,
                                       locks=mirror.company_id._format_lock_dates(violated))
        return fix, False

    # =========================================================================
    # Rules
    # =========================================================================

    def _same_value(self, a, b, precision=AMOUNT_PRECISION):
        if isinstance(a, float) or isinstance(b, float):
            return float_compare(a or 0.0, b or 0.0, precision_digits=precision) == 0
        if isinstance(a, models.BaseModel):
            return a.ids == b.ids
        return (a or False) == (b or False)

    def _shared_or_official(self, record, official_co):
        return not record.company_id or record.company_id == official_co

    def _same_partner(self, partner, mirror_partner, official_co):
        """The mirror has the same contact (same partner_id): the sync never
        uses another contact (nv.sync.engine._map_partner)."""
        return partner == mirror_partner

    def _same_by_name(self, record, mirror_record, official_co):
        if not record or self._shared_or_official(record, official_co):
            return record == mirror_record
        return bool(mirror_record) and mirror_record.name == record.name

    def _same_account(self, account, mirror_account):
        if not account or not mirror_account:
            return account.ids == mirror_account.ids == []
        source_co = account.company_ids[:1]
        mirror_co = mirror_account.company_ids[:1]
        return account.with_company(source_co).code == mirror_account.with_company(mirror_co).code

    def _account_label(self, account):
        return account.with_company(account.company_ids[:1]).display_name if account else False

    def _same_taxes(self, taxes, mirror_taxes):
        key = lambda tax: (tax.name, round(tax.amount, 4), tax.amount_type)  # noqa: E731
        return sorted(map(key, taxes)) == sorted(map(key, mirror_taxes))

    def _analytic_key(self, distribution):
        """{frozenset of account labels: percentage}. Label of an analytic
        account: a shared one (no company) by id, so the mirror must use the
        same account; a company-specific one by plan and code (else name),
        so a main-company account matches its official-company twin."""
        if not distribution:
            return {}
        Analytic = self.env['account.analytic.account'].sudo()
        result = {}
        for key, percentage in distribution.items():
            accounts = Analytic.browse(int(i) for i in str(key).split(',')).exists()
            labels = frozenset(
                ('id', a.id) if not a.company_id else ('code', a.plan_id.id, a.code or a.name)
                for a in accounts
            )
            result[labels] = round(percentage, 2)
        return result

    def _same_analytic(self, distribution, mirror_distribution):
        return self._analytic_key(distribution) == self._analytic_key(mirror_distribution)

    def _analytic_text(self, distribution):
        if not distribution:
            return False
        Analytic = self.env['account.analytic.account'].sudo()
        parts = []
        for key, percentage in distribution.items():
            names = Analytic.browse(int(i) for i in str(key).split(',')).exists().mapped('display_name')
            parts.append(f"{' / '.join(names)}: {percentage:g}%")
        return '; '.join(sorted(parts))

    # =========================================================================
    # Display
    # =========================================================================

    def _selection(self, record, name):
        return dict(record._fields[name]._description_selection(self.env)).get(record[name], record[name])

    def _text(self, value):
        if isinstance(value, models.BaseModel):
            return ', '.join(value.mapped('display_name')) or '—'
        if value is False or value is None or value == '':
            return '—'
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if isinstance(value, float):
            return f"{value:,.2f}"
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        return str(value)

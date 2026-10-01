import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """
    One-time update: switch every journal's Official/Non-Official sequence
    prefix from the old "-O"/"-NO" suffix (e.g. INV-O, INV-NO) to the new
    "/1"/"/2" style (e.g. INV/1, INV/2) requested by the client, so Official
    invoices read PREFIX/1/YY/NNNNNN and Non-Official PREFIX/2/YY/NNNNNN.

    Overwrites unconditionally (not just when blank) since production has
    no real invoices posted yet - safe to run on every journal.
    """
    env = _get_env(cr)
    journals = env['account.journal'].sudo().search([])
    updated = 0
    for journal in journals:
        code = (journal.code or 'JNL').upper()
        journal.write({
            'seq_prefix_o': '%s/1' % code,
            'seq_prefix_no': '%s/2' % code,
        })
        updated += 1
    _logger.info(
        "nv_dual_bookkeeping post-migrate 19.0.1.0.1: updated seq_prefix_o/"
        "seq_prefix_no on %d journal(s) to the new /1 /2 style.",
        updated,
    )


def _get_env(cr):
    from odoo import api, SUPERUSER_ID
    return api.Environment(cr, SUPERUSER_ID, {})

import logging

from . import models
from . import services
from . import wizard

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """
    Post-installation hook: ensure every existing journal across every company
    has seq_prefix_o and seq_prefix_no populated.

    Called once by Odoo after the module tables are created and data files are
    loaded.  Safe to re-run — writes only when a prefix is not already set.

    Summary logged at INFO level so administrators can verify the migration
    in the server log without enabling debug mode.
    """
    _logger.info("nv_dual_bookkeeping post_init_hook: populating journal prefixes …")

    journals = env['account.journal'].sudo().search([], order='company_id, id')

    total = len(journals)
    updated = 0
    warnings = 0

    current_company = None

    for journal in journals:
        if journal.company_id != current_company:
            current_company = journal.company_id
            _logger.info(
                "nv_dual_bookkeeping post_init_hook: processing company '%s' …",
                current_company.name,
            )

        # Auto-populate prefixes that were never set
        code = (journal.code or 'JNL').upper()
        updates = {}
        if not journal.seq_prefix_o:
            updates['seq_prefix_o'] = '%s/1' % code
        if not journal.seq_prefix_no:
            updates['seq_prefix_no'] = '%s/2' % code
        if updates:
            journal.write(updates)
            updated += 1
            _logger.debug(
                "  [UPDATED] prefixes for journal '%s' (id=%d, company: %s): %s",
                journal.name, journal.id, journal.company_id.name, updates,
            )

        # --- Official journal mapping warning ---
        if not journal.official_journal_id:
            warnings += 1
            _logger.warning(
                "  [UNLINKED] Journal '%s' (id=%d, company: %s) has no official_journal_id. "
                "Use Accounting > Dual Bookkeeping > Link Official Journals to configure.",
                journal.name, journal.id, journal.company_id.name,
            )

    _logger.info(
        "nv_dual_bookkeeping post_init_hook complete. "
        "Total journals: %d | prefixes updated: %d | journals with no official mapping: %d",
        total, updated, warnings,
    )

    if warnings:
        _logger.warning(
            "nv_dual_bookkeeping: %d journal(s) are not linked to an official company journal. "
            "Official transactions in these journals will fall back to standard Odoo sequencing "
            "until you complete the mapping.",
            warnings,
        )

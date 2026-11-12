"""Link the existing mirrors to their source (nv_mirror_move_id, new in
19.0.1.0.2, D2).

Before 19.0.1.0.2 a mirror was found through its "synced" sync log
(target_move_id) or its name (source_move_ref). Without the link the first
re-post of a mirrored transaction would create a second mirror instead of
rewriting the existing one.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # the latest synced log of each source whose mirror still exists
    cr.execute(
        """
        UPDATE account_move source
           SET nv_mirror_move_id = log.target_move_id
          FROM (
                SELECT DISTINCT ON (l.source_move_id)
                       l.source_move_id, l.target_move_id
                  FROM nv_sync_log l
                  JOIN account_move mirror ON mirror.id = l.target_move_id
                 WHERE l.sync_state = 'synced'
                   AND l.source_move_id IS NOT NULL
              ORDER BY l.source_move_id, l.sync_date DESC, l.id DESC
               ) log
         WHERE source.id = log.source_move_id
           AND source.nv_mirror_move_id IS NULL
        """
    )
    _logger.info("%s mirrors linked from their sync log", cr.rowcount)
    # mirrors without a log: same number in the linked official journal
    cr.execute(
        """
        UPDATE account_move source
           SET nv_mirror_move_id = mirror.id
          FROM account_journal journal, account_move mirror
         WHERE journal.id = source.journal_id
           AND mirror.journal_id = journal.official_journal_id
           AND mirror.is_mirror
           AND mirror.source_move_ref = source.name
           AND source.nv_mirror_move_id IS NULL
           AND NOT source.is_mirror
           AND NOT EXISTS (
                   SELECT 1
                     FROM account_move other
                    WHERE other.nv_mirror_move_id = mirror.id
               )
        """
    )
    _logger.info("%s mirrors linked by their number", cr.rowcount)
    # logs name their source (new field, kept when the source is deleted)
    cr.execute(
        """
        UPDATE nv_sync_log log
           SET source_ref = move.name
          FROM account_move move
         WHERE move.id = log.source_move_id
           AND log.source_ref IS NULL
        """
    )
    _logger.info("%s sync logs named after their source", cr.rowcount)
